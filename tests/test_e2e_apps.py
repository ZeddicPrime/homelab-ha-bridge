"""End-to-end scenarios for the application plugins (Jellyfin, Foundry VTT): real
mosquitto, fake application servers (fakes/fake_apps.py) and fake host commands
(systemctl, dpkg-query, apt, nvidia-smi, an update command) written into a temp
folder. Also covers --check-discovery and the "don't restart during an install" rule."""
import json, os, shutil, subprocess, sys, tempfile, textwrap, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAKES = os.path.join(HERE, "fakes")
BRIDGE = os.path.join(REPO, "bridge", "bridge.py")
import paho.mqtt.client as mqtt  # noqa: E402

BPORT, APORT = 18832, 18285
API = f"http://127.0.0.1:{APORT}"
T = tempfile.mkdtemp(prefix="habridge-apps-")
PASS = FAIL = 0


def mk_client(cid):
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    return mqtt.Client(client_id=cid)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  ok  ", name)
    else:
        FAIL += 1; print("  FAIL", name, str(detail)[:600])


def jw(path, obj):
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(path + ".tmp", path)


def jr(path):
    with open(path) as f:
        return json.load(f)


def wait(fn, t=20):
    end = time.time() + t
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


# ------------------------------------------------------------- the broker
procs = [subprocess.Popen(["mosquitto", "-p", str(BPORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
state = {"jellyfin": {}, "foundry": {}, "manifests": {}}
jw(f"{T}/apps.json", state)
procs.append(subprocess.Popen([sys.executable, "-I", f"{FAKES}/fake_apps.py", str(APORT), f"{T}/apps.json"]))
time.sleep(1)

msgs, lock = [], threading.Lock()
retained = {}


def on_msg(c, u, m):
    with lock:
        msgs.append((m.topic, m.payload.decode(errors="replace"), m.retain))


sub = mk_client("t-sub")
sub.on_message = on_msg
sub.connect("127.0.0.1", BPORT)
sub.subscribe("#")
sub.loop_start()
ctl = mk_client("t-ctl")
ctl.connect("127.0.0.1", BPORT)
ctl.loop_start()


def last(topic, js=False):
    with lock:
        for t, p, r in reversed(msgs):
            if t == topic:
                return json.loads(p) if js and p else p
    return None


def mark():
    with lock:
        return len(msgs)


def since(n, topic):
    with lock:
        return [(p, r) for t, p, r in msgs[n:] if t == topic]


def calls():
    try:
        with open(f"{T}/apps.json.calls") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except OSError:
        return []


def run_bridge(name, cfg_path, log_dir, **extra):
    env = dict(os.environ, no_proxy="localhost,127.0.0.1", NO_PROXY="localhost,127.0.0.1",
               HABRIDGE_ROOT=f"{T}/root-{name}", HABRIDGE_NO_APT="1",
               **{f"{name.upper()}_HA_CONFIG": cfg_path, f"{name.upper()}_HA_LOG_DIR": log_dir}, **extra)
    out = open(f"{T}/{name}.out", "a")
    return subprocess.Popen([sys.executable, "-I", BRIDGE, "--name", name], env=env, stdout=out,
                            stderr=subprocess.STDOUT)


def stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def check_discovery(name, cfg_path):
    env = dict(os.environ, no_proxy="localhost,127.0.0.1", NO_PROXY="localhost,127.0.0.1")
    r = subprocess.run([sys.executable, "-I", BRIDGE, "--name", name, "--config", cfg_path, "--check-discovery"],
                       env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


try:
    # =================================================================== Jellyfin
    print("J. Jellyfin plugin")
    HOST = f"{T}/media-host.json"
    BIN = f"{T}/media-bin"
    os.makedirs(BIN)
    jw(HOST, {"unit": "active", "installed": "10.11.3+ubu2404", "candidate": "10.11.4+ubu2404",
              "os_updates": ["libfoo", "jellyfin-ffmpeg7", "openssl"], "restarts": 0,
              "gpu": "12, 3, 4, 900, 8192, 45, Test GPU", "update_sleep": 0, "update_rc": 0})
    shim = textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        H = {HOST!r}
        h = json.load(open(H))
        def save():
            json.dump(h, open(H + ".tmp", "w")); os.replace(H + ".tmp", H)
        cmd, a = os.path.basename(sys.argv[0]), sys.argv[1:]
        """)
    bodies = {
        "systemctl": """
if a[0] == "is-active": print(h["unit"]); sys.exit(0 if h["unit"] == "active" else 3)
if a[0] == "show": print("Mon 2026-11-09 10:00:00 UTC"); sys.exit(0)
if a[0] == "restart": h["restarts"] += 1; save(); sys.exit(0)
sys.exit(1)
""",
        "dpkg-query": """print(h["installed"], end="")""",
        "apt-cache": """print("jellyfin-server:\\n  Installed: %s\\n  Candidate: %s" % (h["installed"], h["candidate"]))""",
        "apt-get": """
if "-s" in a:
    for p in h["os_updates"]: print(f"Inst {p} [1.0] (1.1 Debian:stable [amd64])")
sys.exit(0)
""",
        "nvidia-smi": """
if not h["gpu"]: print("NVIDIA-SMI has failed", file=sys.stderr); sys.exit(9)
fields = a[0].split("=", 1)[1].split(",")
vals = [v.strip() for v in h["gpu"].split(",")]
print(", ".join(vals) if len(fields) == 7 else ", ".join([vals[0], vals[3], vals[4], vals[5], vals[6]]))
""",
        "fake-update": """
print("\\x1b[32mUpdating Jellyfin\\x1b[0m"); sys.stdout.flush()
time.sleep(h["update_sleep"])
if h["update_rc"]: print("E: Could not get lock /var/lib/dpkg/lock-frontend"); sys.exit(h["update_rc"])
h["installed"] = h["candidate"]; save(); print("done")
""",
    }
    for cmd, body in bodies.items():
        with open(f"{BIN}/{cmd}", "w") as f:
            f.write(shim + body)
        os.chmod(f"{BIN}/{cmd}", 0o755)

    state["jellyfin"] = {
        "key": "jf-key",
        "info": {"ServerName": "Test Server", "Version": "10.11.3", "HasPendingRestart": False},
        "sessions": [
            {"UserName": "Alex", "DeviceName": "TV", "Client": "Android TV",
             "NowPlayingItem": {"Name": "Pilot", "Type": "Episode", "SeriesName": "A Show",
                                "ParentIndexNumber": 1, "IndexNumber": 1, "RunTimeTicks": 1000},
             "PlayState": {"PositionTicks": 250, "IsPaused": False, "PlayMethod": "Transcode"},
             "TranscodingInfo": {"VideoCodec": "h264", "AudioCodec": "aac", "IsVideoDirect": False,
                                 "IsAudioDirect": True, "HardwareAccelerationType": "nvenc",
                                 "Width": 1920, "Height": 1080, "TranscodeReasons": "VideoCodecNotSupported"}},
            {"UserName": "Sam", "DeviceName": "Phone", "Client": "Web",
             "NowPlayingItem": {"Name": "A Film", "Type": "Movie", "ProductionYear": 2001},
             "PlayState": {"IsPaused": True, "PlayMethod": "DirectPlay"}},
            {"UserName": "Sam", "DeviceName": "Laptop", "Client": "Web"}],
        "tasks": [{"Key": "RefreshLibrary", "Name": "Scan Media Library", "State": "Idle",
                   "LastExecutionResult": {"StartTimeUtc": "2026-11-10T01:00:00.1234567Z",
                                           "EndTimeUtc": "2026-11-10T01:05:00.1234567Z", "Status": "Completed"}}],
        "counts": {"MovieCount": 10, "SeriesCount": 3, "EpisodeCount": 40, "AlbumCount": 5, "SongCount": 60},
    }
    state["bridge_release"] = {
        "tag_name": "v99.0.0", "html_url": "https://github.com/example/bridge/releases/tag/v99.0.0",
        "draft": False, "prerelease": False, "body": "test", "assets": []}
    jw(f"{T}/apps.json", state)
    JCFG = f"{T}/media.json"
    jcfg = {"mqtt": {"host": "127.0.0.1", "port": BPORT}, "plugins": ["jellyfin"],
            "topic_base": "media-ha", "availability_topic": "media-ha/status", "device_id": "media-ct",
            "device_name": "Media Server", "device_manufacturer": "Jellyfin", "device_model": "Media server",
            "device_url": "https://media.example.com",
            "self_update": {"api": f"{API}/gh-bridge", "repo": "example/bridge"},
            "jellyfin": {"url": f"{API}/jf", "api_key": "jf-key", "public_url": "https://media.example.com",
                         "path": BIN, "update_command": [f"{BIN}/fake-update"], "poll_seconds": 1}}
    jw(JCFG, jcfg)
    JLOG = f"{T}/media-log"
    jb = run_bridge("media", JCFG, JLOG)

    def disc(c, node, obj):
        return last(f"homeassistant/{c}/{node}/{obj}/config", js=True)

    check("J1 discovery: active streams", wait(lambda: disc("sensor", "media_ha", "active_streams")),
          open(f"{T}/media.out").read()[-2000:])
    a = disc("sensor", "media_ha", "active_streams") or {}
    check("J1 unique_id and entity ID", a.get("unique_id") == "media_ha_active_streams"
          and a.get("default_entity_id") == "sensor.media_active_streams", a)
    check("J1 availability: last will + API", a.get("availability") == [{"topic": "media-ha/status"},
                                                                         {"topic": "media-ha/api"}]
          and a.get("availability_mode") == "all", a)
    check("J1 device from the config", (a.get("device") or {}).get("identifiers") == ["media-ct"]
          and a["device"].get("name") == "Media Server"
          and a["device"].get("configuration_url") == "https://media.example.com", a.get("device"))
    check("J1 state topics under topic_base", a.get("state_topic") == "media-ha/streams/state", a)
    mp = disc("binary_sensor", "media_ha", "mqtt_plugin") or {}
    check("J1 MQTT plugin sensor has no availability", mp and "availability" not in mp
          and mp.get("state_topic") == "jellyfin/status", mp)
    check("J1 GPU entities (nvidia-smi answers)", wait(lambda: disc("sensor", "media_ha", "gpu_encoder")))
    cb = disc("button", "media_ha", "check_for_updates") or {}
    check("J1 Check for updates button", cb.get("command_topic") == "media-ha/check"
          and cb.get("default_entity_id") == "button.media_check_for_updates", cb)
    check("J1 bridge update entity", (disc("update", "media_ha", "bridge-update") or {}).get("default_entity_id")
          == "update.media_ha_bridge")

    check("J2 online", wait(lambda: last("media-ha/status") == "online"))
    check("J2 server running", wait(lambda: last("media-ha/server/state") == "ON"))
    check("J2 two active streams, one transcode", wait(lambda: last("media-ha/streams/state") == "2"
                                                         and last("media-ha/transcodes/state") == "1"),
          (last("media-ha/streams/state"), last("media-ha/transcodes/state")))
    st_attr = last("media-ha/streams/attributes", js=True) or {}
    check("J2 stream titles", any(s.get("title") == "A Show S01E01 · Pilot" for s in st_attr.get("streams", [])), st_attr)
    check("J2 library counts", wait(lambda: last("media-ha/library/movies") == "10"
                                    and last("media-ha/library/songs") == "60"))
    check("J2 scan idle", wait(lambda: last("media-ha/scan/state") == "Idle"))
    check("J2 API online", wait(lambda: last("media-ha/api") == "online"))
    check("J2 update offered", wait(lambda: (last("media-ha/update/state", js=True) or {}).get("latest_version") == "10.11.4"),
          last("media-ha/update/state"))
    us = last("media-ha/update/state", js=True) or {}
    check("J2 installed version and release link", us.get("installed_version") == "10.11.3"
          and us.get("release_url") == "https://github.com/jellyfin/jellyfin/releases/tag/v10.11.4", us)
    check("J2 OS updates exclude jellyfin packages", wait(lambda: last("media-ha/os_updates/state") == "2"),
          last("media-ha/os_updates/attributes"))
    check("J2 GPU load", wait(lambda: last("media-ha/gpu/utilization") == "12"
                              and last("media-ha/gpu/encoder") == "3"))

    n = len(calls())
    ctl.publish("media-ha/scan/run", "SCAN")
    check("J3 Scan libraries calls the API", wait(lambda: any(c["path"] == "/jf/Library/Refresh" for c in calls()[n:])))
    ctl.publish("media-ha/scan/run", "rm -rf /")
    time.sleep(1)
    check("J3 any other payload does nothing", sum(c["path"] == "/jf/Library/Refresh" for c in calls()[n:]) == 1)

    ctl.publish("media-ha/restart/run", "RESTART")
    check("J4 Restart restarts the unit", wait(lambda: jr(HOST)["restarts"] == 1))

    h = jr(HOST); h["update_sleep"] = 5; jw(HOST, h)
    ctl.publish("media-ha/update/install", "INSTALL")
    check("J5 controls go offline during the update", wait(lambda: last("media-ha/controls") == "offline", 10))
    check("J5 update shows in progress", wait(lambda: (last("media-ha/update/state", js=True) or {}).get("in_progress") is True, 10))
    ctl.publish("media-ha/bridge-update/install", "INSTALL")
    check("J5 bridge update refused while Jellyfin updates",
          wait(lambda: "Not started: Jellyfin update" in ((last("media-ha/bridge-update/attributes", js=True) or {})
                                                         .get("last_install_result") or ""), 10),
          last("media-ha/bridge-update/attributes"))
    check("J5 updated", wait(lambda: "Updated 10.11.3 → 10.11.4" in ((last("media-ha/update/attributes", js=True) or {})
                                                                     .get("last_install_result") or ""), 30),
          last("media-ha/update/attributes"))
    check("J5 nothing left to offer", wait(lambda: (last("media-ha/update/state", js=True) or {}).get("installed_version") == "10.11.4"
                                           and last("media-ha/update/state", js=True).get("latest_version") == "10.11.4"
                                           and last("media-ha/update/state", js=True).get("release_summary") == ""),
          last("media-ha/update/state"))
    check("J5 controls back online", wait(lambda: last("media-ha/controls") == "online"))
    log_text = open(f"{JLOG}/last-update.log").read()
    check("J5 update log kept", "Updating Jellyfin" in log_text, log_text[:200])

    h = jr(HOST); h["update_sleep"] = 0; h["update_rc"] = 100; h["candidate"] = "10.11.5+ubu2404"; jw(HOST, h)
    ctl.publish("media-ha/check", "CHECK")
    check("J5 next version offered after Check", wait(lambda: (last("media-ha/update/state", js=True) or {}).get("latest_version") == "10.11.5"))
    ctl.publish("media-ha/update/install", "INSTALL")
    check("J5 a failing updater is reported with a hint",
          wait(lambda: "apt was busy" in ((last("media-ha/update/attributes", js=True) or {}).get("last_install_result") or ""), 20),
          last("media-ha/update/attributes"))

    state["jellyfin"]["key"] = "rotated"; jw(f"{T}/apps.json", state)
    check("J6 rejected API key: API offline", wait(lambda: last("media-ha/api") == "offline"))
    check("J6 server attributes say why", "rejected" in ((last("media-ha/server/attributes", js=True) or {}).get("api") or ""))
    state["jellyfin"]["key"] = "jf-key"; jw(f"{T}/apps.json", state)
    check("J6 back online with the key", wait(lambda: last("media-ha/api") == "online"))

    rc, out = check_discovery("media", JCFG)
    check("J9 --check-discovery: nothing lost", rc == 0 and "keep their IDs" in out, out)
    ctl.publish("homeassistant/sensor/media_ha/old_thing/config",
                json.dumps({"unique_id": "media_ha_old_thing", "name": "Old"}), retain=True).wait_for_publish()
    rc, out = check_discovery("media", JCFG)
    check("J9 an entity only the old program had is reported", rc == 2 and "media_ha_old_thing" in out, out)
    ctl.publish("homeassistant/sensor/media_ha/old_thing/config", "", retain=True).wait_for_publish()
    moved = dict(jcfg, device_id="media_lxc")
    jw(f"{T}/media-moved.json", moved)
    rc, out = check_discovery("media", f"{T}/media-moved.json")
    check("J9 a device change is reported", rc == 2 and "another device" in out, out)

    stop(jb)
    check("J10 offline on stop", wait(lambda: last("media-ha/status") == "offline"))
    h = jr(HOST); h["gpu"] = ""; jw(HOST, h)
    # A retained command (published while the bridge is down) arrives flagged as
    # retained when it reconnects: it must be ignored and cleared, never run.
    ctl.publish("media-ha/scan/run", "SCAN", retain=True).wait_for_publish()
    n, nc = mark(), len(calls())
    jb = run_bridge("media", JCFG, JLOG)
    check("J10 without a GPU its entities are removed",
          wait(lambda: ("", False) in since(n, "homeassistant/sensor/media_ha/gpu_encoder/config")),
          since(n, "homeassistant/sensor/media_ha/gpu_encoder/config"))
    check("J10 retained command cleared", wait(lambda: ("", False) in since(n, "media-ha/scan/run"), 10),
          since(n, "media-ha/scan/run"))
    time.sleep(1)
    check("J10 retained command not run", not any(c["path"] == "/jf/Library/Refresh" for c in calls()[nc:]))
    stop(jb)

    # ================================================================= Foundry VTT
    print("F. Foundry VTT plugin")
    D = f"{T}/vtt/data"
    def fw(path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        jw(path, obj)
    fw(f"{D}/Data/worlds/alpha/world.json", {"id": "alpha", "system": "testsys"})
    fw(f"{D}/Data/systems/testsys/system.json", {"id": "testsys", "version": "3.0.0", "manifest": f"{API}/manifest/testsys"})
    fw(f"{D}/Data/systems/othersys/system.json", {"id": "othersys", "version": "1.0.0"})
    for mid, title, ver in (("mod-a", "Module A", "1.0.0"), ("art-pack", "Art Pack", "2.0.0"),
                            ("mod-held", "Held Module", "1.0.0"), ("mod-incompat", "Next Gen Module", "1.0.0")):
        fw(f"{D}/Data/modules/{mid}/module.json", {"id": mid, "title": title, "version": ver,
                                                   "manifest": f"{API}/manifest/{mid}"})
    fw(f"{D}/Data/modules/mod-local/module.json", {"id": "mod-local", "title": "Local Module", "version": "0.1"})
    fw(f"{T}/vtt/app/package.json", {"release": {"generation": 13, "build": 351}})
    os.makedirs(f"{D}/Backups/worlds/alpha")
    open(f"{D}/Backups/worlds/alpha/alpha-backup.zip", "wb").write(b"x" * 2048)
    open(f"{D}/Backups/worlds/alpha/alpha-backup.json", "w").write("{}")
    os.makedirs(f"{D}/Logs")
    LOGF = f"{D}/Logs/debug.2026-11-10.log"
    with open(LOGF, "w") as f:
        for ts, lvl, msg in (("2026-11-10 18:00:00", "info", "Server started and listening on port 30000"),
                             ("2026-11-10 18:01:00", "info", "Launching World | Complete"),
                             ("2026-11-10 18:01:05", "info", "Vended World data to User [abc123] in 1234ms"),
                             ("2026-11-10 18:02:00", "info", "User authentication successful for user Gamemaster"),
                             ("2099-01-01 00:00:00", "warn", "Module Next Gen uses a deprecated API")):
            f.write(json.dumps({"timestamp": ts, "level": lvl, "message": msg}) + "\n")
    state["foundry"] = {"admin_password": "pw", "status": {
        "active": True, "version": "13.351", "world": "alpha", "system": "testsys", "systemVersion": "3.0.0",
        "users": 2, "uptime": 100}}
    state["manifests"] = {
        "testsys": {"version": "3.0.0"},
        "mod-a": {"version": "1.1.0", "compatibility": {"minimum": "12", "maximum": "13"}},
        "art-pack": {"version": "2.1.0"},
        "mod-held": {"version": "1.5.0"},
        "mod-incompat": {"version": "2.0.0", "compatibility": {"minimum": "14"}},
    }
    state["releases_html"] = ('<a href="/releases/13.351">13.351</a> <span>Stable</span>'
                              '<a href="https://foundryvtt.com/releases/14.360">14.360</a> <span>Stable</span>'
                              '<a href="/releases/14.361">14.361</a> <span>Testing</span>')
    jw(f"{T}/apps.json", state)
    FCFG = f"{T}/vtt.json"
    fcfg = {"mqtt": {"host": "127.0.0.1", "port": BPORT}, "plugins": ["foundry"], "device_name": "VTT Host",
            "self_update": {"enabled": False},
            "foundry": {"status_url": f"{API}/api/status", "data_dir": D, "app_dir": f"{T}/vtt/app",
                        "public_url": "https://vtt.example.com", "admin_password": "pw",
                        "module_groups": {"art": {"name": "Art modules outdated", "pattern": "^art-"}},
                        "held_modules": ["mod-held"], "releases_url": f"{API}/releases/",
                        "status_interval_seconds": 1}}
    jw(FCFG, fcfg)
    FLOG = f"{T}/vtt-log"
    fb = run_bridge("vtt", FCFG, FLOG)

    check("F1 discovery: game system version", wait(lambda: disc("sensor", "vtt_vtt", "testsys_version")),
          open(f"{T}/vtt.out").read()[-2000:])
    sv = disc("sensor", "vtt_vtt", "testsys_version") or {}
    check("F1 IDs", sv.get("unique_id") == "vtt_vtt_testsys_version"
          and sv.get("default_entity_id") == "sensor.vtt_testsys_version", sv)
    check("F1 Foundry VTT device", (sv.get("device") or {}).get("identifiers") == ["vtt_vtt"]
          and sv["device"].get("name") == "Foundry VTT"
          and sv["device"].get("configuration_url") == "https://vtt.example.com", sv.get("device"))
    check("F1 device version from the server", wait(lambda: (disc("sensor", "vtt_vtt", "testsys_version") or {})
                                                     .get("device", {}).get("sw_version") == "13.351"))
    check("F1 module group entity", (disc("sensor", "vtt_vtt", "art_modules_outdated") or {}).get("name")
          == "Art modules outdated")
    check("F1 only systems a world uses", disc("sensor", "vtt_vtt", "othersys_version") is None)
    lb = disc("button", "vtt_vtt", "launch_world") or {}
    check("F1 buttons need the server and an idle world", lb.get("availability") == [
        {"topic": "vtt/availability"}, {"topic": "vtt/launch_world/available"}], lb)
    check("F1 host device keeps Check for updates", (disc("button", "vtt_ha", "check_for_updates") or {})
          .get("device", {}).get("name") == "VTT Host")

    check("F2 server up", wait(lambda: last("vtt/server/state") == "ON"))
    check("F2 active world and players", last("vtt/active_world/state") == "alpha"
          and last("vtt/players_online/state") == "2")
    check("F2 versions", last("vtt/version/state") == "13.351" and wait(lambda: last("vtt/testsys_version/state") == "3.0.0"))
    check("F2 last restart from the log", last("vtt/last_restart/state") == "2026-11-10T18:00:00+00:00",
          last("vtt/last_restart/state"))
    check("F2 world load time", last("vtt/world_load_time/state") == "1234")
    lbk = last("vtt/last_backup/attributes", js=True) or {}
    check("F2 last backup (sidecar ignored)", lbk.get("file") == "alpha-backup.zip" and lbk.get("backup_count") == 1, lbk)
    check("F2 compatibility warning counted", last("vtt/compatibility_warnings/state") == "1")
    check("F2 no login event replayed from old logs", last("vtt/login/event") is None)
    check("F2 return to setup available", wait(lambda: last("vtt/return_to_setup/available") == "online"))
    check("F2 launch unavailable while a world runs", last("vtt/launch_world/available") == "offline")

    ctl.publish("vtt/check", "CHECK")
    check("F3 core update found (stable only)", wait(lambda: last("vtt/core_update/state") == "14.360 available", 30),
          last("vtt/core_update/state"))
    check("F3 modules checked", wait(lambda: last("vtt/modules_outdated/state") == "2", 30),
          last("vtt/modules_outdated/attributes"))
    mo = last("vtt/modules_outdated/attributes", js=True) or {}
    check("F3 incompatible update flagged", mo.get("incompatible_updates") == ["Next Gen Module"], mo)
    check("F3 held module listed, not counted", mo.get("held") == ["Held Module: 1.0.0 (1.5.0 available)"], mo)
    check("F3 module without a manifest unchecked", mo.get("unchecked") == ["Local Module"], mo)
    check("F3 module group counted apart", last("vtt/art_modules_outdated/state") == "1"
          and "Art Pack: 2.0.0 → 2.1.0" in (last("vtt/art_modules_outdated/attributes", js=True) or {}).get("outdated", []))
    check("F3 game system up to date", wait(lambda: last("vtt/testsys_update/state") == "Up to date"))

    n = mark()
    with open(LOGF, "a") as f:
        f.write(json.dumps({"timestamp": "2026-11-11 08:00:00", "level": "warn",
                            "message": "User authentication failed for user Player 2: wrong password"}) + "\n")
    check("F4 live failed login becomes an event", wait(lambda: since(n, "vtt/login/event")))
    ev = since(n, "vtt/login/event")
    check("F4 event with user and reason", ev and json.loads(ev[0][0]) == {
        "event_type": "login_failed", "user": "Player 2", "time": "2026-11-11T08:00:00+00:00",
        "reason": "wrong password"}, ev)
    late, got = mk_client("t-late"), []
    late.on_message = lambda c, u, m: got.append(m.topic)
    late.connect("127.0.0.1", BPORT); late.subscribe("vtt/login/event"); late.loop_start()
    time.sleep(1.5); late.loop_stop(); late.disconnect()
    check("F4 events are not retained", got == [], got)

    state["foundry"]["status"].update(active=False, world=None, users=0)
    jw(f"{T}/apps.json", state)
    check("F5 launch available when no world runs", wait(lambda: last("vtt/launch_world/available") == "online"))
    n = len(calls())
    ctl.publish("vtt/launch_world/press", "PRESS")
    check("F5 launches the only world with the admin password",
          wait(lambda: any(c["path"] == "/setup" and c["body"] == {"action": "launchWorld", "world": "alpha",
                                                                       "adminPassword": "pw"} for c in calls()[n:])),
          calls()[n:])
    check("F5 result shown", wait(lambda: (last("vtt/launch_world/attributes", js=True) or {}).get("last_result") == "success"))

    stop(fb)
    fcfg["foundry"]["systems"] = ["othersys"]
    jw(FCFG, fcfg)
    n = mark()
    fb = run_bridge("vtt", FCFG, FLOG)
    check("F6 configured systems replace detected ones",
          wait(lambda: disc("sensor", "vtt_vtt", "othersys_version")
               and ("", False) in since(n, "homeassistant/sensor/vtt_vtt/testsys_version/config")),
          since(n, "homeassistant/sensor/vtt_vtt/testsys_version/config"))
    rc, out = check_discovery("vtt", FCFG)
    check("F7 --check-discovery: a removal the bridge made itself isn't a loss", rc == 0, out)
    stop(fb)

    # ============================================================ Anthias (a Pi)
    print("A. Anthias plugin on a Pi that is often switched off")
    PI = f"{T}/pi"
    PBIN = f"{PI}/bin"
    os.makedirs(PBIN)
    PH = f"{PI}/host.json"
    jw(PH, {"cec": "1", "viewer_started": "2026-11-10T00:00:00.000000000Z", "calls": []})
    pshim = textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        H = {PH!r}
        h = json.load(open(H))
        cmd, a = os.path.basename(sys.argv[0]), sys.argv[1:]
        h["calls"].append([cmd] + a)
        json.dump(h, open(H + ".tmp", "w")); os.replace(H + ".tmp", H)
        """)
    pbodies = {
        "docker": """
if a[0] == "exec": print(h["cec"])
elif a[0] == "inspect": print("true " + h["viewer_started"])
elif a[0] == "restart": print(a[1])
""",
        "vcgencmd": """print("throttled=" + h.get("throttled", "0x0"))""",
        "systemctl": """sys.exit(0)""",
    }
    for cmd, body in pbodies.items():
        with open(f"{PBIN}/{cmd}", "w") as f:
            f.write(pshim + body)
        os.chmod(f"{PBIN}/{cmd}", 0o755)
    for fn, text in (("uptime", "1000.5 2000.1\n"), ("boot_id", "boot-1\n"), ("temp", "48312\n"),
                     ("model", "Raspberry Pi 4 Model B Rev 1.4\x00")):
        open(f"{PI}/{fn}", "w").write(text)
    import base64
    state["anthias"] = {"auth": "Basic " + base64.b64encode(b"admin:secret").decode(), "info": {
        "anthias_version": "v2026.10.0 (a8586a6)", "free_space": "204.6 GB",
        "storage": {"status": "ok", "device": "sda2", "fstype": "ext4", "write_ok": True, "errors_count": 0,
                    "errors_this_boot": False, "write_fail_count": 0, "read_only": False,
                    "media": {"name": "Test SSD"}}}}
    state["anthias_release"] = {"tag_name": "v2026.11.0", "html_url": "https://github.com/Screenly/Anthias/releases/tag/v2026.11.0",
                                "body": "## Highlights\n\nA *stability* release with [fixes](https://x).\n\n- more"}
    jw(f"{T}/apps.json", state)
    PCFG = f"{PI}/config.json"
    pcfg = {"mqtt": {"host": "127.0.0.1", "port": BPORT, "keepalive": 2}, "plugins": ["anthias"],
            "device_name": "Test Projector Pi", "self_update": {"enabled": False},
            "anthias": {"url": f"{API}/anth", "username": "admin", "password": "secret", "poll_seconds": 1,
                        "github_api": f"{API}/gh-anthias", "vcgencmd": f"{PBIN}/vcgencmd",
                        "systemctl": f"{PBIN}/systemctl", "uptime_file": f"{PI}/uptime",
                        "boot_id_file": f"{PI}/boot_id", "thermal_file": f"{PI}/temp", "model_file": f"{PI}/model"}}
    jw(PCFG, pcfg)
    PLOG = f"{PI}/log"
    PENV = {"PI_HA_DOCKER": f"{PBIN}/docker"}
    pb = run_bridge("pi", PCFG, PLOG, **PENV)

    check("A1 discovery: Online", wait(lambda: disc("binary_sensor", "pi_ha", "online")), open(f"{T}/pi.out").read()[-2000:])
    on = disc("binary_sensor", "pi_ha", "online") or {}
    check("A1 Online follows the last will, with no availability of its own",
          on.get("state_topic") == "pi/availability" and "availability_topic" not in on
          and on.get("unique_id") == "pi_ha_online" and on.get("default_entity_id") == "binary_sensor.pi_online", on)
    dev = on.get("device") or {}
    check("A1 the device is the Pi", dev.get("identifiers") == ["pi_pi"] and dev.get("manufacturer") == "Raspberry Pi"
          and dev.get("model") == "Raspberry Pi 4 Model B Rev 1.4" and dev.get("name") == "Test Projector Pi", dev)
    stg = disc("binary_sensor", "pi_ha", "storage") or {}
    check("A1 health keeps its value while off (no availability)", stg and "availability_topic" not in stg, stg)
    upd = disc("update", "pi_ha", "anthias") or {}
    check("A1 Anthias update entity is read-only", upd and "command_topic" not in upd
          and "availability_topic" not in upd, upd)
    sd = disc("button", "pi_ha", "shutdown") or {}
    check("A1 buttons only while the Pi is up", sd.get("availability_topic") == "pi/availability"
          and sd.get("command_topic") == "pi/cmd/shutdown", sd)
    check("A1 CPU temperature only while up", (disc("sensor", "pi_ha", "cpu_temperature") or {})
          .get("availability_topic") == "pi/availability")

    check("A2 online", wait(lambda: last("pi/availability") == "online"))
    check("A2 CPU temperature", wait(lambda: last("pi/cpu_temperature") == "48.3"), last("pi/cpu_temperature"))
    check("A2 storage fine", wait(lambda: last("pi/storage") == "OFF")
          and (last("pi/storage/attributes", js=True) or {}).get("media") == "Test SSD")
    check("A2 Anthias web fine", last("pi/anthias") == "OFF"
          and (last("pi/anthias/attributes", js=True) or {}).get("free_space") == "204.6 GB")
    check("A2 CEC adapter", wait(lambda: last("pi/cec") == "ON"))
    check("A2 no undervoltage", wait(lambda: last("pi/undervoltage") == "OFF"))
    check("A2 Anthias update offered", wait(lambda: (last("pi/update/state", js=True) or {}).get("latest_version") == "v2026.11.0"))
    us = last("pi/update/state", js=True) or {}
    check("A2 installed tag, summary and link", us.get("installed_version") == "v2026.10.0"
          and us.get("release_summary") == "A stability release with fixes."
          and us.get("release_url", "").endswith("/v2026.11.0"), us)

    n = len(calls())
    ctl.publish("pi/cmd/projector_standby", "PRESS")
    check("A3 Projector standby asks Anthias (with its login)",
          wait(lambda: any(c["path"] == "/anth/api/v2/display/off" for c in calls()[n:])), calls()[n:])
    check("A3 result shown", wait(lambda: last("pi/last_action") == "Projector standby: OK"))
    ctl.publish("pi/cmd/projector_standby", "please")
    ctl.publish("pi/cmd/rm", "PRESS")
    time.sleep(1)
    check("A3 unknown buttons and payloads do nothing",
          sum(c["path"] == "/anth/api/v2/display/off" for c in calls()[n:]) == 1)

    ctl.publish("pi/cmd/shutdown", "PRESS")
    check("A4 Shut down powers the Pi off", wait(lambda: ["systemctl", "poweroff"] in jr(PH)["calls"]))
    check("A4 and says so first", (last("pi/last_action/attributes", js=True) or {}).get("detail")
          == "systemctl poweroff requested" and last("pi/last_action") == "Shut down: OK")

    # systemd stops the service on the way down
    stop(pb)
    check("A5 Online turns off on a clean stop", wait(lambda: last("pi/availability") == "offline"))
    late, got = mk_client("t-late2"), {}
    late.on_message = lambda c, u, m: got.__setitem__(m.topic, m.payload.decode())
    late.connect("127.0.0.1", BPORT); late.subscribe("pi/#"); late.loop_start()
    time.sleep(1.5); late.loop_stop(); late.disconnect()
    check("A5 health and the update stay in the broker while off",
          got.get("pi/storage") == "OFF" and "v2026.11.0" in got.get("pi/update/state", "")
          and got.get("pi/last_action") == "Shut down: OK", sorted(got))

    # Next boot: Redis came up after the viewer, so CEC was never reported
    open(f"{PI}/boot_id", "w").write("boot-2\n")
    h = jr(PH); h["cec"] = ""; h["calls"] = []; jw(PH, h)
    pb = run_bridge("pi", PCFG, PLOG, **PENV)
    check("A6 back online", wait(lambda: last("pi/availability") == "online"))
    check("A6 viewer restarted once to get CEC reported",
          wait(lambda: sum(c[:2] == ["docker", "restart"] for c in jr(PH)["calls"]) == 1, 15), jr(PH)["calls"][-5:])
    time.sleep(3)
    check("A6 only once per boot", sum(c[:2] == ["docker", "restart"] for c in jr(PH)["calls"]) == 1)
    check("A6 shown as an automatic action", "Automatic" in ((last("pi/last_action/attributes", js=True) or {})
                                                              .get("detail") or ""))

    state["anthias"]["down"] = True; jw(f"{T}/apps.json", state)
    check("A7 Anthias web problem after repeated failures", wait(lambda: last("pi/anthias") == "ON", 15))
    state["anthias"]["down"] = False; jw(f"{T}/apps.json", state)
    check("A7 clears when it answers", wait(lambda: last("pi/anthias") == "OFF", 10))

    rc, out = check_discovery("pi", PCFG)
    check("A8 --check-discovery: nothing lost", rc == 0, out)

    # Power cut: no clean stop, the broker's last will has to say it
    pb.kill(); pb.wait()
    check("A9 Online turns off after a power cut (last will)", wait(lambda: last("pi/availability") == "offline", 15))
finally:
    for p in procs:
        p.terminate()
    sub.loop_stop(); ctl.loop_stop()

print(f"\n{PASS} passed, {FAIL} failed")
if FAIL:
    print(f"(logs in {T})")
else:
    shutil.rmtree(T, ignore_errors=True)
sys.exit(1 if FAIL else 0)
