"""Fast checks of the pure helpers (no broker, no fakes). Run: python3 -m unittest tests/test_units.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))

from habridge.compose import compose_changes, metrics_address  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
