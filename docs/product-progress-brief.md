# Linkplane: Product and Progress Brief

**Current version:** 0.9.0  
**Updated:** September 10, 2026 (evening)

> **Direction change, 2026-09-10:** the founder's Core v0.1 brief (`docs/core-v0.1-brief.md`)
> repositions Linkplane as an open, local-first device control plane and sets Core v0.1
> (Device / Capability / Provider / Result / Error / Configuration, plus `devices`,
> `capabilities`, `status`, `battery`, `ping`, `doctor`) as the next milestone. Automation,
> API/MCP, and GUI work is paused until it is done. Status against it: `docs/core-v0.1-plan.md`.

## The Vision

See `docs/vision.md` for the full founding product vision this section summarizes —
the reasoning behind "orchestrate, don't rewrite," the 0.1–0.5 roadmap phases, the
Automations differentiator, and the three-layer GUI/CLI/API audience model.

Linkplane makes an Android phone feel like a first-class part of the Linux desktop.
It provides fast, private, local integration without requiring a cloud account or sending
personal data through a third-party service.

> Your Android phone, fully integrated with Linux. Fast, private, and under your control.

Instead of asking users to configure and remember separate ADB, scrcpy, SSH, Termux, and
LocalSend commands, Linkplane presents one device model and one consistent interface.

```text
User action
    |
    +-- Back up my photos
    +-- Use my phone camera
    +-- Send this file
    +-- Sync my clipboard
    +-- Show my phone screen
    |
    v
Linkplane: identity, capabilities, safety, and orchestration
    |
    +-- ADB
    +-- Termux / SSH
    +-- scrcpy
    +-- LocalSend
```

## Current Progress

```text
Core device operations       [###################-]  96%
Foundation and reliability   [##########----------]  50%
Advanced device workflows    [####################] 100%
Automation platform          [--------------------]   0%
API, MCP, and desktop GUI    [###-----------------]  13%
```

These percentages measure roadmap coverage and count partially implemented items as
half-complete. They do not estimate development effort; automation and graphical product
work are larger than individual CLI features.

| Roadmap status | Items |
|---|---:|
| Complete | 12 of 27 |
| Partial | 6 of 27 |
| Not started | 9 of 27 |

The technical CLI product is already useful. The remaining work primarily turns that
working core into a reliable background platform and an approachable consumer product.

## What Works Today

| Capability | Current state |
|---|---|
| Device status | Unified ADB/SSH status with partial results for failed probes |
| Screen control | scrcpy quality presets, audio, control, and recording |
| File transfer | Direct ADB transfer with LocalSend fallback |
| Photo backup | Incremental backup with manifests and SHA-256 verification |
| Phone notifications | Delivery through ADB or Termux/SSH |
| Find phone | Ring volume, vibration, and camera-torch flash, each verified end to end |
| Clipboard | Get, set, push, pull, and opt-in continuous synchronization |
| Camera capture | Termux camera capture with validated JPEG transfer |
| Camera preview | scrcpy camera presets, recording, microphone, and torch options |
| Audio-only sessions | scrcpy `--no-video` forwarding and recording of playback or microphone |
| Webcam | Linux V4L2 sink backed by scrcpy and v4l2loopback, with tracked start/stop |
| Pairing | Guided USB, wireless ADB, and Termux SSH workflows |
| Multiple devices | Named profiles with ADB aliases and explicit SSH association |
| Endpoint refresh | Corrects a drifted SSH host or wireless ADB alias via any reachable link, or via an opt-in verified subnet scan when none is |
| Discovery | Logical devices, endpoints, capabilities, and host diagnostics |
| Machine integration | Versioned JSON for status, discovery, and diagnostics |

Linkplane currently uses only the Python standard library. The automated suite contains
291 passing tests, supplemented by safe dry-run and connected-device checks.

## Product Journey

```text
[Complete] Unified command-line bridge
           Status, screen, files, notifications, clipboard

[Complete] Rich phone workflows
           Verified backup, camera capture, camera preview

[Complete] Device identity and setup
           Pairing, named profiles, endpoint aliases, strict selection

[Complete] Shared application services
           Typed contracts, frozen at contract version 1, with cooperative cancellation

[Complete] Core v0.1 provider architecture
           Provider interface (SSH, ADB), capability catalogue, coded errors, coded
           doctor, provider-backed status, ping/battery/capabilities, test tiers, ADRs

[Complete] Core v0.2 persistent control plane
           Observed state, canonical events, history, `linkplane events`, linkplaned
           with subscriptions and a state snapshot, systemd user service, `devices --observed`

[Complete] Automation as a daemon subscriber
           Rule engine, notify/run actions, `watch`, rules file hosted in linkplaned,
           reload, audit log, job-tracked backup/send/clipboard-sync with records

[Complete] Refinement pass
           Event ids + correlation, job/rule state machines, one audit shape, API
           invariants, release smoke test, current-state doc, full Master Product Vision

[Next]     Interfaces over the daemon
           Local API, then MCP tools and the desktop GUI, each a thin client of
           linkplaned's socket, snapshot, history, and audit log

[Complete] Host dependency planning
           Distro-specific plans for current ADB, SSH, scrcpy, transfer, and clipboard tools

[Complete] Opportunistic endpoint refresh
           Corrects a drifted SSH host or wireless ADB alias via any reachable link

[Complete] Find phone
           Ring volume, vibration, and camera-torch flash, each verified end to end

[Complete] Audio-only forwarding and recording
           scrcpy --no-video sessions for playback or microphone, with audio recording

[Complete] Linux V4L2 webcam
           scrcpy and v4l2loopback backed webcam sink with tracked detached start/stop

[Complete] Cold-start endpoint recovery
           Opt-in bounded subnet scan, verified against a recorded USB serial, when no
           alias is reachable at all

[Planned]  Background automation
           Events, triggers, jobs, permissions, audit history

[Planned]  Product interfaces
           Local API, MCP tools, desktop GUI, onboarding, packages

[End goal] A complete local Android-to-Linux integration platform
```

