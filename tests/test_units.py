"""Fast checks of the pure helpers (no broker, no fakes). Run: python3 -m unittest tests/test_units.py"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))

from habridge.compose import compose_changes, metrics_address  # noqa: E402
from habridge.hostos import disk_usage, failure_hint, kernel_reboot_reason, os_label, parse_simulation  # noqa: E402
from habridge.registry import parse_ref  # noqa: E402
from habridge.util import is_prerelease, label_version, release_url, slug, version_tuple  # noqa: E402

SRC = {"org.opencontainers.image.source": "https://github.com/example/app"}


class Versions(unittest.TestCase):
    def test_prerelease(self):
        for v in ("v3.4.0-rc.1", "2.0.0beta2", "1.2-dev", "v2.11.0-preview21", "2026.11.0-dev3", "1.0.0-alpha",
                  "v1.0.0-RC1", "4.0.0.next", "1.0.0+nightly.5"):
            self.assertTrue(is_prerelease(v), v)
        for v in ("v3.3.6", "2026.10.7", "9.0.1", "", None, "latest", "0f3a9c1b2d4e", "ab12cd34ef56",
                  "v2.10.7 (2ccf6006d518)", "devices"):
            self.assertFalse(is_prerelease(v), v)

    def test_label_version_prefix(self):
        self.assertEqual(label_version({"org.opencontainers.image.version": "2.10.7"}, "v"), "v2.10.7")
        self.assertEqual(label_version({"org.opencontainers.image.version": "v2.10.7"}, "v"), "v2.10.7")
        self.assertIsNone(label_version({"org.opencontainers.image.version": "latest"}))
        self.assertIsNone(label_version({}))

    def test_release_url(self):
        self.assertEqual(release_url(SRC, "v1.2.3"), "https://github.com/example/app/releases/tag/v1.2.3")
        self.assertEqual(release_url(SRC, "nightly-abc"), "https://github.com/example/app/releases")
        self.assertEqual(release_url({"org.opencontainers.image.source": "https://gitlab.com/x/y.git"}), "https://gitlab.com/x/y")
        self.assertIsNone(release_url({}))

    def test_version_tuple(self):
        self.assertGreater(version_tuple("2.0.10"), version_tuple("v2.0.9"))
        self.assertEqual(version_tuple("2.0.0-rc.1"), ())


class Refs(unittest.TestCase):
    def test_parse_ref(self):
        self.assertEqual(parse_ref("redis:7"), ("docker.io", "library/redis", "7", "redis"))
        self.assertEqual(parse_ref("ghcr.io/a/b"), ("ghcr.io", "a/b", "latest", "ghcr.io/a/b"))
        self.assertEqual(parse_ref("localhost:5000/x/y:1@sha256:ab"), ("localhost:5000", "x/y", "1", "localhost:5000/x/y"))

    def test_slug_never_has_hyphens(self):
        self.assertEqual(slug("Network-Optimizer"), "network_optimizer")
        self.assertNotIn("-", slug("bridge-update"))

    def test_metrics_address(self):
        self.assertEqual(metrics_address(["tunnel", "--metrics", "0.0.0.0:2000", "run"]), "127.0.0.1:2000")
        self.assertEqual(metrics_address("tunnel --metrics=198.51.100.5:2000 run"), "198.51.100.5:2000")
        self.assertEqual(metrics_address([]), "127.0.0.1:20241")


def svc(image, **kw):
    return {"image": image, **kw}


class ComposeCheck(unittest.TestCase):
    T = "app:${VERSION:-release}"

    def test_unchanged_release_is_clean(self):
        old = {"server": svc(self.T, environment=["A=1"]), "db": svc("pg:14@sha256:aa")}
        local = {"server": svc("app:release", environment={"A": "1"}), "db": svc("pg:14@sha256:aa")}
        self.assertEqual([i for i in compose_changes(old, old, local) if i[1] != "ok"], [])

    def test_new_env_var(self):
        old = {"ml": svc(self.T)}
        new = {"ml": svc(self.T, environment={"NEW": "1"})}
        self.assertIn(("ml: new environment variable NEW", "missing"), compose_changes(old, new, {"ml": svc("app:r")}))
        self.assertIn(("ml: NEW changed", "ok"),
                      compose_changes(old, new, {"ml": svc("app:r", environment={"NEW": "1"})}))

    def test_changed_literal_env_value(self):
        old = {"ml": svc(self.T, environment={"WORKERS": "1"})}
        new = {"ml": svc(self.T, environment={"WORKERS": "2"})}
        self.assertIn(("ml: WORKERS changed to 2", "missing"),
                      compose_changes(old, new, {"ml": svc("x", environment={"WORKERS": "1"})}))

    def test_templated_env_value_only_needs_the_name(self):
        old = {"ml": svc(self.T, environment={"P": "${P:-1}"})}
        new = {"ml": svc(self.T, environment={"P": "${P:-2}"})}
        self.assertIn(("ml: P changed", "ok"), compose_changes(old, new, {"ml": svc("x", environment={"P": "1"})}))

    def test_pinned_image_and_services(self):
        old = {"server": svc(self.T), "db": svc("pg:14@sha256:aa")}
        new = {"server": svc(self.T), "db": svc("pg:16@sha256:bb"), "cache": svc("valkey:9")}
        local = {"server": svc("x"), "db": svc("pg:14@sha256:aa"), "old": svc("y")}
        got = dict(compose_changes(old, new, local))
        self.assertEqual(got["db image pg:14 → pg:16"], "missing")
        self.assertEqual(got["new service cache"], "missing")
        self.assertEqual(got["service old removed"], "missing")

    def test_other_settings_need_review(self):
        old = {"ml": svc(self.T, devices=["/dev/a"])}
        new = {"ml": svc(self.T, devices=["/dev/a", "/dev/b"], healthcheck={"test": "x"})}
        got = dict(compose_changes(old, new, {"ml": svc("x", devices=["/dev/a"])}))
        self.assertEqual(got["ml: devices changed"], "review")
        self.assertEqual(got["ml: new setting healthcheck"], "missing")

    def test_without_old_release_new_vars_are_still_caught(self):
        new = {"ml": svc(self.T, environment={"NEW": "1"})}
        self.assertIn(("ml: new environment variable NEW", "missing"), compose_changes(None, new, {"ml": svc("x")}))
        self.assertEqual(compose_changes(None, new, {"ml": svc("x", environment={"NEW": "1"})}), [])


class Guard(unittest.TestCase):
    """guard.py counts failed starts per boot: a power cut is not a failed start."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        os.makedirs(f"{self.d}/releases/old")
        os.makedirs(f"{self.d}/releases/new")
        os.symlink(f"{self.d}/releases/new", f"{self.d}/current")
        self.marker = f"{self.d}/self-update.json"
        with open(self.marker, "w") as f:
            json.dump({"from_version": "1", "from_path": f"{self.d}/releases/old", "to_version": "2",
                       "attempts": 0}, f)

    def start(self, boot):
        import subprocess
        guard = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge", "guard.py")
        subprocess.run([sys.executable, "-I", guard, "--name", "x", "--log-dir", self.d, "--root", self.d],
                       env=dict(os.environ, HABRIDGE_BOOT_ID=boot), check=True, capture_output=True)
        with open(self.marker) as f:
            return json.load(f)

    def test_three_failed_starts_in_one_boot_roll_back(self):
        for _ in range(3):
            self.assertFalse(self.start("a").get("rolled_back"))
        m = self.start("a")
        self.assertTrue(m.get("rolled_back"))
        self.assertEqual(os.path.realpath(f"{self.d}/current"), os.path.realpath(f"{self.d}/releases/old"))

    def test_power_cuts_do_not_count(self):
        for boot in ("a", "b", "c", "d", "e"):  # started once per boot, then the power went
            m = self.start(boot)
            self.assertEqual(m["attempts"], 1)
            self.assertFalse(m.get("rolled_back"))
        self.assertEqual(os.path.realpath(f"{self.d}/current"), os.path.realpath(f"{self.d}/releases/new"))

    def test_count_starts_again_after_a_reboot(self):
        self.start("a"); self.start("a")
        self.assertEqual(self.start("b")["attempts"], 1)


