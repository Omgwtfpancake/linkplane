from __future__ import annotations

import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from linkplane.dependencies import (
    DependencyUnavailable,
    dependency_install_hint,
    localsend_dependency_plan,
)
from linkplane.operations import (
    CancellationToken,
    OperationCancelled,
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    cancel_on_interrupt,
    check_cancelled,
    is_cancelled,
    report_progress,
)
from linkplane.transports import AdbTransport, BridgeError


DEFAULT_DESTINATION = "/sdcard/Download"


@dataclass(frozen=True)
class SendRequest:
    paths: tuple[str, ...]
    transport: str = "auto"
    destination: str = DEFAULT_DESTINATION
    serial: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class SendItem:
    path: str
    size: int


@dataclass(frozen=True)
class SendResult:
    transport: str
    items: tuple[SendItem, ...]
    total_bytes: int
    destination: str | None
    device: str | None
    serial: str | None
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_sources(values: list[str]) -> list[Path]:
    sources: list[Path] = []
    for value in values:
        source = Path(value).expanduser().resolve()
        if not source.exists():
            raise BridgeError(f"source does not exist: {value}")
        sources.append(source)
    return sources


def source_size(source: Path) -> int:
    try:
        if source.is_file():
            return source.stat().st_size
        return sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
    except OSError as error:
        raise BridgeError(f"unable to read {source}: {error}") from error


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


def adb_transfer(
    sources: list[Path],
    destination: str,
    serial: str | None,
    *,
    dry_run: bool,
) -> int:
    _validate_destination(destination)
    transport = AdbTransport(serial)
    device = transport.select_device()
    _adb_transfer_selected(
        sources,
        destination,
        transport,
        device,
        dry_run=dry_run,
        progress=render_send_progress,
    )
    return 0


def _adb_transfer_selected(
    sources: list[Path],
    destination: str,
    transport: AdbTransport,
    device: dict[str, str],
    *,
    dry_run: bool,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> SendResult:
    _validate_destination(destination)
    selected_serial = transport.serial
    if selected_serial is None:
        raise BridgeError("unable to determine the ADB device serial")

    items = tuple(SendItem(str(source), source_size(source)) for source in sources)
    total_size = sum(item.size for item in items)
    model = device.get("model", "Android device").replace("_", " ")
    mkdir_command = [
        "adb",
        "-s",
        selected_serial,
        "shell",
        f"mkdir -p -- {shlex.quote(destination)}",
    ]
    push_commands = [
        ["adb", "-s", selected_serial, "push", str(source), f"{destination.rstrip('/')}/"]
        for source in sources
    ]

    result = SendResult(
        transport="adb",
        items=items,
        total_bytes=total_size,
        destination=destination,
        device=model,
        serial=selected_serial,
        dry_run=dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "send",
            "started",
            "ADB transfer prepared",
            current=0,
            total=len(items),
            unit="items",
            details=result.to_dict(),
        ),
    )
    if dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "send",
                "command",
                shlex.join(mkdir_command),
                details={"command": mkdir_command},
            ),
        )
        for command in push_commands:
            report_progress(
                progress,
                ProgressEvent(
                    "send",
                    "command",
                    shlex.join(command),
                    item=command[-2],
                    details={"command": command},
                ),
            )
        report_progress(
            progress,
            ProgressEvent(
                "send",
                "completed",
                "Dry run completed",
                current=0,
                total=len(items),
                unit="items",
            ),
        )
        return result

    check_cancelled(cancel)
    # Captured: the service reports progress through ProgressEvent, not adb's own chatter
    # (which otherwise leaks onto whatever stdout the daemon or CLI is using).
    mkdir_result = subprocess.run(mkdir_command, check=False, capture_output=True, text=True)
    if mkdir_result.returncode != 0:
        raise BridgeError(f"unable to create destination {destination}")
    for index, command in enumerate(push_commands, start=1):
        if is_cancelled(cancel):
            _report_send_cancelled(progress, index - 1, len(items))
            check_cancelled(cancel)
        report_progress(
            progress,
            ProgressEvent(
                "send",
                "item_started",
                f"Sending {command[-2]}",
                current=index - 1,
                total=len(items),
                unit="items",
                item=command[-2],
            ),
        )
        process = subprocess.run(command, check=False, capture_output=True, text=True)
        if process.returncode != 0:
            if is_cancelled(cancel):
                # A terminal Ctrl+C reaches the child `adb push` too, so a failed push
                # right after a cancel request is the cancellation, not a transfer error.
                _report_send_cancelled(progress, index - 1, len(items))
                check_cancelled(cancel)
            raise BridgeError(f"transfer failed for {command[-2]}")
        report_progress(
            progress,
            ProgressEvent(
                "send",
                "item_completed",
                f"Sent {command[-2]}",
                current=index,
                total=len(items),
                unit="items",
                item=command[-2],
            ),
        )
    report_progress(
        progress,
        ProgressEvent(
            "send",
            "completed",
            "ADB transfer completed",
            current=len(items),
            total=len(items),
            unit="items",
        ),
    )
    return result


def _report_send_cancelled(
    progress: ProgressCallback | None, sent: int, total: int
) -> None:
    report_progress(
        progress,
        ProgressEvent(
            "send",
            "cancelled",
            f"Transfer cancelled after {sent} of {total} item(s)",
            current=sent,
            total=total,
            unit="items",
        ),
    )


