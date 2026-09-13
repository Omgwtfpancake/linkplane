> **Note (2026-09-10):** the project was renamed from PhoneBridge to **Linkplane** after this
> document was written. It is kept verbatim; read `PhoneBridge` as `Linkplane` throughout.

# PhoneBridge — AI Terminal Build Context (Core v0.1 brief)

Received from the founder on 2026-09-10, verbatim. This supersedes the priority ordering in
`docs/product-progress-brief.md` "Near-Term Priorities" and pauses the automation runtime
(`docs/automation-design.md`) — the automation engine is an explicit non-goal for v0.1.
`docs/vision.md` remains the founding vision; the "Master Product Vision" that accompanied
this brief expands it and is appended at the end (the copy received was truncated).

---

## Project Status

PhoneBridge is an existing experimental Android ↔ Linux bridge that is being converted into a real software product.

Current prototype work already exists and should be inspected before creating new code.

Known current direction:

* Linux host is the first supported platform.
* Android is the first device platform.
* Existing prototype uses Termux + SSH.
* Existing functionality includes phone status/battery information and JSON-oriented commands.
* Python is the current host-side implementation language.
* The project should remain Python for the initial product.
* A native Android Agent, probably written in Kotlin, is a future milestone.
* Do not rewrite working code unnecessarily.
* Do not prematurely build GUI, cloud, AI, device-farm, or Android-native functionality.

The immediate task is to turn the existing prototype into a clean **PhoneBridge Core v0.1**.

---

# Product Direction

PhoneBridge is intended to become:

**An open, local-first control plane for real devices, starting with Android.**

Product positioning:

**Connect once. Control, observe, automate, and integrate.**

Long term, PhoneBridge may support:

* Android device control
* File transfer
* Clipboard
* Notifications
* Screen control
* Camera/audio
* Automation
* Device events
* Developer tooling
* Device labs
* Testing
* AI-agent access
* Physical and virtual devices
* Remote/self-hosted infrastructure

However:

## DO NOT BUILD THE LONG-TERM PRODUCT NOW.

Build today's small product through an architecture that can support tomorrow's larger product.

---

# Core Architectural Principles

The long-term PhoneBridge domain model is:

```text
IDENTITY
   │
 POLICY
   │
   ▼
RESOURCE
   │
   ├── DEVICE
   │
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
```

For v0.1, implement only the subset needed now:

```text
Resource
Device
Capability
Provider
Result
Error
Configuration
```

The other concepts should influence naming/interfaces where appropriate, but must not be overbuilt.

---

# Critical Design Rule: Capability != Implementation

Never expose backend implementations as the public PhoneBridge model.

Bad:

```python
ssh_get_battery()
scrcpy_start_screen()
```

Preferred public concepts:

```python
device.battery()
device.status()
device.ping()
```

Providers implement those capabilities.

Today:

```text
battery.read
     ↓
SSHProvider
     ↓
Termux
```

Future:

```text
battery.read
     ↓
AndroidAgentProvider
```

The CLI/API above the provider should not need to change.

---

# Provider Architecture

Providers answer:

**How is this capability implemented?**

Initial provider:

```text
SSHProvider
```

Future providers may include:

```text
ADBProvider
AndroidAgentProvider
ScrcpyProvider
EmulatorProvider
RemoteProvider
CloudDeviceProvider
```

Do not implement future providers now.

Design the interface so they can exist later.

---

# Resource / Device Model

A device should have a stable logical identity independent of its IP address.

Conceptual structure:

```python
Device(
    id=...,
    name=...,
    type=...,
    provider=...,
    capabilities=...,
    connection_state=...,
    metadata=...
)
```

Do not assume there will only ever be one phone.

Even though development currently uses one Android device, multi-device semantics must exist from the start.

---

# Capability Model

A capability describes what a resource can do.

Examples:

```text
device.ping
device.status
battery.read
storage.read
```

Later:

```text
files.send
clipboard.read
screen.control
apps.install
logs.read
camera.capture
```

For v0.1, likely capabilities are:

```text
device.ping
device.status
battery.read
storage.read
```

