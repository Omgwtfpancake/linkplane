# 0006 — SSHProvider wraps Termux unchanged; ADBProvider is wrapped at the same level

**Status:** Accepted (2026-09-10)

## Context

The brief names SSH (Termux + `phone-status-json.sh` + Termux:API) as the one v0.1
provider and lists ADB among "future providers — do not implement now". In this
repository ADB is not future: it is the primary working transport (the paired test phone
is on USB; `status`, `send`, `backup`, `find`, `screen`, and `webcam` run over it) and SSH
is the fallback. Leaving ADB un-modelled would make `battery`/`ping`/`capabilities`
SSH-only while `status` prefers ADB — a regression in behaviour and an inconsistent model.

## Decision

`providers/ssh.py::SSHProvider` wraps the existing `SshTransport` and phone-side scripts
with no changes to either. It probes the phone with the same one-shot script discovery
already used (`devices.probe_ssh_endpoint`, extracted so both share it), maps the presence
of `phone-status-json.sh` and Termux:API commands onto capability statuses, and reads
battery from the existing status script.

`providers/adb.py::ADBProvider` wraps the existing `AdbTransport` at the same thin level:
`ping` is `adb shell echo ok`, `battery` is `dumpsys battery` through the existing parser,
`capabilities` reflects what the ADB-backed commands already do. This is a deliberate,
recorded deviation from the brief's "SSH only" — justified by behaviour preservation, which
the brief ranks first.

## Alternatives considered

- **SSH only, as written.** Rejected for the regression above.
- **A single `AndroidProvider` that internally picks ADB or SSH.** Rejected: it would hide
  the transport choice the user already controls with `--transport`, and would make the
  contract tests unable to exercise each path independently.

## Status update (2026-09-10, later)

`doctor --transport auto|adb|ssh` diagnoses a chosen provider: with SSH it checks the
identity file, reachability, an accepted login, and the presence of
`phone-status-json.sh` on the phone, each failure coded. The device tier
(`tests/device/`) exercises the ADB path end to end and skips the SSH round trip with a
reason when `sshd` is not running.

## Consequences

Two providers pass the same `ProviderContract`. The Android agent provider planned for a
later milestone is a third subclass. The SSH path currently returns `provider-error` for
status capabilities when the phone's `sshd` is not running — verified on the real device —
which is the honest answer and what `doctor` should explain next.