def _validate_destination(destination: str) -> None:
    if not destination.startswith("/") or "\n" in destination or "\r" in destination:
        raise BridgeError("ADB destination must be an absolute Android path")


def _localsend_transfer(
    sources: list[Path],
    *,
    dry_run: bool,
    progress: ProgressCallback | None = None,
) -> SendResult:
    dependency = localsend_dependency_plan()
    executable = dependency.executable_path
    if not dependency.available or executable is None:
        raise DependencyUnavailable(
            f"LocalSend CLI is not installed; {dependency_install_hint(dependency)}"
        )
    command = [executable]
    for source in sources:
        command.extend(["--file", str(source)])
    items = tuple(SendItem(str(source), source_size(source)) for source in sources)
    total_size = sum(item.size for item in items)
    result = SendResult(
        transport="localsend",
        items=items,
        total_bytes=total_size,
        destination=None,
        device=None,
        serial=None,
        dry_run=dry_run,
    )

    report_progress(
        progress,
        ProgressEvent(
            "send",
            "started",
            "LocalSend transfer prepared",
            current=0,
            total=len(items),
            unit="items",
            details=result.to_dict(),
        ),
    )
    if dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "send",
                "command",
                shlex.join(command),
                details={"command": command},
            ),
        )
        report_progress(
            progress,
            ProgressEvent("send", "completed", "Dry run completed"),
        )
        return result
    try:
        process = subprocess.run(command, check=False)
    except OSError as error:
        raise BridgeError(f"unable to open LocalSend: {error}") from error
    if process.returncode != 0:
        raise BridgeError(f"LocalSend exited with code {process.returncode}")
    report_progress(
        progress,
        ProgressEvent(
            "send",
            "completed",
            "LocalSend transfer session completed",
            current=len(items),
            total=len(items),
            unit="items",
        ),
    )
    return result


def localsend_transfer(sources: list[Path], *, dry_run: bool) -> int:
    _localsend_transfer(sources, dry_run=dry_run, progress=render_send_progress)
    return 0


def send_files(
    request: SendRequest,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> OperationResult[SendResult]:
    try:
        sources = resolve_sources(list(request.paths))
        if request.transport not in {"auto", "adb", "localsend"}:
            raise BridgeError(f"unsupported send transport: {request.transport}")
        if request.transport == "localsend":
            if request.serial:
                raise BridgeError("--serial cannot be used with the LocalSend transport")
            if request.destination != DEFAULT_DESTINATION:
                raise BridgeError("--destination is only supported by the ADB transport")
        else:
            _validate_destination(request.destination)
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))

    try:
        if request.transport == "localsend":
            return OperationResult.success(
                _localsend_transfer(sources, dry_run=request.dry_run, progress=progress)
            )

        transport = AdbTransport(request.serial)
        try:
            device = transport.select_device()
        except BridgeError as adb_error:
            if request.transport == "adb" or request.serial:
                return OperationResult.failure("transport_unavailable", str(adb_error))
            if request.destination != DEFAULT_DESTINATION:
                return OperationResult.failure("transport_unavailable", str(adb_error))
            try:
                return OperationResult.success(
                    _localsend_transfer(sources, dry_run=request.dry_run, progress=progress)
                )
            except BridgeError as localsend_error:
                return OperationResult.failure(
                    "transport_unavailable",
                    f"ADB unavailable ({adb_error}); LocalSend unavailable ({localsend_error})",
                )
        return OperationResult.success(
            _adb_transfer_selected(
                sources,
                request.destination,
                transport,
                device,
                dry_run=request.dry_run,
                progress=progress,
                cancel=cancel,
            )
        )
    except OperationCancelled as error:
        return OperationResult.failure("cancelled", str(error))
    except DependencyUnavailable as error:
        return OperationResult.failure("dependency_missing", str(error))
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))


def render_send_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Send")
        print(f"Transport   {str(result['transport']).upper()}")
        if result.get("device") and result.get("serial"):
            print(f"Device      {result['device']} ({result['serial']})")
        print(f"Items       {len(result['items'])} ({format_bytes(result['total_bytes'])})")
        destination = result.get("destination") or "Select a nearby device in LocalSend"
        print(f"Destination {destination}", flush=not result["dry_run"])
    elif event.phase == "command":
        print(f"Command     {event.message}")
    elif event.phase == "cancelled":
        print(f"Cancelled   {event.current} of {event.total} sent", flush=True)
    elif event.phase == "completed" and event.message != "Dry run completed":
        if event.message.startswith("LocalSend"):
            print("Completed   LocalSend transfer session")
        else:
            count = event.total or 0
            print(f"Sent        {count} item{'s' if count != 1 else ''}")


def send(arguments: Any) -> int:
    with cancel_on_interrupt(CancellationToken()) as cancel:
        result = send_files(
            SendRequest(
                paths=tuple(arguments.paths),
                transport=arguments.transport,
                destination=arguments.destination,
                serial=arguments.serial,
                dry_run=arguments.dry_run,
            ),
            progress=render_send_progress,
            cancel=cancel,
        )
    if result.error is not None:
        if result.error.code == "cancelled":
            raise OperationCancelled(result.error.message)
        raise BridgeError(result.error.message)
    return 0