Capability discovery should be machine-readable.

Example CLI:

```bash
phonebridge capabilities
```

Possible output:

```text
Galaxy S22

device.ping        ✓
device.status      ✓
battery.read       ✓
storage.read       ✓
clipboard.read     ✗
files.send         ✗
screen.control     ✗
```

Unsupported capabilities are acceptable.

The architecture must distinguish:

```text
supported
unsupported
unavailable
permission-denied
provider-error
```

where appropriate.

---

# CLI Target for v0.1

The initial CLI should provide:

```bash
phonebridge devices
phonebridge capabilities
phonebridge status
phonebridge battery
phonebridge ping
phonebridge doctor
```

Each relevant command should support structured output:

```bash
phonebridge status --json
phonebridge battery --json
phonebridge devices --json
phonebridge capabilities --json
```

Human output should be readable.

JSON output must be stable and machine-consumable.

---

# Device Selection

Do not hardcode a single device into commands.

Desired semantics:

```bash
phonebridge status
```

Uses the configured default device.

Explicit selection should conceptually support:

```bash
phonebridge --device galaxy status
```

or equivalent clean CLI syntax.

Do not build elaborate device management yet.

Only avoid single-device architectural assumptions.

---

# Result Model

Actions should not return random dictionaries or rely only on printed strings.

Define a common result abstraction.

Conceptually:

```python
OperationResult(
    success=True,
    operation=...,
    resource_id=...,
    provider=...,
    data=...,
    warnings=[],
    error=None,
)
```

Future-compatible fields may include:

```text
operation_id
started_at
completed_at
artifacts
metrics
```

Do not implement unnecessary complexity yet.

Use the smallest typed result model that allows growth.

---

# Error Model

Create structured PhoneBridge errors.

Do not depend only on text such as:

```text
Error connecting to phone.
```

Internal errors should have stable categories/codes.

Initial examples:

```text
PB-CONNECT-001
PB-AUTH-001
PB-PROVIDER-001
PB-CAPABILITY-001
PB-TIMEOUT-001
PB-CONFIG-001
```

User-facing CLI output should still be friendly.

Concept:

```text
Phone unreachable.

Check that:
• the phone is on the same network
• Termux SSH is running
• the configured host is correct

Error: PB-CONNECT-001
```

Machine JSON should expose structured error information.

---

# Configuration

Configuration must not be scattered throughout scripts.

Use a centralized config model.

Potential config location on Linux:

```text
~/.config/phonebridge/
```

Possible initial configuration:

```text
default_device
device name
host/IP
SSH port
SSH user
identity/key path
provider type
timeouts
```

Do not store sensitive credentials in plaintext if avoidable.

Do not introduce cloud accounts.

PhoneBridge v0.1 is local-first.

---

# Existing SSH / Termux Backend

Treat the current Termux + SSH implementation as a provider.

Do not treat SSH as part of the permanent public PhoneBridge API.

Desired architecture:

```text
CLI
 ↓
PhoneBridge Core
 ↓
Device
 ↓
Capability
 ↓
Provider Interface
 ↓
SSHProvider
 ↓
Existing Termux scripts
```

Reuse existing working phone scripts where practical.

Do not rewrite the Android side merely for style.

Refactor only when necessary to establish the provider boundary.

---

# Protocol Direction

PhoneBridge should eventually have a versioned logical protocol.

Do not build a network protocol stack in v0.1.

However, design structured responses so they can naturally evolve into something like:

```json
{
  "protocol": "phonebridge/1",
  "request_id": "abc123",
  "resource": "galaxy",
  "action": "battery.read"
}
```

and:

```json
{
  "protocol": "phonebridge/1",
  "request_id": "abc123",
  "status": "ok",
  "data": {}
}
```

Current SSH transport is temporary.

Do not tightly couple domain objects to subprocess/SSH output.

---

# Doctor Command

`phonebridge doctor` is a first-class v0.1 feature.

It should diagnose the current installation.

Initial checks may include:

