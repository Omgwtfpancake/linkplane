from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable

from linkplane.dependencies import (
    DependencyPlan,
    DependencyUnavailable,
    require_dependency,
    scrcpy_dependency_plan,
)
from linkplane.notification import adb_notification_command
from linkplane.operations import (
    JSON_SCHEMA_VERSION,
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    report_progress,
)
from linkplane.transports import AdbTransport, BridgeError, run_command


# AudioManager.STREAM_RING. A stable public Android framework constant, not device-specific.
RING_STREAM = 2

_VOLUME_PATTERN = re.compile(r"volume is (\d+) in range \[\d+\.\.(\d+)\]")


@dataclass(frozen=True)
class FindPhoneRequest:
    serial: str | None = None
    duration: float = 5.0
    ring: bool = True
    torch: bool = True
    vibrate: bool = True
    message: str = "Locate this phone"
    install: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class FindPhoneResult:
    device: str
    serial: str
    duration: float
    ring: bool
    torch: bool
    vibrate: bool
    notification_command: tuple[str, ...]
    ring_get_command: tuple[str, ...] | None
    ring_set_command: tuple[str, ...] | None
    torch_command: tuple[str, ...] | None
    vibrate_command: tuple[str, ...] | None
    dry_run: bool
    ring_applied: bool = False
    ring_restored: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_stream_volume(output: str) -> tuple[int, int] | None:
    """Parse `cmd media_session volume --get` output: "volume is N in range [0..M]"."""
    match = _VOLUME_PATTERN.search(output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def ring_volume_command(
    serial: str, *, get: bool = False, index: int | None = None, show: bool = False
) -> list[str]:
    remote = ["cmd", "media_session", "volume", "--stream", str(RING_STREAM)]
    if get:
        remote.append("--get")
    if index is not None:
        remote.extend(["--set", str(index)])
    if show:
        remote.append("--show")
    return ["adb", "-s", serial, "shell", *remote]


def build_torch_command(serial: str, duration: float) -> list[str]:
    return [
        "scrcpy",
        "--serial",
        serial,
        "--video-source=camera",
        "--camera-torch",
        "--no-playback",
        f"--time-limit={max(1, round(duration))}",
    ]


def vibrate_command(serial: str, duration: float) -> list[str]:
    """Build a `cmd vibrator_manager` invocation.

    The `vibrator` shell service name doesn't exist (see the `find` module docstring in
    README.md), but `vibrator_manager` does and is verified working: `adb shell cmd
    vibrator_manager synced -f -B -d <description> oneshot <ms>` was confirmed to
    actually vibrate the paired device via `dumpsys vibrator_manager`'s own vibration
    log (entries attributed to `com.android.shell`, matching the requested duration and
    `-d` description). `-f` forces past Do Not Disturb, which a locate-my-phone command
    needs to be reliable; `-B` backgrounds it on-device so it runs concurrently with the
    torch flash instead of blocking the whole operation for `duration` seconds twice.
    """
    duration_ms = max(1, round(duration * 1000))
    remote = [
        "cmd",
        "vibrator_manager",
        "synced",
        "-f",
        "-B",
        "-d",
        "linkplane-find",
        "oneshot",
        str(duration_ms),
    ]
    return ["adb", "-s", serial, "shell", *remote]


def locate_phone(
    request: FindPhoneRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    command_runner: Callable[..., str] | None = None,
    process_runner: Callable[..., Any] | None = None,
    dependency_factory: Callable[[], DependencyPlan] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[FindPhoneResult]:
    # Resolved at call time rather than bound as signature defaults: a default such as
    # `= AdbTransport` is captured when the module is imported, so
    # `patch("linkplane.find.AdbTransport")` never reaches it and the "hermetic" test
    # quietly talks to real ADB (found 2026-09-12; see tests/test_find.py DependencySeamTests).
    adb_factory = adb_factory or AdbTransport
    command_runner = command_runner or run_command
    process_runner = process_runner or subprocess.run
    dependency_factory = dependency_factory or scrcpy_dependency_plan

    if request.duration <= 0:
        return OperationResult.failure("invalid_request", "duration must be positive")
    if not request.ring and not request.torch and not request.vibrate:
        return OperationResult.failure(
            "invalid_request", "select at least one of ring, torch, or vibrate"
        )

    transport = adb_factory(request.serial)
    try:
        device = transport.select_device()
    except BridgeError as error:
        return OperationResult.failure("transport_unavailable", str(error))
    serial = transport.serial
    if serial is None:
        return OperationResult.failure(
            "transport_unavailable", "unable to determine the ADB device serial"
        )
    model = device.get("model", "Android device").replace("_", " ")

    notification_command = tuple(
        adb_notification_command(
            serial, request.message, title="Find my phone", notification_id=8766
        )
    )
    torch_command = tuple(build_torch_command(serial, request.duration)) if request.torch else None
    vibrate_command_built = (
        tuple(vibrate_command(serial, request.duration)) if request.vibrate else None
    )
    ring_get_command = tuple(ring_volume_command(serial, get=True)) if request.ring else None

    current_volume: int | None = None
    maximum_volume: int | None = None
    note: str | None = None
    if request.ring:
        try:
            output = command_runner(list(ring_get_command))
        except BridgeError as error:
            note = f"unable to read ring volume; skipping ring: {error}"
        else:
            parsed = parse_stream_volume(output)
            if parsed is None:
                note = "unable to parse ring volume; skipping ring"
            else:
                current_volume, maximum_volume = parsed

    ring_set_command = (
        tuple(ring_volume_command(serial, index=maximum_volume, show=True))
        if maximum_volume is not None
        else None
    )

    result = FindPhoneResult(
        device=model,
        serial=serial,
        duration=request.duration,
        ring=request.ring,
        torch=request.torch,
        vibrate=request.vibrate,
        notification_command=notification_command,
        ring_get_command=ring_get_command,
        ring_set_command=ring_set_command,
        torch_command=torch_command,
        vibrate_command=vibrate_command_built,
        dry_run=request.dry_run,
        note=note,
    )
    report_progress(
        progress,
        ProgressEvent("find", "started", "Find phone prepared", details=result.to_dict()),
    )
    if request.dry_run:
        report_progress(
            progress, ProgressEvent("find", "completed", "Dry run completed")
        )
        return OperationResult.success(result)

    try:
        command_runner(list(notification_command))
        report_progress(
            progress, ProgressEvent("find", "notified", "Notification posted")
        )
        ring_applied = False
        ring_restored = False
        try:
            if ring_set_command is not None:
                command_runner(list(ring_set_command))
                ring_applied = True
                report_progress(
                    progress, ProgressEvent("find", "ring", "Ring volume raised")
                )
            if request.vibrate:
                command_runner(list(vibrate_command_built))
                report_progress(
                    progress, ProgressEvent("find", "vibrate", "Vibration pulse started")
                )
            if request.torch:
                require_dependency(dependency_factory(), install=request.install)
                process_runner(list(torch_command), check=False)
                report_progress(
                    progress,
                    ProgressEvent("find", "torch", "Camera torch cycle completed"),
                )
        finally:
            # Never leave the ringer raised, even if the torch step failed.
            if ring_applied and current_volume is not None:
                try:
                    command_runner(ring_volume_command(serial, index=current_volume))
                    ring_restored = True
                except BridgeError:
                    pass
    except DependencyUnavailable as error:
        return OperationResult.failure("dependency_missing", str(error))
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))

    result = replace(result, ring_applied=ring_applied, ring_restored=ring_restored)
    report_progress(
        progress, ProgressEvent("find", "completed", "Find phone completed", details=result.to_dict())
    )
    return OperationResult.success(result)


