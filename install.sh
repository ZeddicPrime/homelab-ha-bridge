#!/bin/bash
# homelab-ha-bridge installer. Run as root on the host, from an unpacked release:
#
#   bash homelab-ha-bridge-<version>/install.sh --name <name> [options]
#
# Installs this release under /opt/homelab-ha-bridge, keeps (or creates) the
# config in /etc/<name>-ha/config.json, and runs it as the <name>-ha systemd
# service. Re-running it on a host that already has a <name>-ha service keeps the
# config and state, and first checks that every existing entity keeps its ID.
#
# Options:
#   --name NAME            host name; drives topics, entity IDs and paths (required)
#   --device-name TEXT     Home Assistant device name (only added if the config has none)
#   --device-model TEXT    Home Assistant device model (only added if the config has none)
#   --compose-root DIR     Compose root for a new config (default /opt/NAME)
#   --force                install even if the key check finds entities that would change
set -euo pipefail

PROJECT=homelab-ha-bridge
ROOT=${HABRIDGE_ROOT:-/opt/$PROJECT}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VER=$(cat "$HERE/VERSION" 2>/dev/null || sed -n 's/^VERSION = "\(.*\)"/\1/p' "$HERE/bridge/habridge/__init__.py")
SRC="$HERE"; [ -f "$HERE/bridge.py" ] || SRC="$HERE/bridge"   # release archive or git checkout

NAME="" DEV_NAME="" DEV_MODEL="" COMPOSE_ROOT="" FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --device-name) DEV_NAME="$2"; shift 2 ;;
    --device-model) DEV_MODEL="$2"; shift 2 ;;
    --compose-root) COMPOSE_ROOT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
