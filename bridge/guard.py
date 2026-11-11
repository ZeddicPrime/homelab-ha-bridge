#!/usr/bin/env python3
"""Runs before every start of a bridge (systemd ExecStartPre). Standard library only.

If a self-update is waiting to be confirmed and the new release has already
failed to start three times in this boot, point `current` back at the previous
release and leave a note for it to report. Never fails the start itself.

Starts are counted per boot: a host that loses power (or is switched off) right
after an update starts the new release again with a fresh count, because a
power cut says nothing about whether the release works.

    guard.py --name <name> [--log-dir /var/log/<name>-ha] [--root /opt/homelab-ha-bridge]
"""
import argparse
import json
import os
import sys

MAX_ATTEMPTS = 3


def boot_id():
    v = os.environ.get("HABRIDGE_BOOT_ID")  # tests only
    if v:
        return v
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--log-dir")
    ap.add_argument("--root", default=os.environ.get("HABRIDGE_ROOT", "/opt/homelab-ha-bridge"))
    a = ap.parse_args()
    log_dir = a.log_dir or f"/var/log/{a.name}-ha"
    marker = os.path.join(log_dir, "self-update.json")
    try:
        with open(marker) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return 0
    if m.get("rolled_back"):
        return 0
    boot = boot_id()
    if m.get("boot") != boot:
        m["boot"] = boot
        m["attempts"] = 0
    m["attempts"] = int(m.get("attempts", 0)) + 1
    current = os.path.join(a.root, "current")
    if m["attempts"] > MAX_ATTEMPTS and os.path.isdir(m.get("from_path", "")):
        tmp = os.path.join(a.root, ".current.rollback")
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(m["from_path"], tmp)
        os.replace(tmp, current)
        fsync_dir(a.root)
        m["rolled_back"] = True
        m["attempts"] -= 1
        print(f"guard: {m.get('to_version')} failed to start {m['attempts']} times; "
              f"back on {m.get('from_version')}", file=sys.stderr)
    with open(marker + ".tmp", "w") as f:
        json.dump(m, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(marker + ".tmp", marker)
    fsync_dir(log_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001  (a broken guard must never block the service)
        print(f"guard: {e}", file=sys.stderr)
        sys.exit(0)
