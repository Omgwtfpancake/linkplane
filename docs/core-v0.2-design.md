# Core v0.2 — Persistent Control Plane: Design

Status: **complete — all three slices built and verified; tagged `core-v0.2`.** Written
2026-09-10 immediately after Core v0.1 was tagged, from `docs/core-v0.1-continuation.md`
"Post-v0.1 Direction". It replaces the runtime half of `docs/automation-design.md`; that
document's event-source research (sections 1–2) is reused here and its automation schema
(sections 3–6) stays paused until v0.2 exists to host it.

## What v0.2 is

The layer that turns a CLI toolkit into a control plane: something that *keeps knowing*
about devices between commands.

```text
CLI ───────────┐
Future GUI ────┤
Future API ────┼── linkplaned
Future AI ─────┤        │
               │        ├── Device Registry        which devices exist (profiles + seen)
               │        ├── Provider Connections   which provider reaches each one now
               │        ├── Observed State         last known state per device
               │        ├── Event Bus              canonical, ordered state changes
               │        ├── Subscriptions          who wants which events
               │        └── Local History          append-only event log
               └──────── Providers ── Devices
```

Everything above already exists in embryo: profiles are the registry's static half,
`select_provider` is a one-shot connection, `status`/`battery`/`ping` are one-shot
observations, `ProgressEvent` is a per-operation event stream, and the automation design
already found the cheap sources (`adb track-devices` is push-based; battery and Wi-Fi are
short polls). v0.2 makes those persistent and canonical.

## Non-goals for v0.2

Automation rules, GUI, API/MCP, remote access, plugins, Android agent, more CLI utility
commands. `linkplane events` and `linkplane daemon ...` are the only new commands.

## Domain model (v0.2 subset of the permanent model)

| Concept | v0.2 shape | Lives in |
|---|---|---|
| Device (registry entry) | `DeviceProfile` for named devices; an unnamed seen serial is registered as `serial:<x>` until paired | `profiles.py` (unchanged) + registry snapshot |
| State | `DeviceState(device, provider, connection, address, battery, wifi_ssid, last_seen, updated)` — one record per device, the last thing observed | `core/state.py` |
| Event | `Event(seq, ts, type, device, provider, data)` — one immutable record per state change | `core/events.py` |
| Provider connection | the `Provider` chosen for a device right now; v0.2 keeps one per device, ADB preferred | daemon |
| Subscription | a filter (`types`, `devices`) plus a sink | daemon |
| History | JSON Lines, append-only, `seq` monotonic per file | `history.py` |

`DeviceState` and `Event` are new additive contracts; nothing pinned changes.

### Event catalogue (v1 of the catalogue; additive afterwards)

| Type | When | `data` |
|---|---|---|
| `observer.started` / `observer.stopped` | the observer (in-process or daemon) starts/stops | `sources`, `interval` |
| `device.connected` / `device.disconnected` | a device appears in / vanishes from the tracker | `address`, `transport` |
| `device.authorized` / `device.unauthorized` | tracker state `unauthorized` ↔ `device` | `address` |
| `battery.changed` | `level` differs from the last observation | `level`, `previous`, `status`, `powered_by` |
| `battery.low` / `battery.ok` | `level` crosses the threshold downward / upward (edge-triggered) | `level`, `threshold` |
| `charging.started` / `charging.stopped` | `powered_by` becomes non-empty / empty | `powered_by` |
| `wifi.connected` / `wifi.disconnected` | SSID appears / disappears / changes | `ssid`, `previous` |

