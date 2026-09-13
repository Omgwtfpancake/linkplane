# Linkplane — Current State

> What exists right now (user-facing docs: `README.md`, `docs/install.md`,
> `docs/troubleshooting.md`, `docs/cli-reference.md`; public snapshot: `tools/public-export.sh`).
> Originally written for the refinement pass (2026-09-10). Strategy lives in
> `docs/MasterProductVision.md`; the *why* of each design lives in `docs/adr/`. This
> document is the one to update when the code changes.

**Linkplane** — *Make real devices programmable.* An open, local-first control plane for real
devices, starting with Android. (Formerly PhoneBridge; ADR 0010.)

## What exists

A Python, stdlib-only host application for Linux, driving one or more Android devices over
ADB (primary) or Termux/SSH (fallback), with a persistent daemon.

```text
CLI (22 commands) · linkplane watch · local HTTP API clients (GUI / SDK / MCP later)
        │                                        │
   linkplaned ─── Unix control socket: status · state · subscribe · reload · stop
        ├── Local API     http://127.0.0.1:8741/v1 (bearer token, scopes, SSE) — ADR 0012
        ├── Observer      adb track-devices (push) + battery / Wi-Fi polls
        ├── State         DeviceState per device  → state.json (observed state)
        ├── Events        canonical, sequenced, identified, correlated → events.jsonl
        ├── Rules         automations.json · conditions · cooldown · consent-gated run
        ├── Jobs          backup / send / clipboard-sync · records · retry · cancel on disconnect
        └── Audit         audit.jsonl, one structured shape per line
        │
   Providers: ADBProvider · SSHProvider   (capability catalogue, LP- coded errors)
        │
   Typed services: status · backup · send · notify · clipboard · screen · camera · webcam · audio · find · profiles
```

Milestones: `core-v0.1` (Device/Capability/Provider/Result/Error models around the existing
commands), `core-v0.2` (daemon, events, history, systemd unit), the automation section
(rules, jobs, audit), this refinement pass, and local API Slices 0–2 (registry, clients,
the HTTP listener inside the daemon: devices, state, capabilities, actions, jobs and
cancel, events and SSE, rules and reload, audit). Next: the API milestone review
(`api-v0.1`) by the founder; `camera.capture` over HTTP stays deferred.

## Vocabulary

| Term | Meaning | In code |
|---|---|---|
| Device | a logical device; canonical id `device_id` (hardware serial), profile name and `serial:<x>` are aliases | `registry.DeviceRecord`, `DeviceProfile`, `models.Device` (discovery) |
| Provider | how a capability is implemented for one endpoint (adb, ssh) | `providers.Provider` |
| Capability | what a device can do, `battery.read` etc., with a five-state status | `core.capability` |
| State | what Linkplane currently believes about a device (observed) | `core.state.DeviceState` |
| Event | one immutable record of a change, with `event_id`, `correlation_id`, `seq` | `core.events.Event` |
| Rule | a definition in `automations.json`: when / if / do | `core.automation.Automation` (alias `Rule`) |
| Action | an operation a rule requests (`notify-desktop`, `backup`, …) | `core.automation.Step`, `actions.py` |
| Job | the tracked execution of a long action | `jobs.JobRecord`, `JobRunner` |
| Result | what happened when an action ran | `operations.OperationResult`, `StepOutcome` |
| Error | a coded failure `LP-<CATEGORY>-<NNN>` | `core.errors.PhoneBridgeError` |
| Audit | the append-only record of decisions and outcomes | `automations.AuditWriter` |

Frozen fields that predate this vocabulary keep their names; the preferred term is
documented beside them in `docs/api-contracts.md` (`transport` → provider, `automation` →
rule, `kind` → event type, `succeeded` → completed).

