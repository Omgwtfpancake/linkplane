> **Note (2026-09-10):** the project was renamed from PhoneBridge to **Linkplane** after this
> document was written. It is kept verbatim; read `PhoneBridge` as `Linkplane` throughout.

# PhoneBridge — AI Terminal Continuation Context

Received from the founder on 2026-09-10 (late evening), verbatim. It supersedes the
priority list in `docs/core-v0.1-brief.md` where the two differ, confirms the reconciliation
recorded in `docs/core-v0.1-plan.md` and the ADRs, and names the post-v0.1 milestone
(persistent daemon / state / event layer, "Core v0.2").

---

## Current Project State

PhoneBridge is no longer an early prototype.

It is now a working Android-to-Linux command-line codebase with a substantial service layer, provider architecture, frozen contracts, real-device verification, and a nearly complete Core v0.1 milestone.

Repository:

```text
~/Projects/MyMobile
```

Current known state:

```text
38 commits
17 CLI commands
291 tests
8,788 lines of Python
stdlib-only host implementation
```

The full test suite runs without a live phone.

All 17 CLI commands have been exercised against the paired Android device.

The current Android test device is a Samsung SM-S901U running Android 16.

The project currently supports both:

```text
ADB
Termux / SSH
```

ADB is now the primary transport for much of the working functionality.

SSH remains supported and is being preserved behind the Provider abstraction.

Do not assume the repository is still a simple Termux-only prototype.

---

# Strategic Product Direction

PhoneBridge is being built toward:

**An open, local-first control plane for real devices, starting with Android.**

Long-term positioning:

**Connect once. Control, observe, automate, and integrate.**

The ultimate architecture may eventually support:

```text
Consumer device integration
Cross-device continuity
Automation
Device events
Developer tooling
QA/testing
Self-hosted device labs
Remote support
Plugins
SDK/API
AI-agent access
Physical and virtual devices
Federated device infrastructure
```

However:

## DO NOT START BUILDING THE LONG-TERM PRODUCT YET.

The immediate engineering priority is to finish and freeze **Core v0.1** correctly.

After Core v0.1 is complete, the next architectural milestone should be the persistent daemon/state/event layer.

---

# Existing Functionality

Preserve all working functionality.

The project already includes:

```text
status
screen
file transfer
notifications
doctor
device discovery
clipboard get/set/pull/push
continuous clipboard sync
photo backup
camera capture
camera preview
device profiles
USB pairing
wireless ADB pairing
SSH pairing
audio forwarding
audio recording
find-phone functionality
V4L2 webcam support
dependency installation abstraction
profile refresh/reconnection behavior
subnet scanning
cancellation support
structured progress
typed services
```

Specific working capabilities already implemented include:

```text
File transfer over ADB
LocalSend fallback
Phone notifications
Clipboard sync with conflict handling
Incremental photo backup
SHA-256 verification
Camera capture
scrcpy-backed preview
Audio-only forwarding
Audio recording
Ring volume control
Vibration
Camera torch
Linux V4L2 webcam
Guided pairing
Reconnect support
```

Do not reimplement or redesign these unless required to complete the Core architecture.

---

# Existing Architectural Work

Each major CLI command has already been moved toward:

```text
CLI wrapper
    ↓
typed request/result service
```

This was done so future callers can use the same service layer:

```text
CLI
GUI
API
Automation
AI
```

without duplicating business logic.

Existing typed services include:

```text
file transfer
photo backup
status
notification
screen
camera
diagnostics
device discovery
clipboard
profiles
```

Structured progress events already exist.

Some operations already support cooperative cancellation.

Ctrl+C handling exits cleanly with code:

```text
130
```

Do not undo these patterns.

---

# Frozen Contracts

The project has already frozen its existing external contracts at version 1.

There is a contract snapshot test.

There is also an API contract reference document.

A prior bug caused multiple modules to hardcode the JSON schema version; this was fixed and regression-tested.

Important rule:

## Preserve the frozen v1 contracts.

Do not casually rename fields, change JSON shapes, or replace result structures.

If Core v0.1 requires additional data, prefer:

```text
additive fields
backward-compatible extension
adapter layer
```

rather than replacement.

If a breaking change is unavoidable, stop and explain exactly why before doing it.

---

# Core v0.1 Strategic Reconciliation

The original Core v0.1 brief assumed the repository mainly consisted of:

```text
Termux
SSH
status
battery
JSON
```

That assumption is outdated.

The correct reconciliation has already been established:

```text
Preserve everything that works
Wrap BOTH ADB and SSH behind providers
Add new abstractions without moving/rebuilding the whole project
Evolve frozen contracts additively
Complete the new Core architecture around the existing codebase
```

Do not force the repository to resemble the old brief if the current design is already better.

