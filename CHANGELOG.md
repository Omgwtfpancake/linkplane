# Changelog

All notable user-facing changes to Linkplane. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow `docs/versioning.md`
(package SemVer; engineering milestones are Git tags such as `onboarding-v0.1` and are not
listed here).

## [Unreleased]

In development for v0.6 on the `dev/v0.6` branch; not released.

### Added
- Automatic camera-photo backup preset: `linkplane automations presets|enable|disable
  photo-backup` writes an ordinary rule that backs up a phone's camera folder
  (`/sdcard/DCIM/Camera` only) whenever it connects into `~/Pictures/Linkplane/<device>`,
  notifies the desktop when new files arrived, and notifies once when a run fails.
  `linkplane setup` offers it once for a newly registered phone (default: no); phones set up
  with v0.5.x turn it on with `linkplane automations enable photo-backup`. `linkplane status`
  shows its state and last run. USB only: it does not yet follow a phone onto wireless ADB.
- Rule steps may carry their own `if` condition over earlier results; rules may carry
  `on_error` steps that run once when a step fails.

### Fixed
- A phone plugged in after the daemon started was reported as an `initial` observation, so
  `device.connected` rules without `on_initial` missed the first plug-in after login. Only
  devices present when observation starts are initial now.

### Changed
- `backup` lists the phone folder with one `adb shell` call instead of one per file: on a
  652-file camera folder, listing went from about 30 s to about 0.1 s, and a nothing-new
  automatic backup from 27 s to 2 s. Phones whose toybox lacks `find -printf` use the old
  per-file listing.
- `backup` checks free space before downloading (`LP-STORAGE-001`), never overwrites a file
  in the destination that it did not create, and refuses to write through a symbolic link
  inside the destination.

## [0.5.1] — 2026-09-13

First-run onboarding polish from alpha tester #1's feedback (GitHub pre-release).
See `docs/releases/v0.5.1.md`.

### Changed
- Setup's completion screen suggests the phone notification first, then `status`, then a
  complete, copy-pasteable file send (it used to print `linkplane send <file>`, which
  alpha tester #1 ran as `linkplane send` and got an argument error).
- Installation instructions list one operating system per section and say to run only
  one; `pipx ensurepath` is advised only when `linkplane` is not found afterwards.

## [0.5.0] — 2026-09-13

First public alpha (GitHub pre-release). See `docs/releases/v0.5.0.md`.

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
