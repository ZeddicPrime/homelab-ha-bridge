"""Scenario tests for the Immich plugin's monitor against a real mosquitto, a fake Immich API and a docker shim."""
import json, os, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge"))
import paho.mqtt.client as mqtt  # noqa: E402


def mk_client(cid):
    """paho-mqtt 1.x and 2.x (hosts run whichever their OS packages)."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    return mqtt.Client(client_id=cid)
from habridge import immich as im  # noqa: E402

BROKER, BPORT, APORT = "127.0.0.1", 18830, 18283
TMP = tempfile.mkdtemp(prefix="imha-")
API_STATE = os.path.join(TMP, "api.json")
LOG = os.path.join(TMP, "server.log")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  ok  ", name)
    else:
        FAIL += 1; print("  FAIL", name, detail)


def now():
    return datetime.now(timezone.utc)


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def write_api(st):
    json.dump(st, open(API_STATE + ".tmp", "w")); os.replace(API_STATE + ".tmp", API_STATE)


ALEX = {"id": "u-alex", "name": "Alex", "email": "alex@example.com"}
SAM = {"id": "u-sam", "name": "Sam", "email": "sam@example.com"}


def sess(sid, uid, created, updated=None, os_="iOS", typ="Immich app", app="2.6.0", expires=None):
    return {"id": sid, "userId": uid, "createdAt": created.isoformat(), "updatedAt": (updated or created).isoformat(),
            "deviceOS": os_, "deviceType": typ, "appVersion": app, "current": False, "isPendingSyncReset": False,
            "expiresAt": expires.isoformat() if expires else None}


api = {
    "users": [ALEX, SAM],
    "sessions": [sess("s1", "u-alex", now() - timedelta(days=3), os_="Windows", typ="Chrome", app=None),
                 sess("s2", "u-alex", now() - timedelta(hours=2))],
    "stats": {"photos": 8123, "videos": 902, "usage": 412_000_000_000, "usagePhotos": 210_000_000_000,
              "usageVideos": 202_000_000_000,
              "usageByUser": [{"userId": "u-alex", "userName": "Alex", "photos": 8123, "videos": 902,
                               "usage": 412_000_000_000, "usagePhotos": 1, "usageVideos": 1, "quotaSizeInBytes": None}]},
    "queues": [{"name": "thumbnailGeneration", "isPaused": False,
                "statistics": {"active": 0, "completed": 0, "delayed": 0, "failed": 0, "paused": 0, "waiting": 0}},
               {"name": "smartSearch", "isPaused": False,
                "statistics": {"active": 2, "completed": 0, "delayed": 0, "failed": 1, "paused": 0, "waiting": 40}}],
}
write_api(api)

broker = subprocess.Popen(["mosquitto", "-p", str(BPORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fake = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "fakes", "fake_api.py"), str(APORT), API_STATE])
time.sleep(1)

msgs = []
lock = threading.Lock()


def on_msg(c, u, m):
    with lock:
        msgs.append((m.topic, m.payload.decode(), m.retain))


sub = mk_client("test-sub")
sub.on_message = on_msg
sub.connect(BROKER, BPORT)
sub.subscribe("immich/#"); sub.subscribe("homeassistant/#")
sub.loop_start()

pub = mk_client("test-bridge")
pub.connect(BROKER, BPORT); pub.loop_start()

STATE_FILE = os.path.join(TMP, "state.json")


def make(state):
    def publish(topic, payload, retain):
        pub.publish(topic, payload, qos=1, retain=retain)

    def save():
        json.dump(state, open(STATE_FILE, "w"))

    env_docker = os.path.join(HERE, "fakes", "bin", "docker")
    device = {"identifiers": ["immich_lxc"], "name": "Immich", "manufacturer": "Proxmox LXC"}
    return im.ImmichMonitor({"url": f"http://127.0.0.1:{APORT}", "api_key": "test-key", "stats_interval_seconds": 0},
                            "immich", device, {"name": "immich-ha"}, publish,
                            lambda lvl, m: print(f"    [{lvl}] {m}"), state, save, docker=env_docker)


def events(kind=None):
    with lock:
        out = [json.loads(p) for t, p, r in msgs if t == "immich/login/event"]
    return [e for e in out if kind is None or e["event_type"] == kind]


def last(topic):
    with lock:
        for t, p, r in reversed(msgs):
            if t == topic:
                return p
    return None


os.environ["FAKE_LOG"] = LOG
open(LOG, "w").close()

print("1. startup baseline")
state = {}
mon = make(state)
for t, cfg in mon.discovery():
    pub.publish(t, json.dumps(cfg), qos=1, retain=True)
mon.start_log_follower()
mon.poll(force_stats=True)
time.sleep(1)
check("no login events for existing sessions", len(events()) == 0, events())
check("sessions = 2", last("immich/sessions/state") == "2", last("immich/sessions/state"))
ll = json.loads(last("immich/login/last/attributes"))
check("last sign-in seeded from newest session", ll.get("device", "").startswith("iOS") and "note" in ll, ll)
check("server ON", last("immich/api/state") == "ON")
check("photos/videos/usage", (last("immich/stats/photos"), last("immich/stats/videos")) == ("8123", "902"))
check("version", last("immich/stats/version") == "v3.3.5")
check("jobs waiting 42, processing ON", last("immich/jobs/state") == "42" and last("immich/jobs/processing") == "ON",
      (last("immich/jobs/state"), last("immich/jobs/processing")))
check("discovery has event entity with both types",
      any(t.startswith("homeassistant/event/immich_ha/") and json.loads(p)["event_types"] == ["login", "login_failed"]
          for t, p, r in msgs))

print("2. new sign-in")
api["sessions"].append(sess("s3", "u-sam", now(), os_="Android", typ="Immich app", app="2.6.0"))
write_api(api); mon.poll(); time.sleep(0.5)
ev = events("login")
check("one login event", len(ev) == 1, ev)
check("event names Sam on Android", ev and ev[0]["user"] == "Sam" and "Android" in ev[0]["device"], ev)
check("sessions = 3", last("immich/sessions/state") == "3")
mon.poll(); time.sleep(0.3)
check("no repeat on next poll", len(events("login")) == 1)

print("3. expired session not counted")
api["sessions"].append(sess("s4", "u-alex", now() - timedelta(days=40), expires=now() - timedelta(days=1)))
write_api(api); mon.poll(); time.sleep(0.3)
check("sessions still 3", last("immich/sessions/state") == "3", last("immich/sessions/state"))
check("expired old session not announced", len(events("login")) == 1, events("login"))

print("4. failed logins from the server log")
t1 = now()
with open(LOG, "a") as f:
    f.write(f"{ts(t1)} [Nest] 33  - 11/03/2026, 5:11:16 PM    LOG [Api:Bootstrap] unrelated line\n")
    f.write(f"{ts(t1 + timedelta(seconds=1))} [Nest] 33  - 11/03/2026, 5:11:17 PM   WARN [Api:AuthService~ab12] "
            f"Failed login attempt for user sam@example.com from ip address 198.51.100.50\n")
    f.write(f"{ts(t1 + timedelta(seconds=2))} \x1b[33m[Nest] 33  - \x1b[39m11/03/2026, 5:11:18 PM \x1b[33m  WARN\x1b[39m "
            f"\x1b[33m[Api:AuthService~ab13]\x1b[39m \x1b[33mFailed login attempt for user admin@example.org "
            f"from ip address 203.0.113.9\x1b[39m\n")
time.sleep(1.5)
fe = events("login_failed")
check("two failed events", len(fe) == 2, fe)
check("known user mapped", fe and fe[0]["user"] == "Sam" and fe[0]["known_user"] is True, fe[:1])
check("unknown email kept", len(fe) > 1 and fe[1]["email"] == "admin@example.org" and fe[1]["known_user"] is False, fe[1:])
check("failed count 2", last("immich/login/failed/state") == "2", last("immich/login/failed/state"))
fa = json.loads(last("immich/login/failed/attributes"))
check("recent newest first + ip list", fa["recent"][0]["ip"] == "203.0.113.9" and fa["ip_addresses"] == ["198.51.100.50", "203.0.113.9"], fa)

print("5. container recreated: log follow restarts without duplicates")
open(LOG + ".restart", "w").close()
time.sleep(6.5)
check("still two failed events", len(events("login_failed")) == 2, len(events("login_failed")))
with open(LOG, "a") as f:
    f.write(f"{ts(now())} WARN [Api:AuthService] Failed login attempt for user alex@example.com from ip address 198.51.100.20\n")
time.sleep(1.5)
check("new failure after restart seen", len(events("login_failed")) == 3, len(events("login_failed")))

print("6. bridge restart keeps state")
mon.stop(); time.sleep(0.5)
saved = json.load(open(STATE_FILE))
with lock:
    msgs.clear()
state2 = saved
mon2 = make(state2)
mon2.start_log_follower(); mon2.poll(force_stats=True); time.sleep(1.5)
check("no login events re-announced", len(events("login")) == 0, events())
check("no failed events replayed", len(events("login_failed")) == 0, events("login_failed"))
check("failed count persisted (3)", last("immich/login/failed/state") == "3", last("immich/login/failed/state"))

print("7. API down, then back")
api["down"] = True; write_api(api); mon2.poll(); time.sleep(0.3)
check("server OFF", last("immich/api/state") == "OFF")
check("error recorded", json.loads(last("immich/api/attributes"))["last_error"] != "")
api.pop("down"); write_api(api); mon2.poll(); time.sleep(0.3)
check("server ON again", last("immich/api/state") == "ON")
check("no logins invented after outage", len(events("login")) == 0)

print("8. bad API key")
mon2.key = "wrong"; mon2.poll(); time.sleep(0.3)
check("401 explained", "API key" in json.loads(last("immich/api/attributes"))["last_error"],
      last("immich/api/attributes"))
mon2.key = "test-key"

print("9. 24-hour window")
mon2.state["failed"][0]["time"] = im.iso(now() - timedelta(hours=25))
mon2.poll(); time.sleep(0.3)
check("old failure dropped (2 left)", last("immich/login/failed/state") == "2", last("immich/login/failed/state"))

print("10. schema: no nulls anywhere")
with lock:
    allm = list(msgs)


def has_null(o):
    if o is None:
        return True
    if isinstance(o, dict):
        return any(has_null(v) for v in o.values())
    if isinstance(o, list):
        return any(has_null(v) for v in o)
    return False


bad = []
for t, p, r in allm:
    if p.startswith("{") or p.startswith("["):
        if has_null(json.loads(p)):
            bad.append(t)
check("zero null values", not bad, bad)
check("events never retained", all(not r for t, p, r in allm if t == "immich/login/event"))

mon2.stop()
broker.terminate(); fake.terminate()
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
