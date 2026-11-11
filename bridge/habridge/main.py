"""Command line: run the bridge for one host, or check a config / a new release."""

import argparse
import json
import logging
import os
import re
import signal
import sys

from . import VERSION, registry, util
from .core import DISCOVERY_PREFIX, Bridge

log = logging.getLogger("habridge")


def available_plugins():
    from .anthias import AnthiasPlugin
    from .compose import ComposePlugin
    from .foundry import FoundryPlugin
    from .immich import ImmichPlugin
    from .jellyfin import JellyfinPlugin
    from .selfupdate import SelfUpdatePlugin
    return {"compose": ComposePlugin, "immich": ImmichPlugin, "jellyfin": JellyfinPlugin,
            "foundry": FoundryPlugin, "anthias": AnthiasPlugin, "selfupdate": SelfUpdatePlugin}


def build_plugins(bridge, cfg):
    """Plugins come from config "plugins"; without it: compose, plus immich,
    jellyfin, foundry or anthias when the config has that section, and selfupdate."""
    available = available_plugins()
    names = cfg.get("plugins")
    if names is None:
        names = ["compose"]
        if (cfg.get("immich") or {}).get("api_key"):
            names.append("immich")
        for app in ("jellyfin", "foundry", "anthias"):
            if cfg.get(app):
                names.append(app)
    names = [n for n in names if n != "selfupdate"] + ["selfupdate"]  # always last, always present
    unknown = [n for n in names if n not in available]
    if unknown:
        raise SystemExit(f"Unknown plugin(s) in config: {', '.join(unknown)}")
    return [available[n](bridge, cfg) for n in names]


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="bridge.py", description=f"homelab-ha-bridge {VERSION}")
    ap.add_argument("--name", default=os.environ.get("HABRIDGE_NAME"),
                    help="host name: drives topics, entity IDs and paths (e.g. media)")
    ap.add_argument("--config", help="config file (default /etc/<name>-ha/config.json)")
    ap.add_argument("--log-dir", help="state and logs (default /var/log/<name>-ha)")
    ap.add_argument("--self-test", action="store_true", help="load the config and plugins, then exit")
    ap.add_argument("--print-keys", action="store_true",
                    help="print the Compose keys this version would publish, as JSON, then exit")
    ap.add_argument("--check-discovery", action="store_true",
                    help="compare the entities this version would publish with the ones the MQTT "
                         "broker holds for this host now; exit 2 if any would disappear")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args(argv)
    if not a.name or not re.fullmatch(r"[a-z][a-z0-9_]*", a.name):
        ap.error("--name is required: lowercase letters, digits and _ (e.g. media)")
    return a


def main(argv=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    env = f"{a.name.upper()}_HA_"
    config_path = a.config or os.environ.get(f"{env}CONFIG", f"/etc/{a.name}-ha/config.json")
    log_dir = a.log_dir or os.environ.get(f"{env}LOG_DIR", f"/var/log/{a.name}-ha")
    util.DOCKER = os.environ.get(f"{env}DOCKER", "docker")
    registry.INSECURE_REGISTRY = os.environ.get(f"{env}INSECURE_REGISTRY")  # tests only
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(levelname)s %(message)s")
    with open(config_path) as f:
        cfg = json.load(f)
    if "mqtt" not in cfg or "host" not in cfg["mqtt"]:
        raise SystemExit(f"{config_path}: mqtt.host is missing")

    if a.print_keys:
        from .compose import discover_projects
        projects, failed = discover_projects(cfg.get("compose_root", f"/opt/{a.name}"), a.name)
        print(json.dumps({"keys": sorted(projects), "groups": sorted(cfg.get("groups") or {}),
                          "unreadable_folders": sorted(failed)}))
        return 0

    if a.check_discovery:
        return check_discovery(cfg, a.name, config_path)

    if a.self_test:
        # Everything short of connecting: imports, config, plugin construction.
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix=f"{a.name}-ha-self-test-")
        try:
            b = Bridge(cfg, a.name, config_path, tmp)
            b.plugins = build_plugins(b, cfg)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"self-test OK: {VERSION}, plugins {', '.join(p.name for p in b.plugins)}")
        return 0

    b = Bridge(cfg, a.name, config_path, log_dir)
    b.plugins = build_plugins(b, cfg)
    log.info("%s %s starting (plugins: %s)", b.service, VERSION, ", ".join(p.name for p in b.plugins))
    signal.signal(signal.SIGTERM, b.shutdown)
    signal.signal(signal.SIGINT, b.shutdown)
    b.run()
    return b.exit_code


