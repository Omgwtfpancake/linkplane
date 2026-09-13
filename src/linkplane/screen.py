from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from linkplane.dependencies import (
    DependencyPlan,
    DependencyUnavailable,
    ScrcpyCompatibility,
    install_dependency,
    require_dependency,
    scrcpy_compatibility,
    scrcpy_dependency_plan,
)
from linkplane.operations import (
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    report_progress,
)
from linkplane.transports import AdbTransport, BridgeError


SCREEN_PRESETS: dict[str, list[str]] = {
    "low": ["--max-size=1280", "--max-fps=30", "--video-bit-rate=4M"],
    "balanced": ["--max-size=1600", "--max-fps=60", "--video-bit-rate=8M"],
    "high": ["--max-size=1920", "--max-fps=60", "--video-bit-rate=12M"],
    "game": [
        "--max-size=1920",
        "--max-fps=120",
        "--video-bit-rate=16M",
        "--video-codec=h264",
        "--video-buffer=0",
        "--audio-buffer=50",
        "--gamepad=uhid",
    ],
}


@dataclass(frozen=True)
class ScreenRequest:
    serial: str | None = None
    quality: str = "balanced"
    audio: bool = True
    record: str | None = None
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ScreenResult:
    device: str
    serial: str
    quality: str
    command: tuple[str, ...]
    dependency: DependencyPlan
    dry_run: bool
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def installation_command() -> list[str] | None:
    command = scrcpy_dependency_plan().install_command
    return list(command) if command else None


def install_scrcpy() -> None:
    install_dependency(scrcpy_dependency_plan())


def ensure_scrcpy(install: bool) -> None:
    plan = scrcpy_dependency_plan()
    if plan.available:
        return

    print("\nscrcpy is required for high-performance Android screen control.")
    if not install:
        if not sys.stdin.isatty():
            hint = (
                shlex.join(plan.install_command)
                if plan.install_command
                else "your system package manager"
            )
            raise BridgeError(f"scrcpy is not installed; install it with: {hint}")
        answer = input("Install it now? [Y/n] ").strip().lower()
        if answer not in {"", "y", "yes"}:
            raise BridgeError("scrcpy installation cancelled")
    install_dependency(plan)


def build_screen_command(
    serial: str,
    quality: str,
    *,
    audio: bool = True,
    record: str | None = None,
    extra_arguments: list[str] | None = None,
) -> list[str]:
    command = ["scrcpy", "--serial", serial, *SCREEN_PRESETS[quality]]
    if not audio:
        command.append("--no-audio")
    if record:
        command.extend(["--record", str(Path(record).expanduser())])
    if extra_arguments:
        command.extend(extra_arguments)
    return command


def launch_screen(
    request: ScreenRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    process_runner: Callable[..., Any] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[ScreenResult]:
    if request.quality not in SCREEN_PRESETS:
        return OperationResult.failure(
            "invalid_request", f"unsupported screen quality: {request.quality}"
        )

    transport = (adb_factory or AdbTransport)(request.serial)
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
    extra_arguments = list(request.extra_arguments)
    if extra_arguments[:1] == ["--"]:
        extra_arguments.pop(0)
    command = build_screen_command(
        serial,
        request.quality,
        audio=request.audio,
        record=request.record,
        extra_arguments=extra_arguments,
    )
    get_dependency = dependency_factory or scrcpy_dependency_plan
    result = ScreenResult(
        device=model,
        serial=serial,
        quality=request.quality,
        command=tuple(command),
        dependency=get_dependency(),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "screen", "started", "Screen session prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "screen", "command", shlex.join(command), details={"command": command}
            ),
        )
        report_progress(
            progress, ProgressEvent("screen", "completed", "Dry run completed")
        )
        return OperationResult.success(result)

    try:
        if dependency_handler is not None:
            dependency_handler(request.install)
        else:
            require_dependency(result.dependency, install=request.install)
        active_dependency = get_dependency()
        compatibility = (compatibility_factory or scrcpy_compatibility)(active_dependency)
        if not compatibility.screen_supported:
            detail = compatibility.probe_error or (
                "missing required options: "
                f"{', '.join(compatibility.missing_screen_options)}"
            )
            raise DependencyUnavailable(f"scrcpy is incompatible: {detail}")
        process = (process_runner or subprocess.run)(command, check=False)
    except DependencyUnavailable as error:
        return OperationResult.failure("dependency_missing", str(error))
    except FileNotFoundError as error:
        return OperationResult.failure("dependency_missing", "scrcpy is not installed")
    except OSError as error:
        return OperationResult.failure("operation_failed", str(error))
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))
    result = replace(result, dependency=get_dependency(), exit_code=process.returncode)
    report_progress(
        progress,
        ProgressEvent(
            "screen", "completed", "Screen session ended", details=result.to_dict()
        ),
    )
    return OperationResult.success(result)


def render_screen_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Screen")
        print(f"Device      {result['device']} ({result['serial']})")
        print(f"Preset      {result['quality']}")
    elif event.phase == "command":
        print(f"Command     {event.message}")


def screen(arguments: Any) -> int:
    result = launch_screen(
        ScreenRequest(
            serial=arguments.serial,
            quality=arguments.quality,
            audio=not arguments.no_audio,
            record=arguments.record,
            extra_arguments=tuple(arguments.scrcpy_args),
            install=arguments.install,
            dry_run=arguments.dry_run,
        ),
        dependency_handler=ensure_scrcpy,
        progress=render_screen_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful screen operation did not return a value")
    return result.value.exit_code or 0
