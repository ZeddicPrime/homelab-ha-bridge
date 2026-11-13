# homelab-ha-bridge

One small Python service per homelab host that puts the host into Home Assistant
over MQTT discovery. For Docker Compose services: an update entity with an Install
button, a running sensor, a power switch and a restart button for each service.
Plugins add applications that run outside Docker or need more than that (Jellyfin,
Foundry VTT, Anthias signage on a Raspberry Pi, Immich). The bridge updates itself
the same way: new releases of this repo show up in Home Assistant as an update you
install with a click.

It runs as root on the host itself (a Proxmox LXC, a VM, a Raspberry Pi), not in
Docker, and needs only Python 3.11+, `python3-paho-mqtt` (1.6 or 2.x) and
`python3-yaml`.

## What Home Assistant gets

One device per host (`<name>` is the host name you give the installer, e.g. `media`):

| Entity | What it does |
| --- | --- |
| `update.<name>_<key>` | Installed vs. latest image. Install pulls and recreates that one service, and rolls back to the previous image if it won't stay up. |
| `binary_sensor.<name>_<key>_running` | Running / stopped, with health, restart count and exit code as attributes |
| `switch.<name>_<key>` | Start / stop the container |
| `button.<name>_<key>_restart` | Restart the container |
| `binary_sensor.<name>_<key>_connected` | Cloudflare Tunnel connectors only: edge connections from cloudflared's `/ready` |
| `button.<name>_check_for_updates` | Check everything now |
| `update.<name>_ha_bridge` | This bridge: installed vs. the newest release here |
| `update.<name>_os_packages` | Pending apt package updates (installed: the release, e.g. Debian 12.8; latest: "N updates"). Install runs a fixed `apt-get --with-new-pkgs upgrade`. Hosts with apt only |
| `binary_sensor.<name>_reboot_required` | The system asked for a reboot, or (outside a container) a newer kernel is installed |
| `sensor.<name>_disk_used` | Percent used of `/` (and of any other `os.disk_paths`) |
| `binary_sensor.<name>_disk_almost_full` | A disk at or above `os.disk_warn_percent` (90) |

`<key>` is the Compose folder name under the compose root (`/opt/<name>/<folder>/`), or
`<folder>_<service>` for a folder with several services. A folder named like the host
(`/opt/<name>/<name>/`) gives bare service keys.

Versions come from the `org.opencontainers.image.version` label (or the image digest when
there is none), and release notes link to that version's own GitHub release page.

## Plugins

Turned on per host with the config `plugins` list (or, without one, by the config having
that plugin's section). Each describes how an application works, never how a particular
install uses it: anything that varies is found at runtime or set in the host's config.

| Plugin | For | Adds |
| --- | --- | --- |
| `compose` | Docker Compose services under the compose root | the entities above |
| `immich` | Immich (with a `compose` group) | sign-ins, signed-in devices, library size, job queues |
| `jellyfin` | Jellyfin installed as a package | server, streams and transcodes, library counts and scan, restart, GPU load, OS updates, and an update entity that runs the host's update command (e.g. a community script's `update`) |
| `foundry` | Foundry VTT | a "Foundry VTT" device: server, world, players, logins (event), backups, core / game-system / module updates (optional module groups), launch world and return to setup |
| `anthias` | Anthias on a Raspberry Pi | online, health (undervoltage, storage, CEC, Anthias web), CPU temperature, projector standby/wake, next clip, restart viewer, reboot, shut down, and a read-only Anthias update entity |
| `os` | every host, unless the config says `"os": false` | OS packages (update entity with Install), reboot required, disk used, disk almost full |
| `selfupdate` | every host | the "Bridge" update entity |

### OS package updates

Install finishes an interrupted dpkg run, runs `apt-get update`, then
`apt-get -y --with-new-pkgs upgrade` with `--force-confdef --force-confold`: it never removes
packages, keeps your edited config files, waits for apt locks, and tells needrestart to list
rather than restart services. apt runs in its own session, so a restart of the bridge doesn't
stop it. The update's summary lists the packages, says how many are security updates, and warns
when Docker itself is among them (every container restarts). Nothing installs on its own.

Only one install runs on a host at a time: OS packages, a container update or a bridge update
started while another is running is refused and says why. On a Pi, Shut down and Reboot wait
for a running install to finish (up to `anthias.busy_wait_minutes`, 45) and then go ahead.

### Hosts that are switched off

A Pi on the same smart plug as its display is off most of the time. The `anthias` plugin
keeps health and history (and the Anthias update, and the `os` plugin's entities) at their last value while it's off, and
shows **Online** off; only live values and buttons go unavailable. Shut down powers the Pi
off cleanly before the plug is cut. Set `mqtt.keepalive` to 15 or so, so that a Pi that
loses power without shutting down shows offline within about 20 seconds.

The bridge's own update is made for this too: every file is on disk before it switches
releases, a power cut at any point leaves the old release or the new one, and a power cut
right after an update doesn't count as a failed start. A check that fails just after
power-on (no network or clock yet) is retried ten minutes later.

## Rules it follows

- **Click to install.** Nothing updates on its own. A person presses Install in Home Assistant.
- **Stable releases only.** A version that looks like a pre-release (`rc`, `beta`, `dev`, `nightly`, ...)
  is reported in the attributes but never offered or installed, unless its key is in `allow_prereleases`.
- **Fixed commands.** Only exact payloads (`INSTALL`, `ON`/`OFF`, `RESTART`, `CHECK`) on a known
  service's own command topic do anything. Nothing from a message is ever run.
- **Retained commands are ignored** and cleared from the broker, so nothing replays on reconnect.
- **Entity IDs never change** between versions. The installer checks this before switching.
- **Groups update together** (config `groups`): e.g. an app server and its worker always get the same
  version, after an optional database dump, and only when the release's `docker-compose.yml`
  doesn't change settings your compose file lacks (new environment variables, images, services,
  devices, healthchecks, ...).

