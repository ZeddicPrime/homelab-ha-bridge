# Changelog

Bridges show the section for a release as its release notes in Home Assistant, so the
first lines of each section should say what changes for someone pressing Install.

## 0.1.0

First release. One shared program for every host instead of a copy per host. Entity IDs,
topics and config files from the earlier single-file `<name>-ha` bridges are kept as they are.

- **Self-update**: a "Bridge" update entity on every host installs new releases of this
  repo with a click; checksums verified, self-tested first, rolled back automatically if
  the new version won't start.
- **Compose check covers every setting**: a group's release `docker-compose.yml` is compared
  setting by setting (environment variables, devices, healthchecks, services, images), not
  just pinned images. Install is refused until the local compose file has the change.
- Pre-releases are held back on every host (was: some hosts only), and Install re-checks
  first, so a tag that moved to a pre-release since the last check is never pulled.
- Retained commands are cleared from the broker on every host (was: some hosts only).
- Release links go to the exact version's page on every host.
- Fixed: a group's release link could point at a newer release than the one installed
  when a pre-release was held back during a check that was already running.
- Fixed: a folder whose compose file was briefly unreadable could lose its entities on
  hosts that titled it differently from its folder name.
- State is saved through a unique temp file (no rename races between threads).
- The installer checks that every existing entity keeps its ID before switching.
