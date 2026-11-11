#!/usr/bin/env python3
"""homelab-ha-bridge entry point. systemd runs it as:

    /usr/bin/python3 -I /opt/homelab-ha-bridge/current/bridge.py --name <name>
"""
import os
import sys

# -I leaves the script's folder off sys.path; the package lives next to this file.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from habridge.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
