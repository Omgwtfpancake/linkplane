# Core v0.1 — Assessment and Implementation Plan

Written 2026-09-10 in response to `docs/core-v0.1-brief.md` ("First Task for the AI
Agent"). This is the six-point report the brief asks for, the assessment of the brief
against the real repository, the deviations taken and why, and the Definition-of-Done
checklist tracked as work lands.

## 1. Repository structure (as found)

```text
src/linkplane/        flat modular monolith, stdlib only, Python ≥ 3.11
  operations.py         OperationResult / OperationError / ProgressEvent / CancellationToken, CONTRACT_VERSION = 1
  models.py             Device, Endpoint, Capability, Check, DiscoveryResult
  transports.py         AdbTransport, SshTransport (both .status()), run_command, config load/resolve
  status.py devices.py doctor.py profiles.py
  transfer.py backup.py clipboard.py notification.py find.py screen.py audio.py camera.py webcam.py
  dependencies.py       distro-aware install plans
  cli.py                argparse, per-command --device / --json
tests/                  flat unittest files + integration_clipboard.py (live phone); 243 tests, no phone needed
legacy/termux/          phone-status-json.sh, battery-cache.sh — still what SshTransport.status() calls
legacy/linux/           old shell wrappers, superseded by the CLI
docs/                   vision, roadmap, progress brief, api-contracts, architecture, automation-design
```

## 2. Existing working functionality

Verified on the paired phone (DEVICE_SERIAL, USB): `status`, `devices`, `doctor`, `send`,
`backup`, `find`, `profiles refresh`, `clipboard`, the scrcpy wrappers (`screen`, `camera
preview`, `audio`, `webcam`), Ctrl+C cancellation. Config at `~/.config/linkplane/config.json`
with named profiles and a default device. Multi-device semantics exist (profiles, ADB serial
aliases, SSH association). `status`/`devices`/`doctor` emit a versioned JSON envelope.

## 3. Failing or broken functionality

None. Gaps, not breakage: no `logging`, no debug flag, one error class with string
categories, no ADRs, no unit/integration tier split, the SSH daemon on the phone is not
currently running (so the SSH path reports honestly as unreachable).

## 4. Architectural gaps against Core v0.1 (as found → resolution)

| Gap | Resolution |
|---|---|
| No Provider interface; transports duck-typed on `.status()` | `providers/base.py::Provider`; `SSHProvider`, `ADBProvider`; `select_provider()` |
| Capability names flat (`status`, `send`) with two states | `core/capability.py` catalogue: dotted names, five statuses, `requirements` field |
| One `BridgeError`, six string categories, JSON drops the code | `core/errors.py`: `LinkplaneError(code, hints)`, `classify()`, envelope gains `code`/`hints` |
| Missing `capabilities`, `battery`, `ping` | `commands/` package, wired in `cli.py`, `--json` on each |
| Result lacks provenance | `OperationResult` gains `operation`/`resource_id`/`provider`/`warnings` (additive) |
| No ADRs | `docs/adr/0001`–`0006` |
| No logging / debug mode | `--debug` global flag → `logging` at DEBUG, tracebacks logged |
| Doctor lacks codes and config/provider checks | `doctor.py` coded configuration + provider checks, `--transport` (77779ff) |
| Test tiers not separated | `tests/integration/` package, `tests/device/` tier, `Makefile` (76efbba) |
| Config lacks provider type / timeouts | **deliberately not added**: both providers use one 8 s timeout and profiles already carry every endpoint field; a config key would duplicate profile data with nothing reading it. Revisit when a provider needs a different default. |

## 5. Files created / modified in the first slice

Created: `core/{__init__,errors,capability}.py`, `providers/{__init__,base,ssh,adb}.py`,
`commands/{__init__,battery,ping,capabilities}.py`, `docs/adr/*`, `tests/test_providers.py`,
`tests/test_core_errors.py`, `tests/test_core_commands.py`, this file.
Modified: `cli.py` (three subcommands, `resolve_provider`, coded error rendering, `--debug`),
`operations.py` (additive fields), `devices.py` (`probe_ssh_endpoint` extracted, behaviour
unchanged), `tests/test_cancellation.py` (envelope now carries `code`).
Not moved: any existing module.

## 6. Assessment of the brief

**Right, and adopted as written:** capability ≠ implementation; explicit non-goals; typed
results and coded errors; doctor as a first-class feature; provider contract tests; ADRs;
stdlib modular monolith; clean default output with a debug mode.

**Where it does not match the repository, and the resolution taken:**

- It describes a Termux/SSH prototype with status and battery. The repo has ten working
  commands over ADB (primary) and SSH (fallback). Read literally ("SSHProvider only, ADB is
  future"), it would regress the product. **Both existing transports are wrapped as equally
  thin providers**; ADR 0006 records the deviation.
- Its non-goals include things that already exist (scrcpy integration, camera, audio).
  Read as **freeze — don't extend, don't remove**, consistent with its own "do not delete
  working functionality".
- Its suggested `core/ providers/ commands/` layout would mean moving every module. It also
  says not to reorganize blindly. **New subpackages hold new code; flat modules stay.**
- Its result shape is reachable **additively** from the frozen `OperationResult`; no
  contract bump.
- `--device` as a global flag vs. the existing per-command flag: existing behaviour is
  tested and works; cosmetic cleanup later.
- Config "provider type" and "timeouts": nothing needs them until a second provider needs
  different defaults; deferred.
- The `LP-` code prefix is tied to a provisional name; codes are internal strings and can be
  aliased later, so kept.

**Also noted:** the automation runtime design (`docs/automation-design.md`) is paused, not
cancelled — "Automation engine" is a v0.1 non-goal. The accompanying Master Product Vision
arrived truncated at its section 3.

## Definition of Done — tracking

| # | Criterion | State |
|---|---|---|
| 1 | Installs cleanly in the dev environment | ✓ (`pip install -e .`, stdlib only) |
| 2 | Existing Android/Termux functionality still works | ✓ 291 tests, real-device smoke |
| 3 | Phone represented through the Device abstraction | ✓ existing `models.Device` (ADR 0003) |
| 4 | SSH behind a Provider interface | ✓ `SSHProvider` |
| 5 | Capabilities queryable | ✓ `capabilities [--json]` |
| 6 | `devices` works | ✓ (pre-existing) |
| 7 | `status` works | ✓ routed through the providers (eb00978), behaviour unchanged |
| 8 | `battery` works | ✓ verified on device |
| 9 | `ping` works | ✓ verified on device |
| 10 | `doctor` identifies meaningful failures | ✓ coded configuration/provider checks (77779ff) |
| 11 | JSON output structured and stable | ✓ shared envelope, additive `code`/`hints` |
| 12 | Unit tests need no live phone | ✓ |
| 13 | Live-device tests separated | ✓ `tests/device/` tier, never in the normal run (76efbba) |
| 14 | Errors typed and understandable | ✓ `LinkplaneError` + friendly rendering |
| 15 | Existing functionality not broken | ✓ |
| 16 | Another provider could replace SSH without touching the CLI | ✓ `ProviderContract` + `FakeProvider` CLI tests prove it |
| 17 | Code simple enough for a small project | ✓ ~1,100 lines added, no dependencies |

## Closing the milestone (2026-09-10, late)

Tasks 1–5 of `docs/core-v0.1-continuation.md` are done: doctor (77779ff), provider-backed
status (eb00978), test tiers (76efbba), contract pin (this commit), configuration reviewed
and left alone (above). Release gate: normal suite green without a phone; device tier green
over ADB (SSH round trip skipped while the phone's `sshd` is down); ADRs 0002/0004/0005/0006
updated in place; docs match the code. **Tag:** no convention existed; `core-v0.1` is used
(an annotated tag) rather than `v0.1.0`, because the package already declares version
0.9.0 in `pyproject.toml` and a semver tag below it would mislead. Recommendation for the
founder: decide whether the package version should be reset to track milestones
(`0.1.0` for Core v0.1) before any public release.

## Next steps as originally planned (all done unless noted)

1. **Doctor**: add config / default-device / SSH-identity / reachability / auth / remote
   script / provider-responds checks with `LP-` codes on failures; keep the existing
   dependency checks.
2. **Status through the provider**: route `status` via `resolve_provider().status()` so
   the last core command stops touching transports directly (behaviour identical).
3. **Test tiers**: `tests/integration/` for the live-phone script(s), documented `device`
   tier convention; unit suite unchanged.
4. **Contracts doc**: pin `CapabilityReport`, `PingResult`, `BatteryReading` in
   `tests/test_contracts.py` once their shape has survived a session of use.
5. **Config**: `provider`/`timeouts` keys when a second provider needs them.
