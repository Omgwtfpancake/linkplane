# Automation Runtime — Design

Status: **sections 1–2 and 5 superseded by Core v0.2 (`docs/core-v0.2-design.md`); sections 3–4 and 6 revised below and being built as a daemon subscriber.**
It exists because `docs/roadmap.md` "Automation" is the next priority and, unlike the
Foundation items before it, it is a multi-session build with real product choices in it.
The purpose here is to make those choices explicit, recommend one, and leave a plan a
session can start on immediately once the scope is confirmed.

Read `docs/vision.md` first. The relevant intent, in the founder's words: automations are
"the actual selling point" — `linkplane watch battery --below 20 --notify ...`, a live
`linkplane events` stream other programs can subscribe to, and eventually
`from linkplane import Phone`. The GUI mockup ends with an "AUTOMATIONS" panel listing
"Backup photos when connected", "Alert PC when battery < 20%", "Sync clipboard". Every
layer below is in service of exactly those three examples working end to end.

## Principles carried over from the rest of the codebase

- **Same services, new driver.** Automations do not get their own copies of backup,
  send, notify, or clipboard logic. An action *is* a call to an existing typed service
  function (`backup_photos`, `send_files`, `post_notification`, ...) with a
  `CancellationToken` and a `progress` callback — the contract frozen as
  `CONTRACT_VERSION = 1` in `docs/api-contracts.md`. That freeze and the cancellation work
  were done specifically so this runtime could be built on top without touching them.
- **Foreground first, daemon second.** Every capability must work as a plain foreground
  CLI command before it is wrapped in a background service. `linkplane events` and
  `linkplane watch` are useful on their own in a terminal or a script, and they are how
  the daemon gets debugged.
- **Orchestrate, don't rewrite.** Desktop notifications go through `notify-send`
  (present on this host, `libnotify`), not a D-Bus client we maintain. Device connection
  events come from `adb track-devices`, which the ADB server already streams — no polling
  loop for the most important event. Process supervision is left to `systemd --user`.
- **Stdlib only, no new runtime dependency.** Threads plus `subprocess` plus JSON files.
  No asyncio rewrite of the transports; the services are synchronous and stay that way.
- **Nothing destructive by default.** An automation that runs arbitrary shell must be
  explicitly allowed to; the audit log is append-only; the daemon never modifies
  `config.json`.

## 1. Event model

An event is one immutable record:

```json
{"ts": "2026-09-10T17:31:04-05:00", "event": "battery.changed", "device": "phone",
 "serial": "DEVICE_SERIAL", "data": {"level": 19, "previous": 21}}
```

`device` is the profile name when the serial belongs to a profile, else the serial.
`data` is per-event and additive. As JSON Lines this is the `linkplane events --json`
stream; the human rendering is the `10:31:04  battery.changed  19%` column format from
the vision doc.

### Event catalogue (v1)

| Event | Source | Detection |
|---|---|---|
| `phone.connected` / `phone.disconnected` | `adb track-devices` | push: the ADB server streams a length-prefixed device list on every change; diff against the last list, emit per serial. Zero cost when idle. |
| `phone.authorized` | same stream | state changes `unauthorized` → `device` |
| `battery.changed` | `dumpsys battery` poll | emit when `level` changes; `data` carries `level`, `previous`, `status`, `powered_by` |
| `battery.low` / `battery.ok` | derived from `battery.changed` | threshold crossing (default 20, configurable per automation); edge-triggered, once per crossing |
| `charging.started` / `charging.stopped` | `dumpsys battery` poll | `powered_by` becomes non-empty / empty |
| `wifi.connected` / `wifi.disconnected` | `cmd wifi status` poll | SSID appears / disappears; `data.ssid` |
| `files.added` | `find -newer <marker>` on a watched directory (default the backup source) | new files since the last poll; `data.paths`, `data.count`. This is the only event that names files, and only paths. |

Polls run only while at least one device is connected (the `track-devices` stream gates
them), on a default interval of 30 s, and only for the sources some subscriber actually
asked for (`events --only battery,wifi`, or the union of the enabled automations'
triggers). Every source is an object with `poll() -> state` and a pure
`diff(previous, current) -> [events]` so tests feed it canned `dumpsys` output and assert
the emitted events, exactly like the existing `parse_battery`/`parse_adb_devices` tests.

