"""End-to-end scenarios for the self-updater: real mosquitto, a fake GitHub release API serving
release archives built by tools/build_release.py, and a small stand-in for systemd
(guard.py before every start, restart on exit)."""
import json, os, shutil, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAKES = os.path.join(HERE, "fakes")
sys.path.insert(0, os.path.join(REPO, "tools"))
import build_release  # noqa: E402
import paho.mqtt.client as mqtt  # noqa: E402


def mk_client(cid):
    """paho-mqtt 1.x and 2.x (hosts run whichever their OS packages)."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    return mqtt.Client(client_id=cid)

T = tempfile.mkdtemp(prefix="habridge-su-")
BPORT, APORT = 18831, 18284
NAME = "lab"
ROOT = f"{T}/opt/homelab-ha-bridge"
LOG = f"{T}/log"
FILES = f"{T}/files"
CUR = build_release.version()
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  ok  ", name)
    else:
        FAIL += 1; print("  FAIL", name, str(detail)[:500])


def jw(path, obj):
    json.dump(obj, open(path + ".tmp", "w")); os.replace(path + ".tmp", path)


def bump(v, n):
    a = [int(x) for x in v.split(".")]
    a[2] += n
    return ".".join(map(str, a))


def make_release(ver, patch=None):
    """Copy the repo, set VERSION (and optionally break it), build its archive into FILES."""
    src = f"{T}/src-{ver}"
    shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(".git", "dist", "__pycache__", "tests"))
    init = f"{src}/bridge/habridge/__init__.py"
    s = open(init).read().replace(f'VERSION = "{CUR}"', f'VERSION = "{ver}"')
    open(init, "w").write(s)
    if patch:
        patch(src)
    out = subprocess.run([sys.executable, "-I", f"{src}/tools/build_release.py", "--out", f"{T}/build-{ver}"],
                         check=True, capture_output=True, text=True).stdout.split()
    name = f"homelab-ha-bridge-{ver}.tar.gz"
    shutil.copy(f"{T}/build-{ver}/{name}", f"{FILES}/{name}")
    return name, out[1]


def publish_release(ver, name, digest, prerelease=False, sums_digest=None):
    jw(f"{FILES}/SHA256SUMS-{ver}", None)
    open(f"{FILES}/SHA256SUMS-{ver}", "w").write(f"{sums_digest or digest}  {name}\n")
    api["bridge_release"] = {
        "tag_name": f"v{ver}", "html_url": f"https://github.com/example/bridge/releases/tag/v{ver}",
        "draft": False, "prerelease": prerelease,
        "body": f"## What's new in {ver}\n* Faster checks\n* Fixed a thing",
        "assets": [{"name": name, "browser_download_url": f"http://127.0.0.1:{APORT}/files/{name}"},
                   {"name": "SHA256SUMS", "browser_download_url": f"http://127.0.0.1:{APORT}/files/SHA256SUMS-{ver}"}]}
    jw(f"{T}/api.json", api)


os.makedirs(FILES); os.makedirs(f"{T}/units"); os.makedirs(f"{T}/opt/{NAME}")
api = {"files_dir": FILES}
jw(f"{T}/api.json", api)

# "Installed" current version, the way install.sh lays it out
os.makedirs(f"{ROOT}/releases")
shutil.copytree(f"{REPO}/bridge", f"{ROOT}/releases/{CUR}", ignore=shutil.ignore_patterns("__pycache__"))
os.symlink(f"{ROOT}/releases/{CUR}", f"{ROOT}/current")
shutil.copy(f"{REPO}/bridge/guard.py", f"{ROOT}/guard.py")
unit = f"{T}/units/{NAME}-ha.service"
open(unit, "w").write("# an older unit\n")
jw(f"{T}/config.json", {
    "mqtt": {"host": "127.0.0.1", "port": BPORT}, "compose_root": f"{T}/opt/{NAME}", "device_name": "Lab Host",
    "self_update": {"api": f"http://127.0.0.1:{APORT}/gh-bridge", "repo": "example/bridge", "confirm_seconds": 3}})

ENV = dict(os.environ, PATH=f"{FAKES}/bin:" + os.environ["PATH"], no_proxy="localhost,127.0.0.1",
           NO_PROXY="localhost,127.0.0.1", HABRIDGE_ROOT=ROOT, HABRIDGE_NO_APT="1", HABRIDGE_UNIT_DIR=f"{T}/units",
           FAKE_DOCKER_STATE=f"{T}/docker.json", **{"LAB_HA_CONFIG": f"{T}/config.json", "LAB_HA_LOG_DIR": LOG})
jw(f"{T}/docker.json", {"images": {}, "containers": {}})

procs = [subprocess.Popen(["mosquitto", "-p", str(BPORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
         subprocess.Popen([sys.executable, "-I", f"{FAKES}/fake_api.py", str(APORT), f"{T}/api.json"])]
time.sleep(1)

msgs, lock = [], threading.Lock()
sub = mk_client("t-sub")
sub.on_message = lambda c, u, m: (lock.acquire(), msgs.append((m.topic, m.payload.decode(), m.retain)), lock.release())
sub.connect("127.0.0.1", BPORT); sub.subscribe(f"{NAME}/#"); sub.subscribe("homeassistant/#"); sub.loop_start()
ctl = mk_client("t-ctl"); ctl.connect("127.0.0.1", BPORT); ctl.loop_start()


class Supervisor:
    """What systemd does for the unit: guard.py, then the program; start it again when it exits."""

    def __init__(self):
        self.stopping = False
        self.starts = 0
        self.p = None
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        out = open(f"{T}/bridge.out", "a")
        while not self.stopping:
            subprocess.run([sys.executable, "-I", f"{ROOT}/guard.py", "--name", NAME, "--log-dir", LOG],
                           env=ENV, stdout=out, stderr=subprocess.STDOUT)
            self.starts += 1
            self.p = subprocess.Popen([sys.executable, "-I", f"{ROOT}/current/bridge.py", "--name", NAME],
                                      env=ENV, stdout=out, stderr=subprocess.STDOUT)
            self.p.wait()
            if not self.stopping:
                time.sleep(1)

    def stop(self):
        self.stopping = True
        if self.p and self.p.poll() is None:
            self.p.terminate()
        self.t.join(15)


def last(topic, js=False):
    with lock:
        for t, p, r in reversed(msgs):
            if t == topic:
                return json.loads(p) if js and p else p
    return None


def wait(pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


def st():
    return last(f"{NAME}/bridge-update/state", js=True) or {}


def attrs():
    return last(f"{NAME}/bridge-update/attributes", js=True) or {}


def press(topic, payload, retain=False):
    ctl.publish(topic, payload, qos=1, retain=retain)


INSTALL = f"{NAME}/bridge-update/install"
print("A. startup")
name, digest = make_release(CUR)
publish_release(CUR, name, digest)
sv = Supervisor()
check("online", wait(lambda: last(f"{NAME}/availability") == "online"), open(f"{T}/bridge.out").read()[-800:])
d = last(f"homeassistant/update/{NAME}_ha/bridge-update/config", js=True) or {}
check("bridge update entity", d.get("default_entity_id") == f"update.{NAME}_ha_bridge" and d.get("unique_id") == f"{NAME}_ha_bridge-update", d)
check("on the host's device", (d.get("device") or {}).get("name") == "Lab Host", d.get("device"))
check("current, nothing offered", wait(lambda: st().get("installed_version") == CUR and st().get("latest_version") == CUR), st())

print("B. new release offered")
V1 = bump(CUR, 1)
name1, digest1 = make_release(V1)
publish_release(V1, name1, digest1, sums_digest="0" * 64)  # wrong checksum first
press(f"{NAME}/check", "CHECK")
check(f"{V1} offered", wait(lambda: st().get("latest_version") == V1), st())
check("release link to that release", st().get("release_url", "").endswith(f"/tag/v{V1}"), st())
check("summary from the release notes", "Faster checks" in st().get("release_summary", ""), st())

print("C. bad checksum: refused, nothing changed")
press(INSTALL, "INSTALL")
check("refused", wait(lambda: "checksum mismatch" in attrs().get("last_install_result", "")), attrs())
check("still on the old release", os.path.realpath(f"{ROOT}/current") == os.path.realpath(f"{ROOT}/releases/{CUR}"))
check("no new release folder", not os.path.exists(f"{ROOT}/releases/{V1}"))
check("in_progress cleared", wait(lambda: st().get("in_progress") is False), st())

print("D. install, restart into it, confirm")
publish_release(V1, name1, digest1)
starts = sv.starts
press(INSTALL, "INSTALL")
check("restarted", wait(lambda: sv.starts > starts, 30), sv.starts)
check(f"running {V1}", wait(lambda: st().get("installed_version") == V1, 30), st())
check("current -> new release", os.path.realpath(f"{ROOT}/current") == os.path.realpath(f"{ROOT}/releases/{V1}"))
check("unit file updated", "ExecStart=/usr/bin/python3 -I " + ROOT + "/current/bridge.py --name lab" in open(unit).read(),
      open(unit).read())
check("confirmed after it stayed up", wait(lambda: attrs().get("last_install_result") == f"Updated {CUR} → {V1}", 20), attrs())
check("marker cleared", not os.path.exists(f"{LOG}/self-update.json"))

print("E. a release that won't start: guard rolls back")
V2 = bump(CUR, 2)


def break_it(src):
    p = f"{src}/bridge/habridge/main.py"
    s = open(p).read().replace("    b.run()\n    return b.exit_code",
                               "    raise SystemExit('broken on purpose for the rollback test')")
    open(p, "w").write(s)


name2, digest2 = make_release(V2, break_it)
publish_release(V2, name2, digest2)
press(f"{NAME}/check", "CHECK")
check(f"{V2} offered", wait(lambda: st().get("latest_version") == V2), st())
press(INSTALL, "INSTALL")
check("rolled back and reported", wait(lambda: "rolled back" in attrs().get("last_install_result", ""), 60), attrs())
check(f"back on {V1}", st().get("installed_version") == V1, st())
check("current -> previous release", os.path.realpath(f"{ROOT}/current") == os.path.realpath(f"{ROOT}/releases/{V1}"))
check("failed release still offered (you can retry once fixed)", wait(lambda: st().get("latest_version") == V2), st())

print("F. pre-releases are never offered")
V3 = bump(CUR, 3)
name3, digest3 = make_release(V3)
publish_release(V3 + "-rc.1", name3, digest3, prerelease=True)
api["bridge_release"]["tag_name"] = f"v{V3}-rc.1"; jw(f"{T}/api.json", api)
press(f"{NAME}/check", "CHECK")
check("check reports it", wait(lambda: "not a stable version" in attrs().get("last_check_result", "")), attrs())
check("nothing new offered", st().get("latest_version") in (V1, V2), st())

print("G. junk and retained commands")
starts = sv.starts
before = attrs().get("last_install")
press(INSTALL, "install")
time.sleep(1.5)
check("junk payload ignored", sv.starts == starts and attrs().get("last_install") == before, attrs())
# A retained command only arrives flagged as retained when it's replayed on (re)connect.
sv.p.terminate()
sv.p.wait()  # gone before the retained command is published; the supervisor restarts it 1 s later
with lock:
    msgs.clear()
press(INSTALL, "INSTALL", retain=True)
check("back online", wait(lambda: last(f"{NAME}/availability") == "online"))
check("retained command cleared from the broker", wait(lambda: last(INSTALL) == ""), last(INSTALL))
time.sleep(2)
check("retained INSTALL not acted on", attrs().get("last_install") == before, attrs())

print("H. old releases pruned")
check("at most 3 kept", len([d for d in os.listdir(f"{ROOT}/releases") if not d.startswith(".")]) <= 3,
      os.listdir(f"{ROOT}/releases"))

sv.stop()
for p in procs:
    p.terminate()
print(f"\n{PASS} passed, {FAIL} failed   (work dir {T})")
sys.exit(1 if FAIL else 0)
