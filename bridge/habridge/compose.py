"""Docker Compose plugin: update entities and controls for every Compose folder
under one root (default /opt/<name>/<folder>/).

Per service (key = folder, or folder_service when a folder holds several; a
folder named like the host gives bare service keys):
  update.<name>_<key>                installed vs. latest image; Install pulls and
                                     recreates that service only, rolling back if
                                     it won't stay up
  binary_sensor.<name>_<key>_running running/stopped (+ health)
  switch.<name>_<key>                start / stop the container
  button.<name>_<key>_restart        restart the container
A cloudflared connector also gets binary_sensor.<name>_<key>_connected, and its
update entity shows real cloudflared release numbers instead of digests.

Groups (config "groups") are services that only ever update together, e.g. a
server and its machine-learning worker. They get one update entity, an
optional database dump first, and a release compose-file check that refuses
Install while the release changes settings the local compose file lacks.

Stable releases only: a version that looks like a pre-release is never offered
or installed unless its key is in "allow_prereleases". Only the exact fixed
payloads on a known service's command topics do anything; the docker commands
never come from messages.
"""

import gzip
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import util
from .core import Plugin
from .registry import Registry, USER_AGENT, local_arch, parse_ref
from .util import (clean, docker, is_prerelease, label_version, now_iso, release_url, short, slug,
                   strip_ansi)

try:
    import yaml  # python3-yaml; only needed for the release compose-file check
except ImportError:  # pragma: no cover
    yaml = None

log = logging.getLogger("habridge.compose")

PRESS_PAYLOAD = "INSTALL"
RESTART_PAYLOAD = "RESTART"
ON, OFF = "ON", "OFF"
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
CLOUDFLARED_REPO = "cloudflare/cloudflared"
CLOUDFLARED_RELEASES = "https://github.com/cloudflare/cloudflared/releases"
CLOUDFLARED_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
CLOUDFLARED_METRICS = "127.0.0.1:20241"
IMMICH_COMPOSE_URL = "https://github.com/immich-app/immich/releases/download/{version}/docker-compose.yml"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def discover_projects(root, host):
    """({key: {...}} for every compose service under root/<folder>/,
    set of folders whose compose file exists but couldn't be read)."""
    found, failed = {}, set()
    try:
        folders = sorted(os.listdir(root))
    except FileNotFoundError:
        return found, failed
    for folder in folders:
        d = os.path.join(root, folder)
        if not os.path.isdir(d) or not any(os.path.isfile(os.path.join(d, f)) for f in COMPOSE_FILES):
            continue
        try:
            cfg = json.loads(docker("compose", "config", "--format", "json", cwd=d).stdout)
        except Exception as e:  # noqa: BLE001
            log.warning("Skipping %s for now: %s", folder, e)
            failed.add(folder)
            continue
        services = cfg.get("services") or {}
        project = cfg.get("name") or folder
        for svc, sc in services.items():
            image = sc.get("image")
            if not image:
                continue  # build-only service; nothing to pull
            if len(services) == 1:
                key = slug(folder)
            elif slug(folder) == host:
                # /opt/immich/immich: immich_server, not immich_immich_immich_server
                key = slug(svc.removeprefix(f"{folder}-").removeprefix(f"{folder}_"))
            else:
                key = slug(f"{folder}_{svc}")
            found[key] = {
                "key": key,
                "folder": folder,
                "dir": d,
                "service": svc,
                "image": image,
                "container": sc.get("container_name") or f"{project}-{svc}-1",
                "command": sc.get("command") or [],
            }
    return found, failed


# ------------------------------------------------------------ cloudflared
def is_cloudflared(p):
    return parse_ref(p.get("image", ""))[1] == CLOUDFLARED_REPO


def metrics_address(command, default=CLOUDFLARED_METRICS):
    """host:port of `--metrics` in the compose command (list or string)."""
    args = command.split() if isinstance(command, str) else list(command or [])
    addr = None
    for i, a in enumerate(args):
        if a == "--metrics" and i + 1 < len(args):
            addr = args[i + 1]
            break
        if a.startswith("--metrics="):
            addr = a.split("=", 1)[1]
            break
    if addr is None:
        return default
    host, _, port = addr.rpartition(":")
    if host in ("", "0.0.0.0", "[::]", "localhost"):
        host = "127.0.0.1"
    return f"{host}:{port}"


def cloudflared_version(container):
    """'2026.10.0' from `cloudflared --version` inside the container, or None."""
    try:
        r = docker("exec", container, "cloudflared", "--version", check=False, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"version (\d+\.\d+\.\d+)", (r.stdout or "") + (r.stderr or ""))
    return m.group(1) if m else None


def cloudflared_ready(addr):
    """(connections, connector_id, error) from the connector's /ready endpoint."""
    url = f"http://{addr}/ready"
    try:
        with _NOPROXY.open(url, timeout=3) as r:
            body = r.read()
    except urllib.error.HTTPError as e:  # 503 = no connections yet, body still JSON
        body = e.read()
    except Exception as e:  # noqa: BLE001
        return 0, "", f"{url} unreachable: {e}"
    try:
        j = json.loads(body or b"{}")
        return int(j.get("readyConnections") or 0), str(j.get("connectorId") or ""), ""
    except Exception as e:  # noqa: BLE001
        return 0, "", f"{url}: unexpected reply ({e})"


# --------------------------------------------------- release compose check
def _env_map(env):
    """Compose environment (list of K=V / K, or dict) -> {K: V-or-None}."""
    if isinstance(env, dict):
        return {str(k): (None if v is None else str(v)) for k, v in env.items()}
    out = {}
    for item in env or []:
        k, sep, v = str(item).partition("=")
        out[k] = v if sep else None
    return out


def _canon(v):
    return json.dumps(v, sort_keys=True, default=str)


def _templated(v):
    return "${" in _canon(v)