SSH-only profiles (no ADB) get `battery.*` via `termux-battery-status` and nothing else in
v1; that is a documented limitation, not a blocker.

## 2. Layers, bottom up

```text
  linkplane automations   (list / add / enable / disable / remove / log)   [CLI, file]
           │
  linkplane daemon        (loads automations.json, runs the engine, writes audit log)
           │
  linkplane watch         (one automation, foreground: "when X [if Y] do Z")
           │
  linkplane events        (the raw stream; --json for machines)
           │
  event sources             (track-devices, battery, wifi, files)  +  job runner
           │
  existing typed services   (backup_photos, send_files, notify, clipboard, ...)
```

Each layer is a separate module and is independently useful:

- `events.py` — sources, diffing, the `EventStream` iterator, and `events` CLI.
- `automations.py` — the automation schema, matching (`when`/`if`), action dispatch,
  `watch` CLI, and the `automations.json` load/save.
- `jobs.py` — runs one service call on a worker thread with a `CancellationToken`,
  turns its progress events into a job record, handles retry/backoff for
  `transport_unavailable`, and persists the record.
- `daemon.py` — the long-running loop: one `EventStream`, N automations, the job runner,
  a control socket (a Unix socket under `$XDG_RUNTIME_DIR/linkplane/`) for
  `status`/`reload`/`stop`/`tail-events`, and the audit log.

## 3. Automation schema

`~/.config/linkplane/automations.json` (JSON, not TOML: consistent with `config.json`,
and stdlib can write it — `tomllib` is read-only):

```json
{
  "schema_version": 1,
  "automations": [
    {
      "name": "backup-when-home",
      "enabled": true,
      "device": "phone",
      "when": "wifi.connected",
      "if": {"ssid": "CenturyLink2347"},
      "do": [
        {"action": "backup", "destination": "~/Pictures/Linkplane"},
        {"action": "notify-desktop", "message": "Photos backed up ({downloaded} new)"}
      ],
      "cooldown_seconds": 3600
    },
    {
      "name": "low-battery",
      "when": "battery.low",
      "if": {"below": 20},
      "do": [{"action": "notify-desktop", "message": "Phone battery {level}%", "urgency": "critical"}]
    },
    {
      "name": "phone-home-script",
      "when": "phone.connected",
      "do": [{"action": "run", "command": "~/scripts/phone-home.sh"}],
      "allow": ["run"]
    }
  ]
}
```

- `when` is one event name. `if` is a flat map of equality/threshold conditions over the
  event's `data` (v1: `==` on strings, `below`/`above` on numbers, `in` on lists). No
  expression language.
- `do` is an ordered list; actions run sequentially, and a failure stops the list
  unless `"continue_on_error": true`.
- `{placeholders}` in strings are filled from the event `data` and the previous action's
  result (`to_dict()` keys), which is how "Notify desktop when complete" gets the count.
- `cooldown_seconds` (default 0) suppresses re-triggering; `battery.low` is additionally
  edge-triggered at the source so it cannot fire every poll.

### Actions (v1)

| Action | Backed by | Notes |
|---|---|---|
| `notify-desktop` | `notify-send` | urgency, title, message |
| `notify-phone` | `notification.post_notification` | existing service |
| `backup` | `backup.backup_photos` | cancellable, resumable, job-tracked |
| `send` | `transfer.send_files` | cancellable, job-tracked |
| `clipboard-sync` | `clipboard.use_clipboard(action="sync")` | long-lived; stopped by `phone.disconnected` or daemon stop via the token |
| `run` | `subprocess` | **off unless the automation lists `"run"` in `allow`**; environment gets `LINKPLANE_EVENT`, `LINKPLANE_DEVICE`, and `data` as `LINKPLANE_DATA` JSON |

The `watch` CLI is exactly one automation built from flags and run in the foreground:

```sh
linkplane watch battery.low --below 20 --notify "Phone battery low"
linkplane watch phone.connected --run ~/scripts/phone-home.sh      # implies --allow run
linkplane watch wifi.connected --ssid Home --backup ~/Pictures/Linkplane
```

## 4. Jobs