```text
Configuration exists
Default device exists
SSH executable available
SSH identity exists
Phone hostname/IP configured
Phone reachable
SSH authentication works
Required remote scripts exist
Required Termux commands work
Provider responds
JSON can be parsed
```

Output example:

```text
PhoneBridge Doctor

✓ Configuration
✓ SSH installed
✓ Device configured
✓ Phone reachable
✓ Authentication
✓ Remote status command
✗ Battery command

1 problem found.

Battery data could not be retrieved.
Error: PB-PROVIDER-003
```

Doctor should help solve support issues.

Avoid making it simply dump exceptions.

---

# Provider Contract Tests

Provider implementations should satisfy common behavior.

Create tests around contracts rather than only implementation details.

For example:

```text
SSHProvider

✓ reports capabilities
✓ returns device status
✓ returns battery data
✓ handles unreachable device
✓ handles malformed remote JSON
✓ handles timeout
✓ produces structured errors
```

A future AndroidAgentProvider should be able to pass the same conceptual tests.

---

# Testing Requirements

Use automated tests from the beginning.

At minimum:

```text
Unit tests
Provider tests/mocks
CLI tests
JSON-output tests
Error-path tests
Config tests
```

Avoid tests that require the physical phone unless clearly marked integration tests.

Separate:

```text
unit
integration
device
```

where useful.

The developer should be able to run normal tests without the Android phone connected.

---

# Project Structure

Inspect the existing repository before changing structure.

A possible target structure is:

```text
src/
  phonebridge/
    __init__.py
    cli.py

    core/
      device.py
      capability.py
      result.py
      errors.py
      config.py

    providers/
      base.py
      ssh.py

    commands/
      devices.py
      capabilities.py
      status.py
      battery.py
      ping.py
      doctor.py

tests/
  unit/
  integration/

docs/
  adr/
```

This is guidance, not a command to reorganize blindly.

Preserve good existing structure.

Do not move files unnecessarily.

---

# Architecture Decision Records

Create:

```text
docs/adr/
```

Start recording major architecture decisions.

Initial ADR candidates:

```text
0001-python-host-core.md
0002-provider-abstraction.md
0003-resource-device-model.md
0004-capability-model.md
0005-structured-results-errors.md
0006-ssh-provider.md
```

ADR format:

```text
Title
Status
Context
Decision
Alternatives Considered
Consequences
```

Keep ADRs short.

---

# Security Principles

Even in v0.1:

* Do not execute untrusted shell strings unnecessarily.
* Prefer subprocess argument arrays over shell=True.
* Validate remote output.
* Set connection timeouts.
* Do not leak private key content.
* Do not print secrets.
* Keep privileged capabilities separate from normal capabilities.
* Assume future clients may include plugins and AI agents.
* Do not design APIs around unrestricted shell execution.

Long term, PhoneBridge will use capability-based authorization.

Do not implement a full policy engine now.

Just avoid designs that would prevent one.

---

# Android Capability Reality

Do not assume every future feature is possible from an ordinary Android application.

Future capabilities will fall into categories such as:

```text
A — Normal Android API
B — Requires user permission
C — Requires foreground/visible operation
D — Requires ADB / developer mode
E — Requires enterprise/device-owner privilege
F — Requires root/OEM support
X — Not reliably available
```

This classification is not needed for every v0.1 command yet, but capability metadata should be extensible enough to include requirements later.

---

# Non-Goals for v0.1

Do NOT implement:

```text
GUI
Android native app
Remote cloud access
Relay server
MCP
AI integration
Appium
Device farm
Automation engine
Workflow marketplace
Plugin marketplace
Windows support
macOS support
iOS support
scrcpy integration
Camera
Audio
Home Assistant
MQTT
Enterprise RBAC
Cloud accounts
Microservices
Kubernetes
Distributed event infrastructure
```

Those are roadmap items.

---

# Technology Guidance

Use Python for the host implementation.

Do not rewrite the project in Rust/Go.

Use simple, maintainable dependencies.

Prefer the standard library where reasonable.

Do not introduce infrastructure such as:

```text
Kafka
Redis
RabbitMQ
PostgreSQL
Docker services
microservices
```