def planned_discovery(cfg, name, config_path):
    """{discovery topic: config} for every entity this version would publish on
    this host, found without connecting (each plugin's prepare(), then discovery
    into a capture instead of the broker)."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix=f"{name}-ha-check-")
    planned = {}

    def capture(topic, payload, retain=True, qos=0):
        if topic.startswith(f"{DISCOVERY_PREFIX}/") and topic.endswith("/config") and payload not in ("", None):
            planned[topic] = payload if isinstance(payload, dict) else json.loads(payload)

    try:
        b = Bridge(cfg, name, config_path, tmp)
        b.plugins = build_plugins(b, cfg)
        b.publish = capture
        b.client.publish = lambda topic, payload=None, qos=0, retain=False: capture(topic, payload)
        for p in b.plugins:
            p.prepare()
        b.publish_discovery()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return b, planned


def check_discovery(cfg, name, config_path, wait=3.0):
    import threading
    import time

    import paho.mqtt.client as mqtt
    b, planned = planned_discovery(cfg, name, config_path)
    nodes = sorted({t.split("/")[2] for t in planned})
    m = cfg["mqtt"]
    cid = f"{b.service}-check-{os.getpid()}"  # never the running service's client ID
    if hasattr(mqtt, "CallbackAPIVersion"):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)
    else:
        c = mqtt.Client(client_id=cid)
    if m.get("username"):
        c.username_pw_set(m["username"], m.get("password"))
    held, subscribed = {}, threading.Event()

    def on_connect(client, userdata, flags, rc, *args):
        if rc == 0:
            client.subscribe([(f"{DISCOVERY_PREFIX}/+/{n}/+/config", 0) for n in nodes])

    def on_message(client, userdata, msg):
        if msg.retain and msg.payload:
            try:
                held[msg.topic] = json.loads(msg.payload)
            except ValueError:
                held[msg.topic] = {}

    c.on_connect = on_connect
    c.on_subscribe = lambda *a: subscribed.set()
    c.on_message = on_message
    try:
        c.connect(m["host"], int(m.get("port", 1883)), keepalive=30)
    except Exception as e:  # noqa: BLE001
        print(f"Couldn't reach the MQTT broker to check entities: {e}")
        return 1
    c.loop_start()
    try:
        if not subscribed.wait(10):
            print("The MQTT broker didn't answer; entities couldn't be checked")
            return 1
        time.sleep(wait)  # retained messages arrive right after the subscription
    finally:
        c.loop_stop()
        c.disconnect()

    def ident(conf):
        return conf.get("unique_id") or "?"

    def device(conf):
        d = conf.get("device") or conf.get("dev") or {}
        ids = d.get("identifiers") or d.get("ids") or []
        return sorted([ids] if isinstance(ids, str) else ids)

    planned_ids = {ident(conf) for conf in planned.values()}
    lost = sorted((t, ident(conf)) for t, conf in held.items()
                  if t not in planned and ident(conf) not in planned_ids)
    changed = sorted((t, ident(conf), ident(planned[t])) for t, conf in held.items()
                     if t in planned and ident(conf) != ident(planned[t]))
    moved = sorted((t, device(conf), device(planned[t])) for t, conf in held.items()
                   if t in planned and device(conf) and device(conf) != device(planned[t]))
    new = sorted(t for t in planned if t not in held)
    kept = len(held) - len(lost) - len(changed)
    print(f"{kept} existing entities keep their IDs; {len(new)} new")
    for t, uid in lost:
        print(f"  would disappear: {uid} ({t})")
    for t, old, now in changed:
        print(f"  would change unique ID: {old} -> {now} ({t})")
    for t, old, now in moved:
        print(f"  would move to another device: {', '.join(old)} -> {', '.join(now)} ({t})")
    return 2 if lost or changed or moved else 0