A job is one action invocation of a cancellable service. `jobs.py` runs it on a thread
with a fresh `CancellationToken`, consumes its `ProgressEvent`s, and maintains a record:

```json
{"id": "2026-09-10T17:31:05-backup-when-home-1", "automation": "backup-when-home",
 "action": "backup", "state": "running", "started": "...", "progress": {"current": 51, "total": 640, "unit": "files"},
 "result": null, "error": null, "attempt": 1}
```

- Records live in `~/.local/state/linkplane/jobs/<id>.json` (XDG state dir, env
  override `LINKPLANE_STATE_DIR`); `linkplane automations log` reads them.
- **Resumable** means what it already means for backup: the manifest is written per
  file, so a job cancelled by `phone.disconnected` simply re-runs on the next
  `phone.connected` and skips what it has. Send is not resumable (documented).
- **Retries:** only `transport_unavailable` is retried (3 attempts, 10 s → 30 s → 90 s);
  `invalid_request`, `dependency_missing`, `operation_failed` fail the job immediately.
- **Cancellation:** `phone.disconnected` for the job's device cancels its running jobs;
  daemon stop cancels everything and waits up to 10 s (cooperative, at file boundaries)
  before exiting. This is why the cancellation contract had to come first.
- One running job per (device, action) — a second `backup` trigger while one is running
  is recorded as `skipped: already running`, not queued. Keeps v1 free of a scheduler.

## 5. Daemon

`linkplane daemon run` is the foreground process; `linkplane daemon install` writes a
`systemd --user` unit (`linkplane.service`, `Restart=on-failure`) and enables it, and
`daemon status/reload/stop` talk to it over the Unix control socket. `install` is the
only part that is Linux-desktop specific; `run` works anywhere.

Reload re-reads `automations.json` without restarting jobs. The daemon never edits
`config.json`; endpoint drift is left to the existing opportunistic
`profiles refresh`, which the daemon may *call* on `phone.connected` (opt-in automation).

## 6. Audit log and permissions

`~/.local/state/linkplane/audit.jsonl`, append-only, one line per: event received
(optional, `--audit-events`), automation matched, action started/finished/failed/skipped,
job cancelled, daemon start/stop/reload. `linkplane automations log [--follow]` tails
it. This is the "Understandable permissions and activity history" line from the brief's
"Why People Would Pay".

Permissions in v1 are deliberately small: a per-automation `allow` list, and only `run`
requires it. The daemon refuses to load an automation whose actions are not allowed
(`automations list` shows it as `blocked`), rather than silently skipping. The schema
has the field from day one so the GUI and MCP layers can grow it (e.g. `send` limited to
a directory) without a migration.

## 7. Scope options

| | A — Foreground only | **B — Recommended** | C — Full roadmap line |
|---|---|---|---|
| `events` stream (all sources) | ✓ | ✓ | ✓ |
| `watch` (single automation, foreground) | ✓ | ✓ | ✓ |
| `automations.json` + `automations list/add/...` | | ✓ | ✓ |
| `daemon run` + control socket | | ✓ | ✓ |
| `daemon install` (systemd unit) | | ✓ | ✓ |
| jobs with records, retry, cancel-on-disconnect | | ✓ | ✓ |
| audit log | | ✓ | ✓ |
| `allow` permission list | | `run` only | full (per-action scoping, `send` dirs, prompts) |
| Waybar/`events --format` output | | | ✓ |
| `files.added` source | | | ✓ |
| Estimated sessions | 1 | 3–4 | 5–6 |

**Recommendation: B.** A alone does not deliver the vision's headline ("backup when I get
home" needs something running when you are not at a terminal). C's extra permission
scoping and Waybar output are cheap to add later and are not what makes the feature
exist. B is also the smallest scope at which the GUI's "AUTOMATIONS" panel and the future
MCP `watch_event` tool have something real to sit on.

## 8. Build order (for scope B)

1. `events.py`: sources + diffing + `EventStream` + `linkplane events [--json] [--only ...]`.
   Tests: canned `track-devices` frames and `dumpsys`/`cmd wifi` output → expected events.
   Verify on the real phone by unplugging/replugging USB and toggling Wi-Fi.
