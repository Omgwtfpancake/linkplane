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


# Verified against `scrcpy --help` (scrcpy 4.1).
AUDIO_SOURCES: tuple[str, ...] = (
    "output",
    "playback",
    "mic",
    "mic-unprocessed",
    "mic-camcorder",
    "mic-voice-recognition",
    "mic-voice-communication",
    "voice-call",
    "voice-call-uplink",
    "voice-call-downlink",
    "voice-performance",
)

AUDIO_CODECS: tuple[str, ...] = ("opus", "aac", "flac", "raw")

# --record-format values that are valid for an audio-only (--no-video) recording.
AUDIO_RECORD_FORMATS: tuple[str, ...] = ("m4a", "mka", "opus", "aac", "flac", "wav")

_RECORD_FORMAT_BY_EXTENSION: dict[str, str] = {
    f".{extension_format}": extension_format for extension_format in AUDIO_RECORD_FORMATS
}


@dataclass(frozen=True)
class AudioRequest:
    serial: str | None = None
    source: str = "playback"
    record: str | None = None
    record_format: str | None = None
    codec: str | None = None
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class AudioResult:
    device: str
    serial: str
    source: str
    command: tuple[str, ...]
    dependency: DependencyPlan
    dry_run: bool
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_scrcpy(install: bool) -> None:
    plan = scrcpy_dependency_plan()
    if plan.available:
        return

    print("\nscrcpy is required for phone audio forwarding.")
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


def resolve_record_format(record: str, record_format: str | None) -> str:
    """Determine the --record-format to use for an audio-only recording.

    Rejects a format or a filename extension that isn't one of scrcpy's audio-only
    --record-format values, so build_audio_command never emits a --no-video recording
    forced into a video container (mp4/mkv).
    """
    if record_format is not None:
        if record_format not in AUDIO_RECORD_FORMATS:
            raise BridgeError(
                f"--record-format {record_format} is not an audio-only format; "
                f"expected one of: {', '.join(AUDIO_RECORD_FORMATS)}"
            )
        return record_format
    extension = Path(record).suffix.lower()
    resolved = _RECORD_FORMAT_BY_EXTENSION.get(extension)
    if resolved is None:
        raise BridgeError(
            f"--record {record} does not have an audio-only extension; use one of "
            f"{', '.join(f'.{fmt}' for fmt in AUDIO_RECORD_FORMATS)} or pass "
            "--record-format explicitly"
        )
    return resolved


def build_audio_command(
    serial: str,
    source: str,
    *,
    record: str | None = None,
    record_format: str | None = None,
    codec: str | None = None,
    extra_arguments: list[str] | None = None,
) -> list[str]:
    if source not in AUDIO_SOURCES:
        raise BridgeError(f"unsupported audio source: {source}")
    if codec is not None and codec not in AUDIO_CODECS:
        raise BridgeError(f"unsupported audio codec: {codec}")

    command = [
        "scrcpy",
        "--serial",
        serial,
        "--no-video",
        f"--audio-source={source}",
    ]
    if codec is not None:
        command.append(f"--audio-codec={codec}")
    if record:
        resolved_format = resolve_record_format(record, record_format)
        command.extend(["--record", str(Path(record).expanduser())])
        command.append(f"--record-format={resolved_format}")
    if extra_arguments:
        command.extend(extra_arguments)
    return command


def launch_audio(
    request: AudioRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    process_runner: Callable[..., Any] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[AudioResult]:
    if request.source not in AUDIO_SOURCES:
        return OperationResult.failure(
            "invalid_request", f"unsupported audio source: {request.source}"
        )
    if request.codec is not None and request.codec not in AUDIO_CODECS:
        return OperationResult.failure(
            "invalid_request", f"unsupported audio codec: {request.codec}"
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
    try:
        command = build_audio_command(
            serial,
            request.source,
            record=request.record,
            record_format=request.record_format,
            codec=request.codec,
            extra_arguments=extra_arguments,
        )
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))

    get_dependency = dependency_factory or scrcpy_dependency_plan
    result = AudioResult(
        device=model,
        serial=serial,
        source=request.source,
        command=tuple(command),
        dependency=get_dependency(),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "audio", "started", "Audio session prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "audio", "command", shlex.join(command), details={"command": command}
            ),
        )
        report_progress(
            progress, ProgressEvent("audio", "completed", "Dry run completed")
        )
        return OperationResult.success(result)

    try:
        if dependency_handler is not None:
            dependency_handler(request.install)
        else:
            require_dependency(result.dependency, install=request.install)
        active_dependency = get_dependency()
        compatibility = (compatibility_factory or scrcpy_compatibility)(active_dependency)
        if not compatibility.audio_supported:
            detail = compatibility.probe_error or (
                "missing required options: "
                f"{', '.join(compatibility.missing_audio_options)}"
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
            "audio", "completed", "Audio session ended", details=result.to_dict()
        ),
    )
    return OperationResult.success(result)


def render_audio_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Audio")
        print(f"Device      {result['device']} ({result['serial']})")
        print(f"Source      {result['source']}")
    elif event.phase == "command":
        print(f"Command     {event.message}")


def audio(arguments: Any) -> int:
    result = launch_audio(
        AudioRequest(
            serial=arguments.serial,
            source=arguments.source,
            record=arguments.record,
            record_format=arguments.record_format,
            codec=arguments.codec,
            extra_arguments=tuple(arguments.scrcpy_args),
            install=arguments.install,
            dry_run=arguments.dry_run,
        ),
        dependency_handler=ensure_scrcpy,
        progress=render_audio_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful audio operation did not return a value")
    return result.value.exit_code or 0
