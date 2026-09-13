# 0003 — Keep the existing Device/Endpoint model as the resource model

**Status:** Accepted (2026-09-10)

## Context

The brief sketches `Device(id, name, type, provider, capabilities, connection_state,
metadata)`. The repository already has `models.Device(id, name, manufacturer, model, android,
capabilities, endpoints)` and `models.Endpoint(transport, address, state, device_id,
capabilities, details, error)`, frozen at contract version 1 and used by `devices`, `doctor`,
and profiles. A device's stable logical identity is already independent of its address:
profiles bind a hardware serial (`device_id`) to any number of ADB serials and an SSH
endpoint.

## Decision

Keep `models.Device` and `models.Endpoint` unchanged as the resource model. The brief's
fields map onto them: `provider` ≙ `Endpoint.transport`, `connection_state` ≙
`Endpoint.state`, `metadata` ≙ `Endpoint.details`, `type` is implicitly `android` until a
second device type exists. Multi-device semantics come from profiles (`profiles.py`) and
the discovery grouping in `devices.py`, which already exist. Any new field is added with a
default (additive under `docs/api-contracts.md`'s stability policy).

## Alternatives considered

- **A new `core/device.py` matching the brief's field list.** Rejected: it would duplicate
  a frozen, tested model and force every consumer to translate between the two.
- **Renaming `Endpoint.transport` to `provider`.** Rejected as a breaking rename with no
  behavioural gain; the ADR records the mapping instead.

## Consequences

`devices --json` output is unchanged. When a non-Android device type or a device-level
`connection_state` summary is actually needed, add the field with a default and note it in
the contracts document.
