from __future__ import annotations

import base64
import binascii
import shlex
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from linkplane.clipboard import foreground_termux
from linkplane.dependencies import (
    DependencyPlan,
    DependencyUnavailable,
    ScrcpyCompatibility,
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
from linkplane.screen import ensure_scrcpy
from linkplane.transports import AdbTransport, BridgeError, SshTransport


CAMERA_PRESETS: dict[str, list[str]] = {
    "low": ["--max-size=1280", "--camera-fps=30", "--video-bit-rate=4M"],
    "balanced": ["--max-size=1920", "--camera-fps=30", "--video-bit-rate=8M"],
    "high": ["--max-size=3840", "--camera-fps=30", "--video-bit-rate=16M"],
    "motion": ["--max-size=1920", "--camera-fps=60", "--video-bit-rate=16M"],
}

TERMUX_TMP = "/data/data/com.termux/files/usr/tmp"


@dataclass(frozen=True)
class CaptureRequest:
    output: str | None = None
    camera_id: int = 0
    force: bool = False
    foreground: bool = True
    dry_run: bool = False


@dataclass(frozen=True)
class CaptureResult:
    transport: str
    endpoint: str
    camera_id: int
    output: str
    remote_path: str
    foreground_serial: str | None
    command: tuple[str, ...]
    bytes_written: int | None
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CameraPreviewRequest:
    serial: str | None = None
    quality: str = "balanced"
    facing: str | None = None
    camera_id: str | None = None
    audio: bool = True
    torch: bool = False
    record: str | None = None
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class CameraPreviewResult:
    device: str
    serial: str
    selection: str
    quality: str
    command: tuple[str, ...]
    dependency: DependencyPlan
    dry_run: bool
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_capture_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(f"~/Pictures/Linkplane/capture-{timestamp}.jpg").expanduser()


def resolve_capture_path(value: str | None, *, force: bool) -> Path:
    path = Path(value).expanduser().resolve() if value else default_capture_path().resolve()
    if path.exists() and not force:
        raise BridgeError(f"capture destination already exists: {path}; use --force to replace it")
    if path.is_dir():
        raise BridgeError(f"capture destination is a directory: {path}")
    return path


def decode_photo(encoded: str) -> bytes:
    try:
        photo = base64.b64decode("".join(encoded.split()), validate=True)
    except (ValueError, binascii.Error) as error:
        raise BridgeError("phone returned invalid camera image data") from error
    if len(photo) < 4 or not photo.startswith(b"\xff\xd8") or not photo.endswith(b"\xff\xd9"):
        raise BridgeError("phone camera output is not a complete JPEG image")
    return photo


def save_photo(path: Path, photo: bytes, *, force: bool) -> None:
    temporary = path.with_name(f".{path.name}.linkplane-part")
    published = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(photo)
        if path.exists() and not force:
            raise BridgeError(
                f"capture destination already exists: {path}; use --force to replace it"
            )
        temporary.replace(path)
        published = True
    except OSError as error:
        raise BridgeError(f"unable to save camera image at {path}: {error}") from error
    finally:
        if not published:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def capture_photo(
    arguments: Any,
    transport: SshTransport,
    adb_serial: str | None,
) -> int:
    result = capture_camera_photo(
        CaptureRequest(
            output=arguments.output,
            camera_id=arguments.camera_id,
            force=arguments.force,
            foreground=not arguments.no_foreground,
            dry_run=arguments.dry_run,
        ),
        transport,
        adb_serial,
        foreground_runner=foreground_termux,
        progress=render_capture_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    return 0


def capture_camera_photo(
    request: CaptureRequest,
    transport: SshTransport,
    adb_serial: str | None,
    *,
    foreground_runner: Callable[[str], None] = foreground_termux,
    progress: ProgressCallback | None = None,
) -> OperationResult[CaptureResult]:
    if request.camera_id < 0:
        return OperationResult.failure(
            "invalid_request", "camera ID must be zero or greater"
        )
    try:
        output = resolve_capture_path(request.output, force=request.force)
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))

    remote_path = f"{TERMUX_TMP}/linkplane-camera-{uuid.uuid4().hex}.jpg"
    capture_arguments = [
        "termux-camera-photo",
        "-c",
        str(request.camera_id),
        remote_path,
    ]
    try:
        command = transport.command(shlex.join(capture_arguments))
    except BridgeError as error:
        return OperationResult.failure("transport_unavailable", str(error))
    result = CaptureResult(
        transport="ssh",
        endpoint=f"{transport.user}@{transport.host}",
        camera_id=request.camera_id,
        output=str(output),
        remote_path=remote_path,
        foreground_serial=adb_serial if request.foreground else None,
        command=tuple(command),
        bytes_written=None,
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "camera_capture",
            "started",
            "Camera capture prepared",
            details=result.to_dict(),
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "camera_capture",
                "command",
                shlex.join(command),
                details={"command": command},
            ),
        )
        if adb_serial and request.foreground:
            report_progress(
                progress,
                ProgressEvent(
                    "camera_capture",
                    "foreground",
                    f"Bring Termux forward on {adb_serial}",
                    details={"serial": adb_serial},
                ),
            )
        report_progress(
            progress,
            ProgressEvent("camera_capture", "completed", "Dry run completed"),
        )
        return OperationResult.success(result)

    if request.foreground:
        if not adb_serial:
            return OperationResult.failure(
                "invalid_request",
                "camera capture requires an associated ADB device; configure ssh.device_id "
                "or use --no-foreground when Termux is already open",
            )
        try:
            foreground_runner(adb_serial)
        except BridgeError as error:
            return OperationResult.failure("operation_failed", str(error))
        report_progress(
            progress,
            ProgressEvent(
                "camera_capture",
                "foreground",
                f"Termux opened on {adb_serial}",
                details={"serial": adb_serial},
            ),
        )

    try:
        report_progress(
            progress,
            ProgressEvent("camera_capture", "capturing", "Capturing photo"),
        )
        transport.execute(capture_arguments, timeout=60)
        report_progress(
            progress,
            ProgressEvent("camera_capture", "transferring", "Transferring photo"),
        )
        encoded = transport.execute(["base64", remote_path], timeout=120)
        photo = decode_photo(encoded)
        save_photo(output, photo, force=request.force)
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))
    finally:
        try:
            transport.execute(["rm", "-f", remote_path])
        except BridgeError:
            pass
    result = replace(result, bytes_written=len(photo))
    report_progress(
        progress,
        ProgressEvent(
            "camera_capture",
            "completed",
            "Photo captured",
            details=result.to_dict(),
        ),
    )
    return OperationResult.success(result)


