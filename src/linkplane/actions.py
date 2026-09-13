"""The short automation actions: desktop notification, phone notification, shell command.

Long, job-tracked actions (backup, send, clipboard-sync) arrive with the jobs slice; until
then a rule naming them gets an honest "not available yet" outcome rather than a crash.
Every subprocess is an argument array with a timeout; nothing from an event is ever
interpolated into a shell string.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from typing import Any, Callable, Mapping

from linkplane.backup import BackupRequest, backup_photos
from linkplane.clipboard import ClipboardRequest, use_clipboard
from linkplane.core.automation import Automation, Step, StepOutcome, fill
from linkplane.core.events import Event
from linkplane.jobs import JobRecord, JobRunner
from linkplane.transfer import SendRequest, send_files
from linkplane.transports import load_config, ssh_from_config
from linkplane.notification import NotificationRequest
from linkplane.notification import notify_phone as post_notification
from linkplane.transports import BridgeError, run_command

URGENCIES = ("low", "normal", "critical")


def notify_desktop(step: Step, context: Mapping[str, Any], *, runner: Callable[..., str] = run_command) -> StepOutcome:
    message = fill(str(step.options.get("message", "")), context)
    title = fill(str(step.options.get("title", "Linkplane")), context)
    urgency = str(step.options.get("urgency", "normal"))
    if urgency not in URGENCIES:
        return StepOutcome(step.action, False, f"urgency must be one of {URGENCIES}")
    if shutil.which("notify-send") is None:
        return StepOutcome(step.action, False, "notify-send is not installed (libnotify)")
    try:
        runner(["notify-send", "--app-name", "Linkplane", "--urgency", urgency, title, message], timeout=10)
    except BridgeError as error:
        return StepOutcome(step.action, False, str(error))
    return StepOutcome(step.action, True, f"desktop: {message}", {"message": message})


def notify_phone(step: Step, context: Mapping[str, Any], *, sender: Callable[..., Any] = post_notification) -> StepOutcome:
    message = fill(str(step.options.get("message", "")), context)
    title = fill(str(step.options.get("title", "Linkplane")), context)
    serial = context.get("address") if context.get("provider") == "adb" else None
    result = sender(NotificationRequest(message=message, title=title, transport="auto", serial=serial))
    if result.error is not None:
        return StepOutcome(step.action, False, result.error.message)
    return StepOutcome(step.action, True, f"phone: {message}", {"message": message})


def run_shell(step: Step, event: Event, context: Mapping[str, Any], rule: Automation, *, process_runner: Callable[..., Any] = subprocess.run) -> StepOutcome:
    if "run" not in rule.allow:
        return StepOutcome(step.action, False, "shell actions require \"allow\": [\"run\"] on this automation")
    command = step.options.get("command")
    if not command:
        return StepOutcome(step.action, False, "run action needs a 'command'")
    argv = shlex.split(command) if isinstance(command, str) else [str(part) for part in command]
    argv[0] = os.path.expanduser(argv[0])
    environment = {
        **os.environ,
        "LINKPLANE_EVENT": event.type,
        "LINKPLANE_DEVICE": event.device,
        "LINKPLANE_DATA": json.dumps(event.data, sort_keys=True),
    }
    timeout = float(step.options.get("timeout", 300))
    try:
        completed = process_runner(argv, env=environment, timeout=timeout, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return StepOutcome(step.action, False, f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return StepOutcome(step.action, False, f"timed out after {timeout:g}s: {shlex.join(argv)}")
    ok = completed.returncode == 0
    detail = f"exit {completed.returncode}: {shlex.join(argv)}"
    return StepOutcome(step.action, ok, detail, {"exit_code": completed.returncode, "stdout": (completed.stdout or "")[-2000:]})


def _outcome_from_job(step: Step, record: JobRecord) -> StepOutcome:
    data: dict[str, Any] = {"job_id": record.id, "job_state": record.state}
    if record.result:
        data.update(record.result)
    if record.ok:
        summary = record.progress.get("message") or "done"
        return StepOutcome(step.action, True, f"{summary} (job {record.id})", data)
    error = record.error or {}
    return StepOutcome(step.action, False, f"{record.state}: {error.get('message', 'unknown')} (job {record.id})", data,
                       skipped=record.state == "skipped")


def _serial(context: Mapping[str, Any]) -> str | None:
    return context.get("address") if context.get("provider") == "adb" else None


def backup_job(step: Step, event: Event, context: Mapping[str, Any], rule: Automation, jobs: JobRunner, *, service=backup_photos) -> StepOutcome:
    request = BackupRequest(
        destination=fill(str(step.options.get("destination", BackupRequest.destination)), context),
        source=fill(str(step.options.get("source", BackupRequest.source)), context),
        serial=_serial(context),
    )
    record = jobs.run(automation=rule.name, action="backup", device=event.device, correlation_id=event.correlation_id,
                      call=lambda cancel, progress: service(request, progress=progress, cancel=cancel))
    return _outcome_from_job(step, record)


def send_job(step: Step, event: Event, context: Mapping[str, Any], rule: Automation, jobs: JobRunner, *, service=send_files) -> StepOutcome:
    raw_paths = step.options.get("paths") or step.options.get("path")
    if not raw_paths:
        return StepOutcome(step.action, False, "send action needs 'paths'")
    paths = tuple(os.path.expanduser(fill(str(p), context)) for p in (raw_paths if isinstance(raw_paths, list) else [raw_paths]))
    request = SendRequest(paths=paths, transport=str(step.options.get("transport", "auto")),
                          destination=fill(str(step.options.get("destination", SendRequest.destination)), context),
                          serial=_serial(context))
    record = jobs.run(automation=rule.name, action="send", device=event.device, correlation_id=event.correlation_id,
                      call=lambda cancel, progress: service(request, progress=progress, cancel=cancel))
    return _outcome_from_job(step, record)


def clipboard_sync_job(step: Step, event: Event, context: Mapping[str, Any], rule: Automation, jobs: JobRunner, *, service=use_clipboard, transport_factory=None) -> StepOutcome:
    """Long-lived: runs until cancelled (device.disconnected or daemon stop)."""
    def make_transport():
        return ssh_from_config(load_config(), use_environment=False)

    factory = transport_factory or make_transport
    try:
        transport = factory()
    except BridgeError as error:
        return StepOutcome(step.action, False, str(error))
    request = ClipboardRequest("sync", interval=float(step.options.get("interval", 1.0)),
                               prefer=str(step.options.get("prefer", "desktop")), foreground=False)
    record = jobs.run(automation=rule.name, action="clipboard-sync", device=event.device, correlation_id=event.correlation_id,
                      call=lambda cancel, progress: service(request, transport, _serial(context), progress=progress, cancel=cancel))
    return _outcome_from_job(step, record)


JOB_ACTIONS = {"backup": backup_job, "send": send_job, "clipboard-sync": clipboard_sync_job}


def run_step(step: Step, event: Event, context: Mapping[str, Any], rule: Automation, *, jobs: JobRunner | None = None) -> StepOutcome:
    if step.action == "notify-desktop":
        return notify_desktop(step, context)
    if step.action == "notify-phone":
        return notify_phone(step, context)
    if step.action == "run":
        return run_shell(step, event, context, rule)
    if step.action in JOB_ACTIONS:
        if jobs is None:
            return StepOutcome(step.action, False, f"{step.action} needs a job runner (daemon or watch)")
        return JOB_ACTIONS[step.action](step, event, context, rule, jobs)
    return StepOutcome(step.action, False, f"unknown action {step.action}")
