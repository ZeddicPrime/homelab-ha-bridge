"""OCI registry lookups: the digest a tag points at, and an image's labels."""

import json
import platform
import re
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION

# Tests only: a registry host:port answered over plain HTTP (set by bridge.py).
INSECURE_REGISTRY = None

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def parse_ref(ref):
    """'ghcr.io/a/b:tag' -> (registry, repo, tag, familiar_repo)."""
    ref = ref.split("@", 1)[0]
    name, tag = ref, "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    parts = name.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts
    else:
        registry, repo = "docker.io", name
        if "/" not in repo:
            repo = f"library/{repo}"
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
        familiar = repo[len("library/"):] if repo.startswith("library/") else repo
    else:
        familiar = f"{registry}/{repo}"
    return registry, repo, tag, familiar


def local_arch():
    m = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(m, m)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_noredir = urllib.request.build_opener(_NoRedirect)


def registry_base(registry):
    if registry == "docker.io":
        return "https://registry-1.docker.io"
    if INSECURE_REGISTRY and INSECURE_REGISTRY == registry:
        return f"http://{registry}"
    return f"https://{registry}"


class Registry:
    def __init__(self, user_agent, timeout=20):
        self.timeout = timeout
        self.ua = user_agent
        self.tokens = {}

    def _token(self, www_auth, repo):
        m = dict(re.findall(r'(\w+)="([^"]*)"', www_auth or ""))
        if "realm" not in m:
            raise RuntimeError("registry wants auth but sent no realm")
        q = {"scope": m.get("scope") or f"repository:{repo}:pull"}
        if m.get("service"):
            q["service"] = m["service"]
        url = m["realm"] + "?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = json.load(r)
        return data.get("token") or data.get("access_token")

    def _req(self, registry, repo, path, method="GET", accept=ACCEPT, follow=True):
        url = f"{registry_base(registry)}/v2/{repo}/{path}"
        for attempt in range(2):
            h = {"Accept": accept, "User-Agent": self.ua}
            tok = self.tokens.get((registry, repo))
            if tok:
                h["Authorization"] = f"Bearer {tok}"
            req = urllib.request.Request(url, headers=h, method=method)
            try:
                return _noredir.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.tokens[(registry, repo)] = self._token(e.headers.get("WWW-Authenticate"), repo)
                    continue
                if e.code in (301, 302, 303, 307, 308) and follow:
                    # Blob storage (S3/R2) rejects our bearer token, so drop it.
                    loc = urllib.parse.urljoin(url, e.headers["Location"])
                    return urllib.request.urlopen(
                        urllib.request.Request(loc, headers={"User-Agent": self.ua}), timeout=self.timeout)
                raise RuntimeError(f"{registry}/{repo}: HTTP {e.code} on {path.split('/')[0]}")
        raise RuntimeError(f"{registry}/{repo}: authorization failed")

    def digest(self, registry, repo, tag):
        """HEAD only, so it never counts against Docker Hub's anonymous pull limit."""
        r = self._req(registry, repo, f"manifests/{tag}", method="HEAD")
        d = r.headers.get("Docker-Content-Digest")
        r.close()
        if not d:  # some registries only send it on GET
            r = self._req(registry, repo, f"manifests/{tag}")
            d = r.headers.get("Docker-Content-Digest")
            r.close()
        if not d:
            raise RuntimeError(f"{registry}/{repo}: no digest returned")
        return d

    def labels(self, registry, repo, ref, arch):
        """Labels of the image config for this platform (best effort)."""
        with self._req(registry, repo, f"manifests/{ref}") as r:
            m = json.load(r)
        if "manifests" in m:  # index / manifest list
            pick = next((e for e in m["manifests"]
                         if (e.get("platform") or {}).get("os") == "linux"
                         and (e.get("platform") or {}).get("architecture") == arch), None)
            if not pick:
                return {}
            with self._req(registry, repo, f"manifests/{pick['digest']}") as r:
                m = json.load(r)
        cfg = (m.get("config") or {}).get("digest")
        if not cfg:
            return {}
        with self._req(registry, repo, f"blobs/{cfg}", accept="*/*") as r:
            c = json.load(r)
        return (c.get("config") or {}).get("Labels") or {}


USER_AGENT = f"homelab-ha-bridge/{VERSION}"