---

# Permanent Architectural Direction

The long-term domain model remains:

```text
IDENTITY
   │
 POLICY
   │
   ▼
RESOURCE
   │
   ├── DEVICE
   └── PROVIDER

DEVICE
   ├── CAPABILITY
   ├── STATE
   └── EVENT

ACTION
   │
 TASK
   │
 WORKFLOW
   │
 SESSION
   │
 ARTIFACT
   │
 HISTORY
```

Cross-cutting concerns:

```text
Protocol
Security
Authorization
Versioning
Compatibility
Observability
Privacy
Reliability
```

But for the current milestone, only implement the subset needed for Core v0.1:

```text
Resource
Device
Capability
Provider
Result
Error
Configuration
```

Do not overbuild the rest yet.

---

# Critical Design Rule

## Capability != Implementation

Public PhoneBridge concepts should remain:

```text
device.status
battery.read
device.ping
files.send
screen.control
```

not:

```text
adb_status
ssh_battery
scrcpy_screen
```

Providers are implementation details.

Current providers:

```text
ADBProvider
SSHProvider
```

Future providers may include:

```text
AndroidAgentProvider
ScrcpyProvider
EmulatorProvider
RemoteProvider
CloudDeviceProvider
```

Do not implement future providers now.

---

# Current Provider Architecture

The first Core slice already introduced:

```text
Provider interface
SSH provider
ADB provider
Capability catalogue
Structured error codes
ping
battery
capabilities
ADRs
```

This work should be preserved.

The Provider abstraction must support more than one transport.

Do NOT regress to:

```text
SSHProvider = only real provider
```

ADB is already an important active backend.

---

# Current Capability Catalogue

The current live device has already reported a capability table similar to:

```text
device.ping       supported
device.status     supported
battery.read      supported
storage.read      supported
files.send        supported
backup.photos     supported
notify.post       supported
screen.control    supported
```

Some capabilities may differ by provider.

For example, the ADB provider may report some items as unsupported even though another provider implements them.

That is acceptable.

Capabilities should describe:

```text
supported
unsupported
undetermined
unavailable
permission-denied
provider-error
```

where appropriate.

Do not guess unsupported capabilities.

---

# Current Core v0.1 Definition of Done

Status of the original milestone:

```text
1  Installs cleanly                           DONE
2  Existing Android/Termux functionality      DONE
3  Phone represented through Device           DONE
4  SSH behind Provider interface              DONE
5  Capabilities queryable                     DONE
6  devices works                              DONE
7  status works                               PARTIAL — not yet fully routed through provider
8  battery works                              DONE
9  ping works                                 DONE
10 doctor meaningful failures                 PARTIAL — needs coded checks
11 JSON stable                                DONE
12 Unit tests need no phone                   DONE
13 Live-device tests separated                OPEN
14 Errors typed/understandable                DONE
15 Existing functionality preserved           DONE
16 Provider replaceability                    DONE
17 Code remains appropriately simple          DONE
```

The remaining Core v0.1 work is therefore narrow.

Do not expand the milestone.

---

# Immediate Priority Order

Finish Core v0.1 in this order:

```text
1. Complete doctor
2. Route status through provider layer
3. Formalize test tiers
4. Stabilize/pin new contracts
5. Add only necessary provider/config fields
6. Run complete test suite
7. Run live-device smoke verification
8. Update ADRs/docs
9. Tag/release Core v0.1
```

Do not resume automation before this is complete.

---

# Task 1 — Complete Doctor

`phonebridge doctor` already exists.

Extend/refine it so Core v0.1 diagnostics include structured, coded checks where applicable.

Target checks may include:

```text
Configuration available
Default device valid
Provider selected
Required executable exists
SSH binary available when needed
SSH identity exists when needed
Host/device reachable
Authentication works
Remote scripts exist when needed
ADB available when needed
Provider responds
Expected JSON parses
Capability query works
```

Do not make doctor specific to SSH only.

The command should respect the configured/current provider.

Failures should use stable PhoneBridge error codes.

Examples:

```text
PB-CONNECT-001
PB-AUTH-001
PB-PROVIDER-001
PB-CAPABILITY-001
PB-TIMEOUT-001
PB-CONFIG-001
```

Reuse existing coded-error infrastructure where possible.

Human-readable output should remain actionable.

JSON output should remain structured.

Do not dump raw tracebacks during normal operation.

---

# Task 2 — Route Status Through Provider Layer

`status` currently works, but it still touches transport-specific logic too directly.

Move the final transport dependency behind the Provider boundary.

Desired flow:

```text
CLI
 ↓
Status service
 ↓
Device
 ↓
Provider interface
 ↓
ADBProvider or SSHProvider
```

