"""Foundry VTT plugin: server status, logins, worlds, backups and update checks.

Publishes a second Home Assistant device, "Foundry VTT" (identifiers <name>_vtt),
next to the host device. Entities (object IDs fixed; entity IDs <component>.<name>_<key>):

  binary_sensor server
  sensor active_world, players_online, version, core_update, modules_outdated,
         last_restart, last_world_launch, last_backup, world_load_time,
         compatibility_warnings, <system>_version, <system>_update,
         <group>_modules_outdated
  event login (login, login_failed, admin_login, admin_login_failed)
  button launch_world, return_to_setup  (need admin_password)

Game systems are the ones this server's worlds use (or foundry.systems).
Module groups are optional: foundry.module_groups maps a key to a name and a
regular expression on module IDs, and each group gets its own count. A tunnel
connector on the same host is a Compose project: the compose plugin covers it.

Config, all under "foundry":
  status_url (https://localhost/api/status), data_dir (/opt/<name>/data),
  app_dir (/opt/<name>/app), public_url, admin_password, launch_world,
  systems [ids], module_groups {key: {name, pattern, icon}}, held_modules [ids],
  status_interval_seconds (60), update_check_hours (12), warning_window_days (7)
"""

import glob
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import PROJECT, VERSION
from .core import Plugin

log = logging.getLogger("habridge.foundry")

USER_AGENT = f"{PROJECT}/{VERSION}"
RELEASES_URL = "https://foundryvtt.com/releases/"
PRESS = "PRESS"
BUTTONS = ("launch_world", "return_to_setup")
LOGIN_EVENTS = ["login", "login_failed", "admin_login", "admin_login_failed"]

# key -> (component, name, extra discovery fields); the fixed part of the device
BASE_ENTITIES = {
    "server": ("binary_sensor", "Server", {
        "device_class": "connectivity", "payload_on": "ON", "payload_off": "OFF"}),
    "active_world": ("sensor", "Active world", {"icon": "mdi:earth"}),
    "players_online": ("sensor", "Players online", {"icon": "mdi:account-group", "state_class": "measurement"}),
    "version": ("sensor", "Version", {"icon": "mdi:tag", "entity_category": "diagnostic"}),
    "core_update": ("sensor", "Core update", {"icon": "mdi:package-up"}),
    "modules_outdated": ("sensor", "Modules outdated", {"icon": "mdi:puzzle-outline", "state_class": "measurement"}),
    "login": ("event", "Login", {"icon": "mdi:login", "event_types": LOGIN_EVENTS}),
    "last_restart": ("sensor", "Last restart", {"device_class": "timestamp"}),
    "last_world_launch": ("sensor", "Last world launch", {"device_class": "timestamp"}),
    "last_backup": ("sensor", "Last backup", {"device_class": "timestamp"}),
    "world_load_time": ("sensor", "World load time", {
        "device_class": "duration", "unit_of_measurement": "ms", "state_class": "measurement"}),
    "compatibility_warnings": ("sensor", "Compatibility warnings", {
        "icon": "mdi:alert-outline", "state_class": "measurement"}),
    "launch_world": ("button", "Launch world", {"icon": "mdi:play-circle-outline"}),
    "return_to_setup": ("button", "Return to setup", {"icon": "mdi:stop-circle-outline"}),
}


def utc_iso(ts):
    """'2026-10-19 14:47:51' (Foundry log, UTC) -> ISO 8601 with offset."""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------ versions (Foundry's isNewerVersion)
def _parts(v):
    return [p for p in re.split(r"[.\-+]", str(v).strip().lstrip("vV")) if p != ""]


def _cmp_part(a, b):
    if a.isdigit() and b.isdigit():
        return (int(a) > int(b)) - (int(a) < int(b))
    return (a > b) - (a < b)


def compare_versions(a, b, prefix_only=False):
    """1 if a>b, -1 if a<b, 0 if equal. prefix_only compares only as many
    components as the shorter version has (so '14' matches any 14.x)."""
    pa, pb = _parts(a), _parts(b)
    n = min(len(pa), len(pb)) if prefix_only else max(len(pa), len(pb))
    for i in range(n):
        c = _cmp_part(pa[i] if i < len(pa) else "0", pb[i] if i < len(pb) else "0")
        if c:
            return c
    return 0


def is_newer(remote, installed):
    return compare_versions(remote, installed) > 0


def in_range(value, minimum=None, maximum=None):
    if minimum and compare_versions(value, minimum, prefix_only=True) < 0:
        return False
    if maximum and compare_versions(value, maximum, prefix_only=True) > 0:
        return False
    return True


# ------------------------------------------------------------------ HTTP
_local_ctx = ssl.create_default_context()  # the server on this host: self-signed is normal
_local_ctx.check_hostname = False
_local_ctx.verify_mode = ssl.CERT_NONE


def http_get(url, timeout=15, local=False):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_local_ctx if local else None) as r:
        return r.read().decode("utf-8", errors="replace")


