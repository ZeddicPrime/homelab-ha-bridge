"""End-to-end scenarios for the compose + immich plugins:
real mosquitto, fake registry, docker shim, fake Immich API and release files."""
import json, os, re, shutil, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
H = os.path.join(HERE, "fakes")
PROG = os.path.join(HERE, "..", "bridge", "bridge.py")
sys.path.insert(0, H)
import paho.mqtt.client as mqtt  # noqa: E402


def mk_client(cid):
    """paho-mqtt 1.x and 2.x (hosts run whichever their OS packages)."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    return mqtt.Client(client_id=cid)

T = tempfile.mkdtemp(prefix="imha-e2e-")
BPORT, RPORT, APORT = 18830, 5055, 18283
REG = f"localhost:{RPORT}"
os.environ.update({
    "FAKE_REG_STATE": f"{T}/reg.json", "FAKE_DOCKER_STATE": f"{T}/docker.json", "FAKE_DOCKER_LOG": f"{T}/docker.log",
    "FAKE_REG_LOG": f"{T}/reg.log", "FAKE_LOG": f"{T}/server.log",
    "PATH": f"{H}/bin:" + os.environ["PATH"], "no_proxy": "localhost,127.0.0.1", "NO_PROXY": "localhost,127.0.0.1",
    "IMMICH_HA_CONFIG": f"{T}/config.json", "IMMICH_HA_LOG_DIR": f"{T}/log", "IMMICH_HA_INSECURE_REGISTRY": REG,
})
import fakelib  # noqa: E402  (needs FAKE_REG_STATE)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  ok  ", name)
    else:
        FAIL += 1; print("  FAIL", name, str(detail)[:400])


def jw(path, obj):
    json.dump(obj, open(path + ".tmp", "w")); os.replace(path + ".tmp", path)


SRC = {"org.opencontainers.image.source": "https://github.com/immich-app/immich"}
reg = {"test/immich-server": {"release": {"labels": {"org.opencontainers.image.version": "v3.3.5",
                                                     "org.opencontainers.image.title": "immich-server", **SRC}}},
       "test/immich-ml": {"release": {"labels": {"org.opencontainers.image.version": "v3.3.5",
                                                      "org.opencontainers.image.title": "immich-ml", **SRC}}},
       "test/valkey": {"9": {"labels": {"org.opencontainers.image.version": "9.0.1"}}},
       "test/postgres": {"14": {"labels": {"org.opencontainers.image.version": "14.19"}}}}
jw(f"{T}/reg.json", reg)
jw(f"{T}/docker.json", {"images": {}, "containers": {}, "fail_start": []})
D = f"{T}/opt/immich"
os.makedirs(f"{D}/postgres")  # data folder without a compose file: ignored
SVCS = {"immich-server": (f"{REG}/test/immich-server:release", "immich-server"),
        "immich-machine-learning": (f"{REG}/test/immich-ml:release", "immich-machine-learning"),
        "redis": (f"{REG}/test/valkey:9@" + fakelib.objects("test/valkey", "9")[0], "immich-redis"),
        "database": (f"{REG}/test/postgres:14", "immich-postgres")}
open(f"{D}/compose.yaml", "w").write("# placeholder\n")
jw(f"{D}/compose.fake.json", {"name": "immich", "services": {
    s: {"image": i, "container_name": c} for s, (i, c) in SVCS.items()}})
# Move the compose files into /opt/immich/immich (folder named like the host)
os.makedirs(f"{D}/immich")
shutil.move(f"{D}/compose.yaml", f"{D}/immich/compose.yaml")
shutil.move(f"{D}/compose.fake.json", f"{D}/immich/compose.fake.json")
PD = f"{D}/immich"


def release_compose(db="test/postgres:14", ml_env=None):
    env = "".join(f"      {k}: {v}\n" for k, v in (ml_env or {}).items())
    return ("services:\n"
            "  immich-server:\n    image: ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}\n"
            "  immich-machine-learning:\n    image: ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}\n"
            + (f"    environment:\n{env}" if env else "") +
            f"  redis:\n    image: {SVCS['redis'][0]}\n"
            f"  database:\n    image: {REG}/{db}\n")


api = {"users": [{"id": "u-alex", "name": "Alex", "email": "alex@example.com"}],
       "sessions": [{"id": "s1", "userId": "u-alex", "createdAt": "2026-11-01T10:00:00.000Z",
                     "updatedAt": "2026-11-03T10:00:00.000Z", "deviceOS": "Windows", "deviceType": "Chrome",
                     "appVersion": None, "current": False, "isPendingSyncReset": False, "expiresAt": None}],
       "stats": {"photos": 10, "videos": 2, "usage": 1000, "usagePhotos": 600, "usageVideos": 400, "usageByUser": []},
       "queues": [], "release_compose": {"v3.3.6": release_compose(), "v3.3.7": release_compose("test/postgres:16")}}
jw(f"{T}/api.json", api)

for s in SVCS:
    for cmd in (["compose", "pull", s], ["compose", "up", "-d", s]):
        subprocess.run(["docker", *cmd], cwd=PD, check=True, capture_output=True)

os.makedirs(f"{T}/log")
jw(f"{T}/config.json", {
    "mqtt": {"host": "127.0.0.1", "port": BPORT}, "compose_root": D, "device_name": "Immich",
    "device_model": "Immich photo server (test)", "check_interval_hours": 6, "status_interval_seconds": 2,
    "timeout_minutes": 2, "settle_seconds": 1, "health_timeout_seconds": 4, "prune_after_update": True,
    "groups": {"app": {"title": "Immich", "members": ["server", "machine_learning"], "health_of": "server",
                       "icon": "mdi:image-multiple"}},
    "db_dump": {"container": "immich-postgres", "user": "postgres", "dir": f"{T}/dumps", "keep": 2},
    "release_urls": {"app": "https://github.com/immich-app/immich/releases"},
    "release_compose_url": f"http://127.0.0.1:{APORT}/release/{{version}}/docker-compose.yml",
    "cloudflared_releases_api": f"http://127.0.0.1:{APORT}/gh/latest",
    "self_update": {"api": f"http://127.0.0.1:{APORT}/gh-bridge", "repo": "example/bridge"},
    "immich": {"url": f"http://127.0.0.1:{APORT}", "api_key": "test-key", "server_container": "immich-server",
               "poll_seconds": 1, "stats_interval_seconds": 0},
})
open(os.environ["FAKE_LOG"], "w").close()

procs = [subprocess.Popen(["mosquitto", "-p", str(BPORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
         subprocess.Popen([sys.executable, "-I", f"{H}/fakereg.py", str(RPORT)]),
         subprocess.Popen([sys.executable, "-I", f"{H}/fake_api.py", str(APORT), f"{T}/api.json"])]
time.sleep(1)

msgs, lock = [], threading.Lock()
sub = mk_client("t-sub")
sub.on_message = lambda c, u, m: (lock.acquire(), msgs.append((m.topic, m.payload.decode(), m.retain)), lock.release())
sub.connect("127.0.0.1", BPORT); sub.subscribe("immich/#"); sub.subscribe("homeassistant/#"); sub.loop_start()
ctl = mk_client("t-ctl"); ctl.connect("127.0.0.1", BPORT); ctl.loop_start()

bridge = None


def start_bridge():
    global bridge
    bridge = subprocess.Popen([sys.executable, "-I", PROG, "--name", "immich"], stdout=open(f"{T}/bridge.out", "a"), stderr=subprocess.STDOUT)


def last(topic, js=False):
    with lock:
        for t, p, r in reversed(msgs):
            if t == topic:
                return json.loads(p) if js and p else p
    return None


def wait(pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


def press(topic, payload):
    ctl.publish(topic, payload, qos=1)


def app():
    return last("immich/app/state", js=True) or {}


def app_attr():
    return last("immich/app/attributes", js=True) or {}


def disc(component, oid):
    return last(f"homeassistant/{component}/immich_ha/{oid}/config", js=True)


print("A. startup")
start_bridge()
check("connected", wait(lambda: last("immich/availability") == "online"), open(f"{T}/bridge.out").read()[-800:])
check("app update entity", wait(lambda: disc("update", "app") is not None)
      and disc("update", "app")["default_entity_id"] == "update.immich_app")
check("no separate server/ML update entities",
      disc("update", "server") is None and disc("update", "machine_learning") is None)
check("redis + database update entities", disc("update", "redis") is not None and disc("update", "database") is not None)
check("running sensors use bare keys", all(disc("binary_sensor", f"{k}_running") for k in
                                            ("server", "machine_learning", "redis", "database")))
check("label is service name", (disc("binary_sensor", "server_running") or {}).get("name") == "immich-server running",
      disc("binary_sensor", "server_running"))
check("app installed v3.3.5, current", wait(lambda: app().get("installed_version") == "v3.3.5"
                                            and app().get("latest_version") == "v3.3.5"), app())
check("release url", app().get("release_url", "").startswith("https://github.com/immich-app/immich"), app())
check("sign-in entities discovered", disc("event", "login_event") and disc("sensor", "failed_logins"))
check("baseline session, no login event", wait(lambda: last("immich/sessions/state") == "1")
      and last("immich/login/event") is None)

print("A2. pinned redis image ignores newer pushes to its tag")
check("redis titled by service", (last("immich/redis/attributes", js=True) or {}).get("title") == "redis")
reg["test/valkey"]["9"]["labels"]["org.opencontainers.image.version"] = "9.0.2"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
time.sleep(3)
r = last("immich/redis/state", js=True) or {}
check("no redis update for a newer :9 push", r.get("installed_version") == r.get("latest_version") == "9.0.1", r)
check("app entity named after the device", (disc("update", "app") or {}).get("name", "x") is None, disc("update", "app"))

print("B. new release")
for repo, tag in (("test/immich-server", "release"), ("test/immich-ml", "release")):
    reg[repo][tag]["labels"]["org.opencontainers.image.version"] = "v3.3.6"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
check("v3.3.5 → v3.3.6 offered", wait(lambda: app().get("latest_version") == "v3.3.6"), app())
check("summary mentions backup", "backs up the database" in app().get("release_summary", ""), app())
check("compose check OK", wait(lambda: app_attr().get("compose_check") == "OK"), app_attr())
check("both pending", sorted(app_attr().get("pending", [])) == ["immich-machine-learning", "immich-server"], app_attr())

print("C. install")
press("immich/app/install", "INSTALL")
check("in_progress while installing", wait(lambda: app().get("in_progress") is True, 5), app())
press("immich/server/restart", "RESTART")  # must be refused during the install
press("immich/app/install", "INSTALL")    # double press
check("installed v3.3.6", wait(lambda: app().get("installed_version") == "v3.3.6" and not app().get("in_progress"), 30),
      app())
check("result text", wait(lambda: app_attr().get("last_install_result") == "Updated v3.3.5 → v3.3.6", 5),
      app_attr().get("last_install_result"))  # state and attributes are separate messages
a = app_attr()
check("dump written", a.get("dump_file", "").endswith(".sql.gz") and os.path.getsize(a["dump_file"]) > 100, a)
check("previous tags for both", set(a.get("previous_tags", {})) == {"server", "machine_learning"}, a.get("previous_tags"))
dl = open(os.environ["FAKE_DOCKER_LOG"]).read()
check("exactly one install ran", dl.count("compose pull immich-server") == 2, dl.count("compose pull immich-server"))
check("restart refused during install", "restart immich-server" not in dl)
check("pull both before any up", dl.rfind("compose pull immich-machine-learning") < dl.rfind("compose up -d immich-server"))

print("D. ML-only rebuild")
reg["test/immich-ml"]["release"]["labels"]["fake.build"] = "2"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
check("shown as rebuilt", wait(lambda: app().get("latest_version") == "v3.3.6 (rebuilt)"), app())
press("immich/app/install", "INSTALL")
check("rebuild installed", wait(lambda: app().get("latest_version") == "v3.3.6" and not app().get("in_progress"), 30), app())
check("rebuild result text", wait(lambda: app_attr().get("last_install_result") == "Installed rebuilt v3.3.6 images", 5),
      app_attr().get("last_install_result"))  # state and attributes are separate messages

print("E. release that changes docker-compose.yml")
for repo, tag in (("test/immich-server", "release"), ("test/immich-ml", "release")):
    reg[repo][tag]["labels"]["org.opencontainers.image.version"] = "v3.3.7"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
check("drift detected", wait(lambda: any("database" in d for d in app_attr().get("compose_changed", []))), app_attr())
check("summary warns", "changes docker-compose.yml" in app().get("release_summary", ""), app())
n_pull = open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server")
press("immich/app/install", "INSTALL")
check("install refused", wait(lambda: app_attr().get("last_install_result", "").startswith("Failed: Not installed"), 10),
      app_attr().get("last_install_result"))
check("nothing pulled", open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server") == n_pull)

print("F. new version won't start → rollback")
api["release_compose"]["v3.3.7"] = release_compose()  # compose fixed upstream
jw(f"{T}/api.json", api)
idx, _, _ = fakelib.objects("test/immich-server", "release")
st = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
st["fail_images"] = ["sha256:img" + idx[7:19]]
jw(os.environ["FAKE_DOCKER_STATE"], st)
press("immich/check", "CHECK")
check("v3.3.7 offered, compose OK", wait(lambda: app().get("latest_version") == "v3.3.7"
                                         and app_attr().get("compose_check") == "OK"), app_attr())
press("immich/app/install", "INSTALL")
check("rolled back", wait(lambda: "rolled back to the previous images" in app_attr().get("last_install_result", ""), 40),
      app_attr().get("last_install_result"))
check("restore hint names the dump", "restore " in app_attr().get("last_install_result", ""))
check("still v3.3.6, update still offered", wait(lambda: app().get("installed_version") == "v3.3.6"
                                                 and app().get("latest_version") == "v3.3.7"), app())
check("server running again", wait(lambda: last("immich/server/running") == "ON", 10))
st = json.load(open(os.environ["FAKE_DOCKER_STATE"])); st["fail_images"] = []; jw(os.environ["FAKE_DOCKER_STATE"], st)

print("G. database dump fails → nothing changes")
dumps_before = sorted(os.listdir(f"{T}/dumps"))
check("old dumps pruned to keep=2", len(dumps_before) == 2, dumps_before)
bridge.terminate(); bridge.wait()
os.environ["FAKE_DUMP_FAIL"] = "1"
with lock:
    msgs.clear()
start_bridge()
check("reconnected", wait(lambda: last("immich/availability") == "online"))
n_pull = open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server")
check("update still offered after restart", wait(lambda: app().get("latest_version") == "v3.3.7"), app())
# Retained states arrive before the compose scan; commands only count once the scan is done.
check("scan finished", wait(lambda: last("immich/server/running") == "ON"))
press("immich/app/install", "INSTALL")
check("dump failure reported", wait(lambda: "Database dump failed" in app_attr().get("last_install_result", ""), 15),
      app_attr().get("last_install_result"))
check("no pull after dump failure", open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server") == n_pull)
os.environ.pop("FAKE_DUMP_FAIL")

print("H. junk and member installs")
before = open(os.environ["FAKE_DOCKER_LOG"]).read()
press("immich/server/install", "INSTALL")
press("immich/app/install", "install")
press("immich/../app/install", "INSTALL")
press("immich/app/restart", "RESTART")
press("immich/nosuch/install", "INSTALL")
time.sleep(1.5)
after = open(os.environ["FAKE_DOCKER_LOG"]).read()
new_cmds = [l for l in after[len(before):].splitlines() if not l.startswith(("inspect", "image inspect", "logs", "compose config"))]
check("nothing ran", not new_cmds, new_cmds)

print("I. sign-ins through the bridge")
api["sessions"].append({"id": "s2", "userId": "u-alex", "createdAt": datetime.now(timezone.utc).isoformat(),
                        "updatedAt": datetime.now(timezone.utc).isoformat(), "deviceOS": "iOS",
                        "deviceType": "Immich app", "appVersion": "2.6.0", "current": False,
                        "isPendingSyncReset": False, "expiresAt": None})
jw(f"{T}/api.json", api)
check("login event", wait(lambda: (last("immich/login/event", js=True) or {}).get("event_type") == "login"),
      last("immich/login/event"))
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(f"{ts} [Nest] 1 - WARN [Api:AuthService~1] Failed login attempt for user alex@example.com from ip address 198.51.100.9\n")
check("login_failed event", wait(lambda: (last("immich/login/event", js=True) or {}).get("event_type") == "login_failed"),
      last("immich/login/event"))
check("failed count 1", wait(lambda: last("immich/login/failed/state") == "1"))

print("J. restart: nothing deleted, state kept, retained command not replayed")
bridge.terminate(); bridge.wait()
check("offline on clean stop", wait(lambda: last("immich/availability") == "offline", 5))
ctl.publish("immich/app/install", "INSTALL", qos=1, retain=True); time.sleep(0.5)
n_exec = open(os.environ["FAKE_DOCKER_LOG"]).read().count("pg_dumpall")
with lock:
    msgs.clear()
start_bridge()
check("back online", wait(lambda: last("immich/availability") == "online"))
time.sleep(3)
with lock:
    empties = [t for t, p, r in msgs if t.startswith("homeassistant/") and p == ""]
check("no discovery configs cleared", not empties, empties)
check("failed count persisted", wait(lambda: last("immich/login/failed/state") == "1"))
check("no events replayed", last("immich/login/event") is None, last("immich/login/event"))
check("retained INSTALL ignored on reconnect", open(os.environ["FAKE_DOCKER_LOG"]).read().count("pg_dumpall") == n_exec)
ctl.publish("immich/app/install", "", qos=1, retain=True)

print("L. cloudflared connector: real versions, connected sensor, update, kill switch")
CF = f"{D}/cloudflared"
os.makedirs(CF)
reg["cloudflare/cloudflared"] = {"latest": {"labels": {"org.opencontainers.image.source": "https://github.com/cloudflare/cloudflared",
                                                       "fake.cloudflared.version": "2026.10.0"}}}
jw(f"{T}/reg.json", reg)
open(f"{CF}/compose.yaml", "w").write("# placeholder\n")
jw(f"{CF}/compose.fake.json", {"name": "cloudflared", "services": {"cloudflared": {
    "image": f"{REG}/cloudflare/cloudflared:latest", "container_name": "cloudflared",
    "command": ["tunnel", "--metrics", f"127.0.0.1:{APORT}", "run"]}}})
for cmd in (["compose", "pull", "cloudflared"], ["compose", "up", "-d", "cloudflared"]):
    subprocess.run(["docker", *cmd], cwd=CF, check=True, capture_output=True)
api["ready"] = {"status": 200, "readyConnections": 4, "connectorId": "c0ffee"}
api["gh"] = "2026.10.0"
jw(f"{T}/api.json", api)
press("immich/check", "CHECK")


def cf():
    return last("immich/cloudflared/state", js=True) or {}


check("new folder found on CHECK", wait(lambda: disc("update", "cloudflared") is not None), disc("update", "cloudflared"))
check("entity id update.immich_cloudflared", (disc("update", "cloudflared") or {}).get("default_entity_id") == "update.immich_cloudflared")
cd = disc("binary_sensor", "cloudflared_connected") or {}
check("connected sensor discovered", cd.get("default_entity_id") == "binary_sensor.immich_cloudflared_connected"
      and cd.get("device_class") == "connectivity", cd)
check("immich folder has no connected sensor", disc("binary_sensor", "server_connected") is None)
check("installed = latest = 2026.10.0 (not a digest)",
      wait(lambda: cf().get("installed_version") == "2026.10.0" and cf().get("latest_version") == "2026.10.0"), cf())
check("release url is that release", cf().get("release_url", "").endswith("/releases/tag/2026.10.0"), cf())
check("connected ON with 4 connections", wait(lambda: last("immich/cloudflared/connected") == "ON"
      and (last("immich/cloudflared/connected/attributes", js=True) or {}).get("connections") == 4),
      last("immich/cloudflared/connected/attributes"))
api["ready"] = {"status": 503, "readyConnections": 0, "connectorId": "c0ffee"}; jw(f"{T}/api.json", api)
check("503 /ready -> OFF", wait(lambda: last("immich/cloudflared/connected") == "OFF", 8))
api["ready"] = {"status": 200, "readyConnections": 4, "connectorId": "c0ffee"}; jw(f"{T}/api.json", api)
check("back ON", wait(lambda: last("immich/cloudflared/connected") == "ON", 8))

reg["cloudflare/cloudflared"]["latest"]["labels"]["fake.cloudflared.version"] = "2026.11.0"
jw(f"{T}/reg.json", reg)
api["gh"] = "2026.11.0"; jw(f"{T}/api.json", api)
press("immich/check", "CHECK")
check("2026.10.0 -> 2026.11.0 offered", wait(lambda: cf().get("latest_version") == "2026.11.0"
                                              and cf().get("installed_version") == "2026.10.0"), cf())
check("summary names both versions", "2026.10.0" in cf().get("release_summary", "") and "2026.11.0" in cf().get("release_summary", ""), cf())
press("immich/cloudflared/install", "INSTALL")
check("installed 2026.11.0", wait(lambda: cf().get("installed_version") == "2026.11.0"
                                  and cf().get("latest_version") == "2026.11.0" and not cf().get("in_progress"), 30), cf())
check("result names versions", (last("immich/cloudflared/attributes", js=True) or {}).get("last_install_result")
      == "Updated 2026.10.0 → 2026.11.0", last("immich/cloudflared/attributes"))
check("previous tag kept", (last("immich/cloudflared/attributes", js=True) or {}).get("previous_tag", "").endswith("cloudflare/cloudflared:previous"))

reg["cloudflare/cloudflared"]["latest"]["labels"]["fake.build"] = "2"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
check("same release, new build -> rebuilt", wait(lambda: cf().get("latest_version") == "2026.11.0 (rebuilt)"), cf())

press("immich/cloudflared/power/set", "OFF")
check("kill switch stops it", wait(lambda: last("immich/cloudflared/running") == "OFF", 10))
check("connected OFF while stopped", wait(lambda: last("immich/cloudflared/connected") == "OFF"
      and "not running" in (last("immich/cloudflared/connected/attributes", js=True) or {}).get("error", ""), 8),
      last("immich/cloudflared/connected/attributes"))
press("immich/check", "CHECK")
time.sleep(3)
check("version kept while stopped", cf().get("installed_version") == "2026.11.0", cf())
press("immich/cloudflared/power/set", "ON")
check("started again, connected", wait(lambda: last("immich/cloudflared/connected") == "ON", 10))

print("M. stable releases only")
n_pull = open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server")
before_v = app().get("installed_version")
for repo, tag in (("test/immich-server", "release"), ("test/immich-ml", "release")):
    reg[repo][tag]["labels"]["org.opencontainers.image.version"] = "v3.4.0-rc.1"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
check("RC on the followed tag is held back", wait(lambda: app_attr().get("ignored_prerelease") == "v3.4.0-rc.1"), app_attr())
check("no update offered", app().get("latest_version") == app().get("installed_version") == before_v, app())
check("release link is the installed version's page",
      app().get("release_url", "").endswith(f"/releases/tag/{before_v}"), app().get("release_url"))
press("immich/app/install", "INSTALL")
check("install of an RC refused", wait(lambda: "pre-release" in app_attr().get("last_install_result", ""), 10),
      app_attr().get("last_install_result"))
check("nothing pulled for the RC", open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server") == n_pull)
for repo, tag in (("test/immich-server", "release"), ("test/immich-ml", "release")):
    reg[repo][tag]["labels"]["org.opencontainers.image.version"] = "v3.4.0"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
check("stable v3.4.0 offered again", wait(lambda: app().get("latest_version") == "v3.4.0"
                                          and not app_attr().get("ignored_prerelease")), app_attr())
check("link goes to that release", app().get("release_url", "").endswith("/releases/tag/v3.4.0"), app().get("release_url"))

print("N. release changes a setting, not an image (every setting is compared, not just images)")
api["release_compose"]["v3.4.0"] = release_compose()
api["release_compose"]["v3.4.1"] = release_compose(ml_env={"MACHINE_LEARNING_NEW_SETTING": "1"})
jw(f"{T}/api.json", api)
press("immich/check", "CHECK")
check("v3.4.0 compose OK", wait(lambda: app_attr().get("compose_check") == "OK"), app_attr())
press("immich/app/install", "INSTALL")
check("v3.4.0 installed", wait(lambda: app().get("installed_version") == "v3.4.0" and not app().get("in_progress"), 40), app())
for repo, tag in (("test/immich-server", "release"), ("test/immich-ml", "release")):
    reg[repo][tag]["labels"]["org.opencontainers.image.version"] = "v3.4.1"
jw(f"{T}/reg.json", reg)
press("immich/check", "CHECK")
check("new ML variable caught", wait(lambda: any("MACHINE_LEARNING_NEW_SETTING" in c for c in app_attr().get("compose_changed", []))),
      app_attr())
n_pull = open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server")
press("immich/app/install", "INSTALL")
check("install refused for the setting", wait(lambda: "MACHINE_LEARNING_NEW_SETTING" in app_attr().get("last_install_result", ""), 10),
      app_attr().get("last_install_result"))
check("nothing pulled", open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull immich-server") == n_pull)
cf_json = json.load(open(f"{PD}/compose.fake.json"))
cf_json["services"]["immich-machine-learning"]["environment"] = {"MACHINE_LEARNING_NEW_SETTING": "1"}
jw(f"{PD}/compose.fake.json", cf_json)
press("immich/check", "CHECK")
check("compose updated locally -> OK", wait(lambda: app_attr().get("compose_check") == "OK" and not app_attr().get("compose_changed")),
      app_attr())

print("O. an OS package install comes first: container updates wait for it")
OB = f"{T}/osbin"
os.makedirs(OB)
HOLD = f"{T}/os-hold"
open(HOLD, "w").close()
for cmd, body in {
    "apt-get": f"""