def render_capture_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Camera Capture")
        print(f"Transport   SSH ({result['endpoint']})")
        print(f"Camera      {result['camera_id']}")
        print(f"Output      {result['output']}")
    elif event.phase == "command":
        print(f"Command     {event.message}")
    elif event.phase == "foreground" and event.message.startswith("Bring"):
        print(f"Foreground  Termux on {event.details['serial']}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        result = event.details
        print(f"Captured    {result['output']} ({result['bytes_written']} bytes)")


def build_preview_command(
    serial: str,
    quality: str,
    *,
    facing: str | None,
    camera_id: str | None,
    audio: bool,
    torch: bool,
    record: str | None,
    extra_arguments: list[str] | None = None,
) -> list[str]:
    command = [
        "scrcpy",
        "--serial",
        serial,
        "--video-source=camera",
        *CAMERA_PRESETS[quality],
    ]
    if camera_id is not None:
        command.append(f"--camera-id={camera_id}")
    else:
        command.append(f"--camera-facing={facing or 'back'}")
    if not audio:
        command.append("--no-audio")
    if torch:
        command.append("--camera-torch")
    if record:
        command.extend(["--record", str(Path(record).expanduser())])
    if extra_arguments:
        command.extend(extra_arguments)
    return command


def preview_phone_camera(
    request: CameraPreviewRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    process_runner: Callable[..., Any] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[CameraPreviewResult]:
    if request.quality not in CAMERA_PRESETS:
        return OperationResult.failure(
            "invalid_request", f"unsupported camera quality: {request.quality}"
        )
    if request.facing is not None and request.camera_id is not None:
        return OperationResult.failure(
            "invalid_request", "camera facing and camera ID are mutually exclusive"
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
    extra_arguments = list(request.extra_arguments)
    if extra_arguments[:1] == ["--"]:
        extra_arguments.pop(0)
    command = build_preview_command(
        serial,
        request.quality,
        facing=request.facing,
        camera_id=request.camera_id,
        audio=request.audio,
        torch=request.torch,
        record=request.record,
        extra_arguments=extra_arguments,
    )
    model = device.get("model", "Android device").replace("_", " ")
    selection = (
        f"ID {request.camera_id}"
        if request.camera_id is not None
        else request.facing or "back"
    )
    get_dependency = dependency_factory or scrcpy_dependency_plan
    result = CameraPreviewResult(
        device=model,
        serial=serial,
        selection=selection,
        quality=request.quality,
        command=tuple(command),
        dependency=get_dependency(),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "camera_preview",
            "started",
            "Camera preview prepared",
            details=result.to_dict(),
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "camera_preview",
                "command",
                shlex.join(command),
                details={"command": command},
            ),
        )
        report_progress(
            progress,
            ProgressEvent("camera_preview", "completed", "Dry run completed"),
        )
        return OperationResult.success(result)

    try:
        if dependency_handler is not None:
            dependency_handler(request.install)
        else:
            require_dependency(result.dependency, install=request.install)
        active_dependency = get_dependency()
        compatibility = (compatibility_factory or scrcpy_compatibility)(active_dependency)
        if not compatibility.camera_supported:
            detail = compatibility.probe_error or (
                "missing required options: "
                f"{', '.join(compatibility.missing_camera_options)}"
            )
            raise DependencyUnavailable(f"scrcpy camera support is incompatible: {detail}")
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
            "camera_preview",
            "completed",
            "Camera preview ended",
            details=result.to_dict(),
        ),
    )
    return OperationResult.success(result)


def render_preview_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Camera Preview")
        print(f"Device      {result['device']} ({result['serial']})")
        print(f"Camera      {result['selection']}")
        print(f"Preset      {result['quality']}")
    elif event.phase == "command":
        print(f"Command     {event.message}")


def preview_camera(arguments: Any) -> int:
    result = preview_phone_camera(
        CameraPreviewRequest(
            serial=arguments.serial,
            quality=arguments.quality,
            facing=arguments.facing,
            camera_id=arguments.preview_camera_id,
            audio=not arguments.no_audio,
            torch=arguments.torch,
            record=arguments.record,
            extra_arguments=tuple(arguments.scrcpy_args),
            install=arguments.install,
            dry_run=arguments.dry_run,
        ),
        dependency_handler=ensure_scrcpy,
        progress=render_preview_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful camera preview did not return a value")
    return result.value.exit_code or 0
