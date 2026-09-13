# 0007 — Observed state per device, and a pure diff that produces canonical events

**Status:** Accepted (2026-09-10, Core v0.2 slice 1)

## Context

Core v0.2 (`docs/core-v0.1-continuation.md` "Post-v0.1 Direction",
`docs/core-v0.2-design.md`) needs the control plane to *keep knowing* about devices:
last known state, and an ordered stream of state changes that a daemon, a history file,
a subscriber, and eventually an automation rule can all consume. Sources are uneven — the
ADB tracker is push, battery and Wi-Fi are polls — and must not each invent their own
event semantics.

## Decision

- `core/state.py::DeviceState` is the one record of the last observation per device.
- `core/state.py::diff(previous, current) -> [Event]` is the **only** place event
  semantics live. It is pure (no I/O, no clock beyond the event timestamp) and covers the
  whole catalogue in unit tests. Sources produce `DeviceState`s and nothing else.
- `core/events.py::Event` is the canonical, immutable event; `type` is drawn from a closed,
  append-only catalogue with dotted names (`battery.low`) mirroring capability naming;
  `data.initial = true` marks an observation (start-up snapshot, or first telemetry after
  a (re)connect) as opposed to a change.
- `observe.py::Observer` merges the sources on two threads (tracker pump + poll timer)
  behind two locks: one for state, one serializing polls (a real-phone race emitted the
  opening observations twice after `adb usb`).
- `history.py` appends `Event.to_dict()` as JSON Lines with a per-file monotonic `seq`,
  repairs a partial trailing line on open, and fsyncs per record.
- `linkplane events [--json]` is the first consumer and runs the observer in-process;
  the daemon (slice 2) hosts the same `Observer` and adds subscriptions over a socket.

## Alternatives considered

- **Sources emitting events directly.** Rejected: three sources would carry three copies of
  "what counts as a change", and a poll failure would have to decide event semantics.
- **An event envelope reusing `schema_version`/`ok`/`data`.** Rejected: a stream has no
  single result; each line carries `"schema": "linkplane.event/1"` instead.
- **asyncio.** Rejected (ADR 0001): the providers are synchronous; two threads and a
  `CancellationToken` are enough, and Ctrl+C handling already works that way.

## Consequences

Adding an event type = catalogue entry + a `diff` rule + a test. A new source only has to
produce `DeviceState`s. Wi-Fi and battery are cleared silently when a device goes away
(no `wifi.disconnected` on unplug) and re-emitted as `initial` observations on return.
