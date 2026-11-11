"""Small helpers shared by every plugin."""

import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timezone

# Set once at startup by bridge.py from <NAME>_HA_DOCKER (tests point it at a shim).
DOCKER = "docker"


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def short(digest):
    if not digest:
        return None
    return digest.split(":", 1)[-1][:12]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def strip_ansi(s):
    s = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", s)
    return s.replace("\r", "\n")


def docker(*args, cwd=None, timeout=120, check=True):
    """Run the Docker CLI with a fixed argument list (never a shell)."""
    p = subprocess.run([DOCKER, *args], cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: "
                           f"{(p.stderr or p.stdout).strip()[-300:]}")
    return p


def redact(text):
    """Strip anything token-shaped before it reaches a log or an MQTT attribute."""
    return re.sub(r"eyJ[A-Za-z0-9_.=+/-]{10,}", "<redacted>", str(text))


def write_json_atomic(path, obj, mode=None):
    """Write JSON through a unique temp file in the same folder, then rename,
    so two threads saving at once can't trip over a shared .tmp name."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------ versions
# A version that looks like any of these is a pre-release: v3.4.0-rc.1,
# v2.11.0-preview21, 1.2.0-beta, 2.0.0beta2, 2026.11.0-dev3 ...
_PRE_WORDS = r"rc|alpha|beta|pre|preview|dev|nightly|canary|snapshot|next|edge"
_PRE_A = re.compile(rf"(?i)(?:^|[-_.+~])(?:{_PRE_WORDS})(?:[-_.]?\d+)*(?=$|[-_.+~])")
_PRE_B = re.compile(rf"(?i)(?:[-.+_]|\d)(?:{_PRE_WORDS})(?:[-.+_]?\d+)*(?![a-z])")


def is_prerelease(version):
    """'v3.4.0-rc.1', '2.0.0beta2', '1.2-dev' -> True; 'v3.3.6', '2026.10.7', a digest -> False."""
    return bool(version) and bool(_PRE_A.search(version) or _PRE_B.search(version))


def label_version(labels, prefix=""):
    """org.opencontainers.image.version, ignoring '' and 'latest'. prefix is added
    when the image left it off (one image says 2.10.7 where its release tag is v2.10.7)."""
    v = ((labels or {}).get("org.opencontainers.image.version") or "").strip()
    if v.lower() in ("", "latest"):
        return None
    if prefix and v[0].isdigit():
        v = prefix + v
    return v


def release_url(labels, version=None):
    """GitHub release page for this exact version when known, else the release list."""
    src = ((labels or {}).get("org.opencontainers.image.source") or "").rstrip("/").removesuffix(".git")
    if src.startswith("https://github.com/"):
        if version and re.fullmatch(r"v?\d+(\.\d+)*", version):
            return f"{src}/releases/tag/{urllib.parse.quote(version, safe='')}"
        return f"{src}/releases"
    return src or None


def version_tuple(v):
    """'v2.10.3' -> (2, 10, 3); anything unparsable -> ()."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def clean(d):
    """No nulls in anything sent to HA (it drops whole messages over one)."""
    if isinstance(d, dict):
        return {k: clean(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [clean(v) for v in d if v is not None]
    return d