[[ "$NAME" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "--name is required: lowercase letters, digits and _ (e.g. media)"; exit 1; }
SVC="$NAME-ha"
CFG="/etc/$SVC/config.json"
LOGDIR="/var/log/$SVC"
UNIT="/etc/systemd/system/$SVC.service"
COMPOSE_ROOT=${COMPOSE_ROOT:-/opt/$NAME}
echo "$PROJECT $VER -> $SVC"

echo "==> Packages"
missing=()
while read -r pkg; do
  pkg=${pkg%%#*}; pkg=$(echo "$pkg" | xargs)
  [ -n "$pkg" ] || continue
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done < "$SRC/apt-packages.txt"
if [ ${#missing[@]} -gt 0 ]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
  echo "    installed ${missing[*]}"
fi
command -v docker >/dev/null || echo "    Note: docker not found; the compose plugin will find nothing on this host."

echo "==> Config"
install -d -m 0700 "/etc/$SVC"
if [ -f "$CFG" ]; then
  cp -p "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
  DEV_NAME="$DEV_NAME" DEV_MODEL="$DEV_MODEL" python3 -I - "$CFG" <<'PY'
import json, os, sys
p = sys.argv[1]
cfg = json.load(open(p))
added = []
for key, env in (("device_name", "DEV_NAME"), ("device_model", "DEV_MODEL")):
    if os.environ.get(env) and key not in cfg:
        cfg[key] = os.environ[env]
        added.append(key)
if added:
    old = os.umask(0o077)
    json.dump(cfg, open(p + ".tmp", "w"), indent=2)
    os.umask(old)
    os.replace(p + ".tmp", p)
print("    kept " + p + (" (added " + ", ".join(added) + ")" if added else ""))
PY
else
  echo "    No config yet. MQTT login for this service (create it in the Mosquitto broker app first)."
  read -r -p "    Broker address: " MQ_HOST
  [ -n "$MQ_HOST" ] || { echo "A broker address is required"; exit 1; }
  read -r -p "    Username [$SVC]: " MQ_USER
  read -r -s -p "    Password: " MQ_PASS; echo
  [ -n "$MQ_PASS" ] || { echo "A password is required"; exit 1; }
  MQ_HOST="$MQ_HOST" MQ_USER="${MQ_USER:-$SVC}" MQ_PASS="$MQ_PASS" ROOTDIR="$COMPOSE_ROOT" \
  DEV_NAME="$DEV_NAME" DEV_MODEL="$DEV_MODEL" python3 -I - "$CFG" <<'PY'
import json, os, sys
e = os.environ
cfg = {"mqtt": {"host": e["MQ_HOST"], "port": 1883, "username": e["MQ_USER"], "password": e["MQ_PASS"]},
       "compose_root": e["ROOTDIR"], "check_interval_hours": 6, "status_interval_seconds": 30,
       "timeout_minutes": 15, "settle_seconds": 10, "prune_after_update": True, "release_urls": {}}
if e.get("DEV_NAME"):
    cfg["device_name"] = e["DEV_NAME"]
if e.get("DEV_MODEL"):
    cfg["device_model"] = e["DEV_MODEL"]
old = os.umask(0o077)
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
os.umask(old)
print("    wrote " + sys.argv[1])
PY
fi
chmod 0600 "$CFG"

echo "==> Checking this release against the host"
/usr/bin/python3 -I "$SRC/bridge.py" --name "$NAME" --config "$CFG" --log-dir "$LOGDIR" --self-test
if [ -f "$UNIT" ] && ! grep -q "$ROOT/" "$UNIT" && [ ! -f "$LOGDIR/state.json" ]; then
  # Another program already runs as this service and keeps no state this release can
  # read, so its entities can't be checked: don't replace it.
  echo "    $SVC already exists and isn't this bridge; its entities can't be checked (no $LOGDIR/state.json)."
  [ "$FORCE" = 1 ] || { echo "    Nothing was changed. --force replaces it anyway."; exit 1; }
fi
if [ -f "$LOGDIR/state.json" ]; then
  KEYS=$(/usr/bin/python3 -I "$SRC/bridge.py" --name "$NAME" --config "$CFG" --print-keys)
  LOST=$(KEYS="$KEYS" python3 -I - "$LOGDIR/state.json" <<'PY'
import json, os, sys
known = set(json.load(open(sys.argv[1])).get("known", []))
k = json.loads(os.environ["KEYS"])
now = set(k["keys"]) | set(k["groups"])
print(" ".join(sorted(known - now)))
PY
)
  if [ -n "$LOST" ]; then
    echo "    These entities would disappear or change ID: $LOST"
    [ "$FORCE" = 1 ] || { echo "    Nothing was changed. Re-run with --force if that's expected."; exit 1; }
  else
    echo "    Every existing entity keeps its ID"
  fi
fi

echo "==> Installing $VER into $ROOT"
install -d -m 0755 "$ROOT/releases"
DEST="$ROOT/releases/$VER"
if [ "$(readlink -f "$SRC")" != "$(readlink -f "$DEST" 2>/dev/null || true)" ]; then
  rm -rf "$DEST.tmp"; mkdir -p "$DEST.tmp"
  cp -a "$SRC/." "$DEST.tmp/"
  [ -f "$HERE/VERSION" ] || echo "$VER" > "$DEST.tmp/VERSION"
  rm -rf "$DEST"; mv "$DEST.tmp" "$DEST"
fi
ln -sfn "$DEST" "$ROOT/.current.new" && mv -Tf "$ROOT/.current.new" "$ROOT/current"
install -m 0755 "$DEST/guard.py" "$ROOT/guard.py"

echo "==> Service $SVC"
if [ -f "$UNIT" ] && ! grep -q "$ROOT/" "$UNIT"; then
  cp -p "$UNIT" "$UNIT.pre-$PROJECT"
  echo "    replacing the existing $SVC service (its unit is saved as $UNIT.pre-$PROJECT)"
fi
sed -e "s|@NAME@|$NAME|g" -e "s|@ROOT@|$ROOT|g" "$DEST/systemd/bridge.service.in" > "$UNIT"
systemctl daemon-reload
systemctl enable "$SVC.service" >/dev/null
systemctl restart "$SVC.service"
sleep 8
systemctl --no-pager --lines=10 status "$SVC.service" || true
echo
if journalctl -u "$SVC" --since "-1min" --no-pager | grep -q "Connected to MQTT"; then
  echo "Done: $SVC $VER is connected. Updates now show up in Home Assistant as \"Bridge\" on this device."
else
  echo "Started, but not connected to MQTT yet. Check: journalctl -u $SVC -n 30"
fi
if [ -f "$UNIT.pre-$PROJECT" ]; then
  echo "To go back to the previous service: cp $UNIT.pre-$PROJECT $UNIT && systemctl daemon-reload && systemctl restart $SVC"
fi
