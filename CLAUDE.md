# Working on homelab-ha-bridge

This repo is public. It runs on real hosts that update themselves from its releases,
so every release reaches every host as an Install button in Home Assistant.

## Never in the repo

Nothing that identifies a particular home, person or network: house or brand names,
domains, private IPs, Proxmox container IDs, hostnames, mount paths, people's names,
email addresses, tokens. Examples and tests use placeholders (`media`, `example.com`,
192.0.2.x / 198.51.100.x / 203.0.113.x). Host-specific values belong in each host's
`/etc/<name>-ha/config.json` or on the installer's command line, never as code defaults.
`python3 tools/privacy_check.py` must pass (CI runs it with a secret denylist).

## Plugins are per application, not per instance

A plugin knows how an application works (Immich, Foundry VTT, Jellyfin, ...), never how this
particular homelab uses it. No module or add-on lists, library or collection names, user names,
storage layouts, hardware choices or URLs of a real install: anything that varies between
installs is discovered at runtime (from the app's API or the host) or read from the host's config.
Code comments and changelog entries describe behaviour, not a particular host's history.

## Compatibility contract

- Entity unique IDs, default entity IDs, MQTT topics and discovery object IDs never change
  for an existing key. New entities may be added.
- Config files written by any earlier version keep working without edits. New keys get
  defaults that reproduce the old behaviour.
- Command topics accept only their fixed payloads; nothing from a message is executed.
- Nothing installs without a person pressing Install. Pre-releases are never offered unless
  a key is listed in `allow_prereleases`.

## Before every commit

```sh
python3 -m unittest tests/test_units.py
python3 tests/test_immich_monitor.py
python3 tests/test_e2e_selfupdate.py
python3 tests/test_e2e_compose.py
python3 tools/privacy_check.py
```

The e2e suites need `mosquitto`, `python3-paho-mqtt` and `python3-yaml`, and ports 18830,
18831, 5055, 18283 and 18284. Run them one at a time; they start their own fakes. A change in
behaviour gets a scenario in the matching e2e suite.

## Releasing

1. Bump `VERSION` in `bridge/habridge/__init__.py` (semver: patch for fixes, minor for new
   entities or plugins). The project is 0.x until it has run on real hosts for a while; 1.0.0
   is a deliberate decision, not a side effect of a big change. The compatibility contract above
   applies during 0.x too.
2. Add a `## <version>` section at the top of `CHANGELOG.md`, first lines written for the
   person deciding whether to press Install.
3. Commit and push to `main`. That commit is the release: the release workflow sees a
   `VERSION` with no tag yet, runs the tests, tags it `v<version>`, builds
   `homelab-ha-bridge-<version>.tar.gz` and `SHA256SUMS` with `tools/build_release.py`, and
   publishes the GitHub release. Every bridge offers it within 6 hours, or at once on Check for
   updates. Don't create tags by hand.
4. A `VERSION` with a suffix (`0.2.0-rc.1`) becomes a pre-release, which bridges never offer.
5. Changes that aren't ready to release go to a branch, not `main`, or leave `VERSION` alone
   (pushing to `main` without a new `VERSION` releases nothing).

## Layout

- `bridge/` is exactly what ships: `bridge.py` (entry), `guard.py` (rollback, standard library
  only, runs before every start), `habridge/` (the package), `systemd/`, `apt-packages.txt`.
- `habridge/core.py`: MQTT connection, device, availability, Check button, safety rules, state.
- Plugins (`habridge/compose.py`, `immich.py`, `selfupdate.py`) subclass `core.Plugin`. A new
  host type is a new plugin, enabled per host with the config `plugins` list.
