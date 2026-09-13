from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Mapping

from linkplane.dependencies import (
    DependencyPlan,
    dependency_install_hint,
    wayland_clipboard_dependency_plan,
    x11_clipboard_dependency_plan,
)
from linkplane.operations import (
    CancellationToken,
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    cancel_on_interrupt,
    is_cancelled,
    report_progress,
)
from linkplane.transports import AdbTransport, BridgeError, SshTransport, run_command


CommandRunner = Callable[..., str]


@dataclass(frozen=True)
class DesktopClipboard:
    name: str
    read_command: tuple[str, ...]
    write_command: tuple[str, ...]


@dataclass(frozen=True)
class ClipboardRequest:
    action: str
    text: str | None = None
    interval: float = 1.0
    prefer: str = "desktop"
    foreground: bool = True
    dry_run: bool = False


@dataclass(frozen=True)
class ClipboardResult:
    action: str
    transport: str
    endpoint: str
    desktop_backend: str | None
    foreground_serial: str | None
    commands: tuple[tuple[str, ...], ...]
    text: str | None
    bytes_transferred: int | None
    updates: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def desktop_clipboard(
    finder: Callable[[str], str | None] | None = None,
    environment: Mapping[str, str] | None = None,
) -> DesktopClipboard | None:
    for dependency in desktop_clipboard_dependency_plans(finder, environment):
        if not dependency.available:
            continue
        if dependency.executable == "wl-copy":
            return DesktopClipboard(
                "Wayland",
                ("wl-paste", "--no-newline"),
                ("wl-copy",),
            )
        return DesktopClipboard(
            "X11",
            ("xclip", "-selection", "clipboard", "-out"),
            ("xclip", "-selection", "clipboard", "-in"),
        )
    return None


def desktop_clipboard_dependency_plans(
    finder: Callable[[str], str | None] | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[DependencyPlan, DependencyPlan]:
    values = environment if environment is not None else os.environ
    wayland = wayland_clipboard_dependency_plan(finder)
    x11 = x11_clipboard_dependency_plan(finder)
    session_type = values.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "x11" or (
        not session_type and values.get("DISPLAY") and not values.get("WAYLAND_DISPLAY")
    ):
        return x11, wayland
    return wayland, x11


def get_phone_clipboard(transport: SshTransport) -> str:
    return transport.execute(["termux-clipboard-get"])


def set_phone_clipboard(transport: SshTransport, text: str) -> None:
    transport.execute_input(["termux-clipboard-set"], text)
    last_value = ""
    for _ in range(20):
        last_value = get_phone_clipboard(transport)
        if last_value == text:
            return
        time.sleep(0.05)
    raise BridgeError(
        "phone clipboard update was not confirmed; keep Termux in the foreground "
        f"({len(last_value.encode('utf-8'))} bytes read)"
    )


def get_desktop_clipboard(
    backend: DesktopClipboard, runner: CommandRunner = run_command
) -> str:
    try:
        return runner(list(backend.read_command))
    except BridgeError as error:
        message = str(error).lower()
        if "nothing is copied" in message or "selection doesn't exist" in message:
            return ""
        raise


def set_desktop_clipboard(
    backend: DesktopClipboard,
    text: str,
    runner: CommandRunner = run_command,
) -> None:
    runner(list(backend.write_command), input_text=text, discard_output=True)
    if backend.name == "Wayland":
        last_error = None
        for _ in range(20):
            try:
                runner(["wl-paste", "--list-types"])
                return
            except BridgeError as error:
                last_error = error
                time.sleep(0.01)
        raise BridgeError(f"Wayland clipboard did not become ready: {last_error}")


def foreground_termux(serial: str, runner: CommandRunner = run_command) -> None:
    transport = AdbTransport(serial, runner)
    transport.select_device()
    runner(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "am",
            "start",
            "-n",
            "com.termux/com.termux.app.TermuxActivity",
        ]
    )