import os, sys, time
a = sys.argv[1:]
if "-s" in a: print("Inst libfoo [1.0] (1.1 Debian:12/stable [amd64])"); sys.exit(0)
if "upgrade" in a:
    while os.path.exists({HOLD!r}): time.sleep(0.2)
sys.exit(0)
""",
    "dpkg": "import sys\nsys.exit(0)\n",
}.items():
    with open(f"{OB}/{cmd}", "w") as f:
        f.write(f"#!{sys.executable}\n" + body)
    os.chmod(f"{OB}/{cmd}", 0o755)
bridge.terminate(); bridge.wait()
cfg = json.load(open(f"{T}/config.json"))
cfg["os"] = {"path": OB, "first_check_seconds": 0.5, "container": True}
jw(f"{T}/config.json", cfg)
start_bridge()
check("os: update offered", wait(lambda: (last("immich/host-os/packages/state", js=True) or {}).get("latest_version") == "1 update"),
      last("immich/host-os/packages/state"))
press("immich/host-os/packages/install", "install")
check("os: installing", wait(lambda: (last("immich/host-os/packages/state", js=True) or {}).get("in_progress") is True))
n_dump = open(os.environ["FAKE_DOCKER_LOG"]).read().count("pg_dumpall")
press("immich/app/install", "INSTALL")
check("container update not started, and says why",
      wait(lambda: (app_attr().get("last_install_result") or "").startswith("Not started: OS package updates is running")),
      app_attr().get("last_install_result"))
check("nothing ran for it", open(os.environ["FAKE_DOCKER_LOG"]).read().count("pg_dumpall") == n_dump)
os.remove(HOLD)
check("os install finishes", wait(lambda: (last("immich/host-os/packages/state", js=True) or {}).get("in_progress") is False, 20))

print("P. auto-updated service: versions shown, no Install")
bridge.terminate(); bridge.wait()
cfg = json.load(open(f"{T}/config.json"))
cfg["auto_updated"] = {"cloudflared": "its vendor's Watchtower"}
jw(f"{T}/config.json", cfg)
reg["cloudflare/cloudflared"]["latest"]["labels"]["fake.cloudflared.version"] = "2026.12.0"
reg["cloudflare/cloudflared"]["latest"]["labels"]["fake.build"] = "3"
jw(f"{T}/reg.json", reg)
api["gh"] = "2026.12.0"; jw(f"{T}/api.json", api)
start_bridge()
check("auto-updated: update entity has no Install",
      wait(lambda: "command_topic" not in (disc("update", "cloudflared") or {"command_topic": 1})
           and "payload_install" not in disc("update", "cloudflared")), disc("update", "cloudflared"))
check("auto-updated: other services keep Install", "command_topic" in (disc("update", "redis") or {}))
press("immich/check", "CHECK")
check("auto-updated: newer version still shown", wait(lambda: cf().get("latest_version") == "2026.12.0"), cf())
check("auto-updated: summary says who installs it",
      cf().get("release_summary", "").startswith("Installed automatically by its vendor's Watchtower; nothing to do here."), cf())
ca = last("immich/cloudflared/attributes", js=True) or {}
check("auto-updated: attributes", ca.get("auto_updated") is True and ca.get("updated_by") == "its vendor's Watchtower", ca)
n_pull = open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull cloudflared")
press("immich/cloudflared/install", "INSTALL")
time.sleep(3)
check("auto-updated: an install command does nothing",
      open(os.environ["FAKE_DOCKER_LOG"]).read().count("compose pull cloudflared") == n_pull and not cf().get("in_progress"), cf())

print("K. HA schema")
with lock:
    allm = list(msgs)
bad = []
# whole run: re-read everything published under immich/*/state for update entities
for t, p, r in allm:
    if re.fullmatch(r"immich/[a-z_]+/state", t) and p.startswith("{"):
        j = json.loads(p)
        for k, v in j.items():
            if v is None:
                bad.append(f"{t}:{k} null")
            elif k in ("installed_version", "latest_version", "title", "release_summary") and not isinstance(v, str):
                bad.append(f"{t}:{k} not str")
            elif k == "release_url" and not re.match(r"^https?://", v):
                bad.append(f"{t}: bad url")
    # Attribute topics have no schema in HA; everything else this service sends must be null-free.
    # Discovery configs may use "name": null on purpose (HA then shows the device name).
    if p.startswith("{") and "null" in p and not t.endswith("attributes") and not t.startswith("homeassistant/") or (
            t.startswith(("immich/login", "immich/sessions", "immich/stats", "immich/jobs", "immich/api"))
            and "null" in p):
        bad.append(f"{t}: contains null")
check("zero schema violations", not bad, bad[:5])

bridge.terminate(); bridge.wait()
for p in procs:
    p.terminate()
print(f"\n{PASS} passed, {FAIL} failed   (work dir {T})")
sys.exit(1 if FAIL else 0)
