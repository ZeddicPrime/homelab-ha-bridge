"""OS plugin: the host's own operating system, on every host by default.

Entities (on the bridge's device):

  * OS packages (update entity): pending apt package updates. The installed
    version is the release (e.g. "Debian 12.8"), the latest is "N updates" while
    any are pending. Install runs one fixed command sequence: finish an
    interrupted dpkg run, apt-get update, then
    apt-get --with-new-pkgs upgrade (never removes packages; keeps existing
    config files). Only on hosts with apt.
  * Reboot required (problem): /run/reboot-required exists, or, outside a
    container, a newer kernel than the running one is installed (or the running
    kernel's modules are gone).
  * Disk used (%): for each configured path, "/" by default.
  * Disk almost full (problem): any of those at or above disk_warn_percent.

An install never starts while another plugin is installing something, and the
others wait for it (busy_reason). apt runs in its own session, so a restart of
the bridge doesn't kill it half way.

On a host that is switched off most of the time (one with the anthias plugin,
or keep_last_value_while_off: true) the entities have no availability, so
they keep their last value while it's off, like that plugin's health entities.

Config, all optional, under "os" (or "os": false to turn the plugin off):
  check_hours (6)               how often to run apt-get update and look again
  first_check_seconds (120)     after start, before the first apt check
  poll_minutes (5)              disk and reboot checks
  disk_paths (["/"])            one Disk used sensor each
  disk_warn_percent (90)
  timeout_minutes (60)          for an install
  keep_last_value_while_off     see above
"""

import json
import logging
import os
import re
import subprocess
import threading
import time

from . import VERSION, util
from .core import Plugin

log = logging.getLogger("habridge.os")

SYS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
INSTALL = "install"
# Packages whose upgrade restarts the Docker daemon, and with it every container.
DOCKER_PACKAGES = {"docker-ce", "docker.io", "containerd.io", "containerd", "docker-engine", "moby-engine"}
APT_OPTS = ["-o", "DPkg::Lock::Timeout=300", "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold"]

# Entities this plugin can publish; the ones a host doesn't have are removed.
FIXED_OBJECTS = {"update": ["os_packages"], "binary_sensor": ["reboot_required", "disk_almost_full"]}

APT_LOCK = util.APT_LOCK


# ------------------------------------------------------------------ helpers
def run(cmd, timeout=60, env_path=SYS_PATH, extra_env=None):
    env = {"PATH": env_path, "LANG": "C.UTF-8", "HOME": os.path.expanduser("~"),
           "DEBIAN_FRONTEND": "noninteractive", **(extra_env or {})}
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"


def which(cmd, env_path):
    for d in env_path.split(":"):
        p = os.path.join(d, cmd)
        if d and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def parse_simulation(text):
    """apt-get -s upgrade output -> (packages, kept_back).
    packages: [{name, from, to, security}] for every 'Inst' line."""
    pkgs, kept, in_kept = [], [], False
    for ln in (text or "").splitlines():
        if ln.startswith("Inst "):
            in_kept = False
            m = re.match(r"Inst (\S+)(?: \[([^\]]*)\])? \((\S+) ([^\[)]*)", ln)
            if not m:
                continue
            origin = m.group(4)
            pkgs.append({"name": m.group(1), "from": m.group(2), "to": m.group(3),
                         "security": bool(re.search(r"(?i)security", origin))})
        elif ln.startswith("The following packages have been kept back"):
            in_kept = True
        elif in_kept and ln.startswith(" "):
            kept.extend(ln.split())
        else:
            in_kept = False
    return pkgs, sorted(set(kept))


def os_label(os_release_path, debian_version_path):
    """'Debian 12.8', 'Ubuntu 24.04.1', or NAME VERSION_ID; None if unknown."""
    info = {}
    try:
        with open(os_release_path) as f:
            for ln in f:
                k, sep, v = ln.strip().partition("=")
                if sep:
                    info[k] = v.strip().strip('"')
    except OSError:
        return None
    os_id = info.get("ID", "").lower()
    if os_id == "debian":
        try:
            with open(debian_version_path) as f:
                dv = f.read().strip()
            if re.fullmatch(r"\d+(\.\d+)*", dv):
                return f"Debian {dv}"
        except OSError:
            pass
    if os_id == "ubuntu" and info.get("VERSION"):
        return f"Ubuntu {info['VERSION'].split()[0]}"
    name = info.get("NAME") or info.get("ID")
    ver = info.get("VERSION_ID")
    return f"{name} {ver}".strip() if name else None


def _natural(s):
    return [int(x) for x in re.findall(r"\d+", s)]


