# 0005 — Structured results and coded errors, added on top of the frozen contract

**Status:** Accepted (2026-09-10)

## Context

Services already return a typed `OperationResult(value | error)` with short error
categories (`transport_unavailable`, …), frozen as contract version 1 the same day. The
brief asks for stable machine-matchable error codes (`LP-CONNECT-001`), friendly CLI text
with hints, structured JSON errors, and a result carrying operation/resource/provider
provenance.

## Decision

- `linkplane.core.errors.LinkplaneError` extends `BridgeError` with `code`
  (``LP-<CATEGORY>-<NNN>``, never renumbered), `hints`, a `title`, and `to_dict()`.
  `classify()` wraps a plain transport `BridgeError` into the closest code from the
  messages the transports actually raise, so the transports did not need to change.
- The CLI renders a `LinkplaneError` as title / message / "Check that:" hints / `Error:
  code`, and the `--json` error envelope gains `code` (and `hints` when present). Other
  errors keep the legacy one-line rendering.
- `OperationError` gains `error_code` and `hints`; `OperationResult` gains `operation`,
  `resource_id`, `provider`, `warnings`, and a `success` alias. All defaulted, so contract
  version 1 is unchanged and `tests/test_contracts.py` still passes.
- `--debug` turns on `logging` at DEBUG to stderr and logs the traceback of a handled
  error; default output stays clean.

## Alternatives considered

- **Replacing `OperationResult` with the brief's shape.** Rejected: it would break the
  contract frozen hours earlier for adapters that do not yet exist; additive fields reach
  the same shape.
- **Making the transports raise `LinkplaneError` directly.** Deferred: `classify()` gives
  the same codes at the provider boundary without touching 450 lines of transport code.
  When a transport is next edited for its own reasons, it can raise coded errors directly.
- **A neutral prefix instead of `LP-`.** The project name is provisional, but the codes
  are internal strings; renaming them later is a mechanical aliasing job.

## Status update (2026-09-10, later)

- `models.Check` gained an additive `code` so `doctor` failures carry a PB code in both
  renderings; the human output appends `(code)` per line and a "N problems found" summary.
- `read_status` keeps the frozen `transport_unavailable` / `invalid_request` categories and
  sets `error_code` / `hints`; the CLI turns a result with an `error_code` back into a
  `LinkplaneError` so status failures render like the other core commands.
- The set of PB codes is pinned in `tests/test_contracts.py` (codes may be added, never
  removed or renumbered).

## Consequences

Every user-visible failure has a code a support conversation can quote and a JSON consumer
can switch on. New codes are added to `errors.py` with a title; `tests/test_core_errors.py`
enforces that every code has one.
