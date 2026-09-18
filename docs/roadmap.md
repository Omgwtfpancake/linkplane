# Roadmap

**Where Linkplane is now:** package 0.5.1, public alpha. The sections below record the
engineering milestones already completed (Core v0.1, Core v0.2, automation, refinement, the
local API, onboarding). The next product milestone is proposed in
[`v0.6-direction.md`](v0.6-direction.md): *automatic usefulness*, automatic photo backup on
connect as a one-step opt-in, exposing the existing rules / jobs engine rather than building
a new one. Alpha feedback that motivated it: [`alpha-feedback.md`](alpha-feedback.md).

| Milestone | Outcome |
|---|---|
| v0.5.x | alpha feedback and fixes |
| v0.6 (prepared, awaiting release) | the phone does useful work because it is connected: automatic camera-photo backup on connect (`releases/v0.6.0.md`) |
| later | wireless reconnection · tray / bar client · more presets and triggers · Android agent · MCP · multi-device · macOS · Windows |

## Refinement pass (complete)

`docs/refinement-pass-brief.md` → `docs/refinement-pass-plan.md`, ADR 0011: event identity
and correlation ids, explicit job and rule state machines (`docs/state-machines.md`), one
audit shape, API invariants and client-scope direction (`docs/api-invariants.md`), the
release smoke test (`make smoke`, `docs/smoke-test.md`), `docs/current-state.md`, the
complete `docs/MasterProductVision.md`, a versioning recommendation awaiting the founder.

## Core v0.1 (complete, tagged `core-v0.1`)

- Provider interface with SSH and ADB implementations behind one contract test suite
  (available: `providers/`, ADR 0002 and 0006)
- Public capability catalogue with a five-state status vocabulary
  (available: `linkplane capabilities`, ADR 0004)
- Structured errors with stable `LP-` codes, hints, friendly rendering, and JSON exposure
  (available: `core/errors.py`, ADR 0005)
- `battery` and `ping` commands over the provider layer, with `--json` (available)
- Architecture decision records (available: `docs/adr/`)
- `--debug` logging mode (available)
- Doctor checks for configuration, default device, provider, reachability,
  authentication, and remote scripts, each with a code on failure (available)
- `status` routed through the provider layer with unchanged semantics (available)
- Unit / integration / device test tiers with a repeatable device smoke test (available)
- Core v0.1 contracts pinned; milestone tagged `core-v0.1` (available)

## Core v0.2 — persistent control plane (complete, tagged `core-v0.2`)

Design: `docs/core-v0.2-design.md`. Replaces the runtime half of `docs/automation-design.md`.

- Observed device state and a pure, tested diff producing canonical events
  (available: `core/state.py`, `core/events.py`, ADR 0007)
- Event sources: `adb track-devices` push for connection state; battery and Wi-Fi polls
  over the ADB provider (available: `observe.py`)
- Append-only local history with monotonic sequence numbers (available: `history.py`)
- `linkplane events [--json]` foreground stream (available; verified across a real
  disconnect/reconnect on the paired phone)
- `linkplaned`: hosted observer, registry snapshot (`state.json`), Unix control socket
  with `status`/`state`/`subscribe`/`stop`, filtered subscriptions, `events --follow`
  (available: `daemon.py`, ADR 0008; verified on the phone across a reconnect)
- `daemon install|uninstall` systemd user unit with clean SIGTERM shutdown (available:
  `service.py`; verified on this host: enable, status over the socket, stop, remove)
- `devices --observed`: answer from the daemon's snapshot without probing (available)

## Available

- Unified `status` over ADB or Termux/SSH
- scrcpy screen presets and guided dependency installation
- ADB file transfer with LocalSend CLI fallback
- Phone notifications over ADB or Termux/SSH
- Logical device discovery and capability diagnostics
- One-shot clipboard get, set, pull, and push
- Opt-in foreground clipboard synchronization with conflict handling
- Incremental ADB photo backup with SHA-256 verification and manifests
- Termux camera capture and scrcpy camera preview presets
- Linux V4L2 webcam setup and teardown backed by scrcpy and v4l2loopback
- Human-readable and JSON output for status, devices, and diagnostics
- Guided USB, wireless ADB, and Termux SSH pairing
- Named multi-device profiles with strict endpoint identity and selection
- Partial status results when an individual telemetry probe is unavailable
- Find-phone (ring volume plus camera-torch flash) and a reusable notification helper
- Audio-only forwarding and recording without mirroring video

## Foundation

- Dynamic refresh of profile network endpoints without fixed addresses
  (available opportunistically: refreshes a drifted SSH host or wireless ADB alias
  whenever another endpoint on the same profile, typically USB, is already reachable;
  `--scan` additionally sweeps the last-known wireless subnet, verified against a
  recorded USB serial, when no alias is reachable at all)
- Structured operation results and progress events
  (available for all current device, profile, and pairing operations)
- Dependency installation abstraction across supported Linux distributions
  (available for ADB, SSH, scrcpy, LocalSend CLI, and desktop clipboard tools)
- Stabilize and version typed operation contracts for external interfaces
  (available as a documented reference: `docs/api-contracts.md` catalogs every current
  request/result/error/progress contract and the JSON envelope version, for API/MCP/GUI
  adapters to build against directly; frozen as `CONTRACT_VERSION = 1` with a written
  additive-vs-breaking policy and a snapshot test that pins every field, error code, and
  cancellable signature)
- Cooperative cancellation contract for long-running operations
  (available: `CancellationToken` accepted by send, backup, profile refresh/scan, and
  clipboard sync; each stops at its next boundary, emits a `cancelled` progress event, and
  returns a typed `cancelled` error; Ctrl+C in the CLI maps onto it and exits 130)

## Automation (complete on top of Core v0.2)

Design: `docs/automation-design.md` §3–6 and §10 (post-v0.2 review).

- Background service with connection, battery, Wi-Fi events (available: Core v0.2;
  file events are not a source yet)
- Rule schema, conditions, placeholders, cooldown, and the "changes only" rule
  (available: `core/automation.py`)
- `notify-desktop`, `notify-phone`, and consent-gated `run` actions (available: `actions.py`)
- `linkplane watch <event> [--if ...] --notify/--notify-phone/--run` (available; verified
  on the phone with a real notification and a real script)
- Rules from `automations.json` hosted in the daemon on a worker thread, `daemon reload`,
  blocked-until-consent handling, audit log, `automations list|log` (available: ADR 0009;
  verified on the phone)
- Job-tracked `backup`/`send`/`clipboard-sync` actions with records, retry on a lost
  connection, cancel-on-disconnect, one-per-device-and-action, `automations jobs`,
  `watch --backup` (available: `jobs.py`; verified on the phone: a connect-triggered
  Documents backup, then a repeat that skipped unchanged files)
- Per-automation permission controls beyond the `run` allow-list (later)

## Interfaces

- Stable local API using the same application services as the CLI
- MCP tools with explicit capability and permission boundaries
- Desktop GUI for devices, actions, transfers, and automations
- Packaging and first-run onboarding for supported Linux distributions