def sync_clipboards(
    transport: SshTransport,
    backend: DesktopClipboard,
    *,
    interval: float,
    prefer: str,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> int:
    """Poll both clipboards until interrupted (Ctrl+C) or `cancel` is set.

    Stopping is the only way a sync ends, so cancellation is its normal completion:
    the accumulated update count is returned, not an error.
    """
    updates = 0
    try:
        phone_value = get_phone_clipboard(transport)
        desktop_value = get_desktop_clipboard(backend)
        report_progress(
            progress,
            ProgressEvent(
                "clipboard",
                "watching",
                "Clipboard synchronization started",
                details={
                    "backend": backend.name,
                    "interval": interval,
                    "prefer": prefer,
                },
            ),
        )

        while True:
            if cancel is not None:
                if cancel.wait(interval):
                    return updates
            else:
                time.sleep(interval)
            if is_cancelled(cancel):
                return updates
            next_phone = get_phone_clipboard(transport)
            next_desktop = get_desktop_clipboard(backend)
            phone_changed = next_phone != phone_value
            desktop_changed = next_desktop != desktop_value
            direction = None
            conflict = False

            if phone_changed and desktop_changed and next_phone != next_desktop:
                conflict = True
                if prefer == "phone":
                    set_desktop_clipboard(backend, next_phone)
                    next_desktop = next_phone
                    direction = "phone_to_desktop"
                else:
                    set_phone_clipboard(transport, next_desktop)
                    next_phone = next_desktop
                    direction = "desktop_to_phone"
            elif phone_changed and not desktop_changed:
                set_desktop_clipboard(backend, next_phone)
                next_desktop = next_phone
                direction = "phone_to_desktop"
            elif desktop_changed and not phone_changed:
                set_phone_clipboard(transport, next_desktop)
                next_phone = next_desktop
                direction = "desktop_to_phone"

            if direction is not None:
                updates += 1
                report_progress(
                    progress,
                    ProgressEvent(
                        "clipboard",
                        "updated",
                        "Clipboard update synchronized",
                        current=updates,
                        unit="updates",
                        details={"direction": direction, "conflict": conflict},
                    ),
                )

            phone_value = next_phone
            desktop_value = next_desktop
    except KeyboardInterrupt:
        return updates


def use_clipboard(
    request: ClipboardRequest,
    transport: SshTransport,
    adb_serial: str | None = None,
    *,
    foreground_runner: Callable[[str], None] = foreground_termux,
    backend_factory: Callable[[], DesktopClipboard | None] = desktop_clipboard,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> OperationResult[ClipboardResult]:
    if request.action not in {"get", "set", "pull", "push", "sync"}:
        return OperationResult.failure(
            "invalid_request", f"unsupported clipboard action: {request.action}"
        )
    if request.action == "set" and request.text is None:
        return OperationResult.failure("invalid_request", "clipboard set requires text")
    if request.action != "set" and request.text is not None:
        return OperationResult.failure(
            "invalid_request", f"clipboard {request.action} does not accept text"
        )
    if request.action == "sync" and request.interval <= 0:
        return OperationResult.failure(
            "invalid_request", "clipboard sync interval must be greater than zero"
        )
    if request.action == "sync" and request.prefer not in {"desktop", "phone"}:
        return OperationResult.failure(
            "invalid_request", f"unsupported clipboard conflict preference: {request.prefer}"
        )

    remote_commands = {
        "get": ("termux-clipboard-get",),
        "set": ("termux-clipboard-set", "termux-clipboard-get"),
        "pull": ("termux-clipboard-get",),
        "push": ("termux-clipboard-set", "termux-clipboard-get"),
        "sync": ("termux-clipboard-get", "termux-clipboard-set"),
    }[request.action]
    try:
        commands = tuple(
            tuple(transport.command(command)) for command in remote_commands
        )
    except BridgeError as error:
        return OperationResult.failure("transport_unavailable", str(error))

    result = ClipboardResult(
        action=request.action,
        transport="ssh",
        endpoint=f"{transport.user}@{transport.host}",
        desktop_backend=None,
        foreground_serial=adb_serial if request.foreground else None,
        commands=commands,
        text=None,
        bytes_transferred=None,
        updates=0,
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "clipboard",
            "started",
            "Clipboard operation prepared",
            details=result.to_dict(),
        ),
    )
    if request.dry_run:
        report_progress(
            progress, ProgressEvent("clipboard", "completed", "Dry run completed")
        )
        return OperationResult.success(result)

    if request.foreground:
        if not adb_serial:
            return OperationResult.failure(
                "invalid_request",
                "clipboard access requires an associated ADB device; configure ssh.device_id "
                "or use --no-foreground when Termux is already open",
            )
        try:
            foreground_runner(adb_serial)
        except BridgeError as error:
            return OperationResult.failure("operation_failed", str(error))

    backend = None
    if request.action in {"pull", "push", "sync"}:
        try:
            backend = backend_factory()
        except BridgeError as error:
            return OperationResult.failure("dependency_missing", str(error))
        if backend is None:
            dependencies = desktop_clipboard_dependency_plans()
            recommended = next(
                (
                    dependency
                    for dependency in dependencies
                    if dependency.install_command is not None
                ),
                dependencies[0],
            )
            return OperationResult.failure(
                "dependency_missing",
                "no supported desktop clipboard tool; "
                f"{dependency_install_hint(recommended)}",
            )
        result = replace(result, desktop_backend=backend.name)

    try:
        if request.action == "get":
            text = get_phone_clipboard(transport)
            result = replace(
                result, text=text, bytes_transferred=len(text.encode("utf-8"))
            )
        elif request.action == "set":
            text = request.text
            if text is None:
                raise AssertionError("validated clipboard text is missing")
            set_phone_clipboard(transport, text)
            result = replace(result, bytes_transferred=len(text.encode("utf-8")))
        elif request.action == "pull":
            if backend is None:
                raise AssertionError("validated clipboard backend is missing")
            text = get_phone_clipboard(transport)
            set_desktop_clipboard(backend, text)
            result = replace(result, bytes_transferred=len(text.encode("utf-8")))
        elif request.action == "push":
            if backend is None:
                raise AssertionError("validated clipboard backend is missing")
            text = get_desktop_clipboard(backend)
            set_phone_clipboard(transport, text)
            result = replace(result, bytes_transferred=len(text.encode("utf-8")))
        else:
            if backend is None:
                raise AssertionError("validated clipboard backend is missing")
            result = replace(
                result,
                updates=sync_clipboards(
                    transport,
                    backend,
                    interval=request.interval,
                    prefer=request.prefer,
                    progress=progress,
                    cancel=cancel,
                ),
            )
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))

    progress_details = result.to_dict()
    progress_details["text"] = None
    report_progress(
        progress,
        ProgressEvent(
            "clipboard",
            "completed",
            "Clipboard operation completed",
            details=progress_details,
        ),
    )
    return OperationResult.success(result)


