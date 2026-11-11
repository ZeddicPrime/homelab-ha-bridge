# Changelog

Bridges show the section for a release as its release notes in Home Assistant, so the
first lines of each section should say what changes for someone pressing Install.

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
