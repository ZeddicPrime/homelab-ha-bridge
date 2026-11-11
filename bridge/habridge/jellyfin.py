"""Jellyfin plugin: the Jellyfin server installed as a Debian/Ubuntu package.

Entities on the bridge's device (object IDs fixed; entity IDs <component>.<name>_<object>):

  binary_sensor server, restart_pending, mqtt_plugin, gpu_problem
  sensor active_streams, transcodes, movies, series, episodes, albums, songs,
         library_scan, os_updates, gpu_utilization, gpu_encoder, gpu_decoder,
         gpu_memory, gpu_temperature
  button scan_libraries, restart
  update update  (installs with the host's update command, e.g. a community
                  script's `update`; nothing else is ever run)

It complements the Jellyfin MQTT plugin rather than overlapping it: that plugin
publishes each client as a media player; this one covers the server and only
reads the plugin's status topic. GPU entities appear when nvidia-smi answers
(or an NVIDIA device is passed through and nvidia-smi fails, shown as a problem).

Config, all under "jellyfin":
  url, api_key                    the server, from this host (loopback is fine)
  public_url                      shown as the server's address (optional)
  update_command                  default ["bash", "/usr/bin/update"]
  timeout_minutes (30), poll_seconds (15), counts_minutes (10), update_check_hours (6)
  plugin_status_topic             default "jellyfin/status"
  systemd_unit ("jellyfin"), package ("jellyfin-server"), gpu ("auto" | true | false)
"""

import json
import logging
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION
from .core import Plugin
from .util import now_iso

log = logging.getLogger("habridge.jellyfin")

SYS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
SCAN, RESTART, INSTALL = "SCAN", "RESTART", "INSTALL"


def norm_ts(value):
    """Jellyfin returns 7-digit fractions ("...00.1234567Z"); trim to an ISO
    string Home Assistant's as_datetime understands."""
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$", value)
    if not m:
        return value
    if m.group(1).startswith("0001-"):
        return None
    tz = m.group(3) or "Z"
    if tz == "Z":
        tz = "+00:00"
    return f"{m.group(1)}{tz}"


def display_version(raw):
    """'10.11.4+ubu2404' -> '10.11.4'"""
    if not raw:
        return None
    return raw.split("+", 1)[0].split("~", 1)[0]


