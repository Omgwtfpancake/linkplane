# 0004 — Dotted capability names with a five-state status vocabulary

**Status:** Accepted (2026-09-10)

## Context

Discovery already reported flat capability names (`status`, `send`, `notify`) with two
states (`ready`/`unavailable`). The brief asks for machine-readable capability discovery
that names *what* a device can do (`battery.read`) and distinguishes supported /
unsupported / unavailable / permission-denied / provider-error.

## Decision

`linkplane.core.capability` defines the catalogue: a core set every provider must report
(`device.ping`, `device.status`, `battery.read`, `storage.read`) plus the capabilities the
existing commands implement (`files.send`, `backup.photos`, `notify.post`,
`clipboard.read/write`, `screen.control`, `camera.capture`). `Provider.capabilities()`
returns one `CapabilityReport(name, status, detail, requirements, metadata)` per catalogue
entry, in catalogue order; entries a provider does not implement are filled in as
`unsupported` so every device lists the same names. `requirements` is reserved for the
Android capability classes A–F/X from the brief and is populated only where known today
(`screen.control` → `D`, needs ADB).

The legacy flat names in `devices --json` are left as they are (frozen contract); the new
vocabulary is exposed by the new `capabilities` command.

## Alternatives considered

- **Rewriting discovery's capability list to the new names.** Rejected: breaking change to
  `devices --json` for no functional gain; both can coexist until a contract version bump.
- **Reporting only supported capabilities.** Rejected: an explicit `unsupported` vs
  `unavailable` vs `permission-denied` answer is exactly what a GUI, an agent, or a support
  conversation needs.

## Note on "undetermined" (2026-09-10, later)

The continuation brief lists an `undetermined` status alongside the five above. It is
not added as a sixth value: `provider-error` already means exactly "the probe could not
run or failed, so the answer is unknown", and two overlapping unknown states would force
every consumer to treat them as one anyway. `doctor` and the CLI use the word
"undetermined" in human text for `provider-error` reports. Vocabulary and catalogue order
are pinned in `tests/test_contracts.py`.

## Consequences

Adding a capability means adding a catalogue entry and a `CapabilityReport` per provider.
Future authorization (capability-based policy) can key on these names directly.