2. `automations.py`: schema load/validate, `when`/`if` matching, placeholder filling,
   action dispatch for `notify-desktop`/`notify-phone`/`run`; `linkplane watch`.
   Tests: automation × event → matched/not, dispatched action args.
3. `jobs.py`: threaded runner over `backup`/`send`/`clipboard-sync` with cancellation,
   records, retry policy. Tests: fake service returning `transport_unavailable` then
   success; cancel mid-run via the token (the same fixtures as `test_cancellation.py`).
4. `daemon.py`: loop, control socket, audit log, `daemon run/status/reload/stop`.
   Then `daemon install` (systemd unit). Tests: daemon against a fake event source and
   a fake clock; socket commands.
5. Docs: `api-contracts.md` gains `Event`, `Automation`, `JobRecord` contracts (additive,
   no `CONTRACT_VERSION` bump); roadmap/brief updated; a README "Automations" section.

Each step is one commit-able, testable unit and each is independently useful; stopping
after any of them leaves the product better than before.

## 9. Open questions for the founder

1. **Scope:** A, B, or C above? (Recommendation: B.)
2. **Shell actions:** is "off unless `allow: ["run"]`" the right default, or should
   `watch --run` from the CLI be enough consent and only the daemon require the explicit
   allow? (Recommendation: CLI `watch --run` implies allow; the daemon requires it in
   the file.)
3. **Polling cadence:** 30 s default for battery/Wi-Fi while connected, 0 while
   disconnected. Acceptable, or should `battery` be faster when a `battery.low`
   automation is armed?
4. **Where the control socket lives:** `$XDG_RUNTIME_DIR/linkplane/daemon.sock`
   (per-login, cleaned on logout) vs. `~/.local/state/`. (Recommendation: runtime dir.)
5. **`files.added` in v1?** It is the only source that reads file names off the phone on
   a timer; the vision's examples do not need it. (Recommendation: defer to C.)

## 10. Post-v0.2 review (2026-09-10, late)

Core v0.2 built the runtime this document assumed: the event catalogue (ADR 0007), the
daemon with subscriptions (ADR 0008), history, and `events`. Reviewing sections 3–6
against what exists, these are the decisions taken before building:

- **Event names follow the real catalogue.** `phone.connected` → `device.connected`,
  `phone.disconnected` → `device.disconnected`; `battery.low`/`ok`, `charging.*`,
  `wifi.*` are as built. `files.added` is not a source yet, so no rule can use it.
- **Opening observations do not trigger rules.** Every daemon start replays the current
  state as `initial` events; "backup when the phone connects" must not fire on each
  restart. Rules ignore `initial` events unless they set `"on_initial": true`.
- **Conditions are a flat map over `data`.** A scalar value means equality; an object
  value is one operator: `{"below": n}`, `{"above": n}`, `{"in": [...]}`, `{"not": v}`.
  `battery.low` already carries the threshold, so `"if": {"below": 20}` from the draft
  becomes `"if": {"level": {"below": 20}}` on `battery.changed`, or simply `when:
  battery.low` with the daemon's threshold.
- **Placeholders** stay: `{level}`, `{ssid}`, `{device}`, `{type}`, and the previous
  action's result keys; unknown names are left as written, never raise.
- **Scope order.** Slice A (this commit series): rule schema, matching, cooldown, the
  three short actions (`notify-desktop`, `notify-phone`, `run` behind `allow`), and
  `linkplane watch` in the foreground — the vision's headline examples. Slice B: rules
  from `automations.json` hosted in the daemon as a subscriber, `daemon reload`, the
  audit log, `automations list|log`. Slice C: job-tracked `backup`/`send`/`clipboard-sync`
  actions with retry, cancel-on-disconnect, and records.
- **Shell actions.** `watch --run` from the CLI is consent; a rule in the file must list
  `"allow": ["run"]`, and the daemon refuses to load one that does not. Scripts receive
  `LINKPLANE_EVENT`, `LINKPLANE_DEVICE`, `LINKPLANE_DATA` (JSON) and never the
  event text through a shell string.
- **`daemon reload`** is added to the control protocol (re-read rules without restarting).
- **Audit log** as designed (`$XDG_STATE_HOME/linkplane/audit.jsonl`), written by the
  daemon only; `watch` prints outcomes to the terminal instead.

