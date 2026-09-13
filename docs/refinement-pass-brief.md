# Linkplane — AI Terminal Refinement Context

Received from the founder on 2026-09-10 (late), verbatim. Two facts changed between its
writing and its receipt: the package/CLI rename to `linkplane` had already been performed on
explicit instruction (commit 2df56ba, ADR 0010), so §17's "do not rename" no longer applies
and the codebase is uniformly `linkplane`; and `CONTRACT_VERSION` is 2 (the rename), not 1.
The work of this pass is tracked in `docs/refinement-pass-plan.md`.

---

## Current State

Linkplane is now a working local control plane for Android devices.

Current repository status:

```text
50 commits
21 CLI commands
420 tests
11,582 lines of Python
9 ADRs
Core v0.1 tagged
Core v0.2 tagged
Automation section complete
```

The project currently includes:

```text
Device / Capability / Provider / Result / Error models
ADBProvider
SSHProvider
Typed service layer
Persistent daemon
Unix control socket
Observed device state
Canonical event stream
Event history
Automation rules
Consent-gated actions
Tracked jobs
Audit log
Systemd user service
Three test tiers
Stable error codes
Frozen contract version 1
```

All 21 commands have been exercised against the paired Android 16 device.

The normal test suite does not require a live phone.

The next major planned feature is a **local API over the daemon**.

Before implementing that API, perform a focused **stability, consistency, and coherence pass**.

Do not add a new feature family during this pass.

---

# Goal of This Pass

The goal is to make the existing architecture:

```text
consistent
predictable
observable
documented
easy to expose safely through an API
```

The API will multiply whatever abstractions already exist.

Therefore:

> Fix naming, contracts, states, IDs, and boundaries now before those choices become public interfaces.

---

# 1. Standardize Domain Terminology

Inspect the repository for inconsistent or overlapping terms.

The preferred public vocabulary is:

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

Use these concepts consistently across:

```text
services
CLI
daemon
events
automation
jobs
audit
documentation
future API models
```

Transport-specific concepts such as:

```text
adb
ssh
scrcpy
Termux
```

should remain implementation details unless the user is explicitly selecting or diagnosing a provider.

Avoid exposing implementation-specific names where a domain-level name already exists.

Example:

Prefer:

```text
device.status
battery.read
files.send
screen.control
```

over:

```text
adb_status
ssh_battery
scrcpy_screen
```

Do not rename working public CLI commands or frozen contract fields casually.

If terminology cleanup would break version-1 contracts, preserve compatibility and document the preferred term instead.

---

# 2. Define State Machines Explicitly

The project now has enough long-running behavior that job/rule/action states need precise meanings.

Inspect current states and normalize them.

For jobs, define semantics for states such as:

```text
queued
running
retrying
blocked
cancelled
failed
completed
```

Determine:

```text
What transitions are valid?
Which states are terminal?
What happens on disconnect?
What happens on retry?
What happens on user cancellation?
What happens when a duplicate job is requested?
```

Do not invent unnecessary states.

Use the smallest set that accurately describes existing behavior.

If current behavior differs by action type, document why.

---

# 3. Separate Observed State From Operation Results

Preserve a strong conceptual distinction:

```text
Observed state:
What Linkplane currently believes is true about the device.

Operation result:
What happened when Linkplane attempted an action.
```

Examples:

Observed state:

```text
battery = 73%
connected = true
wifi = Home
charging = false
```

Operation result:

```text
backup completed
13 new files
0 unchanged
```

Do not let action results silently become current device state unless an explicit state update occurs.

The future API must be able to distinguish these cleanly.

---

# 4. Add Correlation / Trace IDs

Introduce a lightweight correlation mechanism across:

```text
Event
Rule firing
Action
Job
Result
Audit entry
```

Goal:

A developer should be able to trace:

```text
device.connected event
        ↓
rule matched
        ↓
backup action requested
        ↓
job created
        ↓
provider work performed
        ↓
job completed
        ↓
notification fired
        ↓
audit record written
```

with one shared identifier or a clear parent/child relationship.

Do not build a distributed tracing platform.

A UUID/string ID and consistent propagation is enough.

Requirements:

```text
Generated once at the start of a causal chain
Propagated when practical
Present in structured logs/audit records
Stable during retries
Never contains sensitive information
```

Add tests for propagation.

---

# 5. Normalize Event Schema

Inspect the canonical event format and make sure all event producers follow the same structure.

Target conceptual fields:

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

Only add fields that genuinely help the current system.

Do not break frozen contracts unnecessarily.

Confirm:

```text
Events are ordered
Sequence numbers are monotonic where intended
Initial observations are identifiable
Reconnect behavior does not duplicate opening observations
Malformed partial history is safely repaired
```

