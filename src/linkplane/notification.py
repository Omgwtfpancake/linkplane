from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass
from typing import Any, Callable

from linkplane.operations import (
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    report_progress,
)
from linkplane.transports import AdbTransport, BridgeError, SshTransport, run_command


@dataclass(frozen=True)
class NotificationRequest:
    message: str
    title: str = "Linkplane"
    notification_id: int = 8765
    transport: str = "auto"
    serial: str | None = None
    dry_run: bool = False
    serial_from_profile: bool = False


@dataclass(frozen=True)
class NotificationResult:
    transport: str
    device: str
    serial: str | None
    title: str
    notification_id: int
    message: str
    command: tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def adb_notification_command(
    serial: str,
    message: str,
    *,
    title: str,
    notification_id: int,
) -> list[str]:
    remote_command = shlex.join(
        [
            "cmd",
            "notification",
            "post",
            "--style",
            "bigtext",
            "--title",
            title,
            f"linkplane-{notification_id}",
            message,
        ]
    )
    return ["adb", "-s", serial, "shell", remote_command]


def send_adb_notification(
    arguments: Any,
    transport: AdbTransport | None = None,
    device: dict[str, str] | None = None,
) -> int:
    transport = transport or AdbTransport(arguments.serial)
    device = device or transport.select_device()
    _send_adb_notification(
        _request_from_arguments(arguments),
        transport,
        device,
        progress=render_notification_progress,
    )
    return 0


def _send_adb_notification(
    request: NotificationRequest,
    transport: AdbTransport,
    device: dict[str, str],
    *,
    progress: ProgressCallback | None = None,
    command_runner: Callable[[list[str]], str] | None = None,
) -> NotificationResult:
    serial = transport.serial
    if serial is None:
        raise BridgeError("unable to determine the ADB device serial")
    command = adb_notification_command(
        serial,
        request.message,
        title=request.title,
        notification_id=request.notification_id,
    )
    model = device.get("model", "Android device").replace("_", " ")
    result = NotificationResult(
        transport="adb",
        device=model,
        serial=serial,
        title=request.title,
        notification_id=request.notification_id,
        message=request.message,
        command=tuple(command),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "notify", "started", "Notification prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "notify",
                "command",
                shlex.join(command),
                details={"command": command},
            ),
        )
        report_progress(
            progress, ProgressEvent("notify", "completed", "Dry run completed")
        )
        return result
    (command_runner or run_command)(command)
    report_progress(
        progress, ProgressEvent("notify", "completed", "Notification posted")
    )
    return result


def send_ssh_notification(arguments: Any, transport: SshTransport) -> int:
    _send_ssh_notification(
        _request_from_arguments(arguments),
        transport,
        progress=render_notification_progress,
    )
    return 0


def _send_ssh_notification(
    request: NotificationRequest,
    transport: SshTransport,
    *,
    progress: ProgressCallback | None = None,
) -> NotificationResult:
    remote_arguments = [
        "termux-notification",
        "--id",
        str(request.notification_id),
        "--title",
        request.title,
        "--content",
        request.message,
    ]
    command = transport.command(shlex.join(remote_arguments))
    result = NotificationResult(
        transport="ssh",
        device=f"{transport.user}@{transport.host}",
        serial=None,
        title=request.title,
        notification_id=request.notification_id,
        message=request.message,
        command=tuple(command),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "notify", "started", "Notification prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "notify",
                "command",
                shlex.join(command),
                details={"command": command},
            ),
        )
        report_progress(
            progress, ProgressEvent("notify", "completed", "Dry run completed")
        )
        return result
    transport.execute(remote_arguments)
    report_progress(
        progress, ProgressEvent("notify", "completed", "Notification posted")
    )
    return result


def notify_phone(
    request: NotificationRequest,
    *,
    ssh_factory: Callable[[], SshTransport] | None = None,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[NotificationResult]:
    if request.transport not in {"auto", "adb", "ssh"}:
        return OperationResult.failure(
            "invalid_request", f"unsupported notification transport: {request.transport}"
        )
    if request.transport == "ssh" and request.serial:
        return OperationResult.failure(
            "invalid_request", "--serial cannot be used with the SSH transport"
        )

    make_adb = adb_factory or AdbTransport
    if request.transport == "ssh":
        try:
            if ssh_factory is None:
                raise BridgeError("SSH transport is not configured")
            return OperationResult.success(
                _send_ssh_notification(request, ssh_factory(), progress=progress)
            )
        except BridgeError as error:
            return OperationResult.failure("operation_failed", str(error))

    adb = make_adb(request.serial)
    try:
        device = adb.select_device()
    except BridgeError as adb_error:
        if request.transport == "adb" or (
            request.serial and not request.serial_from_profile
        ):
            return OperationResult.failure("transport_unavailable", str(adb_error))
        try:
            if ssh_factory is None:
                raise BridgeError("SSH transport is not configured")
            return OperationResult.success(
                _send_ssh_notification(request, ssh_factory(), progress=progress)
            )
        except BridgeError as ssh_error:
            return OperationResult.failure(
                "transport_unavailable",
                f"ADB unavailable ({adb_error}); SSH unavailable ({ssh_error})",
            )

    try:
        return OperationResult.success(
            _send_adb_notification(request, adb, device, progress=progress)
        )
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))


def render_notification_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Notify")
        print(f"Transport   {str(result['transport']).upper()}")
        device = result["device"]
        if result.get("serial"):
            device = f"{device} ({result['serial']})"
        print(f"Device      {device}")
        print(f"Title       {result['title']}")
    elif event.phase == "command":
        print(f"Command     {event.message}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        print("Delivered   Notification posted")


def _request_from_arguments(arguments: Any) -> NotificationRequest:
    return NotificationRequest(
        message=arguments.message,
        title=arguments.title,
        notification_id=arguments.notification_id,
        transport=getattr(arguments, "transport", "auto"),
        serial=getattr(arguments, "serial", None),
        dry_run=arguments.dry_run,
        serial_from_profile=getattr(arguments, "serial_from_profile", False),
    )


def notify(arguments: Any, ssh_factory: Callable[[], SshTransport]) -> int:
    result = notify_phone(
        _request_from_arguments(arguments),
        ssh_factory=ssh_factory,
        progress=render_notification_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    return 0
