"""The bridge: one MQTT connection, one Home Assistant device, a set of plugins.

Everything every host shares lives here: availability (last will), the
"Check for updates" button, re-sending discovery when Home Assistant restarts,
the safety rules for command topics, and the state file. Plugins add entities.
"""

import json
import logging
import os
import threading

import paho.mqtt.client as mqtt

from . import PROJECT, VERSION
from .util import write_json_atomic

DISCOVERY_PREFIX = "homeassistant"
CHECK_PAYLOAD = "CHECK"

log = logging.getLogger("habridge")


class Plugin:
    """Base class. A plugin owns some entities on the bridge's device."""

    name = "plugin"
    state_keys = ()  # top-level keys of state.json this plugin owns

    def __init__(self, bridge, cfg):
        self.bridge = bridge
        self.cfg = cfg

    def subscriptions(self):
        return []

    def is_command(self, topic):
        """True for this plugin's command topics (a retained one gets cleared)."""
        return False

    def handle(self, topic, payload):
        """Return True if the message was for this plugin."""
        return False

    def prepare(self):
        """Find out what this host has (folders, GPU, packages) without publishing,
        so --check-discovery can list every entity this plugin will publish."""

    def publish_discovery(self):
        pass

    def busy_reason(self):
        """A short reason while this plugin runs something that a restart, reboot or
        power-off would break (an install), else None."""
        return None

    def republish(self):
        pass

    def on_check(self):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return {}


def make_device(cfg, name, service):
    dev = {
        "identifiers": [cfg.get("device_id", f"{name}_lxc")],
        "name": cfg.get("device_name", f"{name.capitalize()} Host"),
        "manufacturer": cfg.get("device_manufacturer", "Proxmox LXC"),
        "model": cfg.get("device_model", "Docker Compose host"),
        "sw_version": f"{PROJECT} {VERSION}",
    }
    if cfg.get("device_url"):
        dev["configuration_url"] = cfg["device_url"]
    return dev