def compose_changes(old, new, local):
    """What a release changes in docker-compose.yml, and whether the local
    compose config already has it.

    old/new: upstream `services` of the installed and the offered release
    (old may be None when the installed release's file can't be read).
    local: our resolved `docker compose config` services.
    Returns [(text, status)] with status "ok" (local already matches),
    "missing" (local lacks it), or "review" (can't be verified automatically).
    """
    items = []
    old = old or {}
    for svc, nc in new.items():
        nc = nc or {}
        lc = local.get(svc)
        oc = old.get(svc) if old else None
        img = nc.get("image") or ""
        if lc is None:
            items.append((f"new service {svc}", "missing"))
            continue
        # Pinned (literal) images, e.g. the database and cache.
        if img and not _templated(img):
            li = lc.get("image") or ""
            if li != img:
                items.append((f"{svc} image {li.split('@')[0]} → {img.split('@')[0]}"
                              if li.split("@")[0] != img.split("@")[0] else f"{svc} image digest", "missing"))
        if old and oc is None:
            continue  # brand-new upstream service already exists locally: nothing more to compare
        # Environment variables
        nenv, lenv = _env_map(nc.get("environment")), _env_map(lc.get("environment"))
        oenv = _env_map((oc or {}).get("environment")) if old else {}
        for k, v in nenv.items():
            if old and k in oenv and oenv[k] == v:
                continue  # unchanged upstream
            if k not in lenv:
                items.append((f"{svc}: new environment variable {k}", "missing"))
            elif old and v is not None and not _templated(v) and lenv.get(k) != v:
                items.append((f"{svc}: {k} changed to {v}", "missing"))
            elif old:
                items.append((f"{svc}: {k} changed", "ok"))
        if old:
            for k in oenv:
                if k not in nenv and k in lenv:
                    items.append((f"{svc}: {k} no longer used upstream", "ok"))
        # Every other setting
        if not old:
            continue
        for key in sorted(set(nc) | set(oc or {})):
            if key in ("image", "environment"):
                continue
            ov, nv = (oc or {}).get(key), nc.get(key)
            if _canon(ov) == _canon(nv):
                continue
            if nv is None:
                items.append((f"{svc}: setting {key} removed upstream", "ok" if key not in lc else "review"))
            elif ov is None:
                items.append((f"{svc}: new setting {key}", "review" if key in lc else "missing"))
            elif not _templated(nv) and _canon(lc.get(key)) == _canon(nv):
                items.append((f"{svc}: {key} changed", "ok"))
            else:
                items.append((f"{svc}: {key} changed", "review"))
    for svc in local:
        if svc not in new:
            items.append((f"service {svc} removed", "missing"))
    return items


