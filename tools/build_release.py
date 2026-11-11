#!/usr/bin/env python3
"""Build dist/homelab-ha-bridge-<version>.tar.gz and dist/SHA256SUMS.

The archive is reproducible (sorted entries, fixed owner and times) and holds
one folder, homelab-ha-bridge-<version>/, with what install.sh and the
self-updater expect. Usage: python3 tools/build_release.py [--out DIR]
"""
import argparse
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = "homelab-ha-bridge"


def version():
    src = open(os.path.join(ROOT, "bridge", "habridge", "__init__.py")).read()
    return re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1)


def files():
    """(archive path relative to the top folder, source path)."""
    out = []
    b = os.path.join(ROOT, "bridge")
    for dirpath, dirnames, names in os.walk(b):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for n in sorted(names):
            if n.endswith((".pyc", ".pyo")):
                continue
            src = os.path.join(dirpath, n)
            out.append((os.path.relpath(src, b), src))
    for extra in ("install.sh", "LICENSE", "README.md", "CHANGELOG.md"):
        out.append((extra, os.path.join(ROOT, extra)))
    return sorted(out)


def build(out_dir):
    ver = version()
    top = f"{PROJECT}-{ver}"
    os.makedirs(out_dir, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add_dir(path):
            ti = tarfile.TarInfo(path)
            ti.type, ti.mode, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = tarfile.DIRTYPE, 0o755, 0, 0, 0, "", ""
            tar.addfile(ti)
        seen = set()
        add_dir(top)
        for rel, src in files():
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = f"{top}/{'/'.join(parts[:i])}"
                if d not in seen:
                    seen.add(d)
                    add_dir(d)
            data = open(src, "rb").read()
            ti = tarfile.TarInfo(f"{top}/{rel}")
            ti.size, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = len(data), 0, 0, 0, "", ""
            ti.mode = 0o755 if (rel.endswith((".sh", "bridge.py", "guard.py"))) else 0o644
            tar.addfile(ti, io.BytesIO(data))
        vi = tarfile.TarInfo(f"{top}/VERSION")
        vb = (ver + "\n").encode()
        vi.size, vi.mtime, vi.mode = len(vb), 0, 0o644
        tar.addfile(vi, io.BytesIO(vb))
    name = f"{top}.tar.gz"
    path = os.path.join(out_dir, name)
    with open(path, "wb") as f, gzip.GzipFile(filename="", fileobj=f, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    with open(os.path.join(out_dir, "SHA256SUMS"), "w") as f:
        f.write(f"{digest}  {name}\n")
    print(f"{path}\n{digest}")
    return path, digest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    sys.exit(0 if build(ap.parse_args().out) else 1)
