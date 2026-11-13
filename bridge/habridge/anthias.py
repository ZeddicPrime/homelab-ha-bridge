"""Anthias plugin: a Raspberry Pi running the Anthias digital-signage stack.

Made for a Pi that is switched off most of the time (for example on the same
smart plug as its display): entities split into three kinds.

  * Online (binary_sensor.<name>_online) is the bridge's own last-will topic,
    so it turns off when the Pi shuts down or loses power.
  * Health and history keep their last value while the Pi is off (no
    availability): undervoltage, storage, Anthias web, CEC adapter, last boot,
    last action, and the read-only Anthias update entity.
  * Live values and buttons are unavailable while it is off: CPU temperature,
    projector standby/wake (HDMI-CEC through Anthias), next clip, restart
    viewer, reboot, shut down, check for Anthias update.

Shut down and Reboot run systemctl poweroff/reboot after publishing the result,
so a power cut that follows finds a cleanly stopped Pi. Nothing in a message is
ever executed; each button runs one fixed action, one at a time.

Config, all under "anthias":
  url (http://127.0.0.1), username, password   the Anthias web login
  containers {viewer, redis}   default anthias-anthias-viewer-1, anthias-redis-1
  poll_seconds (30), boot_grace_seconds (180), api_failures_before_problem (3),
  auto_fix_cec (true), cec_fix_after_seconds (60), update_check_hours (6),
  github_repo (Screenly/Anthias), busy_wait_minutes (45): how long Shut down and
  Reboot wait for an install on this Pi (OS packages, a bridge update) to finish
"""

import base64
import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import PROJECT, VERSION, util
from .core import Plugin

log = logging.getLogger("habridge.anthias")