def http_json(url, timeout=15, local=False):
    return json.loads(http_get(url, timeout, local))


class FoundryPlugin(Plugin):
    name = "foundry"
    state_keys = ("foundry",)

    RE_LOGIN_OK = re.compile(r"User authentication successful for user (.+)$")
    RE_LOGIN_FAIL = re.compile(r"User authentication failed for user (.+?): (.+)$")
    RE_ADMIN_OK = re.compile(r"Administrator authentication successful")
    RE_ADMIN_FAIL = re.compile(r"Administrator authentication failed[^:]*: (.+)$")
    RE_STARTED = re.compile(r"^Server started and listening on port")
    RE_WORLD_DONE = re.compile(r"^Launching World \| Complete")
    RE_VENDED = re.compile(r"^Vended World data to User \[(\w+)\] in (\d+)ms")
    RE_CORE_AVAIL = re.compile(r"Core software \w+ update (\S+) is available")
    RE_COMPAT = re.compile(r"deprecat|compatib|no longer supported", re.I)

    def __init__(self, bridge, cfg):
        super().__init__(bridge, cfg)
        f = cfg.get("foundry") or {}
        n = bridge.name
        self.f = f
        self.status_url = f.get("status_url", "https://localhost/api/status")
        self.data_dir = f.get("data_dir", f"/opt/{n}/data")
        self.app_dir = f.get("app_dir", f"/opt/{n}/app")
        self.public_url = f.get("public_url")
        self.logs_dir = os.path.join(self.data_dir, "Logs")
        self.status_interval = int(f.get("status_interval_seconds", 60))
        self.check_interval = float(f.get("update_check_hours", 12)) * 3600
        self.held = set(f.get("held_modules") or [])
        self.groups = {}
        for key, g in (f.get("module_groups") or {}).items():
            if not re.fullmatch(r"[a-z0-9_]+", key) or not (g or {}).get("pattern"):
                raise SystemExit(f"foundry.module_groups.{key}: needs a lowercase key and a pattern")
            self.groups[key] = {"name": g.get("name") or f"{key} modules outdated",
                                "re": re.compile(g["pattern"], re.I),
                                "icon": g.get("icon", "mdi:puzzle-outline")}
        self.warning_days = int(f.get("warning_window_days", 7))
        self.admin_password = f.get("admin_password") or None
        self.launch_world_id = f.get("launch_world") or None
        self.releases_url = f.get("releases_url", RELEASES_URL)
        u = urllib.parse.urlsplit(self.status_url)
        self.setup_url = f"{u.scheme}://{u.netloc}/setup"

        self.node = f"{n}_vtt"  # discovery node, device identifier and unique_id prefix
        self.t = lambda key, kind="state": f"{bridge.base}/{key}/{kind}"
        self.st = json.loads(json.dumps(bridge.state.get("foundry") or {}))
        self.lock = threading.Lock()
        self.check_lock = threading.Lock()  # scheduled and button-pressed checks never overlap
        self.action_busy = threading.Lock()
        self.systems = list(f.get("systems") or [])
        self.system_versions = {}
        self.foundry_version = None
        self.active_world = None
        self.prepared = False

    # ------------------------------------------------------------ state
    def snapshot(self):
        with self.lock:
            return {"foundry": json.loads(json.dumps(self.st))}

    def save(self):
        self.bridge.save_state()

    # ---------------------------------------------------------- entities
    def entities(self):
        ents = dict(BASE_ENTITIES)
        for sid in self.systems:
            ents[f"{sid}_version"] = ("sensor", f"{sid} version", {"icon": "mdi:dice-d20",
                                                                    "entity_category": "diagnostic"})
            ents[f"{sid}_update"] = ("sensor", f"{sid} update", {"icon": "mdi:dice-d20-outline"})
        for key, g in self.groups.items():
            ents[f"{key}_modules_outdated"] = ("sensor", g["name"], {"icon": g["icon"],
                                                                     "state_class": "measurement"})
        return ents

    def device(self):
        dev = {"identifiers": [self.node], "name": self.f.get("device_name", "Foundry VTT"),
               "manufacturer": "Foundry Gaming", "model": "Foundry Virtual Tabletop"}
        if self.foundry_version:
            dev["sw_version"] = self.foundry_version
        if self.public_url:
            dev["configuration_url"] = self.public_url
        return dev

    def discovery_topic(self, component, key):
        return f"homeassistant/{component}/{self.node}/{key}/config"

    def publish_discovery(self):
        br, dev, avail = self.bridge, self.device(), self.bridge.avail_topic
        ents = self.entities()
        for key, (component, name, extra) in ents.items():
            p = {"name": name, "unique_id": f"{self.node}_{key}",
                 "default_entity_id": f"{component}.{br.name}_{key}", "has_entity_name": True,
                 "availability_topic": avail, "device": dev, "origin": br.origin, **extra}
            if component == "event":
                p["state_topic"] = self.t(key, "event")
            elif component == "button":
                del p["availability_topic"]
                p["availability"] = [{"topic": avail}, {"topic": self.t(key, "available")}]
                p["availability_mode"] = "all"
                p["command_topic"] = self.t(key, "press")
                p["payload_press"] = PRESS
                p["json_attributes_topic"] = self.t(key, "attributes")
            else:
                p["state_topic"] = self.t(key)
                p["json_attributes_topic"] = self.t(key, "attributes")
            br.publish(self.discovery_topic(component, key), p, qos=1)
        if self.prepared:
            # A system or module group no longer configured: remove its entity once.
            with self.lock:
                before = self.st.get("dynamic", {})
                now = {k: c for k, (c, _, _) in ents.items() if k not in BASE_ENTITIES}
                self.st["dynamic"] = now
            for key, component in before.items():
                if key not in now:
                    br.publish(self.discovery_topic(component, key), "", qos=1)

    # --------------------------------------------------------- publishing
    def pub(self, key, state=None, attrs=None):
        if state is not None:
            self.bridge.publish(self.t(key), str(state), qos=1)
        if attrs is not None:
            self.bridge.publish(self.t(key, "attributes"), json.dumps(attrs), qos=1)
        with self.lock:
            cache = self.st.setdefault("published", {}).setdefault(key, {})
            if state is not None:
                cache["state"] = str(state)
            if attrs is not None:
                cache["attrs"] = attrs

    def republish(self):
        with self.lock:
            cached = json.loads(json.dumps(self.st.get("published", {})))
        for key, v in cached.items():
            if "state" in v:
                self.bridge.publish(self.t(key), v["state"], qos=1)
            if "attrs" in v:
                self.bridge.publish(self.t(key, "attributes"), json.dumps(v["attrs"]), qos=1)

    # ------------------------------------------------------------ systems
    def read_package(self, kind, pid):
        path = os.path.join(self.data_dir, "Data", kind, pid, f"{kind[:-1]}.json")
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    def detect_systems(self):
        """Systems this server's worlds use; all installed ones if there are no worlds."""
        used = set()
        for p in glob.glob(os.path.join(self.data_dir, "Data", "worlds", "*", "world.json")):
            try:
                with open(p) as fh:
                    sid = (json.load(fh) or {}).get("system")
            except Exception:  # noqa: BLE001
                continue
            if sid and re.fullmatch(r"[a-z0-9_-]+", sid):
                used.add(sid)
        if not used:
            for p in glob.glob(os.path.join(self.data_dir, "Data", "systems", "*", "system.json")):
                used.add(os.path.basename(os.path.dirname(p)))
        return sorted(s for s in used if re.fullmatch(r"[a-z0-9_]+", s))

    def prepare(self):
        if self.prepared:
            return
        if not self.systems:
            self.systems = self.detect_systems()
        log.info("Foundry: game systems %s, module groups %s", ", ".join(self.systems) or "none",
                 ", ".join(self.groups) or "none")
        self.prepared = True

    # ------------------------------------------------------------- status
    def read_installed_core(self):
        try:
            with open(os.path.join(self.app_dir, "package.json")) as fh:
                rel = json.load(fh).get("release", {})
            return f"{rel['generation']}.{rel['build']}"
        except Exception:  # noqa: BLE001
            return None

    def poll_status(self):
        status = None
        try:
            status = http_json(self.status_url, timeout=10, local=True)
        except Exception as e:  # noqa: BLE001
            log.debug("status poll failed: %s", e)

        if status is None:
            self.pub("server", "OFF", {"error": "Foundry did not answer /api/status"})
            self.pub("players_online", 0)
            version = self.read_installed_core()
        else:
            world = status.get("world") if status.get("active") else None
            self.active_world = world
            self.pub("server", "ON", {"uptime_seconds_reported": status.get("uptime")})
            self.pub("active_world", world or "No world loaded", {"world_id": world, "system": status.get("system")})
            self.pub("players_online", int(status.get("users") or 0))
            version = status.get("version") or self.read_installed_core()
            if status.get("systemVersion") and status.get("system") in self.systems:
                self.system_versions[status["system"]] = status["systemVersion"]
        active_system = status.get("system") if status and status.get("active") else None
        for sid in self.systems:
            if sid != active_system or sid not in self.system_versions:
                pkg = self.read_package("systems", sid)
                if pkg and pkg.get("version"):
                    self.system_versions[sid] = pkg["version"]
        if version and version != self.foundry_version:
            self.foundry_version = version
            self.publish_discovery()  # the device's sw_version
        if self.foundry_version:
            gen, _, build = self.foundry_version.partition(".")
            self.pub("version", self.foundry_version, {"generation": gen, "build": build})
        for sid, v in self.system_versions.items():
            self.pub(f"{sid}_version", v)
        self.scan_backups()
        self.publish_core_update()
        self.publish_button_availability(status)

    # ------------------------------------------- launch / return-to-setup
    def world_to_launch(self):
        if self.launch_world_id:
            return self.launch_world_id
        worlds = [os.path.basename(os.path.dirname(p)) for p in
                  glob.glob(os.path.join(self.data_dir, "Data", "worlds", "*", "world.json"))]
        return worlds[0] if len(worlds) == 1 else None

    def publish_button_availability(self, status):
        up = status is not None
        running = bool(up and status.get("active"))
        ok = bool(self.admin_password) and not self.action_busy.locked()
        launch = ok and up and not running and self.world_to_launch() is not None
        self.bridge.publish(self.t("launch_world", "available"), "online" if launch else "offline", qos=1)
        self.bridge.publish(self.t("return_to_setup", "available"), "online" if ok and running else "offline", qos=1)

    def setup_post(self, body):
        data = json.dumps({**body, "adminPassword": self.admin_password}).encode()
        req = urllib.request.Request(self.setup_url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT,
            "Sec-Fetch-Site": "same-origin"})  # Foundry rejects POSTs that don't look same-origin
        try:
            with urllib.request.urlopen(req, timeout=180, context=_local_ctx) as r:
                text, code = r.read().decode("utf-8", errors="replace"), r.status
        except urllib.error.HTTPError as e:
            text, code = e.read().decode("utf-8", errors="replace"), e.code
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {"raw": text[:300]}
        if code >= 400 or (isinstance(parsed, dict) and parsed.get("error")):
            msg = (parsed.get("error") or parsed.get("message") or parsed.get("raw")) if isinstance(parsed, dict) else text
            raise RuntimeError(f"HTTP {code}: {str(msg)[:200]}")
        return parsed

    def run_action(self, key):
        if not self.admin_password:
            log.warning("%s pressed but foundry.admin_password is not set", key)
            return
        if not self.action_busy.acquire(blocking=False):
            log.info("%s pressed while another action is running; ignored", key)
            return
        result = "success"
        try:
            self.publish_button_availability(None)  # grey both out while working
            if key == "launch_world":
                world = self.world_to_launch()
                if not world:
                    raise RuntimeError("no world configured (set foundry.launch_world)")
                log.info("launching world %s", world)
                self.setup_post({"action": "launchWorld", "world": world})
            else:
                log.info("returning to setup")
                self.setup_post({"action": "worldShutdown"})
        except Exception as e:  # noqa: BLE001
            result = f"failed: {e}"
            log.warning("%s %s", key, result)
        finally:
            self.action_busy.release()
        self.pub(key, attrs={"last_pressed": now_utc(), "last_result": result})
        for delay in (2, 8, 20):  # refresh quickly so the dashboard catches up
            if self.bridge.stop.wait(delay):
                break
            try:
                self.poll_status()
            except Exception:  # noqa: BLE001
                log.exception("status poll after %s failed", key)

    def scan_backups(self):
        root = os.path.join(self.data_dir, "Backups")
        newest, count, total = None, 0, 0
        for dirpath, _, files in os.walk(root):
            for name in files:
                if name.endswith(".json"):
                    continue  # metadata sidecar written next to each backup
                p = os.path.join(dirpath, name)
                try:
                    s = os.stat(p)
                except OSError:
                    continue
                count += 1
                total += s.st_size
                if newest is None or s.st_mtime > newest[1]:
                    newest = (p, s.st_mtime, s.st_size)
        if newest:
            parts = os.path.relpath(newest[0], root).split(os.sep)
            self.pub("last_backup", datetime.fromtimestamp(newest[1], timezone.utc).replace(microsecond=0).isoformat(),
                     {"file": os.path.basename(newest[0]),
                      "package_type": parts[0] if len(parts) > 1 else None,
                      "package": parts[1] if len(parts) > 2 else None,
                      "size_mb": round(newest[2] / 1048576, 1), "backup_count": count,
                      "total_size_mb": round(total / 1048576, 1)})

    # ---------------------------------------------------------- the log
    def handle_line(self, line, live):
        try:
            rec = json.loads(line)
        except ValueError:
            return
        msg = str(rec.get("message", ""))
        first = msg.split("\n", 1)[0].strip()
        ts = rec.get("timestamp")
        if not ts:
            return
        try:
            iso = utc_iso(ts)
        except ValueError:
            return
        st = self.st
        m = self.RE_LOGIN_OK.search(first)
        if m:
            return self._login("login", iso, live, user=m.group(1))
        m = self.RE_LOGIN_FAIL.search(first)
        if m:
            return self._login("login_failed", iso, live, user=m.group(1), reason=m.group(2))
        if self.RE_ADMIN_OK.search(first):
            return self._login("admin_login", iso, live, user="Administrator")
        m = self.RE_ADMIN_FAIL.search(first)
        if m:
            return self._login("admin_login_failed", iso, live, user="Administrator", reason=m.group(1))
        if self.RE_STARTED.search(first):
            with self.lock:
                st["last_restart"] = iso
                st["restarts"] = [t for t in st.get("restarts", []) if t > self._cutoff(30)] + [iso]
                n = len(st["restarts"])
            self.pub("last_restart", iso, {"restarts_last_30_days": n})
            return
        if self.RE_WORLD_DONE.search(first):
            st["last_world_launch"] = iso
            self.pub("last_world_launch", iso, {"world": self.active_world or st.get("last_world")})
            return
        m = self.RE_VENDED.search(first)
        if m:
            self.pub("world_load_time", int(m.group(2)), {"user_id": m.group(1), "measured_at": iso})
            return
        m = self.RE_CORE_AVAIL.search(first)
        if m:
            st["core_latest_from_log"] = m.group(1)
            if live:
                self.publish_core_update()
            return
        if rec.get("level") == "warn" and self.RE_COMPAT.search(msg):
            with self.lock:
                st.setdefault("compat_warnings", {})[first[:300]] = iso
            self.publish_warnings()

    def _cutoff(self, days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def publish_warnings(self):
        cutoff = self._cutoff(self.warning_days)
        with self.lock:
            warns = {k: v for k, v in self.st.get("compat_warnings", {}).items() if v >= cutoff}
            self.st["compat_warnings"] = warns
        items = sorted(warns.items(), key=lambda kv: kv[1], reverse=True)
        self.pub("compatibility_warnings", len(items), {
            "window_days": self.warning_days,
            "warnings": [{"message": k, "last_seen": v} for k, v in items]})

    def _login(self, event_type, iso, live, user, reason=None):
        if not live:
            return  # history is in HA's logbook; old logins are never replayed
        payload = {"event_type": event_type, "user": user, "time": iso}
        if reason:
            payload["reason"] = reason
        self.bridge.publish(self.t("login", "event"), json.dumps(payload), retain=False, qos=1)
        log.info("login event: %s", payload)

    def log_files(self):
        return sorted(glob.glob(os.path.join(self.logs_dir, "debug*.log")))

    def replay_history(self):
        """Rebuild 'last X' values from the logs Foundry still keeps."""
        for path in self.log_files():
            try:
                with open(path, errors="replace") as fh:
                    for line in fh:
                        self.handle_line(line, live=False)
            except OSError:
                pass
        self.publish_warnings()

    def follow_logs(self):
        current, fh, buf = None, None, ""
        files = self.log_files()
        if files:
            current = files[-1]
            fh = open(current, errors="replace")
            fh.seek(0, os.SEEK_END)
        try:
            while not self.bridge.stop.is_set():
                files = self.log_files()
                newest = files[-1] if files else None
                if newest and newest != current:
                    if fh:  # drain the old file before switching
                        buf += fh.read()
                        fh.close()
                        for line in buf.splitlines():
                            self.handle_line(line, live=True)
                        buf = ""
                    current = newest
                    fh = open(current, errors="replace")
                    log.info("following %s", current)
                if fh:
                    chunk = fh.read()
                    if chunk:
                        buf += chunk
                        *lines, buf = buf.split("\n")
                        for line in lines:
                            self.handle_line(line, live=True)
                        self.save()
                    elif os.path.exists(current) and os.path.getsize(current) < fh.tell():
                        fh.seek(0)  # truncated
                self.bridge.stop.wait(1)
        finally:
            if fh:
                fh.close()

    # ------------------------------------------------------------ updates
    def fetch_core_latest(self):
        html = http_get(self.releases_url, timeout=20)
        links = list(re.finditer(r'href="(?:https://foundryvtt\.com)?/releases/(\d+)\.(\d+)/?"', html))
        best = None
        for i, m in enumerate(links):
            end = links[i + 1].start() if i + 1 < len(links) else m.end() + 600
            window = re.sub(r"<[^>]+>", " ", html[m.end():min(end, m.end() + 600)])
            if not re.search(r"\bStable\b", window):
                continue
            v = f"{m.group(1)}.{m.group(2)}"
            if best is None or is_newer(v, best):
                best = v
        if best is None:
            raise ValueError("no stable releases found on the releases page")
        return best

    def publish_core_update(self):
        installed = self.foundry_version
        if not installed:
            return
        latest = installed
        for v in (self.st.get("core_latest_web"), self.st.get("core_latest_from_log")):
            if v and is_newer(v, latest):
                latest = v
        available = is_newer(latest, installed)
        self.pub("core_update", f"{latest} available" if available else "Up to date",
                 {"installed": installed, "latest": latest, "update_available": available,
                  "release_notes": f"https://foundryvtt.com/releases/{latest}",
                  "last_checked": self.st.get("core_checked_at"),
                  "check_error": self.st.get("core_check_error")})

    def manifest_check(self, pkg_id, pkg):
        info = {"id": pkg_id, "title": pkg.get("title") or pkg_id, "installed": pkg.get("version")}
        url = pkg.get("manifest")
        if not url:
            return {**info, "status": "unchecked", "reason": "no manifest URL"}
        try:
            remote = http_json(url, timeout=20)
        except Exception as e:  # noqa: BLE001
            return {**info, "status": "unchecked", "reason": type(e).__name__}
        rv = remote.get("version")
        info["available"] = rv
        if not rv or not info["installed"] or not is_newer(rv, info["installed"]):
            info["status"] = "current"
            return info
        info["status"] = "outdated"
        comp = remote.get("compatibility") or {}
        ok = True
        if self.foundry_version and not in_range(self.foundry_version, comp.get("minimum"), comp.get("maximum")):
            ok = False
        for rel in (remote.get("relationships") or {}).get("systems") or []:
            have = self.system_versions.get(rel.get("id"))
            if have:
                c = rel.get("compatibility") or {}
                if not in_range(have, c.get("minimum"), c.get("maximum")):
                    ok = False
        info["compatible"] = ok
        return info

    def check_updates(self):
        if not self.check_lock.acquire(blocking=False):
            log.info("Foundry update check already running")
            return
        try:
            self._check_updates()
        except Exception:  # noqa: BLE001
            log.exception("Foundry update check crashed")
        finally:
            self.check_lock.release()

    def _check_updates(self):
        log.info("checking Foundry, game systems and modules for updates")
        try:
            self.st["core_latest_web"] = self.fetch_core_latest()
            self.st["core_check_error"] = None
        except Exception as e:  # noqa: BLE001
            self.st["core_check_error"] = str(e)[:200]
            log.warning("core update check failed: %s", e)
        self.st["core_checked_at"] = now_utc()
        self.publish_core_update()

        for sid in self.systems:
            pkg = self.read_package("systems", sid)
            if not pkg:
                continue
            r = self.manifest_check(sid, pkg)
            if r["status"] == "outdated":
                state = f"{r['available']} available" + ("" if r.get("compatible") else " (incompatible)")
            elif r["status"] == "current":
                state = "Up to date"
            else:
                state = "Unknown"
            self.pub(f"{sid}_update", state, {**r, "last_checked": now_utc()})

        mods = []
        for path in glob.glob(os.path.join(self.data_dir, "Data", "modules", "*", "module.json")):
            try:
                with open(path) as fh:
                    mods.append((os.path.basename(os.path.dirname(path)), json.load(fh)))
            except Exception:  # noqa: BLE001
                continue
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda t: self.manifest_check(*t), mods))

        buckets = {"modules_outdated": []}
        buckets.update({f"{k}_modules_outdated": [] for k in self.groups})
        for r in results:
            key = next((f"{k}_modules_outdated" for k, g in self.groups.items() if g["re"].search(r["id"])),
                       "modules_outdated")
            buckets[key].append(r)
        for key, rs in buckets.items():
            held = [r for r in rs if r["id"] in self.held]
            rest = [r for r in rs if r["id"] not in self.held]
            outdated = sorted((r for r in rest if r["status"] == "outdated"), key=lambda r: r["title"].lower())
            self.pub(key, len(outdated), {
                "outdated": [f"{r['title']}: {r['installed']} → {r['available']}" for r in outdated],
                "incompatible_updates": [r["title"] for r in outdated if not r.get("compatible", True)],
                "held": [f"{r['title']}: {r['installed']}"
                         + (f" ({r['available']} available)" if r["status"] == "outdated" else "") for r in held],
                "unchecked": sorted(r["title"] for r in rest if r["status"] == "unchecked"),
                "installed_count": len(rs),
                "last_checked": now_utc()})
        self.st["updates_checked_at"] = time.time()
        self.save()
        log.info("Foundry update check done: %d modules", len(results))

    def update_loop(self):
        delay = max(30, self.check_interval - (time.time() - float(self.st.get("updates_checked_at", 0))))
        while not self.bridge.stop.wait(delay):
            self.check_updates()
            delay = self.check_interval

    def status_loop(self):
        while not self.bridge.stop.wait(self.status_interval):
            try:
                self.poll_status()
                self.publish_warnings()
                self.save()
            except Exception:  # noqa: BLE001
                log.exception("Foundry status poll crashed")

    # -------------------------------------------------------- commands
    def subscriptions(self):
        return [self.t(k, "press") for k in BUTTONS]

    def is_command(self, topic):
        return topic in self.subscriptions()

    def handle(self, topic, payload):
        for key in BUTTONS:
            if topic == self.t(key, "press"):
                if payload == PRESS:
                    threading.Thread(target=self.run_action, args=(key,), daemon=True).start()
                else:
                    log.warning("Ignoring unexpected message on %s", topic)
                return True
        return False

    def on_check(self):
        threading.Thread(target=self.check_updates, daemon=True).start()

    def start(self):
        self.prepare()
        try:
            self.poll_status()
            self.replay_history()
        except Exception:  # noqa: BLE001
            log.exception("Foundry first poll failed")
        self.save()
        threading.Thread(target=self.follow_logs, name="foundry-logs", daemon=True).start()
        threading.Thread(target=self.update_loop, name="foundry-updates", daemon=True).start()
        threading.Thread(target=self.status_loop, name="foundry-status", daemon=True).start()
