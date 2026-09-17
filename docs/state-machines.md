# State Machines

The exact states and transitions of the long-running parts of Linkplane, as implemented
(refinement pass, `docs/refinement-pass-brief.md` §2, §12, §13). These are the semantics the
local API will expose; anything not listed here does not happen.

## Vocabulary

- **Action** — a requested operation named in a rule (`notify-desktop`, `notify-phone`,
  `run`, `backup`, `send`, `clipboard-sync`). Short actions complete inside the rule firing.
- **Job** — the tracked execution of a long action (`backup`, `send`, `clipboard-sync`):
  one record, progress, retries, cancellation. An action *becomes* a job; a job is never
  created without an action.
- **Rule** — a definition in `automations.json` (class `Automation`, preferred term Rule).
- **Firing** — one rule reacting to one event; produces one outcome per action.

## Jobs

```text
                       ┌──────────── retryable failure ────────────┐
                       ▼                                           │
   (created) ──► running ──────────────────────────────────► retrying
                   │  │  │                                     │   │
                   │  │  └── service ok ───────► completed     │   │ cancel
                   │  └───── non-retryable ────► failed   ◄────┘   │
                   └──────── cancelled ────────► cancelled ◄───────┘

   (duplicate request while a job for the same device+action runs) ──► skipped
```

| State | Meaning | Terminal |
|---|---|---|
| `running` | the service call is executing | no |
| `retrying` | the last attempt failed with `transport_unavailable`; waiting out the backoff before the next attempt | no |
| `completed` | the service returned a successful result (`result` holds it) | yes |
| `failed` | the service returned a non-retryable error, retries were exhausted, or the call raised (`error` holds it) | yes |
| `cancelled` | the job's token was cancelled: the device disconnected, the daemon stopped, or the service reported `cancelled` (`error.message` holds the reason) | yes |
| `skipped` | a job for the same (device, action) was already running; nothing ran | yes |

Rules:

- **Creation** is immediate: there is no queue and no `queued` state. A job starts on the
  firing's thread (daemon: the firing pool) the moment its action is reached.
- **Duplicate start**: one running job per (device, action). The second request is recorded
  as `skipped` (with `error.code = already_running`) and the rule's outcome is `skipped`.
- **Progress**: every `ProgressEvent` from the service updates `progress` and rewrites the
  record file atomically.
- **Retry**: only `transport_unavailable` is retried, with delays 10 s, 30 s, 90 s (three
  retries, four attempts). `attempt` counts attempts; `correlation_id` never changes.
  Any other error code fails immediately. Retry waits are cancellable.
- **Disconnect**: `device.disconnected` for the job's device cancels its running jobs
  (audited as `job.cancelled` with reason `device.disconnected`). A backup cancelled this
  way resumes on the next run because its manifest is written per file; send does not
  resume; clipboard-sync simply stops.
- **Daemon stop**: cancels every running job and waits up to 10 s (cooperative, at file or
  interval boundaries).
- **Cleanup**: the record file stays (it is the job's history); the running-set entry is
  removed in every exit path, including an exception.
- **Audit**: `job.started`, `job.retrying`, and one of `job.completed` / `job.failed` /
  `job.cancelled` / `job.skipped`, each carrying `job_id`, `device`, `rule`, `action`, and
  `correlation_id`.
- **Consent** never reaches the job layer: a rule that lacks `allow` for `run` is blocked at
  load time, so there is no `blocked` job state.

Behaviour is identical for the three job actions; only what a cancelled job leaves behind
differs (above), which is a property of the underlying service, not of the job runner.

## Rules

File level, decided at load (`daemon run`, `daemon reload`):

| State | Meaning |
|---|---|
| `active` | parsed, all actions allowed; will be matched against events |
| `blocked` | uses a privileged action (`run`) without listing it in `allow`; never matched; reported by `daemon status`, `automations list`, and audited as `rule.blocked` |
| (file) `error` | the file did not parse; audited as `rules.error`; the previously loaded rules stay in force |

Per event, for every active rule:

```text
event ──► not matched            (type, device, conditions, or an `initial` event without on_initial)
      └─► matched ──► skipped     (cooldown not elapsed)            → audit rule.skipped
                  └─► fired ──► outcomes, one per action, in order  → audit rule.fired
                                  ok       action completed (job: completed)
                                  failed   action failed  (job: failed / cancelled) — stops the
                                           rule unless continue_on_error
                                  skipped  job skipped (duplicate)
                  └─► error       the firing raised                   → audit rule.error
```

Semantics confirmed by tests:

- Conditions are a flat map over `data`; scalar = equality, `{below|above|in|not}` are
  the only operators; a missing key never matches.
- Cooldown is per rule, measured from the last non-skipped firing.
- Changes only: an `initial` event (start-up snapshot or first telemetry after a
  reconnect) never fires a rule unless `on_initial` is true. Only devices present in the
  observer's first snapshot are initial; a phone plugged in later, including the first time
  this daemon sees it, fires `device.connected` rules without `on_initial`.
- Placeholders fill from event data, event identity, and previous outcomes; unknown names
  are left as written.
- *(v0.6 development)* A step with its own `if` is skipped (ok) when unmet. When a step
  fails (not skipped), the rule's `on_error` steps run once, in order, after the `do` list
  stops; their outcomes are appended to the firing, which stays failed, and they never
  re-enter `on_error`.
- Live reload replaces the rule set atomically; cooldown timers survive a reload for rules
  that keep their name.

## Observed state vs operation result

`DeviceState` is only ever written by the observer from what a provider reports. A job's
result (a `BackupResult`, a `SendResult`) is recorded in the job record and the audit log
and is handed to later actions of the same rule; it never updates `DeviceState`. The two
are different questions — "what is true about the device" and "what happened when we
acted" — and the API keeps them on different resources (`/devices/{id}/state` vs `/jobs/{id}`).
