"""Run one API action through the existing services and job runner (design §8–§10).

This module is the only place the HTTP layer touches services, and it touches them the
way the CLI does: the provider comes from `providers.select_provider` (ADB first, SSH as
the fallback, an explicit single-provider action honoured exactly), the request is the
service's own `Request` dataclass built by `api.actions.build_request`, and long actions
go through the daemon's `JobRunner`. Nothing here retries, deduplicates, or decides
consent — those already live where they live.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from linkplane import backup as backup_module
from linkplane import clipboard as clipboard_module
from linkplane import find as find_module
from linkplane import notification as notification_module
from linkplane import transfer as transfer_module
from linkplane.api.actions import JOB, ActionSpec, build_request
from linkplane.automations import AuditWriter
from linkplane.core import errors
from linkplane.core.capability import PERMISSION_DENIED, SUPPORTED, UNSUPPORTED, CapabilityReport
from linkplane.core.events import now_iso
from linkplane.jobs import JobRecord, JobRunner
from linkplane.operations import CancellationToken, OperationResult, ProgressCallback
from linkplane.providers import Provider, select_provider
from linkplane.registry import DeviceRecord, Target
from linkplane.transports import AdbTransport, BridgeError, SshTransport, ssh_from_config

log = logging.getLogger("linkplane.api")

JOB_START_TIMEOUT = 10.0


from linkplane.api.security import ApiError as ActionError  # noqa: E402 - one transport error type


@dataclass(frozen=True)
class ActionContext:
    device: DeviceRecord
    target: Target
    actor: str
    correlation_id: str


# -- provider selection -----------------------------------------------------------------


def ssh_factory_for(target: Target) -> Callable[[], SshTransport] | None:
    ssh = target.ssh or {}
    if not ssh.get("host") or not ssh.get("user"):
        return None
    return lambda: ssh_from_config({"ssh": ssh}, use_environment=False)


def select(target: Target, spec: ActionSpec, *, adb_factory: Callable[[str | None], AdbTransport] = AdbTransport) -> Provider:
    """The provider for this action on this device: the action's declared providers in
    the existing `auto` order (ADB, then SSH). A device with no ADB endpoint is never
    resolved through `adb_factory(None)` (that would pick *any* connected phone)."""
    wants_adb = "adb" in spec.providers
    wants_ssh = "ssh" in spec.providers
    ssh_factory = ssh_factory_for(target)
    if wants_adb and wants_ssh:
        transport = "auto" if target.serial else "ssh"
    elif wants_adb:
        transport = "adb"
    else:
        transport = "ssh"
    if transport == "adb" and not target.serial:
        raise errors.LinkplaneError(
            errors.CONNECT_NO_DEVICE, f"{target.name} has no ADB endpoint", errors.ADB_HINTS,
        )
    if transport == "ssh" and ssh_factory is None:
        raise errors.LinkplaneError(
            errors.CAPABILITY_UNAVAILABLE, f"{spec.name} needs the SSH provider and {target.name} has no SSH endpoint",
            ("pair it with `linkplane pair ssh`",),
        )
    return select_provider(
        transport, serial=target.serial if transport != "ssh" else None, serial_from_profile=True,
        ssh_factory=ssh_factory, adb_factory=adb_factory,
    )


def check_capability(provider: Provider, spec: ActionSpec, target: Target) -> CapabilityReport:
    """Design §8.2 step 7: the provider's own capability report decides."""
    reports = {report.name: report for report in provider.capabilities()}
    report = reports.get(spec.name)
    if report is None or report.status == UNSUPPORTED:
        raise ActionError(
            errors.CAPABILITY_UNSUPPORTED,
            f"{target.name} cannot do {spec.name} via {provider.name}" + (f": {report.detail}" if report and report.detail else ""),
            details={"capability": report.to_dict() if report else {"name": spec.name, "status": UNSUPPORTED}},
        )
    if report.status == PERMISSION_DENIED:
        raise ActionError(
            errors.AUTH_UNAUTHORIZED_DEVICE, report.detail or f"{target.name} refused {spec.name}",
            errors.ADB_HINTS[1:] if provider.name == "adb" else (), details={"capability": report.to_dict()},
        )
    if report.status != SUPPORTED:
        raise ActionError(
            errors.CAPABILITY_UNAVAILABLE, report.detail or f"{spec.name} is not available right now on {target.name}",
            details={"capability": report.to_dict()},
        )
    return report


# -- runners: one per action, each a thin call to the existing service ------------------

Runner = Callable[..., OperationResult[Any]]


def _seams(spec: ActionSpec, provider: Provider, target: Target) -> dict[str, Any]:
    """Server-set request fields for the service this spec maps to."""
    if spec.request_type is None:
        return {}
    names = {f.name for f in dataclasses.fields(spec.request_type)}
    seams: dict[str, Any] = {}
    if "transport" in names:
        seams["transport"] = provider.name
    if "serial" in names:
        seams["serial"] = target.serial if provider.name == "adb" else None
    if "serial_from_profile" in names:
        seams["serial_from_profile"] = True
    return seams


