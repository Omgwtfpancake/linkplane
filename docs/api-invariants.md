# Local API — Invariants and Design Direction

Recorded before any endpoint exists (refinement pass, `docs/refinement-pass-brief.md`
§8–§11). The API design pass that follows must satisfy everything here; the design itself
(transport, exact resources, auth) is a separate document.

## Invariants

The API **must**:

- Expose Linkplane domain concepts: Device, Capability, State, Event, Action, Job, Rule,
  Audit, Provider, Result, Error — with the vocabulary in `docs/current-state.md`.
- Reuse the typed services (`docs/api-contracts.md`) for every action; reuse the daemon's
  observed state (`state.json` / the `state` op), the canonical event stream (`subscribe`),
  the job runner and records, the rules loader, the audit log, and the `LP-` error codes.
- Keep the canonical event stream as the source of truth for anything live. A client that
  wants "current state" reads observed state; a client that wants "what happened" reads
  events, jobs, or audit — never a synthesized mixture.
- Distinguish **observed state** (`GET /devices/{id}/state`) from **operation results**
  (`GET /jobs/{id}`, the action response): a result never mutates state through the API.
- Carry `correlation_id` through: an action request may supply one; responses, jobs, events
  it causes, and audit entries echo it.

The API **must not**:

- Reimplement provider logic, retries, job scheduling, cooldowns, or consent — those live in
  the services, `JobRunner`, `Engine`, and the rules loader.
- Create a second event model or a second error model; `Event.to_dict()` and
  `PhoneBridgeError.to_dict()` are the wire shapes, versioned by their `schema` tags.
- Call CLI commands as subprocesses.
- Expose ADB/SSH internals as the primary model. `provider` appears as a property of a
  device or a result, and as an explicit selection/diagnosis knob, nothing more.
- Grant arbitrary shell execution. `run` actions exist only inside consent-gated rules; the
  API exposes no "execute this string" endpoint.

## Resource direction (not yet implemented)

```text
GET  /devices                       registry: profiles + observed devices
GET  /devices/{id}                  identity, provider, capabilities summary, connection
GET  /devices/{id}/state            DeviceState (observed)
GET  /devices/{id}/capabilities     CapabilityReport[] (queried live or from the snapshot)
POST /devices/{id}/actions          {action, params, correlation_id?} -> immediate Result or a Job
GET  /jobs, /jobs/{id}              JobRecord; DELETE /jobs/{id} = cancel
GET  /rules, POST /rules/reload     LoadedRules; the file stays the source
GET  /events                        history with since=seq / since=ts
GET  /audit                         audit entries, filter by device, rule, job, correlation
GET  /health                        daemon describe()
```

Ids: `{id}` for a device is the registry name (`phone`, or `serial:<x>`), never an address.
Job ids and correlation ids are the opaque strings already in use.

## Streaming

Subscriptions already exist over the Unix socket (newline-delimited JSON, filtered by type
and device). The API design must keep streaming first-class for: device events, job
progress, state changes, rule activity, daemon health. Candidate transports — Server-Sent
Events over the same HTTP listener, WebSocket, or exposing the socket stream directly —
are to be chosen in the design pass by evaluating the current daemon model, not assumed.
Whatever the transport, each streamed record is an `Event` (or a job/audit record) with its
`seq`/`audit_id`, so a client can resume from the last one it saw via `GET /events?since=`.

## Client identity and capability scopes (direction only)

No caller is fully trusted forever. Before external clients exist the model is:

```text
client:  local-cli | local-gui | <named local process> | <future remote>
scopes:  device.read  device.state.read  events.read  audit.read
         battery.read  files.send  notifications.send  screen.control ...
         rules.reload  jobs.cancel  shell.execute (never granted by default)
```

- Scopes are capability names (`docs/adr/0004`), so policy speaks the same language as the
  capability catalogue and the vision's permission scopes (`docs/MasterProductVision.md`
  §21).
- The Unix socket's filesystem permission (0600) is the only authentication today and is
  adequate for a same-user local API. The design pass decides how a second client class is
  identified (a token file per client is the smallest step); it is not implemented now.
- Every API-originated action is audited with `actor: client:<name>` — the audit schema's
  `actor` field exists for exactly this.
- The `run` action's consent model is the precedent: a scope that can execute arbitrary
  commands requires an explicit grant per client and is never implied by "admin".

## Errors

`PhoneBridgeError` codes (`LP-…`) map to HTTP status in one table in the design; the JSON
body is the existing error object (`type, message, code, hints`). Unknown codes are
treated by clients as `LP-PROVIDER-001`.
