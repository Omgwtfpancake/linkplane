# Refinement Pass — Inspection Report and Plan

Written 2026-09-10 in response to `docs/refinement-pass-brief.md` §23. Facts below were read
from the code at commit 277b08c, not inferred. Status of each item is tracked at the end.

## 1. Git state

Clean tree on `master` at 277b08c (the rename to `linkplane` is 2df56ba; the brief was
written before it and its §17 no longer applies). No remote, nothing published. Tags
`core-v0.1`, `core-v0.2`. Normal suite: 427 tests, no phone.

## 2. Current event schema (`core/events.py`)

`Event(type, device, ts, provider, data, seq)` + `"schema": "linkplane.event/1"` on the
wire. `initial` lives inside `data`. `seq` is assigned only by the history writer (per file,
monotonic, repaired tail). There is no event id, no source, no correlation id. Producers: the
observer (all device events), the observer itself (`observer.started/stopped`). The pinned
field names are `type/device/ts/seq`, not the brief's `event_type/device_id/timestamp/sequence`.

## 3. Current audit schema (`automations.py`, `daemon.py`)

Every line is `{schema: "linkplane.audit/1", ts, kind, ...free fields}`. Kinds and their
ad-hoc fields:

| kind | fields |
|---|---|
| `daemon.started` | pid, socket |
| `daemon.stopped` | reason, fired |
| `rules.loaded` | path, loaded, active, blocked |
| `rules.blocked` | automation, actions, reason |
| `rules.error` | error, code |
| `automation.fired` / `automation.skipped` | automation, event{…}, ok, outcomes[…] |
| `automation.error` | automation, event, error |
| `jobs.cancelled` | device, count, reason |

No actor, no device on most, no rule/job ids, no decision field, no correlation. Job
start/finish is not audited at all (only visible through the firing's outcomes and the job
record).

## 4. Current job states (`jobs.py`)

`running → succeeded | failed | cancelled`, plus `skipped` (duplicate for the same
device+action, terminal, created already finished). Retry is not a state: the record stays
`running` with `progress.phase = "retrying"`. Cancellation reasons: the device disconnected,
daemon stop, or the service returned `cancelled`. Terminal: succeeded, failed, cancelled,
skipped. There is no `queued` (jobs run immediately on the firing thread) and no `blocked`
(consent is decided at rule load, before any job exists). Behaviour is identical for backup,
send, and clipboard-sync; only their resumability differs (backup resumes by manifest).

## 5. Current rule states

File level: a rule is **active**, **blocked** (uses `run` without `allow`), or the whole
file is in **error** (unparseable; previous rules stay). Per event: **not matched**,
**matched → skipped** (cooldown), **matched → fired** with outcomes each `ok | failed |
skipped`; a step failure stops the rule unless `continue_on_error`. `initial` events never
match unless `on_initial`. A firing that raises is audited as `automation.error`.

## 6. Existing IDs and correlation

Job ids: `<ts>-<rule>-<action>-<n>`. History `seq`. Nothing else. A firing carries the
`Event` object, and `StepOutcome.data` carries `job_id`, so the chain is reconstructible by
hand from the audit file but there is no single identifier to grep for.

## 7. Terminology inconsistencies

- **rule vs automation**: the code class is `Automation`, the CLI is `automations`, the
  audit kinds say `automation.*` and `rules.*`, docs say "rule". Preferred: **Rule**.
- **step vs action**: `Step` is a rule's requested action; `StepOutcome.action` names it.
  Preferred: **Action** (requested) vs **Job** (tracked execution).
- **transport vs provider**: `StatusResult.transport`, `Endpoint.transport`,
  `SendResult.transport`, `NotificationResult.transport`, `CaptureResult.transport`,
  `ClipboardResult.transport` are all provider names ("adb"/"ssh"). Frozen fields; the
  preferred public term is **provider**.
- **succeeded vs completed**: job terminal state; the brief's vocabulary is `completed`.
- **device naming**: events, state, jobs, and rules all use `device` = profile name or
  `serial:<x>`; models.Device uses `id`. Consistent enough; the API will call it `device_id`.
- **kind vs event_type**: audit uses `kind`, events use `type`.
- **Firing**: internal name for "a rule fired"; fine internally, "rule firing" in docs.