## What Remains

### Reliable Core

Complete. The typed contracts are frozen at contract version 1 with a test that pins them,
and every long-running operation accepts a cooperative cancellation token (see
`docs/api-contracts.md`, "Stability policy" and "Cancellation"). What remains here is
maintenance: any future breaking change must bump the version and update the snapshot.

### Automation

- Run a background service that observes connection, battery, Wi-Fi, and file events.
- Support declarative triggers, conditions, and actions.
- Add resumable jobs, retries, and desktop notifications.
- Record an audit history and enforce per-automation permissions.

### Consumer and Developer Interfaces

- Expose a stable local API over the same application services as the CLI.
- Provide MCP tools with explicit capability and permission boundaries.
- Build a desktop GUI for devices, actions, transfers, backups, and automations.
- Ship Linux packages, first-run onboarding, compatibility checks, and managed updates.

## Why the Idea Is Valuable

Android-to-Linux integration is fragmented. Individual tools are capable, but users must
assemble them, reconcile different configuration styles, remember device addresses, and
understand each tool's failure and security behavior.

Linkplane creates value by combining those tools into a coherent product:

- One logical identity for each phone across USB, wireless ADB, and SSH.
- One place to discover what a device can currently do.
- Safe defaults for device selection, fallback, file publication, and private data.
- Verified workflows such as checksummed photo backup instead of blind copying.
- Local operation for clipboard contents, photos, notifications, and camera data.
- A foundation for automations that connect phone state to Linux workflows.

The opportunity is not to replace ADB or scrcpy. It is to make excellent but disconnected
tools feel like one dependable system.

## Why People Would Want It

- Linux users want the continuity features available in tightly integrated commercial
  ecosystems without changing desktop or phone platforms.
- Privacy-conscious users want clipboard, photo, notification, and automation data to stay
  on hardware they control.
- Developers want repeatable device workflows without manually composing ADB and scrcpy
  commands every day.
- Creators can reuse a high-quality phone camera for capture, recording, and webcam use.
- Multi-device users need stable names and identities rather than serial numbers and ports.
- Self-hosting and home-lab users value scriptable local infrastructure with transparent
  behavior.
- Small technical teams can use consistent profiles and diagnostics for Android test
  devices.

## Why People Would Pay

The underlying tools are free, so the paid value must be the finished experience rather
than basic command wrapping. Customers would pay for:

- A polished desktop interface and clear first-run setup.
- One supported installer instead of manual dependency assembly.
- Automatic discovery, reconnection, and background synchronization.
- Reliable visual progress, recovery, and verified backups.
- Useful automation without shell scripting.
- Understandable permissions and activity history.
- Tested updates across Linux distributions and Android releases.
- Documentation, responsive support, and long-term maintenance.

The strongest product combines the convenience of a native phone-link application with
the performance of scrcpy, the extensibility of Termux, and the privacy of local-first
software.

## Likely Customers

- Linux desktop enthusiasts
- Software and Android developers
- Privacy-focused professionals
- Creators using phones as cameras
- Self-hosting and home-lab users
- Multi-device power users
- Small teams managing Android test devices
- People leaving proprietary phone-link ecosystems

## Commercial Direction

The current CLI is suitable for technical early adopters. Broad paid appeal begins when
automatic connectivity, background operation, a desktop GUI, onboarding, packaging, and
managed updates are available.

A practical model is an open core with a paid polished desktop edition, advanced
automation, packaged updates, and support. A one-time desktop license with optional paid
upgrades or support is likely to fit Linux customers better than putting basic local
integration behind a mandatory subscription.

## Near-Term Priorities

1. A local API over the daemon (the roadmap's "Interfaces": the same status, state,
   subscribe, reload, and rules operations the socket already answers, plus the typed
   services), designed as a thin adapter — a design pass first, against the frozen
   contracts in `docs/api-contracts.md`.
2. Then MCP tools and the desktop GUI over that API; packaging and `linkplane setup`
   after. Package version vs. milestone tags is still the founder's call.

Linkplane has completed most of its visible command-line workflows. The next phase is
about turning that strong feature set into a durable platform that can support automation,
developer integrations, and a product-quality desktop experience.