# --------------------------------------------------------------- Jellyfin API
class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects as HTTPError so JellyfinAPI can re-base itself (urllib
    would follow them with the default certificate checks, and turn a POST into
    a GET)."""

    def redirect_request(self, *args, **kwargs):
        return None


class JellyfinAPI:
    def __init__(self, url, key, client, timeout=10):
        self.key = key
        self.client = client
        self.timeout = timeout
        self._set_base(url)

    def _set_base(self, url):
        self.url = url.rstrip("/")
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]  # always direct
        if self.url.startswith("https"):
            ctx = ssl.create_default_context()
            if urllib.parse.urlparse(self.url).hostname in LOOPBACK:
                # Loopback never leaves the host, and the certificate names the
                # public address, not 127.0.0.1.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def _rebase(self, path, location):
        """Jellyfin redirected us (e.g. "Require HTTPS" turned on): use where it points."""
        target = urllib.parse.urljoin(self.url + path, location)
        p = urllib.parse.urlparse(target)
        if p.scheme not in ("http", "https") or not p.netloc:
            return False
        req_path = urllib.parse.urlparse(self.url + path).path
        prefix = p.path[: -len(req_path)] if req_path and p.path.endswith(req_path) else ""
        new = f"{p.scheme}://{p.netloc}{prefix}".rstrip("/")
        if new == self.url:
            return False
        log.warning("Jellyfin redirects %s to %s; using %s from now on. Put that URL in the "
                    "config (jellyfin.url) to skip this at startup.", self.url, new, new)
        self._set_base(new)
        return True

    def _auth(self):
        # Jellyfin 12 only accepts the standard Authorization header.
        return (f'MediaBrowser Client="{self.client}", Device="{self.client}", '
                f'DeviceId="{self.client}", Version="{VERSION}", Token="{self.key}"')

    def request(self, method, path, auth=True, _retry=True):
        req = urllib.request.Request(self.url + path, method=method)
        req.add_header("Accept", "application/json")
        if auth:
            req.add_header("Authorization", self._auth())
        if method == "POST":
            req.data = b""
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if _retry and loc and self._rebase(path, loc):
                    return self.request(method, path, auth, _retry=False)
                raise ApiError(f"HTTP {e.code} redirect to {loc} from {path}") from None
            if e.code in (401, 403):
                raise AuthError(f"API key rejected (HTTP {e.code})") from None
            raise ApiError(f"HTTP {e.code} from {path}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ApiError(f"not reachable ({getattr(e, 'reason', e)})") from None
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise ApiError(f"unexpected reply from {path}") from None

    def get(self, path):
        return self.request("GET", path)

    def post(self, path):
        return self.request("POST", path)


# ------------------------------------------------------------------- helpers
def run(cmd, timeout=60, env_path=SYS_PATH):
    env = {"PATH": env_path, "LANG": "C.UTF-8", "HOME": "/root", "DEBIAN_FRONTEND": "noninteractive"}
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"


def summarize_sessions(sessions):
    streams = []
    for s in sessions or []:
        item = s.get("NowPlayingItem")
        if not item:
            continue
        ps = s.get("PlayState") or {}
        ti = s.get("TranscodingInfo") or {}
        method = ps.get("PlayMethod") or ("Transcode" if ti else "DirectPlay")
        name = item.get("Name") or "?"
        itype = item.get("Type")
        if itype == "Episode" and item.get("SeriesName"):
            se = ""
            if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
                se = f" S{item['ParentIndexNumber']:02d}E{item['IndexNumber']:02d}"
            title = f"{item['SeriesName']}{se} · {name}"
        elif itype == "Audio":
            artists = ", ".join(item.get("Artists") or []) or item.get("AlbumArtist")
            title = f"{artists} – {name}" if artists else name
        elif item.get("ProductionYear"):
            title = f"{name} ({item['ProductionYear']})"
        else:
            title = name
        progress = None
        rt, pos = item.get("RunTimeTicks"), ps.get("PositionTicks")
        if rt and pos is not None:
            progress = round(100 * pos / rt)
        entry = {"user": s.get("UserName"), "device": s.get("DeviceName"), "client": s.get("Client"),
                 "title": title, "type": itype, "paused": bool(ps.get("IsPaused")),
                 "play_method": method, "progress": progress}
        if method == "Transcode":
            reasons = ti.get("TranscodeReasons") or []
            if isinstance(reasons, str):
                reasons = [r.strip() for r in reasons.split(",") if r.strip()]
            w, h = ti.get("Width"), ti.get("Height")
            entry["transcode"] = {
                "video": "copy" if ti.get("IsVideoDirect") else (ti.get("VideoCodec") or "?"),
                "audio": "copy" if ti.get("IsAudioDirect") else (ti.get("AudioCodec") or "?"),
                "hw": ti.get("HardwareAccelerationType") or "none",
                "resolution": f"{w}x{h}" if w and h else None,
                "fps": ti.get("Framerate"),
                "reasons": reasons,
            }
        streams.append(entry)
    return streams


GPU_FULL = ["utilization.gpu", "utilization.encoder", "utilization.decoder",
            "memory.used", "memory.total", "temperature.gpu", "name"]
GPU_BASIC = ["utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "name"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def query_gpu(fields, env_path):
    rc, out, err = run(["nvidia-smi", f"--query-gpu={','.join(fields)}",
                        "--format=csv,noheader,nounits"], timeout=15, env_path=env_path)
    if rc != 0:
        msg = [ln.strip() for ln in (err or out).strip().splitlines() if ln.strip()]
        return None, ("; ".join(msg)[:300] if msg else f"nvidia-smi exited {rc}")
    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != len(fields):
        return None, f"unexpected nvidia-smi output: {line!r}"
    return dict(zip(fields, parts)), None


def failure_hint(tail):
    t = (tail or "").lower()
    if "storage" in t and ("80%" in t or "usage" in t):
        return "disk is over 80% full (the updater asks to continue, which a button can't answer)"
    if "could not get lock" in t or "dpkg was interrupted" in t:
        return "apt was busy or interrupted; try again in a few minutes"
    if "resolve" in t or "could not resolve host" in t:
        return "no internet/DNS from this host"
    return None


def log_tail(path, lines=15):
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "\n")
    keep = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(keep[-lines:])


# Entities this plugin can publish; the ones a host doesn't have are removed.
ALL_OBJECTS = {
    "binary_sensor": ["server", "restart_pending", "mqtt_plugin", "gpu_problem"],
    "sensor": ["active_streams", "transcodes", "movies", "series", "episodes", "albums", "songs",
               "library_scan", "os_updates", "gpu_utilization", "gpu_encoder", "gpu_decoder",
               "gpu_memory", "gpu_temperature"],
    "button": ["scan_libraries", "restart"],
    "update": ["update"],
}


class JellyfinPlugin(Plugin):
    name = "jellyfin"
    state_keys = ("jellyfin",)

    def __init__(self, bridge, cfg):
        super().__init__(bridge, cfg)
        jf = cfg.get("jellyfin") or {}
        if not jf.get("url") or not jf.get("api_key"):
            raise SystemExit("jellyfin plugin: jellyfin.url and jellyfin.api_key are required")
        self.jf = jf
        self.api = JellyfinAPI(jf["url"], jf["api_key"], bridge.service)
        self.public_url = jf.get("public_url")
        self.unit = jf.get("systemd_unit", "jellyfin")
        self.package = jf.get("package", "jellyfin-server")
        self.plugin_status_topic = jf.get("plugin_status_topic", "jellyfin/status")
        self.env_path = jf.get("path", SYS_PATH)
        self.poll = int(jf.get("poll_seconds", 15))
        self.counts_every = int(jf.get("counts_minutes", 10)) * 60
        self.apt_every = float(jf.get("update_check_hours", 6)) * 3600
        self.update_command = jf.get("update_command", ["bash", "/usr/bin/update"])
        self.timeout = int(jf.get("timeout_minutes", 30)) * 60

        b = bridge.base
        self.t_api, self.t_controls, self.t_gpu = f"{b}/api", f"{b}/controls", f"{b}/gpu"
        self.t_scan, self.t_restart, self.t_install = f"{b}/scan/run", f"{b}/restart/run", f"{b}/update/install"

        self.lock = threading.RLock()
        self.cache = {}
        self.wake = threading.Event()
        self.installing = False
        self.gpu_fields = None
        self.gpu_ok = None
        self.prepared = False
        self.last_counts = 0.0
        self.last_apt = 0.0
        self.apt_running = False
        self.state = dict(bridge.state.get("jellyfin") or {})

    # ------------------------------------------------------------ state
    def snapshot(self):
        with self.lock:
            return {"jellyfin": json.loads(json.dumps(self.state))}

    def save(self):
        self.bridge.save_state()

    def busy_reason(self):
        return "Jellyfin update" if self.installing else None

    # ---------------------------------------------------------- publish
    def pub(self, topic, payload, force=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, sort_keys=True)
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
        self.pub(self.t_controls, "offline" if self.installing else "online", force=True)

    def entities(self):
        b = self.bridge.base
        a_svc = [{"topic": self.bridge.avail_topic}]
        a_api = a_svc + [{"topic": self.t_api}]
        a_ctl = a_svc + [{"topic": self.t_controls}]
        a_scan = a_api + [{"topic": self.t_controls}]
        a_gpu = a_svc + [{"topic": self.t_gpu}]
        E = []

        def add(component, obj, name, avail, **extra):
            E.append((component, obj, name, avail, extra))

        add("binary_sensor", "server", "Server", a_svc,
            state_topic=f"{b}/server/state", device_class="running",
            json_attributes_topic=f"{b}/server/attributes", icon="mdi:jellyfish")
        add("binary_sensor", "restart_pending", "Restart pending", a_api,
            state_topic=f"{b}/restart_pending/state", device_class="problem",
            entity_category="diagnostic", icon="mdi:restart-alert")
        # The Jellyfin MQTT plugin's own last-will topic; deliberately no
        # availability, so it reflects the plugin even when the bridge is down.
        add("binary_sensor", "mqtt_plugin", "MQTT plugin", None,
            state_topic=self.plugin_status_topic, payload_on="online", payload_off="offline",
            device_class="connectivity", entity_category="diagnostic")
        add("sensor", "active_streams", "Active streams", a_api,
            state_topic=f"{b}/streams/state", state_class="measurement",
            json_attributes_topic=f"{b}/streams/attributes", icon="mdi:play-network")
        add("sensor", "transcodes", "Transcodes", a_api,
            state_topic=f"{b}/transcodes/state", state_class="measurement",
            json_attributes_topic=f"{b}/transcodes/attributes", icon="mdi:swap-horizontal-bold")
        for key, label, icon in (("movies", "Movies", "mdi:movie-open"),
                                 ("series", "Series", "mdi:television-classic"),
                                 ("episodes", "Episodes", "mdi:television-play"),
                                 ("albums", "Albums", "mdi:album"),
                                 ("songs", "Songs", "mdi:music-note")):
            add("sensor", key, label, a_api, state_topic=f"{b}/library/{key}",
                state_class="measurement", icon=icon)
        add("sensor", "library_scan", "Library scan", a_api,
            state_topic=f"{b}/scan/state", json_attributes_topic=f"{b}/scan/attributes",
            icon="mdi:folder-sync")
        add("button", "scan_libraries", "Scan libraries", a_scan,
            command_topic=self.t_scan, payload_press=SCAN, icon="mdi:folder-refresh")
        add("button", "restart", "Restart", a_ctl,
            command_topic=self.t_restart, payload_press=RESTART, device_class="restart")
        add("update", "update", "Update", a_svc,
            state_topic=f"{b}/update/state", command_topic=self.t_install, payload_install=INSTALL,
            json_attributes_topic=f"{b}/update/attributes",
            entity_picture="https://jellyfin.org/images/favicon.ico")
        add("sensor", "os_updates", "OS updates", a_svc,
            state_topic=f"{b}/os_updates/state", json_attributes_topic=f"{b}/os_updates/attributes",
            entity_category="diagnostic", icon="mdi:package-variant")
        if self.gpu_fields:
            add("binary_sensor", "gpu_problem", "GPU", a_svc,
                state_topic=f"{b}/gpu/problem", device_class="problem",
                json_attributes_topic=f"{b}/gpu/attributes", entity_category="diagnostic",
                icon="mdi:expansion-card")
            add("sensor", "gpu_utilization", "GPU load", a_gpu,
                state_topic=f"{b}/gpu/utilization", unit_of_measurement="%",
                state_class="measurement", icon="mdi:expansion-card")
            if "utilization.encoder" in self.gpu_fields:
                add("sensor", "gpu_encoder", "NVENC load", a_gpu,
                    state_topic=f"{b}/gpu/encoder", unit_of_measurement="%",
                    state_class="measurement", icon="mdi:video-box")
                add("sensor", "gpu_decoder", "NVDEC load", a_gpu,
                    state_topic=f"{b}/gpu/decoder", unit_of_measurement="%",
                    state_class="measurement", icon="mdi:video-input-component")
            add("sensor", "gpu_memory", "GPU memory", a_gpu,
                state_topic=f"{b}/gpu/memory", unit_of_measurement="MiB",
                state_class="measurement", icon="mdi:memory",
                json_attributes_topic=f"{b}/gpu/memory_attributes")
            add("sensor", "gpu_temperature", "GPU temperature", a_gpu,
                state_topic=f"{b}/gpu/temperature", unit_of_measurement="°C",
                device_class="temperature", state_class="measurement")
        return E

    def publish_discovery(self):
        br = self.bridge
        published = set()
        for component, obj, name, avail, extra in self.entities():
            conf = {"name": name, "unique_id": f"{br.node}_{obj}",
                    "default_entity_id": f"{component}.{br.name}_{obj}",
                    "device": br.device, "origin": br.origin, **extra}
            if avail:
                conf["availability"] = avail
                conf["availability_mode"] = "all"
            br.publish(br.discovery_topic(component, obj), conf, qos=1)
            published.add((component, obj))
        if not self.prepared:
            return  # GPU not probed yet: don't remove GPU entities on a guess
        for component, objs in ALL_OBJECTS.items():
            for obj in objs:
                if (component, obj) not in published:
                    br.publish(br.discovery_topic(component, obj), "", qos=1)

    # ---------------------------------------------------------- commands
    def subscriptions(self):
        return [self.t_scan, self.t_restart, self.t_install]

    def is_command(self, topic):
        return topic in (self.t_scan, self.t_restart, self.t_install)

    def handle(self, topic, payload):
        actions = {(self.t_scan, SCAN): self.do_scan, (self.t_restart, RESTART): self.do_restart,
                   (self.t_install, INSTALL): self.do_install}
        if topic not in (self.t_scan, self.t_restart, self.t_install):
            return False
        fn = actions.get((topic, payload))
        if fn is None:
            log.warning("Ignored unexpected command on %s", topic)
            return True
        threading.Thread(target=fn, daemon=True).start()
        return True

    def on_check(self):
        if not self.installing:
            threading.Thread(target=self.check_updates, kwargs={"refresh": True}, daemon=True).start()

    # ----------------------------------------------------------- actions
    def do_scan(self):
        if self.installing:
            log.info("Scan ignored: update in progress")
            return
        try:
            self.api.post("/Library/Refresh")
            log.info("Library scan started")
        except ApiError as e:
            log.error("Could not start library scan: %s", e)
        self.wake.set()

    def do_restart(self):
        if self.installing:
            log.info("Restart ignored: update in progress")
            return
        log.info("Restarting %s", self.unit)
        rc, out, err = run(["systemctl", "restart", self.unit], timeout=120, env_path=self.env_path)
        if rc != 0:
            log.error("Restart failed: %s", (err or out).strip())
        with self.lock:
            self.state["last_restart"] = now_iso()
        self.save()
        self.wake.set()

    def do_install(self):
        with self.lock:
            if self.installing:
                log.info("Install ignored: already in progress")
                return
            other = self.bridge.busy_reason()
            if other:
                log.info("Install ignored: %s in progress", other)
                return
            self.installing = True
        try:
            self.pub(self.t_controls, "offline")
            self.run_update()
        finally:
            with self.lock:
                self.installing = False
            self.pub(self.t_controls, "online")
            self.wake.set()

    def run_update(self):
        before = self.installed_version()
        started = now_iso()
        log.info("Starting update (installed: %s)", before)
        with self.lock:
            self.state.update({"last_install": started, "last_install_status": "running",
                               "last_install_result": "Running…", "version_before": before,
                               "version_after": None, "log_tail": None})
        self.save()
        self.publish_update(in_progress=True)

        log_path = os.path.join(self.bridge.log_dir, "last-update.log")
        env = {"PATH": self.env_path, "HOME": "/root", "LANG": "C.UTF-8",
               "TERM": "xterm",  # community-script updaters call `clear`, which fails under "dumb"
               "PHS_SILENT": "1", "DEBIAN_FRONTEND": "noninteractive"}
        cmd = list(self.update_command)
        rc, err = None, None
        try:
            with open(log_path, "w") as lf:
                lf.write(f"# {self.bridge.service} {VERSION}: {' '.join(cmd)} at {started}\n")
                lf.flush()
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                      env=env, cwd="/root" if os.path.isdir("/root") else None,
                                      timeout=self.timeout, start_new_session=True)
                rc = proc.returncode
        except subprocess.TimeoutExpired:
            err = f"Timed out after {self.timeout // 60} minutes"
        except Exception as e:  # noqa: BLE001
            err = f"Could not run the updater: {e}"

        after = self.installed_version()
        tail = log_tail(log_path)
        if err:
            status, result = "failed", err
        elif rc != 0:
            status, result = "failed", f"Updater exited with code {rc}"
            hint = failure_hint(tail)
            if hint:
                result += f": {hint}"
        elif before != after:
            status, result = "updated", f"Updated {display_version(before)} → {display_version(after)}"
        else:
            status, result = "current", f"Already up to date ({display_version(after)})"
        log.info("Update finished: %s", result)
        with self.lock:
            self.state.update({"last_install_status": status, "last_install_result": result,
                               "last_install_finished": now_iso(), "version_after": after,
                               "log_tail": tail if status == "failed" else None})
        self.save()
        self.check_updates(refresh=False)  # the updater already ran apt update

    # ----------------------------------------------------------- updates
    def installed_version(self):
        rc, out, _ = run(["dpkg-query", "-W", "-f=${Version}", self.package], env_path=self.env_path)
        return out.strip() if rc == 0 and out.strip() else None

    def candidate_version(self):
        rc, out, _ = run(["apt-cache", "policy", self.package], env_path=self.env_path)
        m = re.search(r"Candidate:\s*(\S+)", out or "")
        if not m or m.group(1) == "(none)":
            return None
        return m.group(1)

    def os_upgradable(self):
        rc, out, _ = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"],
                         timeout=120, env_path=self.env_path)
        if rc != 0:
            return None
        return sorted({ln.split()[1] for ln in out.splitlines() if ln.startswith("Inst ")})

    def check_updates(self, refresh=True):
        with self.lock:
            if self.apt_running:
                return
            self.apt_running = True
        try:
            if refresh:
                rc, out, err = run(["apt-get", "update", "-qq"], timeout=300, env_path=self.env_path)
                if rc != 0:
                    log.warning("apt-get update failed: %s", (err or out).strip()[-300:])
            self.last_apt = time.time()
            inst, cand, pk = self.installed_version(), self.candidate_version(), self.os_upgradable()
            with self.lock:
                self.state["last_check"] = now_iso()
                self.state["installed_raw"] = inst
                self.state["candidate_raw"] = cand
                if pk is not None:
                    self.state["os_updates"] = [p for p in pk if not p.startswith("jellyfin")]
            self.save()
            self.publish_update()
        finally:
            with self.lock:
                self.apt_running = False

    def publish_update(self, in_progress=False):
        with self.lock:
            st = dict(self.state)
        inst, cand = st.get("installed_raw"), st.get("candidate_raw")
        di, dc = display_version(inst), display_version(cand or inst)
        if inst and cand and inst != cand and di == dc:
            di, dc = inst, cand  # packaging-only change; show full versions
        others = st.get("os_updates") or []
        summary = None
        if dc and di != dc:
            summary = (f"Jellyfin {dc} is available. Installing runs this host's update command, "
                       "which restarts Jellyfin")
            summary += (f" and also installs {len(others)} pending OS package update(s)."
                        if others else ".")
        state = {
            "installed_version": di,
            "latest_version": dc,
            "title": "Jellyfin",
            "release_url": f"https://github.com/jellyfin/jellyfin/releases/tag/v{display_version(dc)}" if dc else None,
            # HA rejects the whole payload if a string field is null, and keeps the
            # old value if a key is missing, so clear the summary with "".
            "release_summary": summary or "",
            "in_progress": bool(in_progress),
        }
        self.pub(f"{self.bridge.base}/update/state", {k: v for k, v in state.items() if v is not None})
        attrs = {k: st.get(k) for k in (
            "last_check", "last_install", "last_install_finished", "last_install_status",
            "last_install_result", "version_before", "version_after", "log_tail", "last_restart")}
        attrs.update(package=self.package, installed_package_version=inst, candidate_package_version=cand)
        b = self.bridge.base
        self.pub(f"{b}/update/attributes", attrs)
        self.pub(f"{b}/os_updates/state", len(others))
        self.pub(f"{b}/os_updates/attributes", {"packages": others[:60], "truncated": len(others) > 60,
                                                 "last_check": st.get("last_check")})

    # -------------------------------------------------------------- poll
    def detect_gpu(self):
        if self.jf.get("gpu", "auto") is False:
            return None
        err = None
        for fields in (GPU_FULL, GPU_BASIC):
            vals, err = query_gpu(fields, self.env_path)
            if vals:
                log.info("GPU detected: %s", vals.get("name"))
                return fields
        # A passed-through GPU whose nvidia-smi fails (typically a host driver update
        # the container's libraries don't match) must still show up, as a problem.
        if os.path.exists("/dev/nvidia0") or self.jf.get("gpu") is True:
            log.warning("GPU passed through but nvidia-smi fails: %s", err)
            return GPU_FULL
        log.info("No NVIDIA GPU here; GPU sensors disabled")
        return None

    def prepare(self):
        if not self.prepared:
            self.gpu_fields = self.detect_gpu()
            self.prepared = True

    def poll_gpu(self):
        if not self.gpu_fields:
            return
        b = self.bridge.base
        vals, err = query_gpu(self.gpu_fields, self.env_path)
        if vals is None:
            if self.gpu_ok is not False:
                log.warning("nvidia-smi failed: %s", err)
            self.gpu_ok = False
            self.pub(self.t_gpu, "offline")
            self.pub(f"{b}/gpu/problem", "ON")
            self.pub(f"{b}/gpu/attributes", {"error": err})
            return
        if self.gpu_ok is False:
            log.info("nvidia-smi is answering again")
        self.gpu_ok = True
        self.pub(f"{b}/gpu/problem", "OFF")
        self.pub(f"{b}/gpu/attributes", {"error": None, "name": vals.get("name")})

        def val(k):
            n = _num(vals.get(k))
            return "" if n is None else (int(n) if n == int(n) else n)

        self.pub(f"{b}/gpu/utilization", val("utilization.gpu"))
        if "utilization.encoder" in self.gpu_fields:
            self.pub(f"{b}/gpu/encoder", val("utilization.encoder"))
            self.pub(f"{b}/gpu/decoder", val("utilization.decoder"))
        self.pub(f"{b}/gpu/memory", val("memory.used"))
        self.pub(f"{b}/gpu/memory_attributes", {"total_mib": val("memory.total")})
        self.pub(f"{b}/gpu/temperature", val("temperature.gpu"))
        self.pub(self.t_gpu, "online")

    def poll_server(self):
        b = self.bridge.base
        rc, out, _ = run(["systemctl", "is-active", self.unit], env_path=self.env_path)
        unit_state = out.strip() or "unknown"
        _, since, _ = run(["systemctl", "show", self.unit, "-p", "ActiveEnterTimestamp", "--value"],
                          env_path=self.env_path)
        attrs = {"service": unit_state, "since": since.strip() or None, "url": self.public_url}
        info = None
        try:
            info = self.api.get("/System/Info")
            attrs["api"] = "ok"
        except AuthError as e:
            attrs["api"] = str(e)
            if self.cache.get(self.t_api) != "offline":
                log.error("Jellyfin %s. Create a new key in Dashboard → API Keys and put it in %s "
                          "(jellyfin.api_key).", e, self.bridge.config_path)
        except ApiError as e:
            attrs["api"] = str(e)
        if info:
            attrs["server_name"] = info.get("ServerName")
            attrs["version"] = info.get("Version")
            self.pub(f"{b}/restart_pending/state", "ON" if info.get("HasPendingRestart") else "OFF")
        self.pub(f"{b}/server/state", "ON" if unit_state == "active" else "OFF")
        self.pub(f"{b}/server/attributes", attrs)
        self.pub(self.t_api, "online" if info else "offline")
        return info is not None

    def poll_sessions(self):
        b = self.bridge.base
        streams = summarize_sessions(self.api.get("/Sessions?activeWithinSeconds=960"))
        trans = [s for s in streams if s["play_method"] == "Transcode"]
        self.pub(f"{b}/streams/state", len(streams))
        self.pub(f"{b}/streams/attributes", {
            "playing": sum(1 for s in streams if not s["paused"]),
            "paused": sum(1 for s in streams if s["paused"]),
            "streams": streams})
        self.pub(f"{b}/transcodes/state", len(trans))
        self.pub(f"{b}/transcodes/attributes", {
            "hardware": sum(1 for s in trans if s["transcode"]["hw"] not in ("none", None)),
            "video": sum(1 for s in trans if s["transcode"]["video"] != "copy"),
            "transcodes": [{"title": s["title"], "device": s["device"], **s["transcode"]} for s in trans]})

    def poll_tasks(self):
        tasks = self.api.get("/ScheduledTasks?isHidden=false") or []
        lib = next((t for t in tasks if t.get("Key") == "RefreshLibrary"), None)
        scanning = bool(lib and lib.get("State") in ("Running", "Cancelling"))
        attrs = {"running_tasks": [f"{t.get('Name')} ({round(t.get('CurrentProgressPercentage') or 0)}%)"
                                   for t in tasks if t.get("State") == "Running"]}
        if lib:
            last = lib.get("LastExecutionResult") or {}
            attrs["progress"] = round(lib.get("CurrentProgressPercentage") or 0, 1) if scanning else None
            attrs["last_scan_start"] = norm_ts(last.get("StartTimeUtc"))
            attrs["last_scan_end"] = norm_ts(last.get("EndTimeUtc"))
            attrs["last_scan_result"] = last.get("Status")
        self.pub(f"{self.bridge.base}/scan/state", "Scanning" if scanning else "Idle")
        self.pub(f"{self.bridge.base}/scan/attributes", attrs)
        return scanning

    def poll_counts(self):
        c = self.api.get("/Items/Counts") or {}
        for key, field in (("movies", "MovieCount"), ("series", "SeriesCount"), ("episodes", "EpisodeCount"),
                           ("albums", "AlbumCount"), ("songs", "SongCount")):
            if field in c:
                self.pub(f"{self.bridge.base}/library/{key}", c[field])
        self.last_counts = time.time()

    def poll_once(self):
        api_ok = self.poll_server()
        scan_topic = f"{self.bridge.base}/scan/state"
        was_scanning = self.cache.get(scan_topic) == "Scanning"
        scanning = False
        if api_ok:
            try:
                self.poll_sessions()
            except ApiError as e:
                log.warning("sessions: %s", e)
            try:
                scanning = self.poll_tasks()
            except ApiError as e:
                log.warning("tasks: %s", e)
            # Counts on schedule, and right after a scan starts or finishes.
            if time.time() - self.last_counts > self.counts_every or scanning != was_scanning:
                try:
                    self.poll_counts()
                except ApiError as e:
                    log.warning("counts: %s", e)
        self.poll_gpu()
        if not self.installing and time.time() - self.last_apt > self.apt_every:
            threading.Thread(target=self.check_updates, daemon=True).start()

    def loop(self):
        while not self.bridge.stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("Poll failed")
            fast = self.cache.get(f"{self.bridge.base}/scan/state") == "Scanning"
            self.wake.wait(timeout=5 if fast else self.poll)
            self.wake.clear()

    def start(self):
        had = self.prepared
        self.prepare()
        if not had and self.bridge.connected.is_set():
            self.publish_discovery()  # with the GPU entities (or their removal) now known
        with self.lock:
            self.state["installed_raw"] = self.installed_version()
        self.publish_update()  # installed version now; latest after the first apt check
        threading.Thread(target=self.loop, name="jellyfin-poll", daemon=True).start()

    def stop(self):
        self.wake.set()