## 8. Documentation issues

- The Master Product Vision existed only as a truncated fragment in the v0.1 brief; the
  complete 99-page PDF has now been converted to `docs/MasterProductVision.md`.
- No current-state document; `product-progress-brief.md` mixes vision, status, and
  commercial thinking; `roadmap.md` mixes done and planned; `core-v0.1-plan.md`,
  `core-v0.2-design.md`, `automation-design.md` are milestone-scoped and partly stale.
- API invariants and client-scope direction are not written down anywhere.
- No smoke-test procedure document; the device tier exists but covers only Core v0.1.
- Founder briefs are verbatim and should reference the vision, not embed it (the v0.1
  brief's appendix still embeds the fragment).

## 9. Versioning facts

`pyproject.toml` has said `0.9.0` since the first pairing commit on 2026-09-08 (be22603) and
never changed. There is no git remote, no PyPI publication, no installer, no external user;
the only install is this machine's launcher. Tags `core-v0.1` and `core-v0.2` mark
architecture milestones and are not package versions.

**Recommendation — approved by the founder and applied (package 0.9.0 → 0.3.0; `docs/versioning.md`):** three separate things.
1. *Architecture milestone tags* stay `core-vX.Y` (and later `api-vX.Y`); they are not
   releases.
2. *Package version* follows SemVer and describes what a user installs. Because 0.9.0 was
   never public, reset it to **0.3.0** now (0.1 = Core v0.1, 0.2 = Core v0.2, 0.3 = the
   automation section), and bump `MINOR` per shipped section until a public 1.0.
3. *Public releases* are tagged `vX.Y.Z` on the exact commit that `pyproject.toml` declares,
   and only those get changelog entries and packages. None exist yet.
No version lower than a public one is created because none is public.

## 10. Files proposed for modification

Code: `core/events.py` (event_id, correlation_id, source), `core/state.py`, `observe.py`
(assign ids/source), `history.py`, `core/automation.py` (`Rule` alias, correlation through
firings, `Action` naming), `actions.py`, `jobs.py` (state names, `retrying` state,
correlation on records), `automations.py` (audit schema), `daemon.py` (audit entries, job
audit), `commands/automations.py`, `commands/events.py`, `commands/watch.py`,
`tests/test_contracts.py` (pin additive fields), tests for propagation.
Docs: `docs/current-state.md`, `docs/state-machines.md`, `docs/api-invariants.md`,
`docs/smoke-test.md`, `tests/device/test_release_smoke.py`, `docs/api-contracts.md`,
`README.md`, ADR 0011 (refinement decisions), `core-v0.1-brief.md` (reference the vision).

## 11. Smallest safe first change

Correlation and event identity, because everything else (audit normalization, job records,
state machines) wants the ids to exist: add `event_id`, `correlation_id`, and `source` to
`Event` with defaults (additive; pinned fields untouched), generate them in the observer,
and propagate through `Engine.fire` → `StepOutcome`/`Firing` → `JobRunner.run` →
`JobRecord` → audit. Tests prove one id survives the whole chain, including a retry.

## Status

| Item | State |
|---|---|
| Terminology plan (§1) | this document; `Rule`/`Action`/`Job` applied in code where non-breaking |
| State machines (§2, §12, §13) | `docs/state-machines.md` |
| Observed state vs result (§3) | documented in `docs/current-state.md`; enforced: jobs never write `DeviceState` |
| Correlation ids (§4) | code + tests |
| Event schema (§5) | additive fields; regression tests kept |
| Audit schema (§6) | normalized writer; readers updated |
| Action vs Job (§7) | naming + docs |
| API invariants + scopes (§8–11) | `docs/api-invariants.md` |
| Smoke test (§14) | `tests/device/test_release_smoke.py` + `docs/smoke-test.md` |
| Docs cleanup (§15–17) | `current-state.md`, `MasterProductVision.md`, brief references |
| Versioning (§18) | policy in `docs/versioning.md`; package reset to 0.3.0 with the founder's approval |
| Logging (§19) | reviewed in `current-state.md` |
| Reliability map (§20) | table in `current-state.md` |
