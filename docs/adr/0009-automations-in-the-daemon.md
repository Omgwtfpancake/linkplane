# 0009 — Automation rules run inside linkplaned, off the observer thread, and are audited

**Status:** Accepted (2026-09-10, automation slice B)

## Context

`linkplane watch` (slice A) proved the rule engine but only for one rule, in the
foreground. The product needs rules that keep working with no terminal open, several at
once, and a record of what fired and why (docs/automation-design.md §6, the brief's
"understandable permissions and activity history").

## Decision

- Rules live in `~/.config/linkplane/automations.json` (`LINKPLANE_AUTOMATIONS`),
  parsed by `core.automation.parse_automations`. The daemon loads them at start and on
  `daemon reload` (a new control-socket op); a broken file never stops the daemon from
  observing — it is audited as `rules.error` and the previous rules stay in force.
- A rule using a privileged action (`run`) without listing it in `allow` is **set aside**
  as blocked, reported in `daemon status`, `automations list`, and the audit log — never
  silently skipped and never rejected with the whole file.
- The daemon's fan-out queues each event to a single rules worker thread; actions
  (scripts, phone notifications) therefore never delay the observer, the history writer,
  or socket subscribers. A full queue drops with a warning rather than blocking.
- The audit log `$XDG_STATE_HOME/linkplane/audit.jsonl` (`LINKPLANE_AUDIT`) is written
  by the daemon only: `daemon.started/stopped`, `rules.loaded/blocked/error`,
  `automation.fired/skipped/error`, each with the full `Firing` record.
  `automations log [--follow]` reads it; `automations list` reads the rules file.

## Alternatives considered

- **Rules as external socket subscribers** (a separate `linkplane automate` process).
  Rejected for now: one more process to supervise, and the daemon already owns history
  and state; the socket API stays available for that shape later.
- **A thread per firing.** Rejected: ordering of a rule's steps and cooldown accounting
  are simpler with one worker; parallelism can be added per rule if a slow action ever
  matters.
- **Rejecting the whole file when one rule lacks consent.** Rejected: it would take every
  other rule down with it; "blocked" is more honest and more useful.

## Slice C addendum: jobs

Long actions (`backup`, `send`, `clipboard-sync`) run as jobs (`jobs.py`): one call to the
existing typed service with a fresh `CancellationToken`, a record under
`$XDG_STATE_HOME/linkplane/jobs/` updated on every progress event, retries only for
`transport_unavailable` (10 s, 30 s, 90 s), one running job per (device, action) — a repeat
trigger is recorded as *skipped*, not queued. Firings now run on a small thread pool
(`Engine.select` on the rules thread, `Engine.fire` on the pool) so a twenty-minute backup
never delays a battery notification. `device.disconnected` cancels that device's jobs;
daemon stop cancels all and waits up to 10 s, which is exactly what the cooperative
cancellation contract (Core v0.1) was built for. The job's result keys join the rule
context, so a notification after a backup can say `{downloaded}`.

## Consequences

Job-tracked actions (slice C) plug in as `run_step` implementations plus records. The
GUI's "AUTOMATIONS" panel and an API have a file to edit, a `reload` to call, and a log to
show. Editing the file is by hand for now; `automations add|enable|disable` are additive.