# ----------------------------------------------------------------- plugin
class ComposePlugin(Plugin):
    name = "compose"
    state_keys = ("known", "results")

    def __init__(self, bridge, cfg):
        super().__init__(bridge, cfg)
        b = bridge
        self.b = b
        self.root = cfg.get("compose_root", f"/opt/{b.name}")
        self.prefix = cfg.get("version_prefix", "")
        self.titles = cfg.get("titles") or {}
        self.interval = float(cfg.get("check_interval_hours", 6)) * 3600
        self.reg = Registry(USER_AGENT)
        self.arch = local_arch()
        self.lock = threading.Lock()          # guards projects/results/busy
        self.work = threading.Lock()          # one check or install at a time
        self.busy = set()                     # keys with an install running
        self.ctl = set()                      # keys with a start/stop/restart running
        self.status = {}                      # key -> latest running/stopped info
        self.last_action = {}                 # key -> last control result
        self.last_restart_check = 0.0
        self.tunnel = {}                      # key -> connected info (cloudflared only)
        self.gh_cache = (0.0, None)           # (fetched_at, latest cloudflared release)
        self.projects = {}
        self.failed_folders = set()
        self.scanned = False                  # no entity changes until the root is scanned
        self.wake = threading.Event()
        st = b.state
        self.known = set(st.get("known", []))
        self.results = st.get("results", {})
        # {"app": {"title": "...", "members": ["server", "machine_learning"], "health_of": "server"}}
        self.groups = cfg.get("groups") or {}
        self.member_of = {m: g for g, gc in self.groups.items() for m in gc.get("members", [])}
        bs = b.base
        self.cmd_re = re.compile(rf"{re.escape(bs)}/([a-z0-9_]+)/(install|power/set|restart)")

    # ------------------------------------------------------------- state
    def snapshot(self):
        with self.lock:
            return {"known": sorted(self.known), "results": json.loads(json.dumps(self.results))}

    def save(self):
        self.b.save_state()

    # -------------------------------------------------------------- mqtt
    def topics(self, key):
        b = f"{self.b.base}/{key}"
        return {"state": f"{b}/state", "attr": f"{b}/attributes", "cmd": f"{b}/install",
                "run": f"{b}/running", "run_attr": f"{b}/running/attributes",
                "power": f"{b}/power/set", "restart": f"{b}/restart",
                "conn": f"{b}/connected", "conn_attr": f"{b}/connected/attributes"}

    def subscriptions(self):
        bs = self.b.base
        return [f"{bs}/+/install", f"{bs}/+/power/set", f"{bs}/+/restart"]

    def is_command(self, topic):
        return bool(self.cmd_re.fullmatch(topic))

    def label(self, p):
        if self.titles.get(p["key"]):
            return self.titles[p["key"]]
        if p["key"] == slug(p["folder"]):
            return p["folder"]
        if slug(p["folder"]) == self.b.name:
            return p["service"]  # "immich-server", "redis", ...
        return f"{p['folder']} {p['service']}"

    def entity_configs(self, p):
        """(component, object_id, config) for every entity of one service."""
        key, n, node = p["key"], self.b.name, self.b.node
        t = self.topics(key)
        label = self.label(p)
        common = self.b.common()
        confs = [
            ("update", key, {
                "name": label,
                "default_entity_id": f"update.{n}_{key}",
                "unique_id": f"{node}_{key}",
                "icon": "mdi:docker",
                "state_topic": t["state"],
                "command_topic": t["cmd"],
                "payload_install": PRESS_PAYLOAD,
                "json_attributes_topic": t["attr"],
                **common}),
            ("binary_sensor", f"{key}_running", {
                "name": f"{label} running",
                "default_entity_id": f"binary_sensor.{n}_{key}_running",
                "unique_id": f"{node}_{key}_running",
                "device_class": "running",
                "state_topic": t["run"],
                "payload_on": ON, "payload_off": OFF,
                "json_attributes_topic": t["run_attr"],
                **common}),
            ("switch", f"{key}_power", {
                "name": f"{label} power",
                "default_entity_id": f"switch.{n}_{key}",
                "unique_id": f"{node}_{key}_power",
                "icon": "mdi:power",
                "state_topic": t["run"],
                "command_topic": t["power"],
                "payload_on": ON, "payload_off": OFF,
                "state_on": ON, "state_off": OFF,
                **common}),
            ("button", f"{key}_restart", {
                "name": f"Restart {label}",
                "default_entity_id": f"button.{n}_{key}_restart",
                "unique_id": f"{node}_{key}_restart",
                "device_class": "restart",
                "command_topic": t["restart"],
                "payload_press": RESTART_PAYLOAD,
                **common}),
        ]
        if is_cloudflared(p):
            confs[0][2]["icon"] = "mdi:cloud-lock-outline"
            confs.append(("binary_sensor", f"{key}_connected", {
                "name": f"{label} connected",
                "default_entity_id": f"binary_sensor.{n}_{key}_connected",
                "unique_id": f"{node}_{key}_connected",
                "device_class": "connectivity",
                "state_topic": t["conn"],
                "payload_on": ON, "payload_off": OFF,
                "json_attributes_topic": t["conn_attr"],
                **common}))
        if key in self.member_of:
            # Grouped services update only through the group's own update entity.
            confs = [c for c in confs if c[0] != "update"]
        return confs

    def group_ready(self, gkey, projects=None):
        projects = self.projects if projects is None else projects
        return all(m in projects for m in self.groups[gkey].get("members", []))

    def group_config(self, gkey):
        g = self.groups[gkey]
        t = self.topics(gkey)
        return ("update", gkey, {
            # null = use the device name, so HA shows "Immich" rather than "Immich Immich"
            "name": None if g.get("title", gkey) == self.b.device["name"] else g.get("title", gkey),
            "default_entity_id": f"update.{self.b.name}_{gkey}",
            "unique_id": f"{self.b.node}_{gkey}",
            "icon": g.get("icon", "mdi:package-up"),
            "state_topic": t["state"],
            "command_topic": t["cmd"],
            "payload_install": PRESS_PAYLOAD,
            "json_attributes_topic": t["attr"],
            **self.b.common()})

    def publish_discovery(self):
        pub = self.b.publish
        with self.lock:
            if not self.scanned:
                return  # entities from the last run stay retained in the broker
            projects = dict(self.projects)
            # A folder whose compose file is temporarily unreadable keeps its entities.
            keep = {k for k in self.known
                    if (self.results.get(k) or {}).get("folder_name") in self.failed_folders}
            gone = self.known - set(projects) - keep - set(self.groups)
        for key, p in projects.items():
            for component, oid, conf in self.entity_configs(p):
                pub(self.b.discovery_topic(component, oid), conf)
        for gkey in self.groups:
            if self.group_ready(gkey, projects):
                component, oid, conf = self.group_config(gkey)
                pub(self.b.discovery_topic(component, oid), conf)
        for key in gone:  # folder removed from the compose root: drop its entities
            log.info("Removing entities for %s (folder gone)", key)
            t = self.topics(key)
            for component, oid in (("update", key), ("binary_sensor", f"{key}_running"),
                                   ("switch", f"{key}_power"), ("button", f"{key}_restart"),
                                   ("binary_sensor", f"{key}_connected")):
                pub(self.b.discovery_topic(component, oid), "")
            for top in (t["state"], t["attr"], t["run"], t["run_attr"], t["conn"], t["conn_attr"]):
                pub(top, "")
            with self.lock:
                self.results.pop(key, None)
                self.status.pop(key, None)
                self.tunnel.pop(key, None)
        with self.lock:
            self.known = set(projects) | keep
        self.save()

    def publish_project(self, key):
        with self.lock:
            r = dict(self.results.get(key) or {})
            in_progress = key in self.busy
        if not r:
            return
        t = self.topics(key)
        state = {
            "installed_version": r.get("installed_version"),
            "latest_version": r.get("latest_version"),
            "title": r.get("title"),
            "release_url": r.get("release_url"),
            "in_progress": in_progress,
            # Always sent (as "" when none) so HA clears an old summary.
            "release_summary": (r.get("release_summary") or "")[:255],
        }
        # HA drops the WHOLE message if any key is null (cv.string / cv.url).
        state = {k: v for k, v in state.items() if v is not None}
        attrs = {k: v for k, v in r.items()
                 if k not in ("installed_version", "latest_version", "release_summary")}
        self.b.publish(t["state"], json.dumps(state))
        self.b.publish(t["attr"], json.dumps(attrs))

    def republish(self):
        with self.lock:
            keys = list(self.projects)
        for k in keys:
            self.publish_project(k)
            self.publish_status(k)
        for g in self.groups:
            self.publish_project(g)

    def publish_status(self, key):
        with self.lock:
            s = dict(self.status.get(key) or {})
        if not s:
            return
        t = self.topics(key)
        self.b.publish(t["run"], ON if s.get("running") else OFF)
        self.b.publish(t["run_attr"], json.dumps(s))
        self.publish_tunnel(key)

    def publish_tunnel(self, key):
        with self.lock:
            tn = dict(self.tunnel.get(key) or {})
        if not tn:
            return
        t = self.topics(key)
        self.b.publish(t["conn"], ON if tn.get("connections", 0) > 0 else OFF)
        self.b.publish(t["conn_attr"], json.dumps(tn))

    def poll_tunnels(self, projects, running):
        """Edge connections for every cloudflared connector; publish what changed."""
        for key, p in projects.items():
            if not is_cloudflared(p):
                continue
            addr = metrics_address(p.get("command"), self.cfg.get("cloudflared_metrics", CLOUDFLARED_METRICS))
            if running.get(key):
                n, cid, err = cloudflared_ready(addr)
            else:
                n, cid, err = 0, "", "container not running"
            tn = {"connections": n, "connector_id": cid, "metrics": addr, "error": err}
            with self.lock:
                changed = self.tunnel.get(key) != tn
                self.tunnel[key] = tn
            if changed:
                self.publish_tunnel(key)

    def on_check(self):
        self.wake.set()

    def handle(self, topic, payload):
        m = self.cmd_re.fullmatch(topic)
        if not m:
            return False
        allowed = {"install": {PRESS_PAYLOAD}, "power/set": {ON, OFF}, "restart": {RESTART_PAYLOAD}}
        if payload not in allowed[m.group(2)]:
            log.warning("Ignoring unexpected message on %s", topic)
            return True
        key, kind = m.group(1), m.group(2)
        action = {"install": "install", "restart": "restart",
                  "power/set": "start" if payload == ON else "stop"}[kind]
        if key in self.groups:
            with self.lock:
                members = self.groups[key].get("members", [])
                if action != "install" or not self.group_ready(key):
                    log.warning("%s requested for group %r; ignored", action, key)
                    return True
                if key in self.busy or any(m in self.busy or m in self.ctl for m in members):
                    log.info("%s: busy; install ignored", key)
                    return True
                self.busy.add(key)
                self.busy.update(members)  # controls on members are refused meanwhile
            self.publish_project(key)
            threading.Thread(target=self.install_group, args=(key,), daemon=True).start()
            return True
        with self.lock:
            if key not in self.projects:
                log.warning("%s requested for unknown service %r; ignored", action, key)
                return True
            if action == "install" and key in self.member_of:
                log.warning("%s updates only with group %r; ignored", key, self.member_of[key])
                return True
            if key in self.busy or key in self.ctl:
                log.info("%s: busy (%s); %s ignored", key,
                         "updating" if key in self.busy else "control running", action)
                if kind == "power/set":
                    threading.Timer(1.0, self.publish_status, args=(key,)).start()  # snap the switch back
                return True
            (self.busy if action == "install" else self.ctl).add(key)
        if action == "install":
            self.publish_project(key)
            threading.Thread(target=self.install, args=(key,), daemon=True).start()
        else:
            threading.Thread(target=self.control, args=(key, action), daemon=True).start()
        return True

    # ---------------------------------------------------------- controls
    def control(self, key, action):
        """start / stop / restart one container. Fixed commands only."""
        try:
            with self.lock:
                p = dict(self.projects[key])
            log.info("%s: %s requested from Home Assistant", key, action)
            if action == "start":
                r = docker("start", p["container"], check=False, timeout=120)
                if r.returncode != 0:  # container was removed: let compose recreate it
                    docker("compose", "up", "-d", p["service"], cwd=p["dir"], timeout=300)
            elif action == "stop":
                docker("stop", p["container"], timeout=120)
            elif action == "restart":
                docker("restart", p["container"], timeout=180)
            result = "OK"
        except Exception as e:  # noqa: BLE001
            result = f"Failed: {e}"
            log.error("%s: %s %s", key, action, result)
        with self.lock:
            self.ctl.discard(key)
            self.last_action[key] = {"last_action": action, "last_action_at": now_iso(),
                                     "last_action_result": result}
        self.poll_status()

    # ------------------------------------------------------------ status
    def poll_status(self):
        """Refresh running/stopped for every service; publish what changed."""
        with self.lock:
            projects = dict(self.projects)
        if not projects:
            return
        names = {p["container"]: k for k, p in projects.items()}
        found = {}
        try:
            r = docker("inspect", *names, check=False, timeout=30)
            for c in json.loads(r.stdout or "[]"):
                found[(c.get("Name") or "").lstrip("/")] = c
        except Exception as e:  # noqa: BLE001
            log.warning("Status poll failed: %s", e)
            return
        for cname, key in names.items():
            c = found.get(cname)
            st = (c or {}).get("State") or {}
            s = {
                "container": cname,
                "status": st.get("Status") or ("missing" if c is None else "unknown"),
                "running": bool(st.get("Running")),
                "started_at": st.get("StartedAt"),
                "finished_at": st.get("FinishedAt"),
                "exit_code": st.get("ExitCode"),
                "restart_count": (c or {}).get("RestartCount"),
                "health": (st.get("Health") or {}).get("Status"),
            }
            with self.lock:
                s.update(self.last_action.get(key) or {})
                prev = self.status.get(key)
                changed = prev != s
                self.status[key] = s
                # Container (re)started outside an install, e.g. first `up -d`
                # or a manual pull: re-check so the installed version is current.
                restarted = (prev is not None and s["running"]
                             and s.get("started_at") != prev.get("started_at")
                             and key not in self.busy)
            if changed:
                self.publish_status(key)
            if restarted and time.monotonic() - self.last_restart_check > float(
                    self.cfg.get("restart_recheck_seconds", 120)):
                # Throttled so a crash-looping container can't hammer the registry.
                self.last_restart_check = time.monotonic()
                log.info("%s: container started; re-checking its version", key)
                self.wake.set()
        with self.lock:
            running = {k: (self.status.get(k) or {}).get("running") for k in projects}
        self.poll_tunnels(projects, running)

    def status_loop(self):
        every = float(self.cfg.get("status_interval_seconds", 30))
        while not self.b.stop.wait(every):
            if self.scanned and self.b.connected.is_set():
                try:
                    self.poll_status()
                except Exception as e:  # noqa: BLE001
                    log.exception("Status poll crashed: %s", e)

    def check_loop(self):
        while not self.b.stop.is_set():
            try:
                self.check_all()
            except Exception as e:  # noqa: BLE001
                log.exception("Check cycle failed: %s", e)
            self.wake.wait(self.interval)
            self.wake.clear()

    def start(self):
        threading.Thread(target=self.status_loop, name="compose-status", daemon=True).start()
        threading.Thread(target=self.check_loop, name="compose-check", daemon=True).start()

    def stop(self):
        self.wake.set()

    # ------------------------------------------------------------- check
    def check_project(self, p):
        key = p["key"]
        registry, repo, tag, familiar = parse_ref(p["image"])
        res = {
            "title": self.label(p),
            "folder_name": p["folder"],
            "folder": p["dir"],
            "service": p["service"],
            "image": p["image"],
            "container": p["container"],
            "last_check": now_iso(),
        }
        with self.lock:
            prev = self.results.get(key) or {}
        for k in ("last_install", "last_install_result", "previous_tag", "log_file"):
            if k in prev:
                res[k] = prev[k]

        # What's actually running (not just what's been pulled).
        local_digest, labels, running = None, {}, False
        try:
            c = json.loads(docker("inspect", p["container"]).stdout)[0]
            running = bool((c.get("State") or {}).get("Running"))
            img_id = c.get("Image")
        except Exception:  # noqa: BLE001
            img_id = p["image"]  # container missing: fall back to the pulled image
        try:
            img = json.loads(docker("image", "inspect", img_id).stdout)[0]
            labels = (img.get("Config") or {}).get("Labels") or {}
            for rd in img.get("RepoDigests") or []:
                name, _, dg = rd.partition("@")
                if parse_ref(name)[3] == familiar:
                    local_digest = dg
                    break
        except Exception as e:  # noqa: BLE001
            res["check_error"] = f"Local image not found: {e}"
        res["running"] = running
        res["installed_digest"] = local_digest
        lv = label_version(labels, self.prefix)
        installed = lv or short(local_digest) or "unknown"
        res["installed_version"] = installed
        fallback_url = (self.cfg.get("release_urls") or {}).get(key)
        installed_url = release_url(labels, lv) or fallback_url
        res["release_url"] = installed_url
        res["installed_url"] = installed_url

        pinned = p["image"].split("@", 1)[1] if "@sha256:" in p["image"] else None
        if pinned:
            # The compose file pins an exact image. Newer pushes to the tag aren't
            # meant for us; the pin only changes with a release, so compare against it.
            res["pinned"] = True
            res["latest_digest"] = pinned
            if local_digest == pinned:
                res["latest_version"] = installed
                res["release_summary"] = ""
            else:
                res["latest_version"] = short(pinned)
                res["release_summary"] = ("The compose file pins a different image than the one running; "
                                          "Install pulls the pinned image.")
            res["last_check_result"] = "OK"
            self.apply_cloudflared(p, res, prev)
            with self.lock:
                self.results[key] = res
            return res
        try:
            remote = self.reg.digest(registry, repo, tag)
            res["latest_digest"] = remote
            if local_digest and remote == local_digest:
                res["latest_version"] = installed
            else:
                rv = None
                try:
                    rlabels = self.reg.labels(registry, repo, remote, self.arch)
                    rv = label_version(rlabels, self.prefix)
                    res["release_url"] = release_url(rlabels, rv) or res["release_url"]
                except Exception as e:  # noqa: BLE001
                    log.info("%s: couldn't read remote labels (%s)", key, e)
                if rv and rv != lv:
                    res["latest_version"] = rv
                    res["release_summary"] = f"{self.label(p)} {installed} → {rv}."
                else:
                    res["latest_version"] = f"{rv} ({short(remote)})" if rv else short(remote)
                    res["release_summary"] = (f"New `{tag}` image published "
                                              f"({short(local_digest) or '?'} → {short(remote)}).")
            res["last_check_result"] = "OK"
            res.pop("check_error", None)
        except Exception as e:  # noqa: BLE001
            # Registry unreachable: keep a previously-seen update only if nothing
            # changed locally since; otherwise (just installed/started) assume
            # current rather than advertising a stale "update".
            res["latest_digest"] = prev.get("latest_digest")
            unchanged = (local_digest is not None
                         and local_digest == prev.get("installed_digest"))
            pending = prev.get("latest_digest") and prev.get("latest_digest") != local_digest
            res["latest_version"] = (prev.get("latest_version")
                                     if unchanged and pending else installed)
            if unchanged and pending and prev.get("release_summary"):
                res["release_summary"] = prev["release_summary"]
            res["last_check_result"] = f"Registry check failed: {e}"
            log.warning("%s: %s", key, res["last_check_result"])
        self.apply_cloudflared(p, res, prev)
        self.hold_prerelease(key, res, installed_url)
        with self.lock:
            self.results[key] = res
        return res

    def prereleases_allowed(self, key):
        return key is not None and key in (self.cfg.get("allow_prereleases") or [])

    def hold_prerelease(self, key, res, installed_url):
        """Stable only: a pre-release on the tag we follow is reported, never offered."""
        latest = res.get("latest_version")
        res.pop("ignored_prerelease", None)
        if (not latest or latest == res.get("installed_version") or not is_prerelease(latest)
                or self.prereleases_allowed(key) or self.prereleases_allowed(self.member_of.get(key))):
            return
        log.warning("%s: %s looks like a pre-release; not offering it (allow_prereleases is off)", key, latest)
        res["ignored_prerelease"] = latest
        res["latest_version"] = res.get("installed_version")
        # The link stays on what's installed, not on the release that was held back.
        res["release_url"] = installed_url or res.get("release_url")
        res["release_summary"] = ""

    def latest_cloudflared(self):
        """Newest cloudflared release on GitHub, cached for an hour: {tag, url} or None."""
        at, rel = self.gh_cache
        if rel and time.monotonic() - at < 3600:
            return rel
        url = self.cfg.get("cloudflared_releases_api", CLOUDFLARED_API)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read())
            tag = str(j.get("tag_name") or "").strip()
            if not re.fullmatch(r"\d+\.\d+\.\d+", tag) or j.get("prerelease") or j.get("draft"):
                raise ValueError(f"not a stable release tag: {tag!r}")
            rel = {"tag": tag, "url": j.get("html_url") or f"{CLOUDFLARED_RELEASES}/tag/{tag}"}
            self.gh_cache = (time.monotonic(), rel)
            return rel
        except Exception as e:  # noqa: BLE001
            log.info("cloudflared: couldn't read the latest GitHub release (%s)", e)
            return rel  # last known, possibly None

    def apply_cloudflared(self, p, res, prev):
        """The cloudflared image has no version label: report real release numbers instead
        of digests. Whether an update exists is still decided by the image digest."""
        if not is_cloudflared(p):
            return
        digest_version = res.get("installed_version")
        pending = res.get("latest_version") not in (None, digest_version)
        iv = cloudflared_version(p["container"]) if res.get("running") else None
        if not iv and prev.get("installed_digest") == res.get("installed_digest"):
            iv = prev.get("cloudflared_version")  # stopped, but the same image as last time
        if not iv:
            res["version_note"] = "cloudflared --version unavailable (container not running?)"
            return
        res.pop("version_note", None)
        res["cloudflared_version"] = iv
        res["installed_version"] = iv
        res["release_url"] = f"{CLOUDFLARED_RELEASES}/tag/{iv}"
        if not pending:
            res["latest_version"] = iv
            res["release_summary"] = ""
            return
        rel = self.latest_cloudflared()
        if rel and rel["tag"] != iv:
            res["latest_version"] = rel["tag"]
            res["release_url"] = rel["url"]
            res["release_summary"] = f"cloudflared {iv} → {rel['tag']}."
        elif not rel and prev.get("latest_version") not in (None, iv, prev.get("installed_version")):
            res["latest_version"] = prev["latest_version"]  # GitHub unreachable: keep what we knew
        else:
            res["latest_version"] = f"{iv} (rebuilt)"
            res["release_summary"] = f"New `latest` image build of cloudflared {iv}."

    def scan(self):
        projects, failed = discover_projects(self.root, self.b.name)
        with self.lock:
            self.projects = projects
            self.failed_folders = failed
            self.scanned = True
        return projects

    def check_all(self):
        with self.work:
            projects = self.scan()
            if self.b.connected.is_set():
                self.publish_discovery()
            for key, p in projects.items():
                with self.lock:
                    if key in self.busy:
                        continue
                try:
                    r = self.check_project(p)
                    log.info("%s: installed %s, latest %s", key,
                             r.get("installed_version"), r.get("latest_version"))
                except Exception as e:  # noqa: BLE001
                    log.exception("%s: check crashed: %s", key, e)
                self.publish_project(key)
            for gkey in self.groups:
                with self.lock:
                    if gkey in self.busy or not self.group_ready(gkey, projects):
                        continue
                try:
                    r = self.check_group(gkey)
                    log.info("%s: installed %s, latest %s", gkey,
                             r.get("installed_version"), r.get("latest_version"))
                except Exception as e:  # noqa: BLE001
                    log.exception("%s: group check crashed: %s", gkey, e)
                self.publish_project(gkey)
            self.save()
        self.poll_status()

    # ------------------------------------------------------------ groups
    def compose_url(self, gkey):
        g = self.groups[gkey]
        url = g.get("compose_check_url") or self.cfg.get("release_compose_url")
        if not url and "immich" in self.cfg:
            url = IMMICH_COMPOSE_URL  # Immich publishes docker-compose.yml with every release
        return url

    def check_group(self, gkey):
        """Combine the members' checks into one update entity (members were just checked)."""
        g = self.groups[gkey]
        members = g.get("members", [])
        lead = g.get("health_of", members[0])
        with self.lock:
            rs = {m: dict(self.results.get(m) or {}) for m in members}
            prev = dict(self.results.get(gkey) or {})
            lead_p = dict(self.projects[lead])
        lr = rs[lead]
        installed = lr.get("installed_version") or "unknown"
        res = {
            "title": g.get("title", gkey),
            "folder": lead_p["dir"],
            "services": [rs[m].get("service") or m for m in members],
            "last_check": now_iso(),
            "installed_version": installed,
            "release_url": lr.get("release_url") or (self.cfg.get("release_urls") or {}).get(gkey),
            "running": all(rs[m].get("running") for m in members),
        }
        for k in ("last_install", "last_install_result", "log_file", "dump_file", "previous_tags",
                  "compose_seen"):
            if k in prev:
                res[k] = prev[k]
        held = sorted({rs[m]["ignored_prerelease"] for m in members if rs[m].get("ignored_prerelease")})
        if held and not self.prereleases_allowed(gkey):
            # The tag we follow points at a pre-release: offer nothing until it moves on.
            res["ignored_prerelease"] = held[0]
            pending = []
        else:
            pending = [m for m in members
                       if rs[m].get("latest_digest") and rs[m].get("latest_digest") != rs[m].get("installed_digest")]
        errors = [f"{m}: {rs[m]['last_check_result']}" for m in members
                  if rs[m].get("last_check_result", "OK") != "OK"]
        res["last_check_result"] = "; ".join(errors) or "OK"
        summary = ""
        if not pending:
            latest = installed
            res.pop("compose_seen", None)
            # Nothing offered: link to what's installed, even when a member's last
            # check saw a newer release than the others.
            res["release_url"] = (lr.get("installed_url") or lr.get("release_url")
                                  or (self.cfg.get("release_urls") or {}).get(gkey))
        else:
            res["pending"] = [rs[m].get("service") or m for m in pending]
            latest = lr.get("latest_version") if lead in pending else None
            if not latest or latest == installed:
                latest = f"{installed} (rebuilt)"
            summary = (f"{res['title']} {latest} is available. Read the release notes first; Install "
                       + ("backs up the database, then " if (self.cfg.get("db_dump") or {}).get("enabled", True)
                          and self.cfg.get("db_dump") else "")
                       + "updates all its containers together.")
            rv = latest if re.fullmatch(r"v?\d+\.\d+\.\d+", latest or "") else None
            url = self.compose_url(gkey)
            check = self.compose_check(gkey, lead_p, installed, rv, url, res) if rv and url else None
            if check is None and rv and url:
                res["compose_check"] = "Couldn't read the release's docker-compose.yml"
            elif check is not None:
                blocking = [t for t, s in check if s == "missing"] + [t for t, s in check if s == "review"]
                res["compose_changes"] = [f"{t} ({s})" for t, s in check]
                if blocking:
                    res["compose_changed"] = blocking
                    summary = (f"{res['title']} {latest} changes docker-compose.yml ({'; '.join(blocking)}). "
                               "Update the compose file first; Install refuses until it matches.")
                else:
                    res["compose_check"] = "OK"
        res["latest_version"] = latest
        res["release_summary"] = summary
        with self.lock:
            self.results[gkey] = res
        return res

    def _fetch_release_compose(self, url, version):
        req = urllib.request.Request(url.format(version=version), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            return (yaml.safe_load(r.read().decode()) or {}).get("services") or {}

    def compose_check(self, gkey, p, installed, version, url, res):
        """[(text, status)] for what the release changes in docker-compose.yml, or None.

        Compares every setting the release changes (environment variables,
        devices, healthchecks, ...), not just pinned images. Changes that can't be
        verified against the resolved local config count as handled once the local
        compose config has changed since the release was first seen."""
        if yaml is None:
            return None
        try:
            new = self._fetch_release_compose(url, version)
            local = (json.loads(docker("compose", "config", "--format", "json", cwd=p["dir"]).stdout)
                     .get("services") or {})
        except Exception as e:  # noqa: BLE001
            log.info("Compose check for %s skipped: %s", version, e)
            return None
        old = None
        if re.fullmatch(r"v?\d+\.\d+\.\d+", installed or ""):
            try:
                old = self._fetch_release_compose(url, installed)
            except Exception as e:  # noqa: BLE001
                log.info("Compose check: no compose file for installed %s (%s); "
                         "comparing images and new variables only", installed, e)
        items = compose_changes(old, new, local)
        local_hash = hashlib.sha256(json.dumps(local, sort_keys=True).encode()).hexdigest()[:16]
        seen = res.get("compose_seen") or {}
        if seen.get("version") != version:
            seen = {"version": version, "local": local_hash, "at": now_iso()}
        res["compose_seen"] = seen
        if seen["local"] != local_hash:
            items = [(t, "ok" if s == "review" else s) for t, s in items]
        return items

    def _stepper(self, lf, cwd, timeout):
        def step(*args, check=True, t=timeout):
            lf.write(f"\n$ docker {' '.join(args)}\n")
            lf.flush()
            pr = docker(*args, cwd=cwd, timeout=t, check=False)
            lf.write(strip_ansi(pr.stdout + pr.stderr))
            lf.flush()
            if check and pr.returncode != 0:
                raise RuntimeError(f"`docker {' '.join(args[:2])}` exited with code {pr.returncode}")
            return pr
        return step

    def dump_database(self, lf, before, gkey):
        dc = self.cfg.get("db_dump") or {}
        if not dc or not dc.get("enabled", True):
            lf.write("\n# Database dump not configured\n")
            return None
        d = dc.get("dir")
        if not d:
            raise RuntimeError("db_dump.dir is not set in the config; nothing was changed")
        os.makedirs(d, exist_ok=True)
        stem = dc.get("prefix", f"{self.b.name}-pre-update")
        fn = os.path.join(d, f"{stem}-{slug(before or 'unknown')}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sql.gz")
        cmd = [util.DOCKER, "exec", dc.get("container", f"{self.b.name}-postgres"), "pg_dumpall",
               "--clean", "--if-exists", f"--username={dc.get('user', 'postgres')}"]
        lf.write(f"\n$ {' '.join(cmd[1:])} | gzip > {fn}\n")
        lf.flush()
        part = fn + ".part"
        with tempfile.TemporaryFile() as err, gzip.open(part, "wb") as gz:
            pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL)
            for chunk in iter(lambda: pr.stdout.read(1 << 20), b""):
                gz.write(chunk)
            rc = pr.wait(timeout=int(dc.get("timeout_minutes", 15)) * 60)
            err.seek(0)
            msg = err.read().decode("utf-8", "replace").strip()
        if rc != 0 or os.path.getsize(part) < 100:
            os.remove(part)
            raise RuntimeError(f"Database dump failed, nothing was changed ({msg[-200:] or f'exit {rc}'})")
        os.replace(part, fn)
        lf.write(f"# Database dump: {os.path.getsize(fn) // 1024} KiB\n")
        keep = int(dc.get("keep", 3))
        dumps = sorted(f for f in os.listdir(d) if f.startswith(f"{stem}-") and f.endswith(".sql.gz"))
        for old in dumps[:-keep] if keep > 0 else []:
            os.remove(os.path.join(d, old))
            lf.write(f"# Removed old dump {old}\n")
        return fn

    def wait_healthy(self, ps, lead, lf):
        """Every member running and the lead's healthcheck 'healthy' (or none)."""
        settle = int(self.cfg.get("settle_seconds", 10))
        deadline = time.monotonic() + int(self.cfg.get("health_timeout_seconds", 300))
        time.sleep(settle)
        while True:
            r = docker("inspect", *[p["container"] for p in ps.values()], check=False, timeout=30)
            try:
                byname = {(c.get("Name") or "").lstrip("/"): c for c in json.loads(r.stdout or "[]")}
            except ValueError:
                byname = {}
            problems = []
            for m, p in ps.items():
                st = (byname.get(p["container"]) or {}).get("State") or {}
                health = (st.get("Health") or {}).get("Status")
                if not st.get("Running"):
                    problems.append(f"{p['service']} not running")
                elif m == lead and health not in (None, "healthy"):
                    problems.append(f"{p['service']} {health}")
            if not problems:
                lf.write("\n# All containers running; lead healthy\n")
                return True, ""
            if time.monotonic() >= deadline:
                lf.write(f"\n# Gave up waiting: {'; '.join(problems)}\n")
                return False, "; ".join(problems)
            time.sleep(5)

    def install_group(self, gkey):
        g = self.groups[gkey]
        members = g.get("members", [])
        lead = g.get("health_of", members[0])
        log_path = os.path.join(self.b.log_dir, f"{gkey}-last-update.log")
        started = now_iso()
        result, dump, prev_tags, ps = None, None, {}, {}
        try:
            with self.work:
                with self.lock:
                    ps = {m: dict(self.projects[m]) for m in members}
                # Re-check now: the tag may have moved since the last check, and a pull
                # must never bring in a pre-release or skip the compose check.
                for p in ps.values():
                    self.check_project(p)
                gr = self.check_group(gkey)
                before = gr.get("installed_version")
                if gr.get("ignored_prerelease"):
                    raise RuntimeError(f"Not installed: {gr['ignored_prerelease']} is a pre-release and "
                                       "allow_prereleases is off. Stable releases only.")
                if gr.get("compose_changed"):
                    raise RuntimeError("Not installed: this release changes docker-compose.yml ("
                                       + "; ".join(gr["compose_changed"]) + "). Update the compose file, "
                                       "then press Check for updates")
                timeout = int(self.cfg.get("timeout_minutes", 15)) * 60
                with open(log_path, "w") as lf:
                    step = self._stepper(lf, ps[lead]["dir"], timeout)
                    lf.write(f"# {self.b.service} {self._version()}: update {gkey} "
                             f"({', '.join(p['service'] for p in ps.values())}) at {started}\n")
                    # 1. Database dump first; if it fails nothing else happens.
                    dump = self.dump_database(lf, before, gkey)
                    # 2. Rollback copies of what's running now.
                    ids = {}
                    for m, p in ps.items():
                        insp = step("inspect", "--format", "{{.Image}}", p["container"], check=False)
                        if insp.returncode == 0 and insp.stdout.strip():
                            ids[m] = insp.stdout.strip()
                            prev_tags[m] = f"{parse_ref(p['image'])[3]}:previous"
                            step("tag", ids[m], prev_tags[m])
                    # 3. Pull everything before touching anything, then recreate together.
                    for p in ps.values():
                        step("compose", "pull", p["service"])
                    for p in ps.values():
                        step("compose", "up", "-d", p["service"])
                    # 4. Healthy, or put the old images back.
                    ok, why = self.wait_healthy(ps, lead, lf)
                    if not ok:
                        lf.write("\n# New version didn't come up: rolling back\n")
                        try:
                            for m, p in ps.items():
                                if m in ids:
                                    step("tag", ids[m], p["image"])
                            for p in ps.values():
                                step("compose", "up", "-d", p["service"])
                            rok, _ = self.wait_healthy(ps, lead, lf)
                        except Exception:  # noqa: BLE001
                            rok = False
                        hint = (f" If it still won't start, its database may already be upgraded: "
                                f"restore {dump}." if dump else "")
                        raise RuntimeError((f"New version didn't come up ({why}); rolled back to the previous images."
                                            if rok else
                                            f"New version didn't come up ({why}) and the rollback didn't either.")
                                           + hint)
                    if self.cfg.get("prune_after_update", True):
                        step("image", "prune", "-f", check=False)
                for p in ps.values():
                    self.check_project(p)
                after = self.check_group(gkey).get("installed_version")
                if before and after and before != after:
                    result = f"Updated {before} → {after}"
                elif gr.get("pending"):
                    result = f"Installed rebuilt {after} images"
                else:
                    result = f"Reinstalled; still {after}"
        except Exception as e:  # noqa: BLE001
            result = f"Failed: {e}"
            log.error("%s: %s", gkey, result)
            try:
                for p in ps.values():
                    self.check_project(p)
                if ps:
                    self.check_group(gkey)
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            r = self.results.setdefault(gkey, {})
            r["last_install"] = started
            r["last_install_result"] = result
            r["log_file"] = log_path
            if dump:
                r["dump_file"] = dump
            if prev_tags:
                r["previous_tags"] = prev_tags
            self.busy.discard(gkey)
            for m in members:
                self.busy.discard(m)
        log.info("%s: %s", gkey, result)
        self.save()
        self.publish_project(gkey)
        self.poll_status()

    @staticmethod
    def _version():
        from . import VERSION
        return VERSION

    # ----------------------------------------------------------- install
    def install(self, key):
        log_path = os.path.join(self.b.log_dir, f"{key}-last-update.log")
        started = now_iso()
        result, prev_tag = None, None
        try:
            with self.work:
                with self.lock:
                    p = dict(self.projects[key])
                    before = (self.results.get(key) or {}).get("installed_version")
                # Re-check now: the tag may have moved since the last check, and a
                # pull must never bring in a pre-release.
                fresh = self.check_project(p)
                if fresh.get("ignored_prerelease"):
                    raise RuntimeError(f"Not installed: {fresh['ignored_prerelease']} is a pre-release and "
                                       "allow_prereleases is off. Stable releases only.")
                registry, repo, tag, familiar = parse_ref(p["image"])
                timeout = int(self.cfg.get("timeout_minutes", 15)) * 60
                with open(log_path, "w") as lf:
                    step = self._stepper(lf, p["dir"], timeout)
                    lf.write(f"# {self.b.service} {self._version()}: update {key} at {started}\n")
                    # 1. Keep a rollback copy of what's running now.
                    insp = step("inspect", "--format", "{{.Image}}", p["container"], check=False)
                    img_id = insp.stdout.strip() if insp.returncode == 0 else ""
                    if img_id:
                        prev_tag = f"{familiar}:previous"
                        step("tag", img_id, prev_tag)
                    # 2. Pull and recreate just this service.
                    step("compose", "pull", p["service"])
                    step("compose", "up", "-d", p["service"])
                    # 3. Make sure it stayed up; if not, put the old image back.
                    time.sleep(int(self.cfg.get("settle_seconds", 10)))
                    st = step("inspect", "--format", "{{.State.Running}}", p["container"], check=False)
                    if st.stdout.strip() != "true":
                        if not img_id:
                            raise RuntimeError("Container isn't running after the update "
                                               "(no previous image to roll back to)")
                        lf.write("\n# New image didn't stay running: rolling back\n")
                        try:
                            step("tag", img_id, p["image"])
                            step("compose", "up", "-d", p["service"])
                            time.sleep(int(self.cfg.get("settle_seconds", 10)))
                            ok = step("inspect", "--format", "{{.State.Running}}",
                                      p["container"], check=False).stdout.strip() == "true"
                        except Exception:  # noqa: BLE001
                            ok = False
                        raise RuntimeError(
                            "New image didn't stay running; rolled back to the previous image"
                            if ok else
                            "New image didn't stay running and the rollback didn't start either. "
                            f"Previous image is tagged {prev_tag}")
                    if self.cfg.get("prune_after_update", True):
                        step("image", "prune", "-f", check=False)
                after = self.check_project(p)
                now = after.get("installed_version")
                result = (f"Updated {before} → {now}" if before and now and before != now
                          else f"Reinstalled; still {now}")
        except Exception as e:  # noqa: BLE001
            result = f"Failed: {e}"
            log.error("%s: %s", key, result)
            try:
                with self.lock:
                    p = dict(self.projects.get(key) or {})
                if p:
                    self.check_project(p)
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            r = self.results.setdefault(key, {})
            r["last_install"] = started
            r["last_install_result"] = result
            r["log_file"] = log_path
            if prev_tag:
                r["previous_tag"] = prev_tag
            self.busy.discard(key)
        log.info("%s: %s", key, result)
        self.save()
        self.publish_project(key)
        self.poll_status()


__all__ = ["ComposePlugin", "compose_changes", "discover_projects", "clean"]