def run_ping(provider: Provider, target: Target, request: Any, **_: Any) -> OperationResult[Any]:
    return OperationResult.success_with(provider.ping(), operation="device.ping", resource_id=target.name, provider=provider.name)


def run_status(provider: Provider, target: Target, request: Any, **_: Any) -> OperationResult[Any]:
    return OperationResult.success_with(provider.status(), operation="device.status", resource_id=target.name, provider=provider.name)


def run_battery(provider: Provider, target: Target, request: Any, **_: Any) -> OperationResult[Any]:
    return OperationResult.success_with(provider.battery(), operation="battery.read", resource_id=target.name, provider=provider.name)


def run_notify(provider: Provider, target: Target, request: Any, *, progress: ProgressCallback | None = None, **_: Any) -> OperationResult[Any]:
    return notification_module.notify_phone(request, ssh_factory=ssh_factory_for(target), progress=progress)


def run_find(provider: Provider, target: Target, request: Any, *, progress: ProgressCallback | None = None, **_: Any) -> OperationResult[Any]:
    return find_module.locate_phone(request, progress=progress)


def run_clipboard(provider: Provider, target: Target, request: Any, *, progress: ProgressCallback | None = None,
                  cancel: CancellationToken | None = None, **_: Any) -> OperationResult[Any]:
    factory = ssh_factory_for(target)
    assert factory is not None  # select() guarantees the SSH endpoint
    return clipboard_module.use_clipboard(request, factory(), target.serial, progress=progress, cancel=cancel)


def run_send(provider: Provider, target: Target, request: Any, *, progress: ProgressCallback | None = None,
             cancel: CancellationToken | None = None, **_: Any) -> OperationResult[Any]:
    return transfer_module.send_files(request, progress=progress, cancel=cancel)


def run_backup(provider: Provider, target: Target, request: Any, *, progress: ProgressCallback | None = None,
               cancel: CancellationToken | None = None, **_: Any) -> OperationResult[Any]:
    return backup_module.backup_photos(request, progress=progress, cancel=cancel)


RUNNERS: dict[str, Runner] = {
    "device.ping": run_ping,
    "device.status": run_status,
    "battery.read": run_battery,
    "notify.post": run_notify,
    "device.find": run_find,
    "clipboard.read": run_clipboard,
    "clipboard.write": run_clipboard,
    "clipboard.sync": run_clipboard,
    "files.send": run_send,
    "backup.photos": run_backup,
}


def error_from(result: OperationResult[Any]) -> errors.LinkplaneError:
    error = result.error
    assert error is not None
    code = error.error_code or errors.OPERATION_CODE_MAP.get(error.code, errors.PROVIDER_FAILED)
    return errors.LinkplaneError(code, error.message, error.hints)


def audit_parameters(spec: ActionSpec, parameters: Mapping[str, Any]) -> dict[str, Any]:
    """What the audit log may keep: never clipboard text; notification text truncated."""
    kept: dict[str, Any] = {}
    for name, value in parameters.items():
        if spec.name.startswith("clipboard.") and name == "text":
            kept["text_length"] = len(str(value))
        elif name == "message" and isinstance(value, str):
            kept[name] = value[:200]
        else:
            kept[name] = value
    return kept


# -- the runtime --------------------------------------------------------------------------