for v0.1 unless an existing requirement absolutely demands it.

This is currently:

```text
One Linux PC
One Android phone
One local application
```

A modular monolith is desirable.

---

# Product Quality Requirements

The CLI should feel like a real utility.

Commands should:

* Have consistent formatting.
* Have useful `--help`.
* Return useful exit codes.
* Handle Ctrl+C cleanly.
* Set sensible timeouts.
* Never expose giant tracebacks to ordinary users unless debug mode is enabled.
* Provide actionable errors.
* Support `--json`.
* Be script-friendly.
* Avoid silent failure.

Possible future global flags:

```text
--device
--json
--verbose
--debug
--timeout
```

Do not implement unnecessary flags if they are not yet needed.

---

# Logging

Use proper logging rather than scattered print debugging.

Conceptually support:

```text
ERROR
WARNING
INFO
DEBUG
```

Default CLI output should remain clean.

Debug mode should expose useful diagnostic detail.

Avoid logging secrets.

---

# Compatibility

Initial target:

```text
Linux
```

Do not unnecessarily hardcode Arch/Omarchy assumptions into the PhoneBridge core.

Platform-specific installer logic may differ later.

The architecture should allow:

```text
Linux
Windows
macOS
```

eventually.

Android is the first device platform.

---

# Naming

"PhoneBridge" is currently a project codename.

Do not invest heavily in permanent branding/package publication until naming/trademark/project conflicts are reviewed.

Internal package naming may remain `phonebridge` during development unless a conflict creates a technical reason to change it.

---

# Licensing

Do not copy code from external open-source projects without explicitly reviewing the license and integration model.

External tools should generally be treated through provider/adaptor boundaries.

Future examples:

```text
scrcpy → Screen Provider
ADB → Debug Provider
```

Do not bundle third-party binaries yet unless explicitly decided.

Project license should be intentionally selected before broad public contribution begins.

---

# Immediate Build Goal

The next milestone is:

## PhoneBridge Core v0.1

Deliver:

```text
Core models:
    Resource
    Device
    Capability
    Provider
    OperationResult
    PhoneBridgeError
    Configuration

Provider:
    SSHProvider

CLI:
    phonebridge devices
    phonebridge capabilities
    phonebridge status
    phonebridge battery
    phonebridge ping
    phonebridge doctor

Output:
    Human-readable output
    Stable --json output

Quality:
    Tests
    Error handling
    Documentation
    ADRs
```

---

# Definition of Done

v0.1 is successful when:

1. PhoneBridge installs cleanly in the development environment.
2. Existing Android/Termux functionality still works.
3. The phone is represented through the Device abstraction.
4. SSH behavior is behind a Provider interface.
5. Capabilities are queryable.
6. `devices` works.
7. `status` works.
8. `battery` works.
9. `ping` works.
10. `doctor` identifies meaningful failures.
11. JSON output is structured and stable.
12. Unit tests do not require a live phone.
13. Live-device integration tests are separate.
14. Errors are typed and understandable.
15. Existing functionality has not been broken unnecessarily.
16. Another provider could theoretically replace SSH without rewriting the CLI.
17. The code remains simple enough for a small project.

---

# First Task for the AI Agent

Before writing code:

1. Inspect the entire existing PhoneBridge/MyMobile repository.
2. Identify existing files, scripts, CLI commands, tests, configuration, and package structure.
3. Run the current test suite.
4. Determine what functionality already exists and what is currently broken.
5. Do NOT delete/rewrite working functionality.
6. Compare the current implementation to the v0.1 target described above.
7. Create a short implementation plan based on the actual repository.
8. Then begin implementing the smallest safe architectural changes.

Prioritize:

```text
Existing behavior preservation
    ↓
Core abstractions
    ↓
SSHProvider boundary
    ↓
devices/capabilities
    ↓
existing status/battery migration
    ↓
ping
    ↓
doctor
    ↓
tests/documentation
```

Do not jump ahead to later roadmap features.

---

# Working Style

When modifying the repository:

