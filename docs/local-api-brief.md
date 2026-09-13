# Linkplane — Local API Design Pass

You are continuing development of **Linkplane**.

**Tagline:** Make real devices programmable.

Linkplane is an open, local-first control plane for real devices, starting with Android.

This pass is **DESIGN ONLY**.

Do **not** implement the HTTP API yet.

The purpose of this pass is to produce a concrete, reviewable local API design that exposes the existing Linkplane control plane without creating a second architecture.

---

# Current Project State

As of the latest checkpoint:

- Package version: `0.4.0`
- Milestones:
  - `core-v0.1`
  - `core-v0.2`
  - `api-v0.1`
- 58 commits
- 21 CLI commands
- 436 hermetic tests
- 11,775 lines of Python
- 11 ADRs
- Python implementation is stdlib-only where practical

The project has completed:

1. Core v0.1
2. Core v0.2
3. Automation
4. Architecture refinement pass

The next milestone is the **local API**.

---

# Existing Architecture

```text
CLI · linkplane watch · future API / GUI / MCP / SDK
        │
        ▼
linkplaned
        │
        ├── Observer
        │     adb track-devices + telemetry polls
        │
        ├── Observed State
        │     state.json
        │
        ├── Canonical Events
        │     events.jsonl
        │     sequence
        │     event_id
        │     correlation_id
        │
        ├── Rules
        │     automations.json
        │
        ├── Jobs
        │     backup
        │     send
        │     clipboard-sync
        │
        └── Audit
              audit.jsonl

Providers
 ├── ADBProvider
 └── SSHProvider

Typed Services
 ├── status
 ├── backup
 ├── send
 ├── notify
 ├── clipboard
 ├── screen
 ├── camera
 ├── webcam
 ├── audio
 ├── find
 └── profiles
```

The daemon already has a private Unix control socket supporting:

```text
status
state
subscribe
reload
stop
```

---

# Existing Domain Vocabulary

Use the vocabulary already established by the refinement pass:

```text
Device
Capability
State
Event
Action
Job
Rule
Audit
Provider
Result
Error
```

Do not introduce alternate concepts for things that already exist.

---

# Fundamental API Rule

The local API must be another interface to the existing Linkplane control plane.

It must **not** become another control plane.

The intended relationship is:

```text
                         ┌── CLI
                         ├── Local HTTP API
Linkplane Core / Daemon ─┼── future Desktop GUI
                         ├── future SDK
                         └── future MCP
```

All interfaces should converge on the same domain behavior.

Permanent principle:

> One capability model. Many interfaces.

---

# Hard API Invariants

Read and obey `docs/api-invariants.md`.

At minimum:

The API must reuse:

- typed services
- daemon
- observed state
- canonical event stream
- jobs
- stable Linkplane errors
- existing contracts

The API must NOT:

- invoke Linkplane by shelling out to the CLI
- duplicate provider logic
- call ADB or SSH directly as its primary architecture
- create a second event model
- create a second job model
- create a second error model
- duplicate retry logic
- expose unrestricted shell execution
- bypass consent rules
- bypass capability checks
- invent API-only device state

---

# Objective

Produce a complete local API design suitable for founder review.

Resolve the following questions.

---

# 1. Resource Model

Design the smallest coherent resource hierarchy.

Start by evaluating:

```text
GET /devices
GET /devices/{device_id}

GET /devices/{device_id}/state
GET /devices/{device_id}/capabilities

GET /events

GET /jobs
GET /jobs/{job_id}

GET /audit

POST /devices/{device_id}/actions
```

Do not accept this list blindly.

Inspect the existing contracts and determine the correct shape.

Prefer domain resources over CLI-command-shaped routes.

Bad:

```text
POST /backup
POST /run-screen-command
POST /adb-command
```

Preferred direction:

```text
POST /devices/{device_id}/actions
```

with a typed action request.

---

# 2. Action Model

Define one consistent action request model.

Example conceptual shape:

```json
{
  "action": "files.backup",
  "parameters": {
    "source": "DCIM",
    "destination": "~/Pictures/Phone"
  }
}
```

This is illustrative only.

Derive the real shape from existing Linkplane types and contracts.

Determine:

- action identifier format
- parameters
- target device
- correlation ID handling
- client identity
- capability validation
- consent requirements
- synchronous vs asynchronous behavior
- returned result
- returned job
- validation errors

---

# 3. Immediate Result vs Job

Define the rule for operations that complete immediately versus operations represented as jobs.

Conceptually:

```text
Action request
     │
     ├── fast operation ─────► Result
     │
     └── tracked operation ──► Job
```

Do not create separate architectural paths if they can share one action model.

Examples to evaluate:

Likely immediate:

```text
battery.read
device.status
notify.send
clipboard.get
clipboard.set
```

Likely job-backed:

```text
files.backup
files.send
clipboard.sync
```

Use the implementation as the source of truth.

Document exactly how a caller determines whether it received an immediate result or a job.

---

# 4. HTTP Status and LP Error Mapping

Inspect the existing stable Linkplane error catalogue.

Design a deterministic mapping:

