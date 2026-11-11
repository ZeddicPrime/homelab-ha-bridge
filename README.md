# homelab-ha-bridge

One small Python service per homelab host that puts the host's Docker Compose
services into Home Assistant over MQTT discovery: an update entity with an
Install button, a running sensor, a power switch and a restart button for each
service. The bridge updates itself the same way: new releases of this repo show
up in Home Assistant as an update you install with a click.

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

`<key>` is the Compose folder name under the compose root (`/opt/<name>/<folder>/`), or
`<folder>_<service>` for a folder with several services. A folder named like the host
(`/opt/<name>/<name>/`) gives bare service keys.

Versions come from the `org.opencontainers.image.version` label (or the image digest when
there is none), and release notes link to that version's own GitHub release page.

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

### Upgrading from a single-file `<name>-ha` bridge

The same command upgrades a host that runs an older single-file bridge
(`/opt/<name>-ha/<name>_ha.py`). It keeps the config and state, adds `--device-name` /
`--device-model` only if the config has none, and first checks that every existing entity keeps
its ID (it stops and changes nothing if one wouldn't; `--force` overrides). The old program
stays where it was, and the installer prints the command to go back to it.

## How self-update works

1. The bridge checks this repo's latest release every 6 hours (and on Check for updates).
2. Install downloads `homelab-ha-bridge-<version>.tar.gz` and `SHA256SUMS`, checks the checksum,
   unpacks it next to the running release, installs any new apt packages it lists, and runs
   its self-test against the host's config. Any failure leaves everything as it was.
3. It points `/opt/homelab-ha-bridge/current` at the new release and exits; systemd starts it again.
4. The new version confirms itself after a minute connected. If it fails to start three times,
   `guard.py` (run by systemd before every start) puts the previous release back, and that
   version reports the failed update in Home Assistant.

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
| `mqtt` | | `host`, `port` (1883), `username`, `password`, `client_id` (`<name>-ha`) |
| `compose_root` | `/opt/<name>` | Folder whose subfolders hold compose files |
| `device_name`, `device_model` | `<Name> Host`, `Docker Compose host` | The Home Assistant device |
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
| `plugins` | `["compose"]` | Add `"immich"` (with an `immich` section) for Immich sign-in, library and job entities |
| `self_update` | on | `{"enabled": false}` to turn it off; `check_interval_hours` |

## Development

```sh
sudo apt-get install mosquitto python3-paho-mqtt python3-yaml
python3 -m unittest tests/test_units.py
python3 tests/test_e2e_compose.py      # compose + groups + cloudflared + Immich, against fakes
python3 tests/test_e2e_selfupdate.py   # self-update, checksum, rollback
python3 tests/test_immich_monitor.py
python3 tools/privacy_check.py         # nothing that identifies a particular home or network
```

The tests need no Docker, registry or Home Assistant: `tests/fakes/` has a Docker CLI shim, an OCI
registry, and fake GitHub and Immich APIs. See [CLAUDE.md](CLAUDE.md) for the release process.

## License

MIT
