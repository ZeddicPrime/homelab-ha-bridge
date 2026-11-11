"""Command line: run the bridge for one host, or check a config / a new release."""

import argparse
import json
import logging
import os
import re
import signal
import sys

from . import VERSION, registry, util
from .core import Bridge

log = logging.getLogger("habridge")


def build_plugins(bridge, cfg):
    """Plugins come from config "plugins"; without it: compose, immich (when an
    Immich API key is configured) and selfupdate — what immich-ha 1.0.x did."""
    from .compose import ComposePlugin
    from .immich import ImmichPlugin
    from .selfupdate import SelfUpdatePlugin
    available = {"compose": ComposePlugin, "immich": ImmichPlugin, "selfupdate": SelfUpdatePlugin}
    names = cfg.get("plugins")
    if names is None:
        names = ["compose"]
        if (cfg.get("immich") or {}).get("api_key"):
            names.append("immich")
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