def kernel_reboot_reason(running, modules_dir):
    """Why the running kernel is stale, or None. Same flavour only (amd64, v8, 2712 …)."""
    try:
        installed = [d for d in os.listdir(modules_dir) if os.path.isdir(os.path.join(modules_dir, d))]
    except OSError:
        return None
    if not installed:
        return None
    if running not in installed:
        return f"the running kernel ({running}) is no longer installed"
    flavour = running.rsplit("-", 1)[-1]
    same = [d for d in installed if d.rsplit("-", 1)[-1] == flavour]
    newest = max(same, key=_natural)
    if newest != running and _natural(newest) > _natural(running):
        return f"kernel {newest} is installed; {running} is running"
    return None


def disk_usage(path):
    """(percent used like df, used bytes, total bytes, free bytes) or None."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    if used + avail <= 0:
        return None
    return round(100.0 * used / (used + avail), 1), used, total, avail


def failure_hint(tail):
    t = (tail or "").lower()
    if "could not get lock" in t or "unable to acquire the dpkg" in t:
        return "apt was busy (another update is running); try again later"
    if "dpkg was interrupted" in t:
        return "an earlier install was interrupted; Install again to finish it"
    if "could not resolve" in t or "temporary failure resolving" in t:
        return "no internet/DNS from this host"
    if "no space left" in t:
        return "the disk is full"
    return None


def log_tail(path, lines=15):
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    keep = [ln.rstrip() for ln in util.strip_ansi(text).splitlines() if ln.strip()]
    return "\n".join(keep[-lines:])


def gib(n):
    return round(n / 1024 ** 3, 1)


# ------------------------------------------------------------------- plugin
class HostOSPlugin(Plugin):
    name = "os"
    state_keys = ("os",)

    def __init__(self, bridge, cfg):
        super().__init__(bridge, cfg)
        o = cfg.get("os")
        o = o if isinstance(o, dict) else {}
        self.o = o
        self.env_path = o.get("path", SYS_PATH)
        self.check_every = float(o.get("check_hours", 6)) * 3600
        self.first_check = float(o.get("first_check_seconds", 120))
        self.poll = float(o.get("poll_minutes", 5)) * 60
        self.warn = float(o.get("disk_warn_percent", 90))
        self.timeout = int(o.get("timeout_minutes", 60)) * 60
        self.disk_paths = list(o.get("disk_paths") or ["/"])
        # Where the host describes itself; tests point these at fixtures.
        self.reboot_file = o.get("reboot_required_file", "/run/reboot-required")
        self.modules_dir = o.get("modules_dir", "/lib/modules")
        self.os_release = o.get("os_release", "/etc/os-release")
        self.debian_version = o.get("debian_version", "/etc/debian_version")
        self.kernel = o.get("kernel_release") or os.uname().release

        # Topics under "<base>/host-os/": slugs never contain '-', so no Compose key collides.
        b = f"{bridge.base}/host-os"
        self.tb = b
        self.t_state, self.t_attr = f"{b}/packages/state", f"{b}/packages/attributes"
        self.t_install = f"{b}/packages/install"
        self.t_reboot, self.t_reboot_attr = f"{b}/reboot_required", f"{b}/reboot_required/attributes"
        self.t_full, self.t_full_attr = f"{b}/disk_almost_full", f"{b}/disk_almost_full/attributes"

        self.lock = threading.RLock()
        self.cache = {}
        self.wake = threading.Event()
        self.check_now = threading.Event()
        self.installing = False
        self.prepared = False
        self.has_apt = False
        self.container = False
        self.label = None
        self.last_refresh = 0.0
        self.st = dict(bridge.state.get("os") or {})
        if self.st.get("last_install_status") == "running":
            # The bridge stopped part way through an install (apt itself kept going).
            self.st.update(last_install_status="interrupted",
                           last_install_result="Interrupted: the bridge restarted during the install; "
                                               "check again to see what got installed")

    # ------------------------------------------------------------ setup
    def disk_key(self, path):
        return "disk_used" if path == "/" else f"disk_used_{util.slug(path) or 'root'}"

    def detect_container(self):
        forced = self.o.get("container")
        if forced is not None:
            return bool(forced)
        rc, out, _ = run(["systemd-detect-virt", "--container"], timeout=10, env_path=self.env_path)
        if rc == 0 and out.strip() and out.strip() != "none":
            return True
        if os.path.exists("/run/systemd/container") or os.path.exists("/.dockerenv"):
            return True
        try:
            with open("/proc/1/environ", "rb") as f:
                return b"container=" in f.read()
        except OSError:
            return False

    def prepare(self):
        self.has_apt = bool(which("apt-get", self.env_path) and which("dpkg", self.env_path))
        self.container = self.detect_container()
        self.label = os_label(self.os_release, self.debian_version)
        self.prepared = True
        log.info("OS: %s%s, apt %s", self.label or "unknown OS", " (container)" if self.container else "",
                 "found" if self.has_apt else "not found")

    def keep_while_off(self):
        v = self.o.get("keep_last_value_while_off")
        if v is None:
            return self.bridge.plugin("anthias") is not None
        return bool(v)

    # ------------------------------------------------------------ state
    def snapshot(self):
        with self.lock:
            return {"os": json.loads(json.dumps(self.st))}

    def save(self):
        self.bridge.save_state()

    def busy_reason(self):
        with self.lock:
            return "OS package updates" if self.installing else None

    # ---------------------------------------------------------- publish
    def pub(self, topic, payload, force=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(util.clean(payload), sort_keys=True)
        elif payload is None:
            payload = ""
        else:
            payload = str(payload)
        with self.lock:
            if not force and self.cache.get(topic) == payload:
                return
            self.cache[topic] = payload
        self.bridge.publish(topic, payload, qos=1)

    def republish(self):
        with self.lock:
            cached = dict(self.cache)
        for topic, payload in cached.items():
            self.bridge.publish(topic, payload, qos=1)

    def entities(self):
        b = self.tb
        E = []
        if self.has_apt:
            E.append(("update", "os_packages", "OS packages", {
                "state_topic": self.t_state, "command_topic": self.t_install, "payload_install": INSTALL,
                "json_attributes_topic": self.t_attr, "icon": "mdi:package-variant-closed"}))
        E.append(("binary_sensor", "reboot_required", "Reboot required", {
            "state_topic": self.t_reboot, "json_attributes_topic": self.t_reboot_attr,
            "device_class": "problem", "icon": "mdi:restart-alert", "entity_category": "diagnostic"}))
        for path in self.disk_paths:
            key = self.disk_key(path)
            E.append(("sensor", key, "Disk used" if path == "/" else f"Disk used ({path})", {
                "state_topic": f"{b}/{key}", "json_attributes_topic": f"{b}/{key}/attributes",
                "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:harddisk",
                "suggested_display_precision": 0}))
        E.append(("binary_sensor", "disk_almost_full", "Disk almost full", {
            "state_topic": self.t_full, "json_attributes_topic": self.t_full_attr,
            "device_class": "problem", "icon": "mdi:harddisk-remove"}))
        return E

    def publish_discovery(self):
        br = self.bridge
        published = set()
        keep = self.keep_while_off()
        for component, obj, name, extra in self.entities():
            conf = {"name": name, "unique_id": f"{br.node}_{obj}",
                    "default_entity_id": f"{component}.{br.name}_{obj}",
                    "device": br.device, "origin": br.origin, **extra}
            if not keep:
                conf["availability_topic"] = br.avail_topic
            br.publish(br.discovery_topic(component, obj), conf, qos=1)
            published.add((component, obj))
        if not self.prepared:
            return
        for component, objs in FIXED_OBJECTS.items():
            for obj in objs:
                if (component, obj) not in published:
                    br.publish(br.discovery_topic(component, obj), "", qos=1)

    def publish_update(self):
        if not self.has_apt:
            return
        with self.lock:
            st = json.loads(json.dumps(self.st))
            installing = self.installing
        pkgs = st.get("packages") or []
        n = len(pkgs)
        interrupted = bool(st.get("dpkg_interrupted"))
        installed = self.label or "Installed packages"
        if n:
            latest = f"{n} update{'s' if n != 1 else ''}"
        elif interrupted:
            latest = "Finish interrupted install"
        else:
            latest = installed
        names = [p["name"] for p in pkgs]
        docker = sorted(set(names) & DOCKER_PACKAGES)
        summary = ""
        if n:
            sec = sum(1 for p in pkgs if p.get("security"))
            summary = f"{n} package update{'s' if n != 1 else ''}"
            if sec:
                summary += f" ({sec} security)"
            summary += ": " + ", ".join(names[:12]) + (f" and {n - 12} more" if n > 12 else "") + "."
            if docker:
                summary += " Includes Docker: every container restarts during the install."
        if interrupted:
            summary = ("An earlier package install was interrupted; Install finishes it. " + summary).strip()
        state = {"installed_version": installed, "latest_version": latest, "title": "OS packages",
                 "release_summary": summary[:250], "in_progress": installing}
        if not st.get("last_check"):
            state.pop("latest_version")  # not checked yet: don't claim "up to date"
        self.pub(self.t_state, state)
        attrs = {k: st.get(k) for k in (
            "last_check", "check_error", "kept_back", "last_install", "last_install_finished",
            "last_install_status", "last_install_result", "log_tail", "dpkg_interrupted")}
        attrs.update(os=self.label, container=self.container, pending=n,
                     security=sum(1 for p in pkgs if p.get("security")), restarts_docker=bool(docker),
                     packages=[f"{p['name']} {p.get('from') or '(new)'} → {p['to']}" for p in pkgs[:60]],
                     truncated=n > 60)
        self.pub(self.t_attr, attrs)

    # ------------------------------------------------------------ polls
    def poll_disk(self):
        b = self.tb
        full = []
        details = {}
        for path in self.disk_paths:
            key = self.disk_key(path)
            u = disk_usage(path)
            if u is None:
                details[path] = "unreadable"
                continue
            pct, used, total, avail = u
            self.pub(f"{b}/{key}", pct)
            self.pub(f"{b}/{key}/attributes", {"path": path, "used_gib": gib(used), "total_gib": gib(total),
                                                  "free_gib": gib(avail)})
            details[path] = f"{pct}% used, {gib(avail)} GiB free"
            if pct >= self.warn:
                full.append(path)
        self.pub(self.t_full, "ON" if full else "OFF")
        self.pub(self.t_full_attr, {"warn_percent": self.warn, "full": full, "paths": details})

    def poll_reboot(self):
        reasons, pkgs = [], []
        if os.path.exists(self.reboot_file):
            reasons.append("the system asked for a reboot after an update")
            try:
                with open(self.reboot_file + ".pkgs") as f:
                    pkgs = sorted({ln.strip() for ln in f if ln.strip()})
            except OSError:
                pass
        if not self.container:
            why = kernel_reboot_reason(self.kernel, self.modules_dir)
            if why:
                reasons.append(why)
        self.pub(self.t_reboot, "ON" if reasons else "OFF")
        self.pub(self.t_reboot_attr, {"reasons": reasons, "packages": pkgs, "running_kernel": self.kernel,
                                      "container": self.container})

    def check_packages(self, refresh=True):
        """apt-get update (when refresh), then simulate the upgrade. Skips if apt is busy here."""
        if not self.has_apt:
            return
        if not APT_LOCK.acquire(timeout=600):
            log.info("OS check skipped: apt busy")
            return
        try:
            err = None
            if refresh:
                rc, out, e = run(["apt-get", "update", "-qq", "-o", "DPkg::Lock::Timeout=120"],
                                 timeout=600, env_path=self.env_path)
                if rc != 0:
                    err = "apt-get update failed: " + (e or out).strip()[-300:]
                    log.warning("%s", err)
                self.last_refresh = time.time()
            rc, out, e = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"],
                             timeout=300, env_path=self.env_path)
            arc, aout, _ = run(["dpkg", "--audit"], timeout=60, env_path=self.env_path)
            with self.lock:
                if rc == 0:
                    pkgs, kept = parse_simulation(out)
                    self.st.update(packages=pkgs, kept_back=kept, last_check=util.now_iso())
                else:
                    err = err or "apt-get -s upgrade failed: " + (e or out).strip()[-300:]
                self.st["dpkg_interrupted"] = bool(arc == 0 and aout.strip())
                self.st["check_error"] = err
            self.save()
        finally:
            APT_LOCK.release()
        self.publish_update()

    # ---------------------------------------------------------- commands
    def subscriptions(self):
        return [self.t_install] if self.has_apt else []

    def is_command(self, topic):
        return topic == self.t_install

    def handle(self, topic, payload):
        if topic != self.t_install:
            return False
        if payload != INSTALL or not self.has_apt:
            log.warning("Ignored unexpected command on %s", topic)
            return True
        with self.lock:
            if self.installing:
                log.info("OS install already running; ignored")
                return True
            other = next((p.busy_reason() for p in self.bridge.plugins if p is not self and p.busy_reason()), None)
            if other:
                log.info("OS install not started: %s is running", other)
                self.st.update(last_install=util.now_iso(), last_install_status="refused",
                               last_install_result=f"Not started: {other} is running; try again when it's done")
            else:
                self.installing = True
        if other:
            self.publish_update()
            return True
        threading.Thread(target=self.install, name="os-install", daemon=True).start()
        return True

    def on_check(self):
        self.check_now.set()
        self.wake.set()

    # ------------------------------------------------------------ install
    def install(self):
        try:
            with APT_LOCK:
                self._install()
        except Exception as e:  # noqa: BLE001
            log.exception("OS install crashed")
            with self.lock:
                self.st.update(last_install_status="failed", last_install_result=f"Error: {e}",
                               last_install_finished=util.now_iso())
        finally:
            with self.lock:
                self.installing = False
            self.save()
            self.poll_reboot()
            self.poll_disk()
            self.check_packages(refresh=False)  # the install just ran apt-get update

    def _install(self):
        with self.lock:
            before = len(self.st.get("packages") or [])
            self.st.update(last_install=util.now_iso(), last_install_status="running",
                           last_install_result="Running…", last_install_finished=None, log_tail=None)
        self.save()
        self.publish_update()
        log.info("Installing OS package updates (%d pending)", before)
        log_path = os.path.join(self.bridge.log_dir, "os-update.log")
        env = {"PATH": self.env_path, "LANG": "C.UTF-8", "HOME": os.path.expanduser("~"),
               "DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none",
               "NEEDRESTART_MODE": "l", "NEEDRESTART_SUSPEND": "1", "UCF_FORCE_CONFFOLD": "1"}
        steps = [
            ["dpkg", "--force-confdef", "--force-confold", "--configure", "-a"],
            ["apt-get", "update", "-o", "DPkg::Lock::Timeout=300"],
            ["apt-get", "-y", *APT_OPTS, "--with-new-pkgs", "upgrade"],
        ]
        deadline = time.time() + self.timeout
        status, result = "updated", None
        with open(log_path, "w") as lf:
            lf.write(f"# {self.bridge.service} {VERSION}: OS package update at {util.now_iso()}\n")
            lf.flush()
            for cmd in steps:
                lf.write(f"\n$ {' '.join(cmd)}\n")
                lf.flush()
                left = deadline - time.time()
                try:
                    # Own session: a restart of this service (KillMode=process) doesn't stop apt.
                    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                       env=env, timeout=max(left, 1), start_new_session=True)
                    rc = p.returncode
                except subprocess.TimeoutExpired:
                    status, result = "failed", f"Timed out after {self.timeout // 60} minutes"
                    break
                except OSError as e:
                    status, result = "failed", f"Could not run {cmd[0]}: {e}"
                    break
                if rc != 0:
                    status, result = "failed", f"{' '.join(cmd[:2])} exited with code {rc}"
                    break
        tail = log_tail(log_path)
        if status == "failed":
            hint = failure_hint(tail)
            if hint:
                result += f": {hint}"
        else:
            rc, out, _ = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"],
                             timeout=300, env_path=self.env_path)
            left = len(parse_simulation(out)[0]) if rc == 0 else None
            if before == 0:
                result = "Up to date" if not left else f"{left} update(s) still pending"
            elif left:
                result = f"Installed updates; {left} still pending"
            else:
                result = f"Installed {before} package update{'s' if before != 1 else ''}"
            if os.path.exists(self.reboot_file) or (not self.container and kernel_reboot_reason(
                    self.kernel, self.modules_dir)):
                result += "; a reboot is needed to finish"
        log.info("OS install finished: %s", result)
        with self.lock:
            self.st.update(last_install_status=status, last_install_result=result,
                           last_install_finished=util.now_iso(),
                           log_tail=tail if status == "failed" else None)

    # -------------------------------------------------------------- loop
    def loop(self):
        started = time.time()
        next_poll = 0.0
        next_apt = started + self.first_check
        while not self.bridge.stop.is_set():
            now = time.time()
            if now >= next_poll:
                try:
                    self.poll_disk()
                    self.poll_reboot()
                except Exception:  # noqa: BLE001
                    log.exception("OS poll failed")
                next_poll = now + self.poll
            forced = self.check_now.is_set()
            if (now >= next_apt or forced) and self.bridge.busy_reason():
                next_apt = now + 600  # apt may be in use by that install; look again later
                self.check_now.clear()
            elif now >= next_apt or forced:
                self.check_now.clear()
                # A Check press refreshes the package lists unless that ran a moment ago.
                refresh = not forced or now - self.last_refresh > 120
                try:
                    self.check_packages(refresh=refresh)
                except Exception:  # noqa: BLE001
                    log.exception("OS package check failed")
                next_apt = time.time() + self.check_every
            self.wake.wait(max(1.0, min(next_poll, next_apt) - time.time()))
            self.wake.clear()

    def start(self):
        if not self.prepared:
            self.prepare()
        self.publish_update()
        threading.Thread(target=self.loop, name="os-loop", daemon=True).start()

    def stop(self):
        self.wake.set()

