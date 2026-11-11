"""Self-update plugin: the bridge offers its own updates in Home Assistant.

update.<name>_ha_bridge shows the running bridge version against the newest
stable GitHub release of this project. Install (a person pressing it in Home
Assistant; never automatic):

  1. downloads the release tarball and SHA256SUMS, and checks the checksum
  2. unpacks it next to the running release (nothing is overwritten)
  3. installs any new apt packages the release lists
  4. runs the new release's self-test against this host's config
  5. switches the `current` link, then exits so systemd restarts the service

The new version confirms itself after it has stayed connected for a minute.
If it can't, guard.py (run by systemd before every start) puts the previous
release back after three failed starts, and the old version reports that.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request

from . import PROJECT, VERSION
from .core import Plugin
from .util import now_iso, version_tuple, write_json_atomic

log = logging.getLogger("habridge.selfupdate")

DEFAULT_REPO = f"ZeddicPrime/{PROJECT}"
DEFAULT_API = "https://api.github.com"
PRESS_PAYLOAD = "INSTALL"
OBJECT_ID = "bridge-update"  # slugs never contain '-', so no Compose key can collide
KEEP_RELEASES = 3


def install_root():
    return os.environ.get("HABRIDGE_ROOT", f"/opt/{PROJECT}")


def marker_path(log_dir):
    return os.path.join(log_dir, "self-update.json")


def _safe_extract(tar, dest, top):
    """Extract only regular files and folders under top/ (no links, no escapes)."""
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or ".." in name.split("/") or not (name == top or name.startswith(top + "/")):
            raise RuntimeError(f"unexpected path in release archive: {name}")
        if not (m.isfile() or m.isdir()):
            raise RuntimeError(f"unexpected entry type in release archive: {name}")
    for m in tar.getmembers():
        target = os.path.join(dest, m.name)
        if m.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with tar.extractfile(m) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)


def render_unit(release_dir, name, root):
    with open(os.path.join(release_dir, "systemd", "bridge.service.in")) as f:
        return f.read().replace("@NAME@", name).replace("@ROOT@", root)


class SelfUpdatePlugin(Plugin):
    name = "selfupdate"
    state_keys = ("selfupdate",)

    def __init__(self, bridge, cfg):
        super().__init__(bridge, cfg)
        sc = cfg.get("self_update") or {}
        self.enabled = sc.get("enabled", True)
        self.repo = sc.get("repo", DEFAULT_REPO)
        self.api = sc.get("api", DEFAULT_API).rstrip("/")
        self.interval = float(sc.get("check_interval_hours", 6)) * 3600
        self.confirm_seconds = float(sc.get("confirm_seconds", 60))
        self.root = install_root()
        b = bridge
        self.topic_state = f"{b.base}/{OBJECT_ID}/state"
        self.topic_attr = f"{b.base}/{OBJECT_ID}/attributes"
        self.topic_cmd = f"{b.base}/{OBJECT_ID}/install"
        self.st = dict(b.state.get("selfupdate") or {})
        self.lock = threading.Lock()
        self.busy = False
        self.wake = threading.Event()
        self.marker = None
        self._read_marker()

    # ------------------------------------------------------------- state
    def snapshot(self):
        with self.lock:
            return {"selfupdate": dict(self.st)}

    def _read_marker(self):
        """What the last self-update left behind: confirm it, or report its rollback."""
        path = marker_path(self.bridge.log_dir)
        try:
            with open(path) as f:
                m = json.load(f)
        except (OSError, ValueError):
            return
        if m.get("rolled_back"):
            self.st["last_install"] = m.get("at")
            self.st["last_install_result"] = (
                f"Failed: {m.get('to_version')} didn't start ({m.get('attempts')} tries); "
                f"rolled back to {m.get('from_version')}")
            log.warning("Self-update to %s was rolled back", m.get("to_version"))
            self._remove_marker()
        elif m.get("to_version") == VERSION:
            self.marker = m  # confirmed once we've stayed up for a while
        else:
            self._remove_marker()

    def _remove_marker(self):
        try:
            os.remove(marker_path(self.bridge.log_dir))
        except OSError:
            pass

    def _confirm_loop(self):
        if not self.marker:
            return
        if not self.bridge.connected.wait(300):
            return
        if self.bridge.stop.wait(self.confirm_seconds):
            return
        m, self.marker = self.marker, None
        with self.lock:
            self.st["last_install"] = m.get("at")
            self.st["last_install_result"] = f"Updated {m.get('from_version')} → {VERSION}"
        self._remove_marker()
        log.info("Self-update to %s confirmed", VERSION)
        self.bridge.save_state()
        self.publish_state()

    # -------------------------------------------------------------- mqtt
    def subscriptions(self):
        return [self.topic_cmd] if self.enabled else []

    def is_command(self, topic):
        return topic == self.topic_cmd

    def publish_discovery(self):
        topic = self.bridge.discovery_topic("update", OBJECT_ID)
        if not self.enabled:
            if self.st.pop("discovered", None):  # turned off since: remove the entity once
                self.bridge.publish(topic, "")
            return
        self.st["discovered"] = True
        b = self.bridge
        self.bridge.publish(topic, {
            "name": "Bridge",
            "default_entity_id": f"update.{b.name}_ha_bridge",
            "unique_id": f"{b.node}_{OBJECT_ID}",
            "entity_category": "config",
            "icon": "mdi:bridge",
            "state_topic": self.topic_state,
            "command_topic": self.topic_cmd,
            "payload_install": PRESS_PAYLOAD,
            "json_attributes_topic": self.topic_attr,
            **b.common()})

    def publish_state(self):
        if not self.enabled:
            return
        with self.lock:
            st = dict(self.st)
            busy = self.busy
        latest = st.get("latest_version") or VERSION
        if version_tuple(latest) <= version_tuple(VERSION):
            latest = VERSION
        state = {"installed_version": VERSION, "latest_version": latest, "title": PROJECT,
                 "in_progress": busy,
                 "release_summary": (st.get("release_summary") or "")[:255] if latest != VERSION else ""}
        url = st.get("release_url") if latest != VERSION else st.get("installed_url")
        if url:
            state["release_url"] = url
        attrs = {k: v for k, v in {
            "repo": self.repo,
            "install_root": self.root,
            "last_check": st.get("last_check"),
            "last_check_result": st.get("last_check_result"),
            "last_install": st.get("last_install"),
            "last_install_result": st.get("last_install_result"),
            "awaiting_confirmation": bool(self.marker) or None,
        }.items() if v is not None}
        self.bridge.publish(self.topic_state, json.dumps(state))
        self.bridge.publish(self.topic_attr, json.dumps(attrs))

    def republish(self):
        self.publish_state()

    def on_check(self):
        self.wake.set()

    def handle(self, topic, payload):
        if topic != self.topic_cmd:
            return False
        if payload != PRESS_PAYLOAD or not self.enabled:
            log.warning("Ignoring unexpected message on %s", topic)
            return True
        with self.lock:
            if self.busy:
                log.info("Bridge update already running; ignored")
                return True
            self.busy = True
        self.publish_state()
        threading.Thread(target=self.install, name="selfupdate-install", daemon=True).start()
        return True

    # ------------------------------------------------------------- check
    def _get(self, url, accept="application/vnd.github+json", timeout=30):
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{PROJECT}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def latest_release(self):
        j = json.loads(self._get(f"{self.api}/repos/{self.repo}/releases/latest"))
        tag = str(j.get("tag_name") or "").strip()
        if j.get("draft") or j.get("prerelease") or not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
            raise ValueError(f"newest release {tag!r} is not a stable version")
        return {"version": tag.lstrip("v"), "tag": tag, "url": j.get("html_url"),
                "body": j.get("body") or "", "assets": {a.get("name"): a.get("browser_download_url")
                                                         for a in j.get("assets") or []}}

    def check(self):
        try:
            rel = self.latest_release()
            newer = version_tuple(rel["version"]) > version_tuple(VERSION)
            summary = re.sub(r"\s+", " ", re.sub(r"[#*`>]", "", rel["body"])).strip()
            with self.lock:
                self.st.update({
                    "latest_version": rel["version"] if newer else VERSION,
                    "release_url": rel["url"],
                    "release_summary": summary,
                    "installed_url": f"https://github.com/{self.repo}/releases/tag/v{VERSION}",
                    "last_check": now_iso(), "last_check_result": "OK"})
            log.info("Bridge: installed %s, latest release %s", VERSION, rel["version"])
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.st.update({"last_check": now_iso(), "last_check_result": f"Check failed: {e}"})
            log.warning("Bridge update check failed: %s", e)
        self.bridge.save_state()
        self.publish_state()

    def check_loop(self):
        while not self.bridge.stop.is_set():
            self.check()
            self.wake.wait(self.interval)
            self.wake.clear()

    def start(self):
        if not self.enabled:
            return
        threading.Thread(target=self.check_loop, name="selfupdate-check", daemon=True).start()
        threading.Thread(target=self._confirm_loop, name="selfupdate-confirm", daemon=True).start()

    def stop(self):
        self.wake.set()

    # ----------------------------------------------------------- install
    def install(self):
        started = now_iso()
        log_path = os.path.join(self.bridge.log_dir, "bridge-last-update.log")
        result, restart = None, False
        try:
            with open(log_path, "w") as lf:
                def note(msg):
                    lf.write(msg + "\n")
                    lf.flush()
                    log.info("Bridge update: %s", msg)
                note(f"# {self.bridge.service} {VERSION}: bridge update at {started}")
                current = os.path.join(self.root, "current")
                if not os.path.islink(current):
                    raise RuntimeError(f"{current} isn't a release link; reinstall with install.sh first")
                rel = self.latest_release()
                ver = rel["version"]
                if version_tuple(ver) <= version_tuple(VERSION):
                    raise RuntimeError(f"Nothing to install: newest release is {ver}")
                tarname = f"{PROJECT}-{ver}.tar.gz"
                if tarname not in rel["assets"] or "SHA256SUMS" not in rel["assets"]:
                    raise RuntimeError(f"release {rel['tag']} lacks {tarname} or SHA256SUMS")
                releases = os.path.join(self.root, "releases")
                os.makedirs(releases, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".incoming-", dir=releases) as tmp:
                    # 1. Download and verify
                    note(f"Downloading {tarname}")
                    data = self._get(rel["assets"][tarname], accept="application/octet-stream", timeout=120)
                    sums = self._get(rel["assets"]["SHA256SUMS"], accept="application/octet-stream").decode()
                    want = next((ln.split()[0] for ln in sums.splitlines()
                                 if ln.strip().endswith(tarname)), None)
                    got = hashlib.sha256(data).hexdigest()
                    if not want or want.lower() != got:
                        raise RuntimeError(f"checksum mismatch for {tarname} (expected {want}, got {got})")
                    note(f"Checksum OK ({got[:16]}…)")
                    # 2. Unpack beside the running release
                    top = f"{PROJECT}-{ver}"
                    tpath = os.path.join(tmp, tarname)
                    with open(tpath, "wb") as f:
                        f.write(data)
                    with tarfile.open(tpath) as tar:
                        _safe_extract(tar, tmp, top)
                    new_dir = os.path.join(tmp, top)
                    # 3. New apt packages, if the release needs any
                    self._apt(new_dir, note)
                    # 4. Self-test the new release against this host's config
                    cmd = [sys.executable, "-I", os.path.join(new_dir, "bridge.py"), "--name", self.bridge.name,
                           "--config", self.bridge.config_path, "--self-test"]
                    note("$ " + " ".join(cmd))
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                    note((p.stdout + p.stderr).strip())
                    if p.returncode != 0:
                        raise RuntimeError(f"the new release's self-test failed (exit {p.returncode}); "
                                           "nothing was changed")
                    final = os.path.join(releases, ver)
                    if os.path.exists(final):
                        shutil.rmtree(final)
                    os.replace(new_dir, final)
                # 5. Switch, keeping a way back
                from_path = os.path.realpath(current)
                write_json_atomic(marker_path(self.bridge.log_dir), {
                    "from_version": VERSION, "from_path": from_path, "to_version": ver,
                    "to_path": final, "at": started, "attempts": 0})
                link = os.path.join(self.root, ".current.new")
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(final, link)
                os.replace(link, current)
                note(f"current -> {final}")
                shutil.copy2(os.path.join(final, "guard.py"), os.path.join(self.root, "guard.py.new"))
                os.replace(os.path.join(self.root, "guard.py.new"), os.path.join(self.root, "guard.py"))
                self._update_unit(final, note)
                self._prune(releases, {final, from_path}, note)
                result = f"Installed {ver}; restarting into it"
                restart = True
        except Exception as e:  # noqa: BLE001
            result = f"Failed: {e}"
            log.error("Bridge update: %s", result)
        with self.lock:
            self.st["last_install"] = started
            self.st["last_install_result"] = result
            self.st["log_file"] = log_path
            self.busy = restart  # stays "in progress" until the restart
        self.bridge.save_state()
        self.publish_state()
        if restart:
            time.sleep(1)
            self.bridge.request_restart()

    def _apt(self, new_dir, note):
        path = os.path.join(new_dir, "apt-packages.txt")
        if os.environ.get("HABRIDGE_NO_APT") or not os.path.exists(path) or not shutil.which("dpkg-query"):
            return
        with open(path) as f:
            wanted = [ln.split("#")[0].strip() for ln in f]
        wanted = [w for w in wanted if re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", w)]
        missing = []
        for pkg in wanted:
            r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", pkg], capture_output=True, text=True)
            if "install ok installed" not in r.stdout:
                missing.append(pkg)
        if not missing:
            return
        note("Installing apt packages: " + " ".join(missing))
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        for cmd in (["apt-get", "update", "-qq"], ["apt-get", "install", "-y", "-qq", *missing]):
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env, stdin=subprocess.DEVNULL)
            note((p.stdout + p.stderr).strip()[-2000:])
            if p.returncode != 0:
                raise RuntimeError(f"`{' '.join(cmd[:2])}` failed (exit {p.returncode}); nothing was changed")

    def _update_unit(self, release_dir, note):
        unit_dir = os.environ.get("HABRIDGE_UNIT_DIR", "/etc/systemd/system")
        path = os.path.join(unit_dir, f"{self.bridge.service}.service")
        try:
            want = render_unit(release_dir, self.bridge.name, self.root)
            with open(path) as f:
                have = f.read()
        except OSError:
            return
        if want == have:
            return
        with open(path + ".new", "w") as f:
            f.write(want)
        os.replace(path + ".new", path)
        note(f"Updated {path}")
        if shutil.which("systemctl") and not os.environ.get("HABRIDGE_UNIT_DIR"):
            subprocess.run(["systemctl", "daemon-reload"], timeout=60)

    def _prune(self, releases, keep, note):
        dirs = [os.path.join(releases, d) for d in os.listdir(releases)
                if not d.startswith(".") and os.path.isdir(os.path.join(releases, d))]
        dirs.sort(key=lambda d: version_tuple(os.path.basename(d)), reverse=True)
        for d in dirs[KEEP_RELEASES:]:
            if os.path.realpath(d) not in {os.path.realpath(k) for k in keep}:
                shutil.rmtree(d, ignore_errors=True)
                note(f"Removed old release {os.path.basename(d)}")