```text
LP error family
        ↓
HTTP status
        ↓
structured API error body
```

The HTTP status must not replace the Linkplane error code.

A client should still receive something conceptually like:

```json
{
  "error": {
    "code": "LP-CONNECT-001",
    "message": "...",
    "hint": "...",
    "correlation_id": "..."
  }
}
```

Determine mappings for at least:

- invalid request
- missing device
- disconnected device
- capability unavailable
- authorization failure
- consent required
- conflict / already running
- timeout
- provider failure
- internal failure

Prefer established HTTP semantics.

Do not encode domain meaning solely in status codes.

---

# 5. Events API

The existing canonical event stream remains the source of truth.

Do not create HTTP-specific event objects unless transport metadata absolutely requires it.

Review the canonical schema produced by the refinement pass.

Expected conceptual fields include:

```text
event_id
sequence
timestamp
event_type
device_id
source
initial
correlation_id
data
```

Document the exact existing schema.

---

# 6. Event Streaming Transport

Evaluate the current Unix socket subscription system and decide the best first local API streaming transport.

Evaluate at least:

### Server-Sent Events

Advantages to consider:

- simple
- HTTP-native
- one-way server → client fits event observation
- easy browser support
- reconnect semantics
- `Last-Event-ID`

### WebSockets

Advantages to consider:

- bidirectional
- flexible
- established realtime ecosystem

But do not select WebSockets simply because they are more powerful.

Determine what Linkplane actually needs.

Unless repository evidence demonstrates a need for bidirectional realtime messaging, strongly prefer the smallest transport that correctly exposes the canonical event stream.

Document the decision and rationale.

---

# 7. Resume From Sequence

Linkplane already has sequenced canonical events.

Design reconnect semantics around that existing fact.

A client should be able to:

```text
connect
receive events
disconnect
remember sequence N
reconnect
resume after sequence N
```

Determine:

- query parameter or header
- interaction with SSE `Last-Event-ID`, if SSE is selected
- whether `sequence` or `event_id` is authoritative for continuation
- behavior when requested history has been pruned
- duplicate delivery expectations
- ordering guarantees
- initial-state event behavior

Do not create a new cursor system if the existing sequence model is sufficient.

---

# 8. Client Identity

The API is local-first, but local does not mean unauthenticated forever.

Introduce the **smallest useful client identity model**.

Possible future clients include:

```text
CLI
Desktop GUI
SDK application
MCP server
automation
AI agent
developer tool
```

Do not build full users/accounts/RBAC.

Design enough identity so an action can answer:

> Who requested this?

Possible conceptual fields:

```text
client_id
client_name
client_type
```

Inspect existing audit and actor concepts first.

Reuse them where possible.

---

# 9. Capability Scopes

Design the first small scope vocabulary.

Scopes should describe allowed capabilities, not implementation details.

Good direction:

```text
device.read
state.read
events.read

battery.read

clipboard.read
clipboard.write

files.read
files.write

notifications.send

screen.control

camera.capture

automation.read
automation.write
```

These are examples only.

Derive the actual vocabulary from Linkplane's capability catalogue.

Do not create hundreds of permissions.

We need enough structure to prevent every future API client from automatically having unrestricted device control.

---

# 10. Sensitive Actions

Identify API actions requiring additional protection.

Examples may include:

- shell/script execution
- camera
- microphone/audio capture
- screen control
- file access
- clipboard reading
- destructive actions
- persistent automation creation

Map those onto the existing consent model.

The API must never turn the existing consent boundary into a bypass.

---

# 11. API Binding and Local-First Defaults

Design safe defaults.

Evaluate:

```text
127.0.0.1
Unix socket HTTP
localhost TCP
```

The initial API must not unexpectedly expose itself on the LAN.

Document:

- default bind address
- default port selection
- collision behavior
- configuration mechanism
- whether remote binding exists yet
- what happens if someone requests `0.0.0.0`

Remote access is not part of this implementation milestone unless already required by repository architecture.

---

# 12. API Versioning

The project currently separates:

```text
package SemVer       0.4.0
milestone tags       core-v0.1 / core-v0.2 / api-v0.1
public release tags  vX.Y.Z
contract version     existing contract schema version
```

Do not create unnecessary additional version numbers.

Determine whether initial HTTP endpoints should use:

```text
/v1/devices
```

or unversioned paths backed by versioned contracts.

Explain the tradeoff.

API compatibility must align with the existing project versioning policy.

---

# 13. Response Envelopes

Determine whether successful responses need a universal wrapper.

Evaluate:

```json
{
  "data": {},
  "correlation_id": "..."
}
```

versus direct resource representations.

Avoid wrappers merely because APIs often have them.

Use wrappers only where they provide concrete value.

Errors should remain consistently structured.

---

# 14. Pagination and History

Determine what pagination is required for:

```text
events
jobs
audit
```

Do not overengineer pagination for resources that will remain small.

Where event ordering already has `sequence`, strongly consider whether that can handle event-history traversal.

Document:

- default limit
- maximum limit
- ordering
- continuation
- filtering
- device filter
- event type filter
- time filter if justified

---

# 15. Jobs API