class Busy(unittest.TestCase):
    def test_bridge_reports_the_first_busy_plugin(self):
        from habridge.core import Bridge, Plugin

        class P(Plugin):
            def __init__(self, why):
                self.why = why

            def busy_reason(self):
                return self.why

        b = Bridge.__new__(Bridge)
        b.plugins = [P(None), P("Jellyfin update"), P("other")]
        self.assertEqual(b.busy_reason(), "Jellyfin update")
        b.plugins = [P(None)]
        self.assertIsNone(b.busy_reason())


class SaveState(unittest.TestCase):
    def make(self):
        import tempfile
        import threading
        from habridge.core import Bridge, Plugin

        class P(Plugin):
            def __init__(self, key):
                self.key, self.lock, self.value = key, threading.RLock(), 0

            def snapshot(self):
                with self.lock:
                    return {self.key: self.value}

        d = tempfile.mkdtemp()
        b = Bridge({"mqtt": {"host": "127.0.0.1"}}, "box", os.path.join(d, "c.json"), d)
        b.plugins = [P("a"), P("b")]
        return b

    def test_plugin_saving_under_its_own_lock_cannot_deadlock(self):
        # A plugin that saves while holding its lock (a log follower does) and another
        # thread saving at the same moment used to wait on each other forever.
        import threading
        b = self.make()
        a = b.plugins[0]
        in_lock, done = threading.Event(), []

        def follower():
            with a.lock:
                a.value = 1
                in_lock.set()
                time.sleep(0.3)  # the other save starts meanwhile and needs a.lock
                b.save_state()
            done.append("follower")

        def other():
            in_lock.wait()
            b.save_state()
            done.append("other")

        ts = [threading.Thread(target=follower, daemon=True), threading.Thread(target=other, daemon=True)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual(sorted(done), ["follower", "other"])
        with open(b.state_file) as f:
            self.assertEqual(json.load(f)["a"], 1)

    def test_an_older_snapshot_never_overwrites_a_newer_one(self):
        b = self.make()
        b.plugins[1].value = 5
        b.saved_seq = 2          # a newer save was written ...
        b.save_seq = 0           # ... so this one (seq 1, snapshot taken earlier) is skipped
        b.save_state()
        self.assertFalse(os.path.exists(b.state_file))
        b.save_seq = 5
        b.save_state()
        with open(b.state_file) as f:
            self.assertEqual(json.load(f)["b"], 5)



SIM = """NOTE: This is only a simulation!
Reading package lists...
The following packages have been kept back:
  linux-image-amd64 somepkg
The following packages will be upgraded:
  libssl3 openssl tzdata
Inst libssl3 [3.0.14-1~deb12u2] (3.0.15-1~deb12u1 Debian-Security:12/stable-security [amd64])
Inst openssl [3.0.14-1~deb12u2] (3.0.15-1~deb12u1 Debian-Security:12/stable-security [amd64])
Inst tzdata [2024a-0+deb12u1] (2024b-0+deb12u1 Debian:12.8/stable [all])
Inst newdep (1.0-1 Debian:12.8/stable [amd64])
Conf libssl3 (3.0.15-1~deb12u1 Debian-Security:12/stable-security [amd64])
"""


class HostOS(unittest.TestCase):
    def test_parse_simulation(self):
        pkgs, kept = parse_simulation(SIM)
        self.assertEqual([p["name"] for p in pkgs], ["libssl3", "openssl", "tzdata", "newdep"])
        self.assertEqual([p["security"] for p in pkgs], [True, True, False, False])
        self.assertEqual(pkgs[0]["from"], "3.0.14-1~deb12u2")
        self.assertEqual(pkgs[0]["to"], "3.0.15-1~deb12u1")
        self.assertIsNone(pkgs[3]["from"])
        self.assertEqual(kept, ["linux-image-amd64", "somepkg"])
        self.assertEqual(parse_simulation(""), ([], []))

    def test_os_label(self):
        import tempfile
        d = tempfile.mkdtemp()
        osr, dv = os.path.join(d, "os-release"), os.path.join(d, "debian_version")
        with open(osr, "w") as f:
            f.write('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\nID=debian\n')
        with open(dv, "w") as f:
            f.write("12.8\n")
        self.assertEqual(os_label(osr, dv), "Debian 12.8")
        with open(dv, "w") as f:
            f.write("trixie/sid\n")
        self.assertEqual(os_label(osr, dv), "Debian GNU/Linux 12")
        with open(osr, "w") as f:
            f.write('NAME="Ubuntu"\nVERSION="24.04.1 LTS (Noble Numbat)"\nID=ubuntu\nVERSION_ID="24.04"\n')
        self.assertEqual(os_label(osr, dv), "Ubuntu 24.04.1")
        self.assertIsNone(os_label(os.path.join(d, "missing"), dv))

    def test_kernel_reboot_reason(self):
        import tempfile
        d = tempfile.mkdtemp()
        for k in ("6.1.0-27-amd64", "6.6.51+rpt-rpi-v8", "6.6.51+rpt-rpi-2712"):
            os.makedirs(os.path.join(d, k))
        self.assertIsNone(kernel_reboot_reason("6.1.0-27-amd64", d))
        self.assertIsNone(kernel_reboot_reason("6.6.51+rpt-rpi-2712", d))
        os.makedirs(os.path.join(d, "6.1.0-28-amd64"))
        self.assertIn("6.1.0-28-amd64", kernel_reboot_reason("6.1.0-27-amd64", d))
        os.makedirs(os.path.join(d, "6.6.62+rpt-rpi-2712"))
        self.assertIn("6.6.62+rpt-rpi-2712", kernel_reboot_reason("6.6.51+rpt-rpi-2712", d))
        self.assertIsNone(kernel_reboot_reason("6.6.51+rpt-rpi-v8", d))  # other flavour unchanged
        self.assertIn("no longer installed", kernel_reboot_reason("6.1.0-26-amd64", d))
        self.assertIsNone(kernel_reboot_reason("6.1.0-27-amd64", os.path.join(d, "missing")))

    def test_disk_usage_and_hints(self):
        pct, used, total, free = disk_usage("/")
        self.assertTrue(0 <= pct <= 100 and total > 0)
        self.assertIsNone(disk_usage("/no/such/path"))
        self.assertIn("busy", failure_hint("E: Could not get lock /var/lib/dpkg/lock-frontend"))
        self.assertIn("interrupted", failure_hint("E: dpkg was interrupted, you must manually run"))
        self.assertIsNone(failure_hint("all fine"))

if __name__ == "__main__":
    unittest.main()