def render_find_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Find Phone")
        print(f"Device      {result['device']} ({result['serial']})")
        print(f"Duration    {result['duration']}s")
        if result.get("note"):
            print(f"Note        {result['note']}")
        if result["dry_run"]:
            print(f"Notify      {' '.join(result['notification_command'])}")
            if result["ring_get_command"]:
                print(f"Ring probe  {' '.join(result['ring_get_command'])}")
            if result["vibrate_command"]:
                print(f"Vibrate     {' '.join(result['vibrate_command'])}")
            if result["torch_command"]:
                print(f"Torch       {' '.join(result['torch_command'])}")
    elif event.phase == "notified":
        print("Notify      posted")
    elif event.phase == "ring":
        print("Ring        raised to maximum")
    elif event.phase == "vibrate":
        print("Vibrate     pulse started")
    elif event.phase == "torch":
        print("Torch       cycle completed")
    elif event.phase == "completed":
        result = event.details
        if result and result.get("ring_restored"):
            print("Ring        restored to previous level")


def find_phone(arguments: Any) -> int:
    result = locate_phone(
        FindPhoneRequest(
            serial=arguments.serial,
            duration=arguments.duration,
            ring=arguments.ring,
            torch=arguments.torch,
            vibrate=arguments.vibrate,
            message=arguments.message,
            install=arguments.install,
            dry_run=arguments.dry_run,
        ),
        progress=None if arguments.json else render_find_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful find operation did not return a value")
    if arguments.json:
        print(
            json.dumps(
                {"schema_version": JSON_SCHEMA_VERSION, "ok": True, "data": result.value.to_dict()},
                indent=2,
            )
        )
    return 0