* Inspect before editing.
* Make small changes.
* Run tests frequently.
* Add tests with new behavior.
* Do not fabricate files/classes that already exist under different names.
* Prefer adapting existing code over duplicating it.
* Explain major architectural changes.
* Record significant decisions in ADRs.
* Avoid speculative abstraction beyond what v0.1 needs.
* Preserve backward compatibility where reasonable.
* If current implementation conflicts with this architecture, choose the smallest migration path.
* Do not ask for unnecessary confirmation if repository evidence resolves the issue.

The guiding principle is:

> **Build today's tiny product through tomorrow's architecture, but do not build tomorrow's product today.**

---

# Start Now

Inspect the repository and current test state.

Then report:

```text
1. Current repository structure
2. Existing working functionality
3. Existing failing/broken functionality
4. Architectural gaps against PhoneBridge Core v0.1
5. Exact files you intend to modify/create
6. First implementation step
```

After that, begin the v0.1 work.

---

# Appendix: Master Product Vision — superseded

The complete vision is `docs/MasterProductVision.md` (from the founder's 99-page PDF,
`docs/MasterProductVision.pdf`). The fragment below is what accompanied this brief and is
kept only because this document is verbatim; do not read it as the vision.

## (original truncated fragment follows)

## One sentence

PhoneBridge turns Android devices into programmable resources that computers, developers,
automations, applications, and AI agents can securely control.

## Short positioning

**Connect once. Control anything. Automate everything.**

## Technical positioning

An open, local-first control plane for Android devices.

## Strategic revision

After comparing the current PhoneBridge plan against scrcpy, KDE Connect, LocalSend, Home
Assistant, Android Enterprise, AWS Device Farm, Appium, and DeviceFarmer/STF, the
highest-value version of PhoneBridge is bigger than an Android/Linux companion utility —
but still focused enough to build. The existing plan already moves toward events,
automation, GUI/CLI/API, and multiple devices. That is the correct foundation.

The major strategic revision: **PhoneBridge should become an open, local-first Android
Device Automation Platform.** Not merely "Android ↔ Linux bridge", and not merely
"KDE Connect + scrcpy + LocalSend bundled together" — those capabilities become modules
inside PhoneBridge. scrcpy already performs mirroring/control extremely well; KDE Connect
already covers everyday phone/desktop interactions; LocalSend provides excellent
zero-configuration encrypted local transfer. **We need to own the layer above them.**

## 1. What PhoneBridge actually is

PhoneBridge begins as Android Phone ↔ Linux PC, but its architecture should become:

```text
                         PHONEBRIDGE
                              │
                   Device Control Plane
                              │
        ┌─────────────────────┼──────────────────────┐
        │                     │                      │
     Devices               Services              Clients
        │                     │                      │
   Android phones         Automation              GUI
   Android tablets        Event Bus               CLI
   Test devices           Transfer                REST
   Kiosks                 Remote Access           WebSocket
   TVs                    Plugins                 Python SDK
   Wearables              Integrations            AI/MCP
```

The bridge itself is not the final product. The control plane is the product.

## 2. The core product principle

Every PhoneBridge capability should be accessible through one unified device model:

```sh
phonebridge device galaxy battery
phonebridge device galaxy send build.apk
phonebridge device galaxy screen
phonebridge device galaxy camera
phonebridge device galaxy notify "Finished"
```

The same operations through Python:

```python
phone = bridge.device("galaxy")
print(phone.battery)
phone.send("build.apk")
phone.notify("Build complete")
phone.screen.start()
```

Through an API:

```text
GET  /devices/galaxy/battery
POST /devices/galaxy/files
POST /devices/galaxy/notifications
POST /devices/galaxy/screen
```

And through AI tools: `get_device_status`, `send_file`, `start_screen`,
`install_application`, `take_screenshot`, `get_latest_photo`.

**One capability model. Many interfaces.** This is one of the most important
architectural rules in the project.

## 3. Device Agent

Eventually PhoneBridge needs its own Android application. The existing Termux/SSH
implementation is excellent f— *(the received copy ends here; the remainder of the vision
document has not been delivered yet)*
