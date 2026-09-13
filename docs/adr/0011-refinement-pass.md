# 0011 — Refinement pass: identity, correlation, states, audit shape

**Status:** Accepted (2026-09-10)

## Context

Before the local API multiplies every abstraction, the founder asked for a coherence pass
(`docs/refinement-pass-brief.md`): consistent vocabulary, explicit state machines, a way to
trace one cause through events, rules, jobs, and audit, and one audit shape.

## Decisions

1. **Every event has an identity and a chain.** `Event.event_id` (uuid4 hex) and
   `Event.correlation_id` (defaults to the event's own id). A rule firing, the job it
   starts, and every audit entry carry the triggering event's `correlation_id`; retries
   never change it. Ids are opaque and carry nothing sensitive. Additive to the pinned
   `Event` fields.
2. **Job states are** `running | retrying | completed | failed | cancelled | skipped`;
   terminal = the last four. No `queued` (jobs start immediately) and no `blocked`
   (consent is decided at rule load). `succeeded` was renamed `completed` — job records
   were not pinned, and the brief's vocabulary wins. Transitions are documented in
   `docs/state-machines.md` and reported through `JobRunner.on_transition`.
3. **One audit shape** (`linkplane.audit/2`): `schema, audit_id, ts, kind, actor, source,
   decision, device?, rule?, job_id?, action?, correlation_id?, details`. Optional keys are
   omitted rather than null. `decision` is a closed vocabulary. Job state changes are
   audited by the daemon through the transition hook. v1 lines remain readable.
4. **Vocabulary**: Rule / Action / Job / State / Result are the public terms. Frozen
   fields keep their names (`transport`, `automation`) with the preferred term documented
   in `docs/api-contracts.md`; `Rule` is exported as an alias and `Firing.to_dict()` writes
   both `automation` and `rule`. No public CLI command was renamed.
5. **Observed state is written only by the observer.** Results feed rule context, job
   records, and audit — never `DeviceState`.
6. **The release smoke test is code** (`tests/device/test_release_smoke.py`, `make smoke`)
   with a written procedure (`docs/smoke-test.md`); the normal suite still needs no phone.
7. **API invariants and the client/scope direction** are recorded in
   `docs/api-invariants.md` before any endpoint exists.
8. **Versioning**: milestone tags (`core-vX.Y`) are not package versions; package SemVer
   and public release tags (`vX.Y.Z`) are separate. With the founder's approval the
   package version was reset from the never-published 0.9.0 to 0.3.0 (`docs/versioning.md`).

## Alternatives considered

- **Separate trace ids per layer (firing id, job id, audit id) linked by parent fields.**
  Rejected for now: a single shared `correlation_id` answers "what did this event cause"
  with one grep; per-record ids (`event_id`, job `id`, `audit_id`) still exist for
  addressing.
- **Renaming frozen `transport` fields to `provider`.** Rejected: a breaking change with
  no functional gain; the API can present the preferred name.
- **A `queued` job state.** Rejected: nothing queues; inventing the state would describe
  behaviour that does not exist.

## Consequences

The API can expose `/events`, `/jobs`, `/audit` with a shared `correlation_id` filter and
present job and rule states verbatim from `docs/state-machines.md`.