Do not rewrite status behavior.

Preserve its current partial-result behavior:

```text
A failed telemetry probe does not necessarily fail the entire status command.
```

Current status semantics allow failed sections to return:

```text
null section
+
issue information
```

rather than failing the entire status request.

Preserve that behavior.

---

# Task 3 — Formal Test Tiers

The normal test suite must continue running without a phone.

Formalize test categories.

Recommended logical tiers:

```text
tests/unit/
tests/integration/
tests/device/
```

or the closest structure compatible with the existing repository.

Definitions:

```text
unit
    no external processes or live device where practical

integration
    exercises multiple project components
    may use mocked/fake subprocess boundaries

device
    requires a real paired Android device
```

Do not move large numbers of files merely to achieve an ideal folder layout.

Use the smallest clean change.

Document how to run:

```text
normal tests
integration tests
device tests
```

Live-device scripts should no longer appear to be part of ordinary unit execution.

---

# Task 4 — Stabilize New Core Contracts

The newer Core functionality includes:

```text
capabilities
ping
battery
provider-backed results
coded errors
```

These should survive real usage before being permanently frozen.

Inspect:

```text
current contract snapshot
API contract documentation
JSON output
typed result structures
```

Then determine whether any additive corrections are required.

Prefer:

```text
no change
```

unless actual inconsistency exists.

Once stable, update the contract snapshot/reference as appropriate.

Do not change existing version-1 contracts unnecessarily.

---

# Task 5 — Configuration

Do not overbuild configuration.

The current project already has profiles and pairing behavior.

Only add Core fields when actually needed by the provider model.

Possible fields:

```text
provider type
timeout
device identifier
provider-specific options
```

Do not duplicate values already represented by profiles.

Do not create a second competing configuration system.

The existing profile/config behavior is the source to inspect first.

---

# Real-Device Verification Already Performed

Important known verification:

```text
All 17 commands exercised against Samsung SM-S901U Android 16
```

Additional observed tests include:

```text
ping ≈ 38 ms over ADB
battery works in text and JSON
capabilities works in text and JSON
SSH provider reports undetermined capability state when SSH daemon is stopped
unreachable host produces PB-CONNECT-001 in both human and JSON output
ring verified via phone service logs
vibration verified via phone service logs
torch verified via phone service logs
```

A particularly important backup test has already been performed:

```text
640 files
2.7 GiB total

Interrupted at file 50

Expected:
exit code 130
resume hint
manifest matches files actually written
```

This behavior must remain intact.

---

# Cancellation

Long operations currently support cooperative cancellation in areas including:

```text
send
backup
refresh
clipboard sync
```

Ctrl+C behavior is already intentionally handled.

Do not replace cooperative cancellation with abrupt process termination unless absolutely necessary.

Future Tasks may build on this.

---

# Dependency Installation

The repository already contains a dependency installation abstraction across Linux distributions.

Do not rebuild dependency handling as Arch/Omarchy-only logic.

Linux remains the first host platform, but the core should avoid unnecessary distribution-specific assumptions.

---

# Reconnection

The current implementation includes:

```text
profile refresh when SSH host drifts
wireless ADB alias refresh
opt-in subnet scan for cold-start reconnection
USB serial verification
```

Preserve this behavior.

Do not introduce a competing device-discovery implementation during Core v0.1 cleanup.

---

# Architectural Decision Records

Six ADRs already exist from the first Core slice.

Inspect them before changing architecture.

Update existing ADRs rather than creating contradictory new ones.

Create a new ADR only for a genuinely new architectural decision.

Keep ADRs concise.

---

# Product Vision Document

The repository already contains the founding/master product vision.

Do not use that vision as the current backlog.

It exists to constrain architecture and preserve the long-term direction.

The immediate milestone remains Core v0.1.

---

# Paused Work

The automation runtime has already been designed/proposed.

It is intentionally paused.

Also paused/non-goals:

```text
Automation runtime
API expansion
MCP
GUI
Packaging expansion
Cloud
Device farm
Android native agent
AI integration
Testing platform
```

Do not resume them until Core v0.1 is closed.

---

# Core Release Gate

Core v0.1 should only be considered complete when:

```text
All normal tests pass
Provider abstraction covers status/battery/ping
Doctor uses structured failures
Capabilities remain queryable
JSON contracts are stable
Live-device tests are clearly separated
Existing functionality remains intact
ADRs are current
Documentation matches reality
Real-device smoke test passes
```

Then create an appropriate release tag.

Do not invent the tag name if repository conventions already exist.

If no convention exists, recommend one.

---

# Post-v0.1 Direction

After Core v0.1 is frozen, the next major milestone should NOT be more random CLI features.

Most of the everyday-utility layer already exists.

