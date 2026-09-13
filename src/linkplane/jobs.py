"""Job-tracked actions: one cancellable service call with a persisted record.

A job wraps one call to an existing typed service (`backup_photos`, `send_files`,
`use_clipboard`) with a fresh `CancellationToken`, turns its `ProgressEvent`s into a
`JobRecord` under `$XDG_STATE_HOME/linkplane/jobs/<id>.json`, retries only
`transport_unavailable` failures, and can be cancelled per device (the daemon does this
on `device.disconnected`) or all at once (daemon stop). One running job per
(device, action): a second trigger while one runs is recorded as skipped, not queued.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator

from linkplane.core.events import now_iso
from linkplane.operations import CancellationToken, OperationResult, ProgressEvent

STATES = ("running", "retrying", "completed", "failed", "cancelled", "skipped")
TERMINAL = ("completed", "failed", "cancelled", "skipped")
DEFAULT_RETRY_DELAYS = (10.0, 30.0, 90.0)
RETRYABLE = {"transport_unavailable"}

JobCall = Callable[[CancellationToken, Callable[[ProgressEvent], None]], OperationResult[Any]]


def resolve_jobs_dir(path: str | None = None) -> Path:
    from linkplane.paths import state_file

    return state_file("jobs", path, env_name="JOBS")


@dataclass(frozen=True)
class JobRecord:
    id: str
    automation: str
    action: str
    device: str
    state: str
    started: str
    finished: str | None = None
    attempt: int = 1
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    # The causal chain this job belongs to (the triggering event's correlation id);
    # stable across retries because the record is one object updated in place.
    correlation_id: str | None = None
    # Who asked for it: `rule:<name>` for rule-started jobs, `client:<id>` for jobs started
    # over the local API (docs/local-api-design.md §10). None on pre-API records; readers
    # fall back to `rule:<automation>`.
    actor: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "completed"

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_jobs(path: str | None = None, *, limit: int | None = None) -> Iterator[JobRecord]:
    directory = resolve_jobs_dir(path)
    if not directory.exists():
        return
    files = sorted(directory.glob("*.json"))
    if limit is not None:
        files = files[-limit:]
    for file in files:
        try:
            yield JobRecord(**json.loads(file.read_text(encoding="utf-8")))
        except (ValueError, TypeError, OSError):
            continue


class JobRunner:
    def __init__(
        self,
        records_dir: str | None = None,
        *,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
        sleep: Callable[[float], None] | None = None,
        write_records: bool = True,
        on_transition: Callable[[JobRecord], None] | None = None,
    ):
        self.records_dir = resolve_jobs_dir(records_dir)
        self.retry_delays = retry_delays
        self.write_records = write_records
        # Called on every state change (started, retrying, terminal); the daemon audits it.
        self.on_transition = on_transition
        self._running: dict[tuple[str, str], CancellationToken] = {}
        self._by_id: dict[str, CancellationToken] = {}
        self._running_ids: dict[tuple[str, str], str] = {}
        # Re-entrant: run() allocates an id while holding it (the skipped path).
        self._lock = threading.RLock()
        self._counter = 0
        self._sleep = sleep

    # -- bookkeeping ------------------------------------------------------------------

    def _new_id(self, automation: str, action: str, actor: str | None = None) -> str:
        # A rule-started job is labelled by its rule; an API-started job (empty rule) by
        # its actor (`client:gui` -> `client-gui`), so ids stay readable in `automations jobs`.
        label = automation or (actor.replace(":", "-") if actor else "job")
        with self._lock:
            self._counter += 1
            return f"{now_iso().replace(':', '').replace('+', 'p')}-{label}-{action}-{self._counter}"

    def _transition(self, record: JobRecord) -> JobRecord:
        self._save(record)
        if self.on_transition is not None:
            try:
                self.on_transition(record)
            except Exception as error:  # noqa: BLE001 - an audit hook must never break a job
                pass
        return record

    def _save(self, record: JobRecord) -> None:
        if not self.write_records:
            return
        self.records_dir.mkdir(parents=True, exist_ok=True)
        target = self.records_dir / f"{record.id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)

    def running(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(self._running)

    def running_id(self, device: str, action: str) -> str | None:
        """The id of the job currently running for (device, action), if any."""
        with self._lock:
            return self._running_ids.get((device, action))

    def cancel_device(self, device: str, reason: str) -> int:
        with self._lock:
            tokens = [token for (dev, _action), token in self._running.items() if dev == device]
        for token in tokens:
            token.cancel(reason)
        return len(tokens)

    def cancel_all(self, reason: str) -> int:
        with self._lock:
            tokens = list(self._running.values())
        for token in tokens:
            token.cancel(reason)
        return len(tokens)

    def cancel(self, job_id: str, reason: str) -> bool:
        """Cancel one running job by id; False when no such job is running (terminal or unknown)."""
        with self._lock:
            token = self._by_id.get(job_id)
        if token is None:
            return False
        token.cancel(reason)
        return True

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return True
            time.sleep(0.05)
        return not self.running()

    # -- the run ----------------------------------------------------------------------

    def _wait(self, token: CancellationToken, seconds: float) -> bool:
        if self._sleep is not None:
            self._sleep(seconds)
            return token.cancelled
        return token.wait(seconds)

    def run(self, *, automation: str, action: str, device: str, call: JobCall,
            correlation_id: str | None = None, actor: str | None = None,
            on_started: Callable[[JobRecord], None] | None = None) -> JobRecord:
        """Run `call` to completion (with retries); returns the final record.

        `on_started` is called once, on the calling thread, as soon as the record exists
        (state `running`, or `skipped` for a duplicate) so a caller that runs the job on
        another thread can hand the id back before the job finishes.
        """
        key = (device, action)
        token = CancellationToken()
        with self._lock:
            if key in self._running:
                record = JobRecord(self._new_id(automation, action, actor), automation, action, device, "skipped",
                                   now_iso(), now_iso(), error={"code": "already_running", "message": f"{action} already running for {device}"},
                                   correlation_id=correlation_id, actor=actor)
                record = self._transition(record)
                if on_started is not None:
                    on_started(record)
                return record
            self._running[key] = token
            record = JobRecord(self._new_id(automation, action, actor), automation, action, device, "running", now_iso(),
                               correlation_id=correlation_id, actor=actor)
            self._by_id[record.id] = token
            self._running_ids[key] = record.id
        self._transition(record)
        if on_started is not None:
            on_started(record)

        def on_progress(event: ProgressEvent) -> None:
            nonlocal record
            progress = {"phase": event.phase, "message": event.message}
            if event.current is not None:
                progress["current"] = event.current
            if event.total is not None:
                progress["total"] = event.total
            if event.unit:
                progress["unit"] = event.unit
            record = replace(record, progress=progress)
            self._save(record)

        try:
            attempt = 1
            while True:
                result = call(token, on_progress)
                if result.ok:
                    value = result.value
                    payload = value.to_dict() if hasattr(value, "to_dict") else value
                    record = replace(record, state="completed", finished=now_iso(), attempt=attempt, result=payload)
                    break
                error = result.error
                assert error is not None
                if error.code == "cancelled" or token.cancelled:
                    record = replace(record, state="cancelled", finished=now_iso(), attempt=attempt,
                                     error={"code": "cancelled", "message": token.reason or error.message})
                    break
                if error.code in RETRYABLE and attempt <= len(self.retry_delays):
                    delay = self.retry_delays[attempt - 1]
                    record = replace(record, state="retrying", attempt=attempt,
                                     progress={"phase": "retrying", "message": f"{error.message}; retry in {delay:g}s"})
                    self._transition(record)
                    if self._wait(token, delay):
                        record = replace(record, state="cancelled", finished=now_iso(), attempt=attempt,
                                         error={"code": "cancelled", "message": token.reason or "cancelled"})
                        break
                    attempt += 1
                    record = replace(record, state="running", attempt=attempt)
                    self._transition(record)
                    continue
                record = replace(record, state="failed", finished=now_iso(), attempt=attempt,
                                 error={"code": error.code, "message": error.message, "error_code": error.error_code})
                break
        except Exception as exc:  # noqa: BLE001 - a job must always leave a record
            record = replace(record, state="failed", finished=now_iso(), error={"code": "exception", "message": str(exc)})
        finally:
            with self._lock:
                self._running.pop(key, None)
                self._by_id.pop(record.id, None)
                self._running_ids.pop(key, None)
            self._transition(record)
        return record
