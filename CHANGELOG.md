# Changelog

Bridges show the section for a release as its release notes in Home Assistant, so the
first lines of each section should say what changes for someone pressing Install.

## 0.3.1

Fixes a lock-up: a bridge could stop updating Home Assistant (and not stop cleanly)
when two parts of it saved their state at the same moment. On an Immich host this was
rare before 0.3.0 and much more likely with it. Install this on every host that has 0.3.0.

## 0.3.0

OS updates from Home Assistant. Every host gets four new entities; nothing existing changes.

- **OS packages** (update entity): pending apt updates, with how many are security updates.
  Install runs a fixed `apt-get --with-new-pkgs upgrade`: no package removals, your edited
  config files are kept, and an interrupted earlier install is finished first. It warns when
  Docker itself is in the list (every container restarts). Nothing installs on its own.
- **Reboot required**: the system asked for one, or a newer kernel is installed (outside containers).
- **Disk used** and **Disk almost full** (90% by default).
- **One install at a time per host**: OS packages, container updates and bridge updates
  refuse to start while another is running, and say why.
- **Pis**: Shut down and Reboot wait for a running install to finish, then go ahead.
- `"os": false` in a host's config turns the new entities off.

## 0.2.0

New plugins for hosts that aren't plain Docker Compose hosts, and a bridge update that is
safe on hosts that lose power. Nothing changes on existing hosts except the points below.

- **Jellyfin plugin**: server, streams and transcodes, library counts and scan, restart,
  GPU load, pending OS updates, and an update entity that runs the host's update command.
- **Foundry VTT plugin**: a "Foundry VTT" device with server, world, players, login events,
  backups, and core / game-system / module update checks (game systems are the ones your
  worlds use; optional module groups), plus Launch world and Return to setup.
- **Anthias plugin** for a Raspberry Pi that's switched off most of the time: health keeps
  its last value while it's off, Online follows the last will, Shut down powers it off cleanly.
- **Power-cut safe bridge updates**: every file is on disk before the switch, a power cut
  right after an update no longer counts as a failed start, and a check that fails just
  after power-on is retried in ten minutes instead of six hours.
- **No bridge update while something else installs**: Install is refused (and says why)
  while a Compose or Jellyfin install is running.
- **Installer compares with the broker**: before replacing a service it checks that every
  entity Home Assistant has from the host keeps its unique ID and device
  (`bridge.py --check-discovery` runs the same check).
- New config keys, all optional: `topic_base`, `availability_topic`, `device_id`,
  `device_manufacturer`, `device_url`, `mqtt.keepalive`.

## 0.1.0

First release.

- **Docker Compose hosts**: an update entity with Install, a running sensor, a power switch
  and a restart button for every Compose service; rollback to the previous image if a new one
  won't stay up. Cloudflare Tunnel connectors also get real version numbers and a connected sensor.
- **Groups**: services that update together, with an optional database dump first and a
  check of the release's `docker-compose.yml`, setting by setting (environment variables,
  devices, healthchecks, services, images). Install is refused until the local compose file
  has the change.
- **Immich plugin**: sign-ins, signed-in devices, library size and job queues.
- **Self-update**: a "Bridge" update entity on every host installs new releases of this
  repo with a click; checksums verified, self-tested first, rolled back automatically if
  the new version won't start.
- **Stable releases only**: pre-releases are held back, and Install re-checks first, so a
  tag that moved to a pre-release since the last check is never pulled.
- Retained commands are ignored and cleared from the broker; only fixed payloads act.
- The installer checks that every existing entity keeps its ID before replacing a service.