The next milestone should be the **persistent control-plane foundation**.

Working milestone concept:

## PhoneBridge Core v0.2 — Persistent Control Plane

Possible architecture:

```text
CLI ───────────┐
Future GUI ────┤
Future API ────┼── phonebridged
Future AI ─────┤        │
               │        ├── Device Registry
               │        ├── Provider Connections
               │        ├── Observed State
               │        ├── Event Bus
               │        ├── Subscriptions
               │        └── Local History
               │
               └──────── Providers
                           │
                           └── Devices
```

Do NOT implement this until Core v0.1 is complete.

But Core v0.1 changes should not make this architecture difficult.

---

# Why Daemon/State/Event Comes Next

The project already has much of the previously planned "everyday utility" layer:

```text
Files
Clipboard
Notifications
Backup
Screen
Camera
Audio
Find-phone
Webcam
Profiles
Discovery
Pairing
```

Therefore the roadmap effectively becomes:

```text
Working utility layer
        +
Core abstraction layer
        ↓
Persistent daemon
        ↓
Observed state
        ↓
Canonical event bus
        ↓
History/subscriptions
        ↓
Automation
        ↓
GUI/API/AI/device-lab layers
```

The daemon/event layer is what turns PhoneBridge from a CLI toolkit into the actual control plane described by the product vision.

---

# Engineering Principles

Preserve these rules:

## Inspect before editing

Always inspect current code and tests before changing architecture.

## Small migrations

Prefer incremental changes over repository-wide rewrites.

## Existing behavior wins

Do not break mature working functionality merely to make the code resemble an abstract design document.

## Additive evolution

Frozen contracts should be extended additively where possible.

## Provider boundaries

Transport-specific behavior stays behind providers.

## Thin interfaces

CLI wrappers should remain thin.

Business logic belongs in callable services/core abstractions.

## Test frequently

Run targeted tests after changes and the complete suite before milestone completion.

## Real-device tests are separate

The core suite must remain runnable without the Android device.

## No speculative infrastructure

Do not introduce:

```text
Kafka
Redis
RabbitMQ
PostgreSQL
microservices
Kubernetes
```

for the current architecture.

## Modular monolith

Prefer one maintainable Python application with clear modules.

---

# Current Technology Direction

Host:

```text
Python
stdlib-only where practical
```

Do not rewrite the host in:

```text
Rust
Go
```

without demonstrated need.

Future native Android Agent:

```text
likely Kotlin
```

but not part of the current milestone.

---

# Security Rules

Continue existing safe subprocess practices.

Prefer:

```text
argument arrays
```

over:

```text
shell=True
```

where possible.

Also:

```text
validate remote output
use timeouts
avoid leaking credentials
never print private key material
avoid unrestricted arbitrary-shell public APIs
keep privileged capabilities distinguishable
```

Future policy/AI/plugin systems will depend on these boundaries.

---

# Current Success Metrics

The immediate goal is not feature count.

Core v0.1 success means:

```text
Architecture is coherent
Current functionality is preserved
Providers are replaceable
Contracts are stable
Errors are structured
Tests are reliable
Live-device testing is formalized
Documentation reflects reality
```

The current codebase already has plenty of functionality.

Do not add features simply to increase command count.

---

# Working Style for This Session

Before modifying anything:

1. Inspect current git status.
2. Inspect latest commits.
3. Inspect the existing Core v0.1 plan.
4. Inspect relevant ADRs.
5. Inspect the Provider interface and ADB/SSH providers.
6. Inspect doctor implementation.
7. Inspect status implementation.
8. Inspect current test layout.
9. Run the test suite.

Then report:

```text
1. Current git state
2. Core v0.1 remaining gaps confirmed from code
3. Any mismatch between this context and repository reality
4. Exact files to modify
5. Test plan
6. First change
```

After reporting, proceed with the smallest safe implementation step.

---

# Immediate Execution Order

Proceed in this sequence unless repository evidence gives a strong technical reason to adjust it:

```text
Doctor coded checks
        ↓
Provider-backed status
        ↓
Test tier separation
        ↓
Contract review/freeze
        ↓
Minimal config cleanup if required
        ↓
Full test suite
        ↓
Live-device smoke test
        ↓
ADRs/docs
        ↓
Core v0.1 release/tag
```

If repository evidence shows one of these steps is already complete, verify it and move to the next one.

Do not redo finished work.

---

# Final Instruction

The project has moved quickly.

Do not respond to that speed by broadening scope.

The next goal is to make the current foundation **boringly reliable**.

The guiding rule remains:

> **Build today's product through tomorrow's architecture, but do not build tomorrow's product today.**

Finish Core v0.1.

Freeze it.

Then begin the persistent daemon/state/event layer.