class Bridge:
    def __init__(self, cfg, name, config_path, log_dir):
        self.cfg = cfg
        self.name = name                        # drives every topic, ID and path
        self.service = f"{name}-ha"             # systemd unit, MQTT client ID
        self.node = f"{name}_ha"                # discovery node ID and unique_id prefix
        # Topic prefix and last-will topic. Both can be set in the config for a host
        # whose entities already live under other topics.
        self.base = cfg.get("topic_base") or name
        self.avail_topic = cfg.get("availability_topic") or f"{self.base}/availability"
        self.check_topic = f"{self.base}/check"
        self.config_path = config_path
        self.log_dir = log_dir
        self.device = make_device(cfg, name, self.service)
        self.origin = {"name": self.service, "sw_version": VERSION,
                       "support_url": f"https://github.com/ZeddicPrime/{PROJECT}"}
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.save_lock = threading.Lock()
        self.save_seq_lock = threading.Lock()
        self.save_seq = 0     # handed out to each save_state call
        self.saved_seq = 0    # the newest one written
        os.makedirs(log_dir, exist_ok=True)
        self.state_file = os.path.join(log_dir, "state.json")
        self.state = self._load_state()
        self.plugins = []
        self.exit_code = 0

        m = cfg["mqtt"]
        client_id = m.get("client_id") or self.service  # must be unique on the broker
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt 2.x
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
        else:  # paho-mqtt 1.x (Ubuntu 24.04 / Debian 12 packages)
            self.client = mqtt.Client(client_id=client_id)
        if m.get("username"):
            self.client.username_pw_set(m["username"], m.get("password"))
        self.client.will_set(self.avail_topic, "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(1, 60)

    # ------------------------------------------------------------- state
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def plugin(self, name):
        return next((p for p in self.plugins if p.name == name), None)

    def busy_reason(self):
        """Why a restart, reboot or power-off would be harmful right now, else None."""
        for p in self.plugins:
            try:
                why = p.busy_reason()
            except Exception:  # noqa: BLE001
                why = None
            if why:
                return why
        return None

    def save_state(self):
        """Write every plugin's state. Plugins may call this while holding their own lock, so
        the snapshots are taken before save_lock: save_lock is never held while waiting for a
        plugin's lock. A save that took its snapshots before a newer save was written is
        skipped (the newer one already includes its change)."""
        with self.save_seq_lock:
            self.save_seq += 1
            seq = self.save_seq
        snaps = {}
        for p in self.plugins:
            snaps.update(p.snapshot())
        with self.save_lock:
            if seq < self.saved_seq:
                return
            self.saved_seq = seq
            data = dict(self.state)
            data.update(snaps)
            self.state = data
            write_json_atomic(self.state_file, data)

    # -------------------------------------------------------------- mqtt
    def publish(self, topic, payload, retain=True, qos=0):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        return self.client.publish(topic, payload, qos=qos, retain=retain)

    def common(self):
        """Fields every discovery config on this device carries."""
        return {"availability_topic": self.avail_topic, "device": self.device, "origin": self.origin}

    def discovery_topic(self, component, object_id):
        return f"{DISCOVERY_PREFIX}/{component}/{self.node}/{object_id}/config"

    def publish_discovery(self):
        btn = {
            "name": "Check for updates",
            "default_entity_id": f"button.{self.name}_check_for_updates",
            "unique_id": f"{self.node}_check_for_updates",
            "icon": "mdi:refresh",
            "command_topic": self.check_topic,
            "payload_press": CHECK_PAYLOAD,
            **self.common(),
        }
        self.publish(self.discovery_topic("button", "check_for_updates"), btn)
        for p in self.plugins:
            try:
                p.publish_discovery()
            except Exception:  # noqa: BLE001
                log.exception("%s: discovery failed", p.name)

    def republish_all(self):
        for p in self.plugins:
            try:
                p.republish()
            except Exception:  # noqa: BLE001
                log.exception("%s: republish failed", p.name)

    def on_connect(self, client, userdata, flags, rc, *args):
        if rc != 0:
            log.error("MQTT connect failed: rc=%s", rc)
            return
        log.info("Connected to MQTT")
        subs = {self.check_topic, f"{DISCOVERY_PREFIX}/status"}
        for p in self.plugins:
            subs.update(p.subscriptions())
        client.subscribe([(t, 1) for t in sorted(subs)])
        self.publish_discovery()
        client.publish(self.avail_topic, "online", retain=True)
        self.republish_all()
        self.connected.set()

    def is_command(self, topic):
        return topic == self.check_topic or any(p.is_command(topic) for p in self.plugins)

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace").strip()
        if msg.topic == f"{DISCOVERY_PREFIX}/status":
            if payload == "online":  # Home Assistant restarted
                threading.Thread(target=self._after_ha_restart, daemon=True).start()
            return
        if not payload:
            return
        if msg.retain:
            # A retained command would replay on every reconnect: ignore it and clear
            # it from the broker. Only our own command topics are ever cleared.
            log.warning("Ignoring retained message on %s", msg.topic)
            if self.is_command(msg.topic):
                client.publish(msg.topic, "", retain=True)
            return
        if msg.topic == self.check_topic:
            if payload == CHECK_PAYLOAD:
                log.info("Check requested from Home Assistant")
                for p in self.plugins:
                    try:
                        p.on_check()
                    except Exception:  # noqa: BLE001
                        log.exception("%s: check failed", p.name)
            return
        for p in self.plugins:
            try:
                if p.handle(msg.topic, payload):
                    return
            except Exception:  # noqa: BLE001
                log.exception("%s: handling %s failed", p.name, msg.topic)
                return
        log.warning("Ignoring unexpected message on %s", msg.topic)

    def _after_ha_restart(self):
        if self.stop.wait(2):
            return
        self.publish_discovery()
        self.client.publish(self.avail_topic, "online", retain=True)
        self.republish_all()

    # -------------------------------------------------------------- main
    def run(self):
        for p in self.plugins:
            try:
                p.prepare()  # so the first discovery already has everything this host has
            except Exception:  # noqa: BLE001
                log.exception("%s: prepare failed", p.name)
        m = self.cfg["mqtt"]
        while not self.stop.is_set():
            try:
                # A shorter keepalive makes the broker notice a host that lost power sooner.
                self.client.connect(m["host"], int(m.get("port", 1883)), keepalive=int(m.get("keepalive", 60)))
                break
            except Exception as e:  # noqa: BLE001
                log.warning("MQTT connect error (%s); retrying in 15s", e)
                self.stop.wait(15)
        self.client.loop_start()
        self.connected.wait(30)
        for p in self.plugins:
            p.start()
        while not self.stop.wait(1):
            pass
        for p in self.plugins:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                log.exception("%s: stop failed", p.name)
        try:
            self.save_state()
        except Exception:  # noqa: BLE001
            log.exception("Saving state on exit failed")
        self.client.publish(self.avail_topic, "offline", retain=True).wait_for_publish(5)
        self.client.loop_stop()
        self.client.disconnect()

    def shutdown(self, *_):
        self.stop.set()

    def request_restart(self):
        """Stop cleanly with exit code 3; systemd (Restart=always) starts us again."""
        log.info("Restarting to finish the update")
        self.exit_code = 3
        self.stop.set()