Every event carries `data.initial = true` when it describes an observation rather than a
change: the state found at observer start (the tracker's first snapshot), and the first
telemetry read after a device (re)connects (a device that was away has no battery or Wi-Fi
to compare against). A device that first appears in a later snapshot was connected while
observing: its `device.connected` is a change (`initial` absent), even though the observer
had never seen it before. When `adb track-devices` reports an empty first snapshot because
the ADB server is still enumerating USB devices, a phone that appears a moment later is
likewise reported as a change. The
`diff(previous, current) -> [Event]` function that produces these is pure and fully
unit-tested; sources only produce `DeviceState`s.

### Sources

- **Connection**: `adb track-devices -l`, one long-lived process; each frame is a 4-hex-digit
  length prefix plus the same text as `adb devices -l`, parsed by the existing
  `parse_adb_devices`. Zero cost when idle.
- **Battery**: `ADBProvider.battery()` per connected device, every `interval` seconds
  (default 30) — the same provider call `linkplane battery` makes.
- **Wi-Fi**: `adb shell cmd wifi status`, parsed for `Wifi is connected to "<ssid>"`.
- SSH-only devices are not observed in v0.2 (no push source; polling over SSH is a later
  additive source).

Polls run only for devices the tracker reports as `device` (authorized and online).

## Slices

**Slice 1 — observed state and the event stream, in-process** (this commit series):
`core/events.py`, `core/state.py`, `observe.py` (tracker, pollers, `Observer`), `history.py`,
`linkplane events [--json] [--interval N] [--no-history] [--low-battery N]`. The observer
runs on the calling thread with a `CancellationToken`; Ctrl+C stops it cleanly. Events are
appended to `$XDG_STATE_HOME/linkplane/events.jsonl` (`~/.local/state/...`) unless
`--no-history`. This slice is independently useful (`linkplane events --json | jq`) and is
how the daemon will be debugged.

**Slice 2 — `linkplaned`:** `daemon.py` hosts one `Observer`, the registry snapshot, and
a Unix control socket under `$XDG_RUNTIME_DIR/linkplane/`; commands `daemon run`,
`daemon status`, `daemon stop`; `events --follow` subscribes over the socket when the daemon
is running and falls back to in-process otherwise. Subscriptions = socket clients with a
filter. Registry snapshot (`state.json`) written on every change so `linkplane devices`
can answer from observed state without probing.

**Slice 3 — `daemon install`:** a `systemd --user` unit, `Restart=on-failure`; the only
Linux-desktop-specific piece. Then the automation design's rules engine can be revisited on
top of subscriptions.

## Contracts and compatibility

- No pinned field changes. `Event`, `DeviceState` are added and pinned once they survive a
  session of use, like the v0.1 shapes.
- `events --json` emits one JSON object per line (`Event.to_dict()`), **not** the
  `schema_version` envelope: a stream has no single result. The line carries `"schema":
  "linkplane.event/1"` instead.
- History files are versioned by that same `schema` field per line, so a reader can skip
  lines it does not understand.

## Security and reliability

- Only argument-array subprocesses, all with timeouts except the tracker (which is
  terminated on cancel).
- No shell strings are executed on the phone; every poll is a fixed command.
- The observer treats every source failure as a state (`provider-error` in the diff, an
  `observer.error`-free design: a failing poll simply leaves the last state and logs at
  WARNING), never a crash.
- The history file is append-only and fsync'd per line; a corrupt trailing line is skipped
  on read.

## Open questions (do not block slice 1)

1. Registry identity for unnamed devices: `serial:<x>` (proposed) vs. refusing to observe
   unpaired devices.
2. Default poll interval 30 s vs. adaptive (faster while a `battery.low` subscriber
   exists) — adaptive is a slice-2 concern.
3. Whether `linkplane devices` should prefer observed state when the daemon is running
   (proposed: yes, with a `--probe` flag to force a live probe).

## Closing note (2026-09-10, late)

Slices 1–3 landed as designed (ADR 0007, ADR 0008): observed state and canonical events;
`linkplaned` with the control socket, subscriptions, and the `state.json` snapshot;
`daemon install|uninstall` as a systemd user unit; and `devices --observed`. Verified on
the paired phone and on this host's systemd user session, including a real
disconnect/reconnect cycle and a real `systemctl --user stop`. Open questions resolved:
(1) unnamed devices are observed as `serial:<x>`; (2) the poll interval stays fixed at
30 s (adaptive polling deferred until a subscriber needs it); (3) `devices` keeps probing
by default — `--observed` is opt-in, so existing behaviour is unchanged. SSH-only devices
remain unobserved (no push source) — the first additive source to add when needed.

