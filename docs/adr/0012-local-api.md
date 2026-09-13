# 0012 — The local HTTP API is a transport adapter inside linkplaned

**Status:** Accepted (2026-09-11, local API Slice 1). Design: `docs/local-api-design.md`
(founder-reviewed); invariants: `docs/api-invariants.md`.

## Context

Every future interface (GUI, SDK, MCP, agents) needs one programmatic way in. The daemon
already owns the observer, the sequenced history, the job runner, the rules engine, and
the audit log; the CLI calls typed services directly. A second control plane would
duplicate all of that.

## Decisions

1. **Hosted inside `linkplaned`** (`api/server.py`, a stdlib `ThreadingHTTPServer` on a
   thread next to the control socket). It is an adapter: parse → Host/Origin check →
   bearer authentication → route → scope → handler; handlers call the registry, the
   observed states, the history file, the job records, and `ActionRuntime`
   (`api/execute.py`), which calls the same typed services and `JobRunner` rules use.
   No provider selection rules, retries, duplicate detection, or consent logic of its own.
   A listener failure (bind error, handler exception) is logged and audited and never
   stops observation; `daemon status` shows `api: null`.
2. **Loopback TCP + bearer token, strict.** Default `127.0.0.1:8741`; any non-loopback bind
   (`0.0.0.0`, `::`, LAN) is refused at start-up with `LP-REQUEST-001` and the daemon runs
   without the API. Tokens only in `Authorization: Bearer`; `?token=`/`?access_token=`/
   `?bearer=` are refused with 400 even when the header is valid; no cookies; tokens never
   logged (`RedactingFilter`), never stored in plaintext (SHA-256), compared in constant
   time. `Host` must be loopback and the bound port; any `Origin` not on the (empty by
   default) allow-list is 403; no `Access-Control-*` headers are ever sent; `OPTIONS` is 403.
3. **`/v1/` paths.** The API major is an interface-compatibility version, distinct from
   the package version, milestone tags, and record schemas (`docs/versioning.md`).
4. **Canonical device identity is `device_id`** (`DeviceProfile.device_id`); profile names
   and `serial:<x>` are aliases resolved by `registry.Registry`. Pinned records keep the
   registry name in their `device` field; the projection carries both.
5. **Actions are capability names with declarative execution** (`api/actions.ActionSpec`):
   `files.send`, `backup.photos`, `clipboard.sync` are jobs (202 + `Location`), everything
   else is immediate (200). No `provider` in the request; Linkplane chooses (declared
   providers in `auto` order) and reports it as provenance. Duplicate jobs surface the
   runner's own `skipped` record as 409 with the running `job_id` in `details`.
6. **`/v1/events/stream` carries canonical Events only** (SSE, `id:` = `seq`). Jobs, audit,
   and rules are their own resources; a transport notification is not a Linkplane Event.
   Resume = `after` (query) or `Last-Event-ID` (header), both "seq > N"; replay from
   history then live from the subscriber queue, de-duplicated by `seq` across the
   boundary; a `stream` control message reports `last_seq` and `gap`.
7. **Errors keep the `LP-` code**: body `{"error": {type, message, code, hints,
   correlation_id, details}}`, status from one table (`api/security.STATUS_FOR_CODE`);
   501 only for a catalogue capability the API does not implement; unexpected exceptions
   are `LP-INTERNAL-001` with a fixed message.
8. **Every action is audited** with `actor: client:<id>`, `source: api`, and the
   correlation id (`action.requested/completed/failed/blocked`, `access.blocked`,
   `api.started/stopped/error`); clipboard text is never written.

## Alternatives considered

- A separate `linkplane api` process over the control socket — rejected: a second
  `JobRunner` and audit writer, or a socket protocol carrying every op (design D1).
- HTTP over a Unix socket by default — rejected for v1: browsers and most SDKs cannot
  reach it; the bearer token closes the multi-user loopback gap (D2).
- WebSockets — rejected: no client→server message exists after subscribing (design §12).
- Job/audit records multiplexed on the event stream — rejected: different identities and
  lifecycles break the one-cursor resume model (D4).

## Slice 2 addendum (2026-09-11)

- **Public contracts describe the domain, not the internal vocabulary.** A Job's public
  `action` is the capability name (`backup.photos`); the record's rules/jobs name is
  `job_action` (`backup`). The persisted record and the rules file are unchanged.
- Every device-associated projection (jobs, events, audit, rules) carries `device_id`,
  resolved from the registry name at read time and null when it no longer resolves.
- `POST /v1/jobs/{id}/cancel` is the existing `JobRunner.cancel`: 202 accepted, 200 already
  cancelled, 409 terminal or stale, 404 unknown. No `cancelling` state.
- `GET /v1/rules` projects the daemon's loaded rules without the path and without `run`
  command text; `POST /v1/rules/reload` takes no body and cannot change the source.
- `GET /v1/audit` reads the log backwards, bounded, through a redaction layer
  (`SENSITIVE_KEYS`); the file is never modified.
- `docs/openapi.json` is generated by `linkplane.api.openapi` and pinned equal to it.
- `camera.capture` stays out of the HTTP runtime until approval gates exist.

## Consequences

The API surface for the `api-v0.1` milestone is complete pending founder review. The CLI stays a direct caller of services;
a `local-cli` client identity is possible later. Moving history and rules to key on
`device_id` remains a deferred, breaking change.