class ActionRuntime:
    """Execute validated actions: immediate ones on the caller's thread, jobs on a small
    executor separate from the rule-firing pool (design §9)."""

    def __init__(
        self,
        jobs: JobRunner,
        *,
        audit: AuditWriter | None = None,
        adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
        max_jobs: int = 4,
    ):
        self.jobs = jobs
        self.audit = audit
        self.adb_factory = adb_factory
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="linkplane-api-job")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _audit(self, kind: str, decision: str, context: ActionContext, spec: ActionSpec, **details: Any) -> None:
        if self.audit is None:
            return
        try:
            self.audit.write(kind, decision=decision, actor=context.actor, source="api", device=context.device.name,
                             action=spec.name, correlation_id=context.correlation_id, **details)
        except Exception as error:  # noqa: BLE001 - auditing must never break an action
            log.warning("audit write failed: %s", error)

    def execute(self, spec: ActionSpec, parameters: Mapping[str, Any] | None, context: ActionContext) -> dict[str, Any] | JobRecord:
        """A Result projection (immediate) or the started JobRecord (job)."""
        given = dict(parameters or {})
        try:
            provider = select(context.target, spec, adb_factory=self.adb_factory)
            check_capability(provider, spec, context.target)
        except ActionError as error:
            self._audit("action.blocked", "blocked", context, spec, code=error.code, reason=str(error),
                        parameters=audit_parameters(spec, given))
            raise
        except errors.LinkplaneError as error:
            self._audit("action.failed", "failed", context, spec, code=error.code, reason=str(error),
                        parameters=audit_parameters(spec, given))
            raise
        request = build_request(spec, given, **_seams(spec, provider, context.target))
        runner = RUNNERS[spec.name]
        if spec.execution == JOB:
            return self._start_job(spec, request, context, provider, runner, given)
        return self._run_immediate(spec, request, context, provider, runner, given)

    def _run_immediate(self, spec: ActionSpec, request: Any, context: ActionContext, provider: Provider,
                       runner: Runner, given: dict[str, Any]) -> dict[str, Any]:
        started = now_iso()
        self._audit("action.requested", "started", context, spec, provider=provider.name, execution=spec.execution,
                    parameters=audit_parameters(spec, given))
        try:
            result = runner(provider, context.target, request)
        except errors.LinkplaneError as error:
            self._audit("action.failed", "failed", context, spec, provider=provider.name, code=error.code)
            raise
        except Exception as error:  # noqa: BLE001 - reported as internal, logged in full server-side
            self._audit("action.failed", "failed", context, spec, provider=provider.name, code=errors.INTERNAL)
            raise
        if not result.ok:
            error = error_from(result)
            self._audit("action.failed", "failed", context, spec, provider=provider.name, code=error.code)
            raise error
        rendered = result.to_dict()
        self._audit("action.completed", "completed", context, spec, provider=result.provider or provider.name)
        return {
            "action": spec.name,
            "device_id": context.device.device_id,
            "device": context.device.name,
            "provider": result.provider or provider.name,
            "actor": context.actor,
            "correlation_id": context.correlation_id,
            "started": started,
            "finished": now_iso(),
            "value": rendered["value"],
            "warnings": rendered["warnings"],
        }

    def _start_job(self, spec: ActionSpec, request: Any, context: ActionContext, provider: Provider,
                   runner: Runner, given: dict[str, Any]) -> JobRecord:
        assert spec.job_action is not None
        ready = threading.Event()
        holder: dict[str, JobRecord] = {}

        def on_started(record: JobRecord) -> None:
            holder["record"] = record
            ready.set()

        def call(cancel: CancellationToken, progress: ProgressCallback) -> OperationResult[Any]:
            return runner(provider, context.target, request, progress=progress, cancel=cancel)

        self._executor.submit(
            self.jobs.run, automation="", action=spec.job_action, device=context.device.name, call=call,
            correlation_id=context.correlation_id, actor=context.actor, on_started=on_started,
        )
        if not ready.wait(JOB_START_TIMEOUT):
            raise errors.LinkplaneError(errors.INTERNAL, "the job did not start in time")
        record = holder["record"]
        if record.state == "skipped":
            running = self.jobs.running_id(context.device.name, spec.job_action)
            self._audit("action.blocked", "blocked", context, spec, provider=provider.name, code=errors.STATE_CONFLICT,
                        job_id=running, skipped_job_id=record.id)
            raise ActionError(
                errors.STATE_CONFLICT, f"{spec.name} is already running for {context.device.name}",
                (f"watch it at /v1/jobs/{running}" if running else "wait for the running job to finish",),
                details={"job_id": running, "skipped_job_id": record.id, "job_action": spec.job_action},
            )
        self._audit("action.requested", "started", context, spec, provider=provider.name, execution=spec.execution,
                    job_id=record.id, job_action=spec.job_action, parameters=audit_parameters(spec, given))
        return record


# -- capabilities (GET /v1/devices/{id}/capabilities) ----------------------------------


def capability_reports(target: Target, *, adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
                       only: str | None = None) -> dict[str, tuple[CapabilityReport, ...]]:
    """`Provider.capabilities()` for each provider the device has, keyed by provider name,
    in `auto` order. Exactly what `linkplane capabilities` reports, per provider."""
    from linkplane.providers import ADBProvider, SSHProvider

    found: dict[str, tuple[CapabilityReport, ...]] = {}
    if target.serial and only in (None, "adb"):
        found["adb"] = ADBProvider(adb_factory(target.serial)).capabilities()
    factory = ssh_factory_for(target)
    if factory is not None and only in (None, "ssh"):
        try:
            found["ssh"] = SSHProvider(factory()).capabilities()
        except BridgeError as error:
            log.warning("ssh capabilities for %s unavailable: %s", target.name, error)
    return found


def effective_capabilities(by_provider: Mapping[str, tuple[CapabilityReport, ...]]) -> list[tuple[str, CapabilityReport]]:
    """One entry per catalogue name: the first provider (auto order) reporting `supported`,
    else the first provider's report."""
    from linkplane.core.capability import CATALOGUE

    order = [name for name in ("adb", "ssh") if name in by_provider] + [name for name in by_provider if name not in ("adb", "ssh")]
    effective: list[tuple[str, CapabilityReport]] = []
    for capability in CATALOGUE:
        chosen: tuple[str, CapabilityReport] | None = None
        for provider in order:
            report = next((r for r in by_provider[provider] if r.name == capability), None)
            if report is None:
                continue
            if chosen is None or (report.status == SUPPORTED and chosen[1].status != SUPPORTED):
                chosen = (provider, report)
            if report.status == SUPPORTED:
                break
        if chosen is not None:
            effective.append(chosen)
    return effective
