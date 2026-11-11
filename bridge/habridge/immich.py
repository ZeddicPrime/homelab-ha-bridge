"""Immich plugin: sign-ins (successful and failed), signed-in devices, library
size and job queues, from Immich's API (read-only key) and its server log.

A successful sign-in is a session that wasn't there on the previous poll
(Immich creates one per password or OAuth login). A failed one is Immich's own
"Failed login attempt" warning in the server log, followed with `docker logs`.
"""
import copy
import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import util
from .core import Plugin
from .util import clean

log = logging.getLogger("habridge.immich")

FAILED_RE = re.compile(r"Failed login attempt for user (?P<email>.*?) from ip address (?P<ip>\S+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TS_RE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s(?P<line>.*)$")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_ts(s):
    """Parse Immich / Docker timestamps (Z or +00:00, any fraction length)."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    # Docker gives nanoseconds; fromisoformat takes at most 6 fraction digits.
    s = re.sub(r"(T\d\d:\d\d:\d\d\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def describe_device(s):
    parts = [p for p in (s.get("deviceOS") or "", s.get("deviceType") or "") if p]
    desc = " · ".join(parts) if parts else "Unknown device"
    if s.get("appVersion"):
        desc += f" (app {s['appVersion']})"
    return desc


class ImmichMonitor:
    """Publishes Immich sign-in, library and queue entities.

    publish(topic, payload, retain) and log(level, msg) come from the bridge.
    state is the bridge's persistent dict (saved to state.json by the bridge);
    this class keeps everything under state["immich"].
    """

    def __init__(self, cfg, base, device, origin, publish, log, state, save_state, docker="docker"):
        self.cfg = cfg
        self.base = base
        self.device = device
        self.origin = origin
        self.publish = publish
        self.log = log
        self.state = state.setdefault("immich", {})
        self.save_state = save_state
        self.docker = docker
        self.lock = threading.RLock()
        self.url = cfg.get("url", "http://127.0.0.1:2283").rstrip("/")
        self.key = cfg.get("api_key", "")
        self.container = cfg.get("server_container", "immich-server")
        self.window = timedelta(hours=int(cfg.get("failed_window_hours", 24)))
        self.recent_limit = int(cfg.get("recent_limit", 10))
        self.stats_interval = int(cfg.get("stats_interval_seconds", 300))
        self.insecure_http = bool(cfg.get("_test_no_proxy", False))
        self._last_stats = 0.0
        self._stop = threading.Event()
        self._follower = None
        self._api_ok = None
        self.state.setdefault("failed", [])          # [{time, email, ip}]
        self.state.setdefault("known_sessions", None)  # None = not baselined yet
        self.state.setdefault("users", {})

    # ---------------- discovery ----------------
    def _t(self, *parts):
        return "/".join([self.base] + list(parts))

    def _common(self, obj, name, default_entity_id, icon=None):
        c = {
            "name": name,
            "unique_id": f"{self.base}_ha_{obj}",
            "default_entity_id": default_entity_id,
            "availability_topic": self._t("availability"),
            "device": self.device,
            "origin": self.origin,
        }
        if icon:
            c["icon"] = icon
        return c

    def discovery(self):
        b = self.base
        node = f"{b}_ha"
        out = []

        def add(component, obj, cfg):
            out.append((f"homeassistant/{component}/{node}/{obj}/config", cfg))

        c = self._common("login_event", "Login", f"event.{b}_login", "mdi:login")
        c.update(state_topic=self._t("login", "event"), event_types=["login", "login_failed"])
        add("event", "login_event", c)

        c = self._common("last_login", "Last sign-in", f"sensor.{b}_last_login", "mdi:account-check")
        c.update(state_topic=self._t("login", "last", "state"), device_class="timestamp",
                 json_attributes_topic=self._t("login", "last", "attributes"))
        add("sensor", "last_login", c)

        c = self._common("failed_logins", "Failed sign-ins (24 h)", f"sensor.{b}_failed_logins", "mdi:account-alert")
        c.update(state_topic=self._t("login", "failed", "state"), state_class="measurement",
                 json_attributes_topic=self._t("login", "failed", "attributes"))
        add("sensor", "failed_logins", c)

        c = self._common("sessions", "Signed-in devices", f"sensor.{b}_sessions", "mdi:devices")
        c.update(state_topic=self._t("sessions", "state"), state_class="measurement",
                 json_attributes_topic=self._t("sessions", "attributes"))
        add("sensor", "sessions", c)

        c = self._common("api", "Server", f"binary_sensor.{b}_server", "mdi:image-multiple")
        c.update(state_topic=self._t("api", "state"), device_class="connectivity",
                 json_attributes_topic=self._t("api", "attributes"))
        add("binary_sensor", "api", c)

        c = self._common("version", "Version", f"sensor.{b}_version", "mdi:tag")
        c.update(state_topic=self._t("stats", "version"))
        add("sensor", "version", c)

        for obj, name, icon in (("photos", "Photos", "mdi:image"), ("videos", "Videos", "mdi:video")):
            c = self._common(obj, name, f"sensor.{b}_{obj}", icon)
            c.update(state_topic=self._t("stats", obj), state_class="measurement")
            add("sensor", obj, c)

        c = self._common("storage", "Library size", f"sensor.{b}_library_size", "mdi:harddisk")
        c.update(state_topic=self._t("stats", "usage"), device_class="data_size", unit_of_measurement="B",
                 suggested_unit_of_measurement="GB", suggested_display_precision=1, state_class="measurement",
                 json_attributes_topic=self._t("stats", "attributes"))
        add("sensor", "storage", c)

        c = self._common("jobs", "Jobs waiting", f"sensor.{b}_jobs_waiting", "mdi:tray-full")
        c.update(state_topic=self._t("jobs", "state"), state_class="measurement",
                 json_attributes_topic=self._t("jobs", "attributes"))
        add("sensor", "jobs", c)

        c = self._common("processing", "Processing", f"binary_sensor.{b}_processing", "mdi:cog-sync")
        c.update(state_topic=self._t("jobs", "processing"), device_class="running")
        add("binary_sensor", "processing", c)
        return out

    # ---------------- API ----------------
    def _get(self, path):
        req = urllib.request.Request(self.url + path, headers={"x-api-key": self.key, "Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # LAN only, never a proxy
        with opener.open(req, timeout=15) as r:
            return json.loads(r.read().decode() or "null")

    # ---------------- polling ----------------
    def poll(self, force_stats=False):
        try:
            users = self._get("/api/admin/users")
            sessions = {}
            for u in users:
                for s in self._get(f"/api/admin/users/{u['id']}/sessions"):
                    s["_user"] = u
                    sessions[s["id"]] = s
            self._set_api(True, None)
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
            msg = str(getattr(e, "reason", e))
            if isinstance(e, urllib.error.HTTPError):
                msg = f"HTTP {e.code}" + (" (check the API key and its permissions)" if e.code in (401, 403) else "")
            self._set_api(False, msg)
            return
        with self.lock:
            self.state["users"] = {u["id"]: {"name": u.get("name") or u.get("email"), "email": u.get("email")} for u in users}
            self._process_sessions(sessions)
            self._publish_failed()
        if force_stats or time.time() - self._last_stats >= self.stats_interval:
            self._poll_stats()
        self._poll_jobs()

    def _set_api(self, ok, error):
        if ok != self._api_ok or not ok:
            self.publish(self._t("api", "state"), "ON" if ok else "OFF", True)
            self.publish(self._t("api", "attributes"), json.dumps(clean({
                "url": self.url, "last_error": error or "", "checked": iso(utcnow())})), True)
            if not ok:
                self.log("warning", f"Immich API unreachable: {error}")
        self._api_ok = ok

    def _process_sessions(self, sessions):
        now = utcnow()
        known = self.state.get("known_sessions")
        live = {}
        for sid, s in sessions.items():
            exp = parse_ts(s.get("expiresAt"))
            if exp and exp < now:
                continue
            live[sid] = s
        if known is None:
            # First run: take what exists as the baseline, announce nothing.
            self.state["known_sessions"] = sorted(sessions.keys())
            self.state["baseline_at"] = iso(now)
            newest = max(live.values(), key=lambda s: s.get("createdAt") or "", default=None)
            if newest and "last_login" not in self.state:
                self.state["last_login"] = self._login_record(newest, baseline=True)
            self.log("info", f"Baselined {len(sessions)} existing Immich session(s)")
        else:
            known_set = set(known)
            since = parse_ts(self.state.get("baseline_at")) or now
            # A session counts as a sign-in when it is new, still valid, and was created after
            # monitoring began (sessions made while the bridge was down are caught up on).
            new = [s for sid, s in live.items() if sid not in known_set
                   and (parse_ts(s.get("createdAt")) or now) >= since - timedelta(minutes=1)]
            for s in sorted(new, key=lambda s: s.get("createdAt") or ""):
                rec = self._login_record(s)
                self.state["last_login"] = rec
                self.publish(self._t("login", "event"), json.dumps(clean(dict(event_type="login", **rec))), False)
                self.log("info", f"Immich sign-in: {rec['user']} on {rec['device']}")
            self.state["known_sessions"] = sorted(sessions.keys())
        self.save_state()
        last = self.state.get("last_login")
        self.publish(self._t("login", "last", "state"), last["time"] if last else "None", True)
        self.publish(self._t("login", "last", "attributes"), json.dumps(clean(last or {})), True)
        devices = []
        for s in sorted(live.values(), key=lambda s: s.get("updatedAt") or "", reverse=True):
            devices.append(clean({
                "user": s["_user"].get("name") or s["_user"].get("email"),
                "device": describe_device(s),
                "signed_in": iso(parse_ts(s.get("createdAt"))) if parse_ts(s.get("createdAt")) else None,
                "last_active": iso(parse_ts(s.get("updatedAt"))) if parse_ts(s.get("updatedAt")) else None,
            }))
        self.publish(self._t("sessions", "state"), str(len(live)), True)
        self.publish(self._t("sessions", "attributes"), json.dumps({"devices": devices}), True)

    def _login_record(self, s, baseline=False):
        u = s["_user"]
        created = parse_ts(s.get("createdAt")) or utcnow()
        rec = {
            "user": u.get("name") or u.get("email") or "unknown",
            "email": u.get("email") or "",
            "device": describe_device(s),
            "device_type": s.get("deviceType") or "",
            "device_os": s.get("deviceOS") or "",
            "app_version": s.get("appVersion") or "",
            "time": iso(created),
        }
        if baseline:
            rec["note"] = "Existing session when monitoring started"
        return rec

    # ---------------- failed logins ----------------
    def start_log_follower(self):
        self._follower = threading.Thread(target=self._follow_logs, name="immich-log-follower", daemon=True)
        self._follower.start()

    def stop(self):
        self._stop.set()
        p = getattr(self, "_proc", None)
        if p and p.poll() is None:
            try:
                p.kill()  # KillMode=process leaves children alone, so end `docker logs -f` ourselves
            except OSError:
                pass

    def _follow_logs(self):
        while not self._stop.is_set():
            with self.lock:
                since = self.state.get("log_since") or iso(utcnow())
                self.state["log_since"] = since
            cmd = [self.docker, "logs", "--timestamps", "--since", since, "-f", self.container]
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                self._proc = p
            except OSError as e:
                self.log("error", f"Cannot run docker logs: {e}")
                self._stop.wait(30)
                continue
            try:
                for raw in p.stdout:
                    if self._stop.is_set():
                        break
                    self.handle_log_line(raw.rstrip("\n"))
            finally:
                try:
                    p.kill()
                except OSError:
                    pass
                p.wait()
            # Container restarted or recreated (update): follow again from where we were.
            self._stop.wait(5)

    def handle_log_line(self, raw):
        m = TS_RE.match(raw)
        if not m:
            return
        ts_s, line = m.group("ts"), ANSI_RE.sub("", m.group("line"))  # Immich colours its log lines
        with self.lock:
            last = self.state.get("log_last_ts")
            if last and ts_s <= last:
                return  # already processed (overlap after a reconnect)
            self.state["log_last_ts"] = ts_s
            self.state["log_since"] = ts_s
            f = FAILED_RE.search(line)
            if not f:
                return
            ts = parse_ts(ts_s) or utcnow()
            email, ip = f.group("email").strip(), f.group("ip").strip()
            users = self.state.get("users", {})
            known = next((u for u in users.values() if (u.get("email") or "").lower() == email.lower()), None)
            rec = {"time": iso(ts), "email": email, "ip": ip,
                   "known_user": bool(known), "user": (known or {}).get("name") or email}
            self.state["failed"].append(rec)
            self._prune_failed()
            self.save_state()
            count = len(self.state["failed"])
        self.publish(self._t("login", "event"), json.dumps(clean(dict(event_type="login_failed", failed_24h=count, **rec))), False)
        self.log("warning", f"Immich failed sign-in for {email} from {ip}")
        self._publish_failed()

    def _prune_failed(self):
        cutoff = utcnow() - self.window
        self.state["failed"] = [r for r in self.state["failed"] if (parse_ts(r.get("time")) or cutoff) >= cutoff][-500:]

    def _publish_failed(self):
        with self.lock:
            self._prune_failed()
            failed = list(self.state["failed"])
        recent = list(reversed(failed[-self.recent_limit:]))
        ips = sorted({r["ip"] for r in failed})
        attrs = {"window_hours": int(self.window.total_seconds() // 3600), "recent": recent,
                 "ip_addresses": ips, "last_failed": recent[0]["time"] if recent else ""}
        self.publish(self._t("login", "failed", "state"), str(len(failed)), True)
        self.publish(self._t("login", "failed", "attributes"), json.dumps(clean(attrs)), True)

    # ---------------- stats & jobs ----------------
    def _poll_stats(self):
        try:
            st = self._get("/api/server/statistics")
            ver = self._get("/api/server/version")
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.log("warning", f"Immich statistics unavailable: {e}")
            return
        self._last_stats = time.time()
        self.publish(self._t("stats", "photos"), str(st.get("photos", 0)), True)
        self.publish(self._t("stats", "videos"), str(st.get("videos", 0)), True)
        self.publish(self._t("stats", "usage"), str(st.get("usage", 0)), True)
        per_user = [clean({"user": u.get("userName"), "photos": u.get("photos"), "videos": u.get("videos"),
                           "usage_bytes": u.get("usage")}) for u in st.get("usageByUser", [])]
        self.publish(self._t("stats", "attributes"), json.dumps({
            "photos_bytes": st.get("usagePhotos", 0), "videos_bytes": st.get("usageVideos", 0), "by_user": per_user}), True)
        v = f"v{ver.get('major')}.{ver.get('minor')}.{ver.get('patch')}"
        self.publish(self._t("stats", "version"), v, True)

    def _poll_jobs(self):
        try:
            queues = self._get("/api/queues")
        except (urllib.error.URLError, OSError, ValueError):
            return
        total = 0
        busy = False
        detail = {}
        for q in queues:
            s = q.get("statistics") or {}
            waiting = int(s.get("waiting", 0)) + int(s.get("delayed", 0))
            active = int(s.get("active", 0))
            total += waiting + active
            busy = busy or active > 0
            if waiting or active or s.get("failed") or q.get("isPaused"):
                detail[q.get("name", "?")] = clean({"waiting": waiting, "active": active,
                                                    "failed": s.get("failed", 0), "paused": q.get("isPaused") or None})
        self.publish(self._t("jobs", "state"), str(total), True)
        self.publish(self._t("jobs", "attributes"), json.dumps({"queues": detail}), True)
        self.publish(self._t("jobs", "processing"), "ON" if busy else "OFF", True)

    def republish(self):
        """After HA restarts: states again (events are never retained or replayed)."""
        with self.lock:
            last = self.state.get("last_login")
        self.publish(self._t("login", "last", "state"), last["time"] if last else "None", True)
        self.publish(self._t("login", "last", "attributes"), json.dumps(clean(last or {})), True)
        self._publish_failed()


class ImmichPlugin(Plugin):
    """Wraps ImmichMonitor: shares the bridge's MQTT client, availability and device."""

    name = "immich"
    state_keys = ("immich",)

    def __init__(self, bridge, cfg):
        super().__init__(bridge, cfg)
        icfg = cfg["immich"]
        self.every = float(icfg.get("poll_seconds", 30))
        self.mon = ImmichMonitor(
            icfg, bridge.base, bridge.device, bridge.origin,
            lambda t, p, r: bridge.client.publish(t, p, retain=r),
            lambda lvl, msg: getattr(log, lvl)(msg),
            {"immich": bridge.state.get("immich", {})}, bridge.save_state, docker=util.DOCKER)

    def publish_discovery(self):
        for topic, conf in self.mon.discovery():
            self.bridge.publish(topic, json.dumps(conf))

    def republish(self):
        self.mon.republish()

    def snapshot(self):
        with self.mon.lock:  # the log follower changes it from its own thread
            return {"immich": copy.deepcopy(self.mon.state)}

    def loop(self):
        first = True
        while not self.bridge.stop.is_set():
            if self.bridge.connected.is_set():
                try:
                    self.mon.poll(force_stats=first)
                    first = False
                except Exception as e:  # noqa: BLE001
                    log.exception("Immich poll crashed: %s", e)
            self.bridge.stop.wait(self.every)

    def start(self):
        self.mon.start_log_follower()
        threading.Thread(target=self.loop, name="immich-poll", daemon=True).start()

    def stop(self):
        self.mon.stop()