Map the existing job state machine directly.

Existing states:

```text
running
retrying
completed
failed
cancelled
skipped
```

Do not rename them for HTTP.

Define:

```text
GET /jobs
GET /jobs/{job_id}
```

Evaluate whether cancellation should eventually be:

```text
POST /jobs/{job_id}/cancel
```

or another REST representation.

Document but do not implement.

Preserve correlation IDs across retries.

---

# 16. Audit API

The audit log has one structured shape after the refinement pass.

Expose it rather than transforming it into a different API-specific audit schema.

Expected conceptual fields:

```text
audit_id
timestamp
actor
source
device_id
rule_id
job_id
action
decision
result
correlation_id
details
```

Confirm actual repository reality.

Consider privacy/security implications before allowing every future client to read audit history.

---

# 17. Device Representation

Determine the canonical API representation of a Device.

It should come from existing Linkplane models.

Do not expose provider-specific implementation details as the primary identity.

A device may ultimately have multiple providers.

The API should reflect:

```text
Device
   │
   ├── identity
   ├── observed state
   ├── capabilities
   └── available providers
```

without making ADB itself the resource model.

---

# 18. Capability Representation

Capabilities should answer:

> What can Linkplane currently do with this device?

Not merely:

> Which providers are installed?

Design a representation that can eventually distinguish concepts such as:

```text
supported
available
temporarily unavailable
permission required
consent required
provider missing
device disconnected
```

Do not implement future complexity unless current models support it.

Document what can be represented today and what is reserved for later.

---

# 19. OpenAPI

Evaluate whether the first implementation should generate or maintain an OpenAPI specification.

Consider:

- handwritten stdlib HTTP implementation
- contract stability
- future SDK generation
- GUI consumption
- MCP adapter development

Do not add a framework simply to obtain OpenAPI.

Recommend an approach.

---

# 20. Implementation Boundary

Conclude the design with the smallest viable implementation milestone.

It should probably include:

```text
local HTTP server
devices
device state
capabilities
actions
jobs
events history
event streaming
structured errors
basic client identity
basic scopes
```

But determine the exact boundary from the repository.

Explicitly list what should NOT ship in this API implementation pass.

Examples:

```text
remote Internet API
cloud accounts
full RBAC
OAuth
multi-user server
MCP
GUI
SDK generation
plugin marketplace
workflow marketplace
native Android agent
device farm
AI execution
```

---

# Required Repository Inspection

Before writing the design, inspect at least:

```text
git log
git tags
pyproject.toml

docs/api-invariants.md
docs/current-state.md
docs/state-machines.md
docs/versioning.md
docs/adr/

existing contract models
Device
Capability
Provider
Result
Error

event model
event history
daemon socket protocol
subscription implementation

job model
job runner
job storage

audit model
audit writer/reader

rule engine
consent implementation

typed service interfaces

CLI JSON serializers
contract snapshot tests
release smoke test
```

Do not rely on this brief when repository code provides a more precise answer.

---

# Deliverable

Create:

```text
docs/local-api-design.md
```

Do not implement the API.

The document must include:

1. Goals
2. Non-goals
3. Existing architectural constraints
4. Resource model
5. Endpoint table
6. Device schema
7. Capability schema
8. Action request schema
9. Immediate result vs job semantics
10. Job representation
11. Event representation
12. Streaming design
13. Resume semantics
14. Error response schema
15. LP error → HTTP status mapping
16. Client identity model
17. Scope model
18. Consent/security behavior
19. Binding/network defaults
20. Versioning strategy
21. Pagination/history behavior
22. Audit exposure
23. OpenAPI recommendation
24. Implementation boundary
25. Deferred capabilities
26. Open questions requiring founder decisions

Include example JSON where it materially clarifies the contract.

---

# Founder Decisions

Do not silently decide product-sensitive questions.

At the end of the document, create:

```text
## Founder Decisions Required
```

Only put genuinely consequential choices there.

For each decision provide:

```text
Question
Option A
Option B
Recommended choice
Reason
Consequence of choosing differently
```

Avoid asking the founder about low-level implementation details the repository can resolve itself.

---

# Tests

Because this is a design-only pass:

- run the existing full hermetic test suite
- do not alter runtime behavior
- do not require the phone unless inspection reveals a genuine need
- if documentation-only changes cause tests to fail, investigate rather than weakening tests

---

# Commit

If the design is complete and verified, make one focused commit.

Suggested message:

```text
Design local API contract
```

Do not tag a release.

Do not increment the package version.

---

# Report Back

After completing the design pass, report:

1. Files inspected
2. Existing API-relevant models discovered
3. Proposed resource hierarchy
4. Proposed endpoint list
5. Action/result/job model
6. Streaming transport recommendation
7. Resume semantics
8. Error mapping strategy
9. Client identity/scopes model
10. Security and consent boundaries
11. Founder decisions required
12. Exact files changed
13. Test results
14. Commit hash
15. Recommended first API implementation slice

Then STOP.

Do not begin API implementation until the founder reviews the design.

---

# Guiding Principle

> **Expose Linkplane. Do not rebuild Linkplane.**