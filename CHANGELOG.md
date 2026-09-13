# Changelog

All notable user-facing changes to Linkplane. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow `docs/versioning.md`
(package SemVer; engineering milestones are Git tags such as `onboarding-v0.1` and are not
listed here).

## [Unreleased]

Nothing yet.

## [0.5.0] — unreleased draft

First public alpha. See `docs/releases/v0.5.0.md`.

### Added
- `linkplane setup`: guided first run — dependency check, USB Android onboarding with
  authorization guidance, device registration, systemd user service install and start,
  observation and first-use verification; idempotent; `--dry-run`, `--json`.
- `linkplane uninstall` and `--purge`: remove the service and runtime, optionally
  Linkplane's own configuration and state directories, never user files or backups.
- Central dependency catalogue behind `doctor` and `setup`; `doctor` reports USB access,
  configuration permissions and desktop-notification availability.
- Stable codes `LP-AUTH-003` (Linux USB permission, distinct from the phone's
  authorization) and `LP-CONFIG-004` (configuration could not be written).
- Daemon status reports its package version; `setup` restarts a stale daemon.
- Public documentation: README quick start, `docs/install.md`, `docs/troubleshooting.md`,
  `docs/cli-reference.md`, clean-machine procedure and run records.

### Changed
- Pairing is strict about identity: a profile name never silently absorbs a different phone.
- Stream shutdown: every SSE and socket subscriber receives `observer.stopped` before the
  daemon closes the stream.

### Fixed
- `setup` starts an installed-but-inactive service instead of reporting it dead.
- `adb devices` `no permissions` state is no longer misreported as "phone not authorized".

### Known issues
- `status` appends SSH hints for a USB-only profile when the phone is unplugged.
- Clipboard and camera capture need Termux:API via the SSH provider.