PRESS = "PRESS"
BUTTONS = {
    "projector_standby": {"name": "Projector standby", "icon": "mdi:projector-off"},
    "projector_wake": {"name": "Projector wake", "icon": "mdi:projector"},
    "next_clip": {"name": "Next clip", "icon": "mdi:skip-next"},
    "restart_viewer": {"name": "Restart viewer", "icon": "mdi:restart", "entity_category": "config"},
    "reboot": {"name": "Reboot", "device_class": "restart", "entity_category": "config"},
    "shutdown": {"name": "Shut down", "icon": "mdi:power", "entity_category": "config"},
    "check_update": {"name": "Check for Anthias update", "icon": "mdi:update", "entity_category": "diagnostic"},
}
DEFAULTS = {
    "url": "http://127.0.0.1", "username": "", "password": "",
    "containers": {"viewer": "anthias-anthias-viewer-1", "redis": "anthias-redis-1"},
    "poll_seconds": 30, "boot_grace_seconds": 180, "api_failures_before_problem": 3,
    "auto_fix_cec": True, "cec_fix_after_seconds": 60, "update_check_hours": 6,
    "github_repo": "Screenly/Anthias", "github_api": "https://api.github.com",
    # Where the host reports itself; tests point these elsewhere.
    "vcgencmd": "vcgencmd", "systemctl": "systemctl",
    "uptime_file": "/proc/uptime", "boot_id_file": "/proc/sys/kernel/random/boot_id",
    "thermal_file": "/sys/class/thermal/thermal_zone0/temp", "model_file": "/proc/device-tree/model",
}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_text(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "not found: " + cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out: " + " ".join(cmd)


def parse_docker_time(s):
    """2026-11-02T04:52:10.123456789Z -> epoch seconds, or None."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", s or "")
    if not m or s.startswith("0001-"):
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def short_summary(body):
    """First prose paragraph of release notes, plain text, at most 250 chars."""
    for para in re.split(r"\n\s*\n", body or ""):
        lines = []
        for line in para.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("<!--"):
                continue
            line = re.sub(r"[*_`]", "", line)
            line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
            line = re.sub(r"^[-+] ", "", line)
            lines.append(line)
        text = " ".join(lines).strip()
        if text:
            return text if len(text) <= 250 else text[:247].rstrip() + "..."
    return ""


class AnthiasPlugin(Plugin):
    name = "anthias"
    state_keys = ("anthias",)

    def __init__(self, bridge, cfg):
        super().__init__(bridge, cfg)
        a = dict(DEFAULTS)
        for k, v in (cfg.get("anthias") or {}).items():
            a[k] = dict(a[k], **v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
        self.a = a
        b = bridge
        # A Pi is its own hardware: describe it as such unless the config says otherwise.
        self.model = (read_text(a["model_file"], "") or "").replace("\x00", "").strip() or "Raspberry Pi"
        if "device_id" not in cfg:
            b.device["identifiers"] = [f"{b.name}_pi"]
        if "device_manufacturer" not in cfg:
            b.device["manufacturer"] = "Raspberry Pi"
        if "device_model" not in cfg:
            b.device["model"] = self.model

        self.lock = threading.Lock()          # state and the publish cache
        self.action_lock = threading.Lock()   # one button action at a time
        self.cache = {}                       # topic -> payload (retained states to resend)
        self.store = json.loads(json.dumps(b.state.get("anthias") or {}))
        self.boot_id = (read_text(a["boot_id_file"], "") or "").strip() or "unknown"
        self.last_boot = datetime.fromtimestamp(time.time() - self.uptime(), timezone.utc).replace(
            second=0, microsecond=0).isoformat()
        self.api_failures = 0
        self.api_last_ok = None
        self.last_temp = None
        self.installed_full = None
        self.last_update_force = 0.0
        if self.store.get("boot_id") != self.boot_id:
            log.info("New boot session (%s)", self.boot_id)
            self.store.update(boot_id=self.boot_id, cec_fix_attempted=False, uv_counted=False)

    # ------------------------------------------------------------ state
    def snapshot(self):
        with self.lock:
            return {"anthias": json.loads(json.dumps(self.store))}

    def save(self):
        self.bridge.save_state()

    def uptime(self):
        try:
            return float((read_text(self.a["uptime_file"], "") or "").split()[0])
        except (IndexError, ValueError):
            return 0.0

    # ---------------------------------------------------------- publish
    def t(self, *parts):
        return "/".join((self.bridge.base,) + parts)

    def pub(self, topic, payload, force=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, sort_keys=True)
        payload = str(payload)
        with self.lock:
            if not force and self.cache.get(topic) == payload:
                return None
            self.cache[topic] = payload
        return self.bridge.publish(topic, payload, qos=1)

    def republish(self):
        with self.lock:
            cached = dict(self.cache)
        for topic, payload in cached.items():
            self.bridge.publish(topic, payload, qos=1)

    def entities(self):
        avail = self.bridge.avail_topic
        ents = [
            ("binary_sensor", "online", False, {
                "name": "Online", "device_class": "connectivity", "state_topic": avail,
                "payload_on": "online", "payload_off": "offline"}),
            ("binary_sensor", "undervoltage", False, {
                "name": "Undervoltage", "device_class": "problem", "state_topic": self.t("undervoltage"),
                "json_attributes_topic": self.t("undervoltage", "attributes")}),
            ("binary_sensor", "storage", False, {
                "name": "Storage", "device_class": "problem", "entity_category": "diagnostic",
                "state_topic": self.t("storage"), "json_attributes_topic": self.t("storage", "attributes")}),
            ("binary_sensor", "anthias", False, {
                "name": "Anthias web", "device_class": "problem", "entity_category": "diagnostic",
                "state_topic": self.t("anthias"), "json_attributes_topic": self.t("anthias", "attributes")}),
            ("binary_sensor", "cec", False, {
                "name": "CEC adapter", "icon": "mdi:hdmi-port", "entity_category": "diagnostic",
                "state_topic": self.t("cec"), "json_attributes_topic": self.t("cec", "attributes")}),
            ("sensor", "last_boot", False, {
                "name": "Last boot", "device_class": "timestamp", "entity_category": "diagnostic",
                "state_topic": self.t("last_boot")}),
            ("sensor", "last_action", False, {
                "name": "Last action", "icon": "mdi:history", "entity_category": "diagnostic",
                "state_topic": self.t("last_action"), "json_attributes_topic": self.t("last_action", "attributes")}),
            ("sensor", "cpu_temperature", True, {
                "name": "CPU temperature", "device_class": "temperature", "unit_of_measurement": "°C",
                "state_class": "measurement", "suggested_display_precision": 1, "entity_category": "diagnostic",
                "state_topic": self.t("cpu_temperature")}),
            ("update", "anthias", False, {
                "name": "Anthias", "title": "Anthias", "state_topic": self.t("update", "state"),
                "json_attributes_topic": self.t("update", "attributes")}),
        ]
        for key, extra in BUTTONS.items():
            ents.append(("button", key, True, dict(extra, command_topic=self.t("cmd", key), payload_press=PRESS)))
        return ents

    def publish_discovery(self):
        br = self.bridge
        for component, key, live, extra in self.entities():
            conf = dict(extra, unique_id=f"{br.node}_{key}", default_entity_id=f"{component}.{br.name}_{key}",
                        device=br.device, origin=br.origin)
            if live:
                conf["availability_topic"] = br.avail_topic
            br.publish(br.discovery_topic(component, key), conf, qos=1)

    # ---------------------------------------------------------- commands
    def subscriptions(self):
        return [self.t("cmd", "+")]

    def is_command(self, topic):
        return topic.startswith(self.t("cmd", ""))

    def handle(self, topic, payload):
        if not self.is_command(topic):
            return False
        key = topic[len(self.t("cmd", "")):]
        if key not in BUTTONS or payload != PRESS:
            log.warning("Ignoring %r on %s", payload[:40], topic)
            return True
        if not self.action_lock.acquire(blocking=False):
            log.warning("Refusing %s: another action is running", key)
            self.set_last_action(key, False, "Refused: another action is still running")
            return True
        threading.Thread(target=self._run_action, args=(key,), daemon=True).start()
        return True

    def on_check(self):
        threading.Thread(target=self.check_update, kwargs={"force": True}, daemon=True).start()

    def set_last_action(self, key, ok, detail):
        label = BUTTONS.get(key, {}).get("name", key)
        self.pub(self.t("last_action"), f"{label}: {'OK' if ok else 'failed'}"[:250])
        return self.pub(self.t("last_action", "attributes"),
                        {"action": key, "ok": bool(ok), "detail": str(detail)[:500], "at": now_iso()})

    def _run_action(self, key):
        try:
            ok, detail = self.do_action(key)
            log.info("Action %s: %s (%s)", key, "ok" if ok else "failed", detail)
            if key not in ("reboot", "shutdown"):
                self.set_last_action(key, ok, detail)
        except Exception as e:  # noqa: BLE001
            log.exception("Action %s crashed", key)
            self.set_last_action(key, False, f"Error: {e}")
        finally:
            self.action_lock.release()

    def do_action(self, key):
        if key in ("projector_standby", "projector_wake"):
            status, body = self.api("POST", f"/api/v2/display/{'off' if key == 'projector_standby' else 'on'}")
            msg = body
            try:
                msg = json.loads(body).get("message", body)
            except (ValueError, AttributeError):
                pass
            return status == 200, f"HTTP {status}: {msg}"
        if key == "next_clip":
            status, body = self.api("GET", "/api/v2/assets/control/next")
            return status == 200, f"HTTP {status}: {body[:200]}"
        if key == "restart_viewer":
            rc, out = run([util.DOCKER, "restart", self.a["containers"]["viewer"]], timeout=120)
            return rc == 0, out.strip()[:300] or f"exit {rc}"
        if key in ("reboot", "shutdown"):
            verb = "reboot" if key == "reboot" else "poweroff"
            # An install (OS packages, a bridge update) must not be cut short: wait for it,
            # then go ahead, so a power-off routine that waits for Online to turn off still works.
            deadline = time.time() + float(self.a.get("busy_wait_minutes", 45)) * 60
            why = self.bridge.busy_reason()
            if why:
                label = BUTTONS[key]["name"]
                log.info("%s waits for %s to finish", label, why)
                self.pub(self.t("last_action"), f"{label}: waiting for {why}"[:250])
                while why and time.time() < deadline and not self.bridge.stop.wait(5):
                    why = self.bridge.busy_reason()
                if why:
                    return False, f"Not done: {why} still running after {self.a.get('busy_wait_minutes', 45)} minutes"
            # Say so first: once systemd stops this service nothing more gets out.
            info = self.set_last_action(key, True, f"systemctl {verb} requested")
            self.save()
            if info is not None:
                try:
                    info.wait_for_publish(3)
                except Exception:  # noqa: BLE001  (older paho has no timeout)
                    time.sleep(1)
            rc, out = run([self.a["systemctl"], verb], timeout=30)
            return rc == 0, out.strip()[:300] or f"systemctl {verb} exit {rc}"
        if key == "check_update":
            if time.time() - self.last_update_force < 300:
                return False, "Checked less than 5 minutes ago"
            self.last_update_force = time.time()
            self.check_update(force=True)
            u = self.store.get("update", {})
            if u.get("error"):
                return False, u["error"]
            return True, f"Latest {u.get('latest', '?')}, installed {self.installed_tag() or '?'}"
        return False, "Unknown action"

    # -------------------------------------------------------- Anthias API
    def api(self, method, path, timeout=20):
        req = urllib.request.Request(self.a["url"].rstrip("/") + path, method=method,
                                     data=b"" if method == "POST" else None)
        if self.a.get("username"):
            tok = base64.b64encode(f"{self.a['username']}:{self.a.get('password', '')}".encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        req.add_header("Accept", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                body = ""
            return e.code, body
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    # ------------------------------------------------------------- polling
    def poll(self):
        self.pub(self.t("last_boot"), self.last_boot)
        self.poll_temperature()
        info = self.poll_api()
        self.poll_undervoltage(info)
        self.poll_cec(info is not None)
        self.check_update(force=False)

    def poll_temperature(self):
        try:
            t = round(int((read_text(self.a["thermal_file"]) or "").strip()) / 1000.0, 1)
        except ValueError:
            return
        if self.last_temp is None or abs(t - self.last_temp) >= 0.5:
            self.last_temp = t
            self.pub(self.t("cpu_temperature"), t)

    def poll_api(self):
        status, body = self.api("GET", "/api/v2/info")
        info = None
        if status == 200:
            try:
                info = json.loads(body)
                if not isinstance(info, dict):
                    info = None
            except ValueError:
                info = None
        if info is not None:
            self.api_failures = 0
            self.api_last_ok = now_iso()
            problem, err = False, ""
        else:
            self.api_failures += 1
            err = f"HTTP {status}: {body[:200]}" if status else body[:200]
            problem = (self.api_failures >= int(self.a["api_failures_before_problem"])
                       and self.uptime() >= float(self.a["boot_grace_seconds"]))
        self.pub(self.t("anthias"), "ON" if problem else "OFF")
        # last_ok only while failing, so a healthy Pi doesn't republish a timestamp every poll
        attrs = {"consecutive_failures": self.api_failures}
        if info is not None:
            attrs["free_space"] = str(info.get("free_space", ""))
        else:
            attrs["last_error"] = err
            if self.api_last_ok:
                attrs["last_ok"] = self.api_last_ok
        self.pub(self.t("anthias", "attributes"), attrs)

        if info is not None:
            ver = info.get("anthias_version")
            if isinstance(ver, str) and ver.strip():
                self.installed_full = ver.strip()
                if self.store.get("installed_full") != self.installed_full:
                    with self.lock:
                        self.store["installed_full"] = self.installed_full
                    self.save()
                self.publish_update()
            st = info.get("storage")
            if isinstance(st, dict) and st.get("supported", True):
                bad = (str(st.get("status", "ok")).lower() != "ok" or st.get("write_ok") is False
                       or bool(st.get("errors_this_boot")) or bool(st.get("write_failed_since_boot"))
                       or bool(st.get("read_only")))
                self.pub(self.t("storage"), "ON" if bad else "OFF")
                media = st.get("media") if isinstance(st.get("media"), dict) else {}
                sattrs = {k: st.get(k) for k in ("status", "device", "fstype", "write_ok", "errors_count",
                                                  "errors_this_boot", "write_fail_count", "read_only",
                                                  "write_reason", "last_error")}
                sattrs["wear_pct"] = media.get("wear_pct")
                sattrs["media"] = media.get("name")
                self.pub(self.t("storage", "attributes"), {k: v for k, v in sattrs.items() if v is not None})
        return info

    def poll_undervoltage(self, info):
        rc, out = run([self.a["vcgencmd"], "get_throttled"], timeout=10)
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out) if rc == 0 else None
        attrs = {"session_started": self.last_boot}
        if m:
            v = int(m.group(1), 16)
            now_uv, seen_uv = bool(v & 0x1), bool(v & 0x10000)
            attrs.update({"source": "vcgencmd", "raw": m.group(1), "undervoltage_now": now_uv,
                          "throttled_now": bool(v & 0x4), "throttled_since_boot": bool(v & 0x40000),
                          "frequency_capped_since_boot": bool(v & 0x20000)})
        elif info is not None and isinstance(info.get("under_voltage"), dict):
            uv = info["under_voltage"]
            now_uv, seen_uv = bool(uv.get("active")), bool(uv.get("seen_since_boot"))
            attrs.update({"source": "anthias", "undervoltage_now": now_uv, "events_this_boot": uv.get("count", 0)})
        else:
            return
        if seen_uv and (now_uv or not self.store.get("uv_counted")):
            with self.lock:
                self.store["uv_last_seen"] = now_iso()  # happening now, or first noticed this session
                if not self.store.get("uv_counted"):
                    self.store["uv_counted"] = True
                    self.store["uv_sessions"] = int(self.store.get("uv_sessions", 0)) + 1
                    log.warning("Undervoltage detected this session (%s)", attrs.get("raw", ""))
            self.save()
        attrs["sessions_with_undervoltage"] = int(self.store.get("uv_sessions", 0))
        if self.store.get("uv_last_seen"):
            attrs["last_seen"] = self.store["uv_last_seen"]
        self.pub(self.t("undervoltage"), "ON" if seen_uv else "OFF")
        self.pub(self.t("undervoltage", "attributes"), attrs)

    def viewer_started_at(self):
        rc, out = run([util.DOCKER, "inspect", "-f", "{{.State.Running}} {{.State.StartedAt}}",
                       self.a["containers"]["viewer"]], timeout=15)
        parts = out.strip().split() if rc == 0 else []
        if len(parts) < 2 or parts[0] != "true":
            return None
        return parse_docker_time(parts[1])

    def poll_cec(self, api_ok):
        rc, out = run([util.DOCKER, "exec", self.a["containers"]["redis"], "redis-cli", "get", "cec:available"],
                      timeout=15)
        if rc != 0:
            return
        raw = out.strip()
        attrs = {"reported": raw != ""}
        if raw == "":
            attrs["reason"] = "The viewer has not reported CEC to Redis since Redis started"
        elif raw != "1":
            attrs["reason"] = "The viewer found no CEC adapter"
        if self.store.get("cec_fix_attempted"):
            attrs["auto_fix"] = self.store.get("cec_fix_result", "attempted")
        self.pub(self.t("cec"), "ON" if raw == "1" else "OFF")
        self.pub(self.t("cec", "attributes"), attrs)
        # Fix only "never reported" (Redis came up after the viewer), once per boot.
        # "0" means the viewer looked and found nothing; a restart wouldn't help.
        if (raw == "" and api_ok and self.a.get("auto_fix_cec") and not self.store.get("cec_fix_attempted")
                and not self.action_lock.locked()):
            started = self.viewer_started_at()
            if started is not None and time.time() - started >= float(self.a["cec_fix_after_seconds"]):
                if self.action_lock.acquire(blocking=False):
                    try:
                        with self.lock:
                            self.store["cec_fix_attempted"] = True
                        self.save()
                        log.warning("CEC not reported to Redis; restarting the viewer once")
                        rc2, out2 = run([util.DOCKER, "restart", self.a["containers"]["viewer"]], timeout=120)
                        result = "restarted viewer" if rc2 == 0 else f"viewer restart failed: {out2.strip()[:200]}"
                        with self.lock:
                            self.store["cec_fix_result"] = f"{result} at {now_iso()}"
                        self.save()
                        self.set_last_action("restart_viewer", rc2 == 0, f"Automatic: CEC not reported; {result}")
                    finally:
                        self.action_lock.release()

    # -------------------------------------------------------- update entity
    def installed_tag(self):
        full = self.installed_full or self.store.get("installed_full")
        return full.split()[0] if full else None

    def check_update(self, force):
        u = dict(self.store.get("update") or {})
        due = time.time() - float(u.get("checked_epoch", 0)) >= float(self.a["update_check_hours"]) * 3600
        if not (force or due):
            self.publish_update()
            return
        url = f"{self.a['github_api'].rstrip('/')}/repos/{self.a['github_repo']}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": f"{PROJECT}/{VERSION}",
                                                   "Accept": "application/vnd.github+json"})
        u["checked_epoch"] = time.time()
        u["checked_at"] = now_iso()
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            tag = data.get("tag_name")
            if not isinstance(tag, str) or not tag:
                raise ValueError("no tag_name in response")
            u["latest"] = tag
            u["url"] = data.get("html_url") if isinstance(data.get("html_url"), str) else ""
            u["summary"] = short_summary(data.get("body") if isinstance(data.get("body"), str) else "")
            u.pop("error", None)
            log.info("Latest Anthias release: %s", tag)
        except Exception as e:  # noqa: BLE001
            u["error"] = f"Release check failed: {e}"[:300]
            # Just switched on: no network or clock yet. Try again in ten minutes.
            u["checked_epoch"] = time.time() - float(self.a["update_check_hours"]) * 3600 + 600
            log.warning("%s", u["error"])
        with self.lock:
            self.store["update"] = u
        self.save()
        self.publish_update()

    def publish_update(self):
        u = self.store.get("update", {})
        installed = self.installed_tag()
        if not installed and not u.get("latest"):
            return
        state = {"title": "Anthias", "release_summary": u.get("summary") or ""}
        if installed:
            state["installed_version"] = installed
        if u.get("latest"):
            state["latest_version"] = u["latest"]
        if u.get("url") and str(u["url"]).startswith("http"):
            state["release_url"] = u["url"]
        self.pub(self.t("update", "state"), state)  # never nulls: HA drops the whole message
        attrs = {"repo": self.a["github_repo"],
                 "installed_full": self.installed_full or self.store.get("installed_full", ""),
                 "checked_at": u.get("checked_at", "")}
        if u.get("error"):
            attrs["check_error"] = u["error"]
        self.pub(self.t("update", "attributes"), attrs)

    # ----------------------------------------------------------- lifecycle
    def loop(self):
        interval = max(1.0, float(self.a["poll_seconds"]))
        while not self.bridge.stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception("Anthias poll failed")
            self.bridge.stop.wait(interval)

    def start(self):
        self.save()
        threading.Thread(target=self.loop, name="anthias-poll", daemon=True).start()
