# 0010 — The project is Linkplane

**Status:** Accepted (2026-09-10)

## Context

"PhoneBridge" was a working codename (the founder's brief said not to invest in it). The
founder chose **Linkplane** as the product name. The rename touches the package, the CLI,
environment variables, config/state/runtime paths, the daemon and its systemd unit, the
error-code prefix, on-disk schema tags, and every living document.

## Decision

- Package `linkplane`, command `linkplane`, daemon `linkplaned`, unit `linkplaned.service`,
  socket `$XDG_RUNTIME_DIR/linkplane/daemon.sock`, config `~/.config/linkplane/`, state
  `~/.local/state/linkplane/`, env `LINKPLANE_*`, codes `LP-<CATEGORY>-<NNN>`, schema tags
  `linkplane.event/1`, `linkplane.state/1`, `linkplane.audit/1`, `linkplane.daemon/1`.
- **Existing users lose nothing.** `linkplane/paths.py` resolves every file location once:
  the new location is used, unless only the pre-rename one exists; `PHONEBRIDGE_*`
  environment overrides are honoured as a fallback for one release; a backup directory
  with a `.phonebridge-manifest.json` is read (and continued) under the new name.
- `CONTRACT_VERSION` 1 → 2. The in-process import path and the code prefix are part of
  the frozen surface, so the rename is, by the policy in `docs/api-contracts.md`, a
  breaking change — the only one — and every field, code number, phase, and event type is
  otherwise identical to version 1. `schema_version` (the `--json` envelope) stays 1: the
  envelope's shape did not change.
- The founder's verbatim documents (`vision.md`, `core-v0.1-brief.md`,
  `core-v0.1-continuation.md`) and the dated progress reports keep the old name with a
  note; ADRs and living docs are rewritten. Git tags `core-v0.1`/`core-v0.2` are history
  and stay.

## Alternatives considered

- **Keeping `PB-` codes and `phonebridge` paths under the new name.** Rejected: the codes
  and paths are user-visible, and "PB" would be a permanent fossil in a product that has
  no B in it.
- **A `phonebridge` compatibility shim package.** Rejected: no external consumer exists
  yet; the path fallback covers the only thing that matters (existing files).

## Consequences

The launcher, the repository folder, and the founder's desktop notes are renamed on this
machine. The legacy fallback can be removed after one release.