def clipboard(
    arguments: Any, transport: SshTransport, adb_serial: str | None = None
) -> int:
    request = ClipboardRequest(
        action=arguments.action,
        text=arguments.text,
        interval=getattr(arguments, "interval", 1.0),
        prefer=getattr(arguments, "prefer", "desktop"),
        foreground=not getattr(arguments, "no_foreground", False),
        dry_run=arguments.dry_run,
    )
    with cancel_on_interrupt(CancellationToken()) as cancel:
        result = use_clipboard(
            request,
            transport,
            adb_serial,
            foreground_runner=foreground_termux,
            backend_factory=desktop_clipboard,
            progress=lambda event: render_clipboard_progress(event, request),
            cancel=cancel,
        )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful clipboard operation did not return a value")
    if arguments.action == "get" and result.value.text is not None:
        text = result.value.text
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def render_clipboard_progress(event: ProgressEvent, request: ClipboardRequest) -> None:
    if event.phase == "started" and request.dry_run:
        result = event.details
        print("Linkplane Clipboard")
        print(f"Action      {request.action}")
        print(f"Transport   SSH ({result['endpoint']})")
        if request.action == "set" and request.text is not None:
            print(f"Input       {len(request.text.encode('utf-8'))} bytes via stdin")
        if request.action == "sync":
            print(f"Interval    {request.interval:g} seconds")
            print(f"Conflicts   Prefer {request.prefer}")
        if result["foreground_serial"]:
            print(f"Foreground  Termux on {result['foreground_serial']}")
    elif event.phase == "watching":
        print("Linkplane Clipboard Sync")
        print(
            f"Watching     Phone and {event.details['backend']} every "
            f"{event.details['interval']:g}s"
        )
        print(f"Conflicts    Prefer {event.details['prefer']}")
        print("Stop         Press Ctrl+C")
    elif event.phase == "updated":
        direction = event.details["direction"]
        label = (
            "Phone to desktop"
            if direction == "phone_to_desktop"
            else "Desktop to phone"
        )
        suffix = " (conflict)" if event.details["conflict"] else ""
        print(f"Synced       {label}{suffix}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        result = event.details
        if request.action == "sync":
            updates = result["updates"]
            print(
                f"\nStopped      {updates} update"
                f"{'s' if updates != 1 else ''} synchronized"
            )
        elif request.action == "set":
            print("Linkplane Clipboard")
            print("Updated     Phone clipboard")
        elif request.action == "pull":
            print("Linkplane Clipboard")
            print(f"Copied      Phone to {result['desktop_backend']} clipboard")
        elif request.action == "push":
            print("Linkplane Clipboard")
            print(f"Copied      {result['desktop_backend']} clipboard to phone")