**Observed state vs operation result.** `DeviceState` is written only by the observer from
provider reports. Results (`BackupResult`, a job's `result`) live in job records and audit
entries and feed later rule actions; they never update `DeviceState`. See
`docs/state-machines.md`.

## Commands

`status battery ping capabilities devices doctor` (doctor also reports USB access, configuration permissions, desktop notifications) · `send backup notify clipboard find` ·
`screen audio camera webcam` · `pair profiles` · `events [--follow]` · `watch` ·
`daemon run|status|stop|reload|install|uninstall` · `automations list|log|jobs` ·
`clients create|list|revoke` · `setup` (guided first run: `docs/install-onboarding-design.md` §8–§9;
composes the dependency catalogue, ADB device states, USB pairing, the unit installer, the
daemon socket, and the ADB provider; no state file of its own; `--dry-run`, `--json`,
`--non-interactive`, `--name`, `--serial`, `--no-daemon`, `--api-client ID`) ·
`uninstall [--purge] [--force]` (stops/disables/removes the user unit and the runtime
socket/discovery files; keeps configuration, profiles, history, audit, jobs, automations;
`--purge` deletes only the fixed roots `config_dir()`, `state_dir()`, `runtime_dir()` after
listing them and an explicit confirmation, never a path read from configuration, never
through a symlink, never while a daemon runs; the software itself is removed by its
installer, e.g. `pipx uninstall linkplane`).
Every machine-readable command supports `--json` (envelope `schema_version` 1); `events
--json` streams `Event` lines instead.

## Files on disk

| Purpose | Path (`~/.config/linkplane`, `~/.local/state/linkplane`, `$XDG_RUNTIME_DIR/linkplane`) |
|---|---|
| config + profiles | `config.json`; `webcam.json`; `automations.json`; `clients.json` (API clients, 0600) |
| observed state | `state.json` (`linkplane.state/1`) |
| events | `events.jsonl` (`linkplane.event/1`, monotonic `seq`, tail repaired on open) |
| audit | `audit.jsonl` (`linkplane.audit/2`) |
| jobs | `jobs/<id>.json` |
| control socket | `daemon.sock` (0600); `status` reports the daemon's package `version` |
| API discovery | `api.json` (`{url, pid, protocol}`, 0600, next to the socket) |

Pre-rename locations (`phonebridge`) and `PHONEBRIDGE_*` overrides still resolve
(`linkplane/paths.py`).

## Dependencies

One catalogue, `dependencies.DEPENDENCIES`, says what every external thing is for
(`core-required` → `android-base` → `capability-optional` / `provider-optional` /
`developer-only`, host or phone side) and which capabilities need it; the `*_dependency_plan`
helpers say how to detect and, per package manager, install it (`pacman`, `apt-get`, `dnf`,
`brew` as data; nothing is ever run without `install_dependency` being called explicitly).
`dependency_report()` is the one answer to "can the USB/ADB onboarding proceed?":
only Python and `adb` (and, at runtime, the USB udev rules → `LP-AUTH-003`) can block it;
`scrcpy`, `notify-send`, `v4l2-ctl`, `v4l2loopback`, `ssh`, `localsend-cli`, the desktop
clipboard tools, and the phone-side Termux / Termux:API only make a capability unavailable.
`clipboard.read/write/sync` and `camera.capture` run through the SSH provider and need
Termux:API on the phone; the ADB provider reports them `unsupported` with that reason.

## Output channels

| Channel | Where | Rule |
|---|---|---|
| user-facing output | stdout (text or `--json`) | clean by default; never tracebacks; exit codes 0/1/130/141 |
| operational log | `logging` (`linkplane.*`) to stderr | WARNING by default, DEBUG with `--debug`; never secrets (paths only, never key material) |
| audit | `audit.jsonl` | decisions and outcomes only, never debug chatter |
| events | `events.jsonl` / socket | domain changes only |
| provider chatter | captured | `adb push` etc. never reach stdout of the daemon or CLI |

## Known limitations

- SSH-only devices are not observed by the daemon (no push source); they are reachable by
  the one-shot commands.
- `files.added` is not an event source; rules cannot react to new photos yet.
- Polling interval is fixed (30 s default); no adaptive polling.
- Send is not resumable after cancellation (backup is, by manifest).
- Consent is per rule for `run` only; the API enforces client scopes but has no
  per-action approval gate (reserved `LP-CONSENT-001`).
- `camera.capture` is in the catalogue but not in the HTTP action runtime (privacy
  boundary; per-action approval gates are deferred).
- No GUI, MCP, packaging, or Android agent yet.
- Package version is `0.5.0`, the first public alpha (`v0.5.0`, a GitHub pre-release;
  `docs/releases/v0.5.0.md`, `docs/versioning.md`). No PyPI or AUR distribution yet.

## Regression safeguards

| Defect found | Safeguard |
|---|---|
| SIGTERM from systemd left socket and snapshot behind | `test_sigterm_stops_the_daemon_cleanly` |
| duplicate opening observations after a reconnect | `test_concurrent_polls_are_serialized_and_do_not_duplicate_observations` |
| partial history tail swallowed the next record | `test_seq_continues_across_writers_and_skips_corrupt_tail` |
| job runner self-deadlock on the duplicate path | `test_second_job_for_same_device_and_action_is_skipped` (re-entrant lock) |
| unit test silently needed a live phone | `test_find_success_envelope_uses_shared_schema_version` patches `run_command` |
| fake observer ignored cancellation (slow stops) | `FakeObserver` polls the token; `test_stop_command` |
| `adb reconnect` detaches the USB device | documented: use `adb usb` (`docs/smoke-test.md`, tests) |
| broken pipe traceback on `events \| head` | `test_closed_stdout_exits_141_without_a_traceback` |
| adb chatter on the daemon's stdout | transfer captures subprocess output |
| adb `no permissions` (udev) reported as "phone has not authorized this computer" | `tests/test_transports.py`: the state is parsed as a phrase and classified `LP-AUTH-003` with host-side hints |
| a second phone paired under an existing profile name inherited that profile's `device_id` | `PairingIdentityTests`: strict identity for USB/SSH pairing, `ro.serialno` verification for wireless, coded conflict with a suggested name |
| an unwritable configuration surfaced as "the phone command failed" | `ConfigWriteFailureTests`: `LP-CONFIG-004` with hints, kept through `OperationResult` and the CLI |
| optional tools could be mistaken for setup blockers; `notify-send`/`v4l2-ctl` had no plan | `DependencyCatalogueTests`: `dependency_report().base_ready` ignores every non-blocking entry; plans for both tools |
| ADB provider said clipboard/camera capture were merely "not implemented" | `test_local_api_capabilities_are_reported_per_provider`: detail names the SSH provider + Termux:API requirement |
| an SSE/socket stream could close on the cancellation flag before `observer.stopped` was produced (flaky shutdown test) | stream loops end only on the daemon's end-of-stream sentinel or a departed client; late subscribers get the sentinel at once; `ApiServer.close()` waits (bounded, condition-driven) for streams to drain; `test_stream_delivers_observer_stopped_even_when_cancelled_before_it_is_produced` |
| onboarding claimed but unproven outside the checkout | `docs/clean-machine-onboarding.md` procedure; `docs/testing/onboarding-run-001.md` (pipx wheel install in a fresh HOME, real phone, real user unit, TTFUA 83 s / 2.9 s / 0.5 s) |
| purge could reach a backup destination, a configured path, `$HOME`, `/`, or a symlink target | `tests/test_uninstall.py`: hostile backup/send paths in `config.json` survive; a symlink inside a root is unlinked not followed; a root that is a symlink is refused; `refuse_reason` bounds |
| a guided setup could drift from reality (state file), duplicate profiles, mint tokens, or block on optional tools | `tests/test_setup.py`: every phase transition, rerun idempotence, install accept/decline, unauthorized/offline/no-permissions, multi-device choice, name collision, daemon absent/current/stale/failing, never observed, first-use failure, Ctrl+C, optional deps, `--api-client` |