## Install

On the host, as root (replace the version and name):

```sh
VER=0.1.0
cd /root
curl -fsSLO https://github.com/ZeddicPrime/homelab-ha-bridge/releases/download/v$VER/homelab-ha-bridge-$VER.tar.gz
curl -fsSLO https://github.com/ZeddicPrime/homelab-ha-bridge/releases/download/v$VER/SHA256SUMS
sha256sum -c SHA256SUMS && tar xzf homelab-ha-bridge-$VER.tar.gz
bash homelab-ha-bridge-$VER/install.sh --name media --device-name "Media Host"
```

The installer asks for the MQTT login the first time and writes `/etc/<name>-ha/config.json`
(mode 600). After that, updates come from Home Assistant.

Running it again (to repair or rebuild a host), or running it where another program already
publishes this host's entities, keeps the existing config and state and first compares with the
MQTT broker: every entity Home Assistant has from this host must still be published with the same
unique ID and device. If one wouldn't, it stops and changes nothing (`--force` overrides). You can
run the same comparison yourself:

```sh
python3 -I homelab-ha-bridge-$VER/bridge/bridge.py --name media --check-discovery
```

## How self-update works

1. The bridge checks this repo's latest release every 6 hours (and on Check for updates).
2. Install downloads `homelab-ha-bridge-<version>.tar.gz` and `SHA256SUMS`, checks the checksum,
   unpacks it next to the running release, installs any new apt packages it lists, and runs
   its self-test against the host's config. Any failure leaves everything as it was.
3. It points `/opt/homelab-ha-bridge/current` at the new release and exits; systemd starts it again.
4. The new version confirms itself after a minute connected. If it fails to start three times in
   one boot, `guard.py` (run by systemd before every start) puts the previous release back, and
   that version reports the failed update in Home Assistant.
5. A bridge update isn't started while a plugin is installing something (a Jellyfin update, a
   Compose install), because the restart would cut it short.

```
/opt/homelab-ha-bridge/
  current -> releases/0.1.1
  releases/0.1.0/  releases/0.1.1/     (the newest three are kept)
  guard.py
/etc/<name>-ha/config.json             (yours; never touched by updates)
/var/log/<name>-ha/                     (state.json, last-update logs)
```

## Config

`/etc/<name>-ha/config.json`. Only `mqtt.host` is required.

| Key | Default | Meaning |
| --- | --- | --- |
| `mqtt` | | `host`, `port` (1883), `username`, `password`, `client_id` (`<name>-ha`), `keepalive` (60) |
| `compose_root` | `/opt/<name>` | Folder whose subfolders hold compose files |
| `device_name`, `device_model` | `<Name> Host`, `Docker Compose host` | The Home Assistant device |
| `device_id`, `device_manufacturer`, `device_url` | `<name>_lxc`, `Proxmox LXC`, none | More of the device (the `anthias` plugin defaults to `<name>_pi` and the Pi's model) |
| `topic_base`, `availability_topic` | `<name>`, `<base>/availability` | Where this host's topics live, for entities that already exist under other topics |
| `check_interval_hours` | 6 | Registry checks |
| `status_interval_seconds` | 30 | Running / health polling |
| `timeout_minutes`, `settle_seconds` | 15, 10 | Install limits; how long a new container must stay up |
| `prune_after_update` | true | `docker image prune -f` after a good update |
| `release_urls` | `{}` | Release-notes link per key, for images without a source label |
| `titles` | `{}` | Display name per key |
| `version_prefix` | `""` | Added to label versions that lack it (`2.10.7` → `v2.10.7`) |
| `allow_prereleases` | `[]` | Keys (or group keys) that may be offered pre-releases |
| `groups` | `{}` | `{"app": {"title": "...", "members": ["server", "worker"], "health_of": "server", "compose_check_url": ".../{version}/docker-compose.yml"}}` |
| `db_dump` | none | For groups: `{"container": "...", "user": "postgres", "dir": "...", "keep": 3}` |
| `plugins` | `["compose"]` | See Plugins; each plugin's own settings go in a section named after it (`immich`, `jellyfin`, `foundry`, `anthias`), described at the top of its module in `bridge/habridge/` |
| `os` | on | `false` to turn the OS plugin off; or `{"check_hours": 6, "disk_paths": ["/"], "disk_warn_percent": 90, "timeout_minutes": 60}` |
| `self_update` | on | `{"enabled": false}` to turn it off; `check_interval_hours` |

## Development

```sh
sudo apt-get install mosquitto python3-paho-mqtt python3-yaml
python3 -m unittest tests/test_units.py
python3 tests/test_e2e_compose.py      # compose + groups + cloudflared + Immich, against fakes
python3 tests/test_e2e_selfupdate.py   # self-update, checksum, rollback
python3 tests/test_e2e_apps.py         # Jellyfin, Foundry VTT, Anthias, --check-discovery
python3 tests/test_immich_monitor.py
python3 tools/privacy_check.py         # nothing that identifies a particular home or network
```

The tests need no Docker, registry or Home Assistant: `tests/fakes/` has a Docker CLI shim, an OCI
registry, and fake GitHub, Immich, Jellyfin, Foundry and Anthias APIs. See [CLAUDE.md](CLAUDE.md) for the release process.

## License

MIT
