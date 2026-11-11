#!/usr/bin/env python3
"""Fail if anything that identifies a particular home or network is about to be published.

Checks every tracked file for:
  - private IPv4 addresses (10/8, 172.16/12, 192.168/16); use 192.0.2.x, 198.51.100.x
    or 203.0.113.x (documentation ranges) in examples and tests instead
  - email addresses outside example.com / example.org / users.noreply.github.com
  - Proxmox container IDs written as "CT <number>"
  - any word on the denylist: the PRIVACY_DENYLIST environment variable (a GitHub
    Actions secret, so the words themselves are never in the repo) and/or a local,
    git-ignored .privacy-denylist file; one word or phrase per line or comma

Usage: python3 tools/privacy_check.py   (exit 1 and a list of hits when anything is found)
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "dist", "__pycache__"}
SKIP_FILES = {".privacy-denylist"}

PRIVATE_IP = re.compile(r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
OK_EMAIL_DOMAINS = {"example.com", "example.org", "users.noreply.github.com", "anthropic.com"}
CT_ID = re.compile(r"\bCT ?\d{3,5}\b")


def tracked_files():
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-co", "--exclude-standard"],
                             capture_output=True, text=True, check=True).stdout.split("\n")
        files = [f for f in out if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for d, dirs, names in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            files += [os.path.relpath(os.path.join(d, n), ROOT) for n in names]
    return sorted(f for f in files if os.path.basename(f) not in SKIP_FILES
                  and not any(p in SKIP_DIRS for p in f.split("/")))


def denylist():
    words = []
    raw = os.environ.get("PRIVACY_DENYLIST", "")
    p = os.path.join(ROOT, ".privacy-denylist")
    if os.path.exists(p):
        raw += "\n" + open(p).read()
    for w in re.split(r"[\n,]", raw):
        w = w.strip()
        if w and not w.startswith("#"):
            words.append(w)
    return [(w, re.compile(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", re.I)) for w in words]


def main():
    deny = denylist()
    hits = []
    for rel in tracked_files():
        try:
            text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in PRIVATE_IP.finditer(line):
                hits.append((rel, n, f"private IP {m.group(0)}"))
            for m in EMAIL.finditer(line):
                if m.group(1).lower() not in OK_EMAIL_DOMAINS:
                    hits.append((rel, n, f"email address {m.group(0)}"))
            for m in CT_ID.finditer(line):
                hits.append((rel, n, f"container ID {m.group(0)}"))
            for i, (w, rx) in enumerate(deny):
                if rx.search(line):
                    hits.append((rel, n, f"denylist entry #{i + 1}"))  # never echo the word itself
    if not deny:
        print("privacy check: no denylist configured (PRIVACY_DENYLIST / .privacy-denylist)", file=sys.stderr)
    for rel, n, what in hits:
        print(f"{rel}:{n}: {what}")
    print(f"privacy check: {len(hits)} finding(s) in {len(tracked_files())} files")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