Existing regression fixes around duplicate reconnect events and partial history must remain covered.

---

# 6. Normalize Audit Schema

The audit log is becoming a core observability and security feature.

Inspect all current audit entries.

Aim for a consistent schema such as:

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

Not every field must be populated for every entry.

Do not add meaningless empty complexity.

The important goals are:

```text
Who or what caused it?
What happened?
Which device was involved?
Was it allowed, blocked, skipped, failed, or completed?
What causal chain did it belong to?
```

Preserve append-only behavior.

Do not turn the audit log into generic debug logging.

---

# 7. Clarify Action vs Job

Make the model clear:

```text
Action = requested operation
Job = tracked long-running execution of an action
```

Examples:

```text
notify
    may complete immediately

backup
    becomes a tracked job

send
    may become a tracked job

clipboard sync
    becomes a tracked job
```

Avoid using "job" and "action" interchangeably.

Future API consumers need predictable semantics.

---

# 8. API Boundary Rules

The local API is next, but do not implement it until this refinement pass is complete.

Record these API invariants first.

The API must:

```text
Expose Linkplane domain concepts
Reuse typed services
Reuse daemon state
Reuse canonical events
Reuse jobs
Reuse existing error codes
Reuse existing contracts where appropriate
```

The API must NOT:

```text
Reimplement provider logic
Reimplement retries
Reimplement job scheduling
Create a second event model
Create a second error model
Call CLI commands as subprocesses
Expose ADB/SSH internals as the primary API model
```

Preferred architecture:

```text
CLI ───────┐
REST API ──┤
Future GUI ├── Linkplane Core / Daemon
Future MCP ┤          │
Future SDK ┘          ├── State
                      ├── Events
                      ├── Rules
                      ├── Jobs
                      └── Providers
```

---

# 9. API Resource Design Direction

Do not copy CLI command names directly into HTTP endpoints.

Prefer resource/domain-oriented concepts.

Possible direction:

```text
GET  /devices
GET  /devices/{id}
GET  /devices/{id}/state
GET  /devices/{id}/capabilities

GET  /events
GET  /jobs
GET  /jobs/{id}
GET  /audit

POST /devices/{id}/actions
```

Exact API design should come from current contracts and services.

Do not implement endpoints yet during this polish pass unless specifically asked after design review.

---

# 10. Event Streaming Must Be First-Class

The daemon already supports subscriptions.

Do not design the API as request/response-only.

The API design should account for live streams such as:

```text
device events
job progress
state changes
rule activity
health changes
```

Possible future transports may include:

```text
Server-Sent Events
WebSocket
streaming local socket
```

Do not choose one prematurely without evaluating the current daemon model.

The key requirement is:

> The canonical event stream remains the source of truth.

---

# 11. Preserve Security Boundaries

The automation layer already requires explicit consent for shell actions.

Keep that precedent.

Before external API clients exist, introduce the concept of:

```text
client identity
capability scopes
```

Conceptually:

```text
client: local-cli

scopes:
  device.read
  battery.read
  files.send
```

Do NOT implement full authentication/RBAC yet unless required.

But the API model must not assume:

```text
every caller is fully trusted forever
```

Avoid public APIs that grant arbitrary shell execution.

---

# 12. Review Rule Semantics

Inspect:

```text
conditions
cooldowns
changes-only behavior
placeholders
consent requirements
reload behavior
blocked rules
```

Confirm they behave consistently.

Document exact behavior for:

```text
rule loaded
rule matched
rule skipped
rule blocked
rule fired
rule action failed
rule action cancelled
```

Preserve current live reload behavior.

Do not expand the rule language during this pass.

---

# 13. Review Job Semantics

Current tracked jobs include:

```text
backup
send
clipboard sync
```

Existing behavior includes:

```text
records
progress
retry
one job per device/action
cancel on disconnect
```

Verify all job types behave consistently around:

```text
creation
duplicate start
progress
retry
disconnect
cancellation
completion
failure
cleanup
audit
```

Keep the regression test covering the previous self-deadlock.

---

# 14. Release Smoke Test

Create or formalize one repeatable live-device smoke-test procedure.

It should be clearly separate from normal tests.

Target coverage:

```text
devices
status
battery
ping
capabilities
doctor

daemon start/status/stop
observed state
event subscription
disconnect/reconnect

rule reload
consent block
rule firing

tracked backup or send job
job completion
audit entry
```

The normal suite must still run without a phone.

The smoke test should document prerequisites and expected output.

Do not make CI depend on the physical phone.

---

# 15. Documentation Cleanup

Create clear separation between three kinds of documentation.

## Current State

A concise document describing:

```text
what exists now
current architecture
working commands
current milestones
known limitations
```

Possible file:

```text
docs/current-state.md
```

## Architecture Decisions

Keep:

```text
docs/adr/
```

ADRs explain why major decisions were made.

## Master Product Vision

Store the complete long-term vision separately:

```text
docs/MasterProductVision.md
```

The current repository copy is truncated and must be replaced with the complete vision.

Do NOT embed the full master vision inside build briefs.

Build briefs should reference it.

---

# 16. Fix the Truncated Vision Document

The repository currently contains only a fragment of the Master Product Vision ending around:

```text
3. Device Agent
```

Replace that fragment with the complete current Linkplane Master Product Vision.

Keep it separate from:

```text
core-v0.1 brief
core-v0.2 design
automation design
API design
```

The vision document is strategic guidance, not an implementation checklist.

---

# 17. Resolve Product Naming in Documentation

The working brand is now:

# Linkplane

Tagline:

**Make real devices programmable.**

Technical positioning:

> **An open, local-first control plane for real devices.**

The repository/package may still internally use:

```text
phonebridge
```

for now.

Do NOT perform a large code/package rename during this refinement pass unless explicitly instructed.

However, user-facing strategy/product documents may begin referring to:

```text
Linkplane
```

with a short note that the implementation/package currently retains the PhoneBridge codename.

Avoid a mixed naming mess inside the codebase.

---

# 18. Resolve Versioning Strategy

Current issue:

```text
pyproject.toml = 0.9.0

milestone tags:
core-v0.1
core-v0.2
```

Do not change version numbers blindly.

First inspect:

```text
package history
git tags
release history
whether anything has been published publicly
whether external users depend on 0.9.0
```

Then propose a simple policy distinguishing:

```text
architecture milestone tags
package SemVer
public releases
```

Do not create a version lower than an already-public version without a clear reason.

Report recommendation before modifying package version.

---

# 19. Logging Review

Separate:

```text
User-facing output
Operational logs
Debug logs
Audit records
Canonical events
```

Do not use one channel for every purpose.

Ensure:

```text
normal CLI output stays clean
debug mode gives useful detail
audit entries remain structured
events remain domain events
logs do not leak credentials
```

---

# 20. Reliability Review

Preserve regression coverage for defects already found:

```text
SIGTERM cleanup
duplicate reconnect observations
partial history tail repair
job-runner self-deadlock
test accidentally requiring live phone
slow cancellation in fake observer
unsafe adb reconnect behavior
```

Inspect whether each has an explicit regression test or documented operational safeguard.

Do not remove these tests during cleanup.

---

# 21. Scope Freeze

During this refinement pass, do NOT add:

```text
new automation features
MCP
GUI
cloud
device farm
native Android agent
new providers
remote access
plugin marketplace
workflow marketplace
AI execution
major packaging work
```

The next feature section remains:

```text
Local API
```

but only after the polish pass is complete.

---

# 22. Definition of Done for Refinement Pass

This pass is complete when:

```text
Domain terminology is consistent
Job states are defined
Rule states are defined
Action vs Job is clear
Observed State vs Operation Result is clear
Correlation IDs exist and are tested
Event schema is consistent
Audit schema is consistent
API invariants are documented
Security/client-scope direction is recorded
Live-device smoke test is formalized
Truncated Master Product Vision is fixed
Current-state docs are separated from vision
Versioning recommendation is documented
All tests pass
Live-device smoke test passes
No working functionality regressed
```

---

# 23. First Task

Before changing code:

1. Inspect git status.
2. Inspect latest commits.
3. Run the complete normal test suite.
4. Inspect:

   * daemon
   * event model
   * event history
   * state model
   * rules
   * jobs
   * audit records
   * typed services
   * current contracts
   * ADRs
   * documentation
5. Compare actual repository behavior against this refinement context.

Then report:

```text
1. Current git state
2. Current event schema
3. Current audit schema
4. Current job states
5. Current rule states
6. Existing IDs/correlation behavior
7. Terminology inconsistencies found
8. Documentation issues found
9. Versioning facts
10. Exact files proposed for modification
11. Smallest safe first change
```

Then begin the refinement work.

---

# Execution Priority

Use this order unless repository evidence strongly suggests otherwise:

```text
Inspect current models
        ↓
Terminology cleanup plan
        ↓
State-machine definitions
        ↓
Correlation IDs
        ↓
Event schema normalization
        ↓
Audit schema normalization
        ↓
Rule/job consistency
        ↓
Release smoke-test formalization
        ↓
Documentation cleanup
        ↓
Versioning recommendation
        ↓
Full regression suite
        ↓
Live-device verification
        ↓
API design pass
```

Do not jump ahead.

---

# Guiding Principle

The project does not need more breadth right now.

It needs coherence.

> **Make the existing control plane predictable before making it public through an API.**

Finish this polish pass first.

Then design the local API on top of the stable model.
