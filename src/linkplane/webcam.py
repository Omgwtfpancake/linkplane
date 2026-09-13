from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from linkplane.dependencies import (
    DependencyPlan,
    DependencyUnavailable,
    ScrcpyCompatibility,
    dependency_install_hint,
    install_dependency,
    require_dependency,
    scrcpy_compatibility,
    scrcpy_dependency_plan,
    v4l2loopback_dependency_plan,
)
from linkplane.operations import (
    JSON_SCHEMA_VERSION,
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    report_progress,
)
from linkplane.screen import ensure_scrcpy
from linkplane.transports import AdbTransport, BridgeError, run_command


_MODPROBE_HINT = (
    'load it with: sudo modprobe v4l2loopback exclusive_caps=1 card_label="Linkplane"'
)


@dataclass(frozen=True)
class WebcamStartRequest:
    serial: str | None = None
    device: int | None = None
    facing: str | None = None
    camera_id: str | None = None
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False
    state_path: str | None = None


@dataclass(frozen=True)
class WebcamStartResult:
    device: str
    serial: str
    video_device: str
    command: tuple[str, ...]
    scrcpy_dependency: DependencyPlan
    loopback_dependency: DependencyPlan
    pid: int | None
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WebcamStopRequest:
    state_path: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class WebcamStopResult:
    pid: int
    video_device: str
    serial: str | None
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_state_path(path: str | None = None) -> Path:
    from linkplane.paths import config_file

    return config_file("webcam.json", path, env_name="WEBCAM_STATE")


def load_webcam_state(path: str | None = None) -> dict[str, Any] | None:
    state_path = resolve_state_path(path)
    if not state_path.exists():
        return None
    try:
        with state_path.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError(f"unable to read webcam state at {state_path}: {error}") from error
    if not isinstance(state, dict):
        raise BridgeError(f"webcam state at {state_path} must contain a JSON object")
    return state


def save_webcam_state(state: dict[str, Any], path: str | None = None) -> Path:
    """Write webcam sink state atomically with private permissions.

    Mirrors `profiles.save_config`: write to a private temporary file in the same
    directory, then atomically replace the target so a reader never observes a partial
    write.
    """
    state_path = resolve_state_path(path)
    temporary: Path | None = None
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{state_path.name}.",
            dir=state_path.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            os.fchmod(state_file.fileno(), 0o600)
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        temporary.replace(state_path)
    except OSError as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise BridgeError(f"unable to save webcam state at {state_path}: {error}") from error
    return state_path


def clear_webcam_state(path: str | None = None) -> None:
    state_path = resolve_state_path(path)
    try:
        state_path.unlink(missing_ok=True)
    except OSError as error:
        raise BridgeError(f"unable to remove webcam state at {state_path}: {error}") from error


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by someone else; treat it as alive.
        return True
    except OSError:
        return False
    return True


def _terminate_process(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def parse_v4l2loopback_devices(list_output: str) -> list[str]:
    """Extract `/dev/videoN` sink paths from `v4l2-ctl --list-devices` output.

    That output groups device paths under an unindented card/driver header line; a
    v4l2loopback sink's header names the driver, e.g.
    "Dummy video device (0x0000) (platform:v4l2loopback-000):".
    """
    devices: list[str] = []
    current_is_loopback = False
    for line in list_output.splitlines():
        if line and not line[0].isspace():
            current_is_loopback = "v4l2loopback" in line.lower()
            continue
        stripped = line.strip()
        if current_is_loopback and stripped.startswith("/dev/video"):
            devices.append(stripped)
    return devices


def resolve_webcam_device(
    device_number: int | None,
    *,
    command_runner: Callable[..., str] = run_command,
) -> str:
    if device_number is not None:
        return f"/dev/video{device_number}"
    try:
        output = command_runner(["v4l2-ctl", "--list-devices"], timeout=10)
    except BridgeError as error:
        raise DependencyUnavailable(
            f"unable to list V4L2 devices (is v4l-utils installed?): {error}"
        ) from error
    devices = parse_v4l2loopback_devices(output)
    if not devices:
        raise DependencyUnavailable(
            "no v4l2loopback sink device was found; pass --device N or confirm the "
            "v4l2loopback module is loaded"
        )
    return devices[0]


def build_webcam_command(
    serial: str,
    video_device: str,
    *,
    facing: str | None = None,
    camera_id: str | None = None,
    extra_arguments: list[str] | None = None,
) -> list[str]:
    command = [
        "scrcpy",
        "--serial",
        serial,
        "--video-source=camera",
        f"--v4l2-sink={video_device}",
        "--no-playback",
    ]
    if camera_id is not None:
        command.append(f"--camera-id={camera_id}")
    else:
        command.append(f"--camera-facing={facing or 'back'}")
    if extra_arguments:
        command.extend(extra_arguments)
    return command


def _v4l2loopback_unavailable_message(plan: DependencyPlan) -> str:
    return f"v4l2loopback is not loaded; {dependency_install_hint(plan)}; then {_MODPROBE_HINT}"


def ensure_v4l2loopback(install: bool) -> None:
    plan = v4l2loopback_dependency_plan()
    if plan.available:
        return

    print(
        "\nv4l2loopback provides the /dev/videoN sink Linkplane streams the phone "
        "camera into."
    )
    if not install:
        if not sys.stdin.isatty():
            raise DependencyUnavailable(_v4l2loopback_unavailable_message(plan))
        answer = input("Install the v4l2loopback package now? [Y/n] ").strip().lower()
        if answer not in {"", "y", "yes"}:
            raise BridgeError("v4l2loopback installation cancelled")
    try:
        install_dependency(plan)
    except DependencyUnavailable as error:
        raise DependencyUnavailable(f"{error}; {_MODPROBE_HINT}") from error
    # Installing the package never loads the module for the running kernel; the module
    # must still be loaded (see the note above) before a sink device can exist.
    raise DependencyUnavailable(
        f"v4l2loopback was installed, but the kernel module still is not loaded; "
        f"{_MODPROBE_HINT}"
    )


def ensure_webcam_dependencies(install: bool) -> None:
    ensure_scrcpy(install)
    ensure_v4l2loopback(install)


def start_webcam(
    request: WebcamStartRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    scrcpy_dependency_factory: Callable[[], DependencyPlan] | None = None,
    loopback_dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    command_runner: Callable[..., str] = run_command,
    process_factory: Callable[..., Any] = subprocess.Popen,
    process_alive: Callable[[int], bool] | None = None,
    state_loader: Callable[[str | None], dict[str, Any] | None] | None = None,
    state_saver: Callable[[dict[str, Any], str | None], Path] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[WebcamStartResult]:
    if request.facing is not None and request.camera_id is not None:
        return OperationResult.failure(
            "invalid_request", "camera facing and camera ID are mutually exclusive"
        )
    if request.device is not None and request.device < 0:
        return OperationResult.failure(
            "invalid_request", "V4L2 device number must be zero or greater"
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

    try:
        video_device = resolve_webcam_device(request.device, command_runner=command_runner)
    except DependencyUnavailable as error:
        return OperationResult.failure("dependency_missing", str(error))

    extra_arguments = list(request.extra_arguments)
    if extra_arguments[:1] == ["--"]:
        extra_arguments.pop(0)
    command = build_webcam_command(
        serial,
        video_device,
        facing=request.facing,
        camera_id=request.camera_id,
        extra_arguments=extra_arguments,
    )
    model = device.get("model", "Android device").replace("_", " ")
    get_scrcpy_dependency = scrcpy_dependency_factory or scrcpy_dependency_plan
    get_loopback_dependency = loopback_dependency_factory or v4l2loopback_dependency_plan
    result = WebcamStartResult(
        device=model,
        serial=serial,
        video_device=video_device,
        command=tuple(command),
        scrcpy_dependency=get_scrcpy_dependency(),
        loopback_dependency=get_loopback_dependency(),
        pid=None,
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "webcam_start", "started", "Webcam sink prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent(
                "webcam_start", "command", shlex.join(command), details={"command": command}
            ),
        )
        report_progress(
            progress, ProgressEvent("webcam_start", "completed", "Dry run completed")
        )
        return OperationResult.success(result)

    load_state = state_loader or load_webcam_state
    is_alive = process_alive or _process_is_alive
    try:
        existing_state = load_state(request.state_path)
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))
    if existing_state is not None:
        existing_pid = existing_state.get("pid")
        if isinstance(existing_pid, int) and is_alive(existing_pid):
            existing_device = existing_state.get("device", "an unknown device")
            return OperationResult.failure(
                "already_running",
                f"a webcam sink is already running (pid {existing_pid}, {existing_device}); "
                "stop it first with `linkplane webcam stop`",
            )

    try:
        if dependency_handler is not None:
            dependency_handler(request.install)
        else:
            require_dependency(result.scrcpy_dependency, install=request.install)
            require_dependency(result.loopback_dependency, install=request.install)
        active_scrcpy = get_scrcpy_dependency()
        compatibility = (compatibility_factory or scrcpy_compatibility)(active_scrcpy)
        if not compatibility.webcam_supported:
            detail = compatibility.probe_error or (
                "missing required options: "
                f"{', '.join(compatibility.missing_webcam_options)}"
            )
            raise DependencyUnavailable(f"scrcpy V4L2 sink support is incompatible: {detail}")
        process = process_factory(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except DependencyUnavailable as error:
        return OperationResult.failure("dependency_missing", str(error))
    except FileNotFoundError:
        return OperationResult.failure("dependency_missing", "scrcpy is not installed")
    except OSError as error:
        return OperationResult.failure("operation_failed", str(error))
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))

    pid = process.pid
    try:
        (state_saver or save_webcam_state)(
            {
                "pid": pid,
                "device": video_device,
                "serial": serial,
                "command": list(command),
                "started_at": datetime.now().isoformat(timespec="seconds"),
            },
            request.state_path,
        )
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))

    result = replace(
        result,
        scrcpy_dependency=get_scrcpy_dependency(),
        loopback_dependency=get_loopback_dependency(),
        pid=pid,
    )
    report_progress(
        progress,
        ProgressEvent(
            "webcam_start", "completed", "Webcam sink started", details=result.to_dict()
        ),
    )
    return OperationResult.success(result)


def stop_webcam(
    request: WebcamStopRequest,
    *,
    state_loader: Callable[[str | None], dict[str, Any] | None] | None = None,
    state_clearer: Callable[[str | None], None] | None = None,
    process_terminator: Callable[[int], None] | None = None,
    process_alive: Callable[[int], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[WebcamStopResult]:
    load_state = state_loader or load_webcam_state
    clear_state = state_clearer or clear_webcam_state
    try:
        state = load_state(request.state_path)
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))
    if state is None:
        return OperationResult.failure(
            "not_found", "no webcam sink is currently tracked; nothing to stop"
        )

    pid = state.get("pid")
    video_device = state.get("device")
    serial = state.get("serial")
    if not isinstance(pid, int) or not isinstance(video_device, str):
        clear_state(request.state_path)
        return OperationResult.failure(
            "operation_failed", "webcam state file was invalid; it has been cleared"
        )

    result = WebcamStopResult(
        pid=pid, video_device=video_device, serial=serial, dry_run=request.dry_run
    )
    report_progress(
        progress,
        ProgressEvent(
            "webcam_stop", "started", "Webcam stop prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress, ProgressEvent("webcam_stop", "completed", "Dry run completed")
        )
        return OperationResult.success(result)

    is_alive = process_alive or _process_is_alive
    if is_alive(pid):
        try:
            (process_terminator or _terminate_process)(pid)
        except OSError as error:
            return OperationResult.failure(
                "operation_failed", f"unable to stop webcam process {pid}: {error}"
            )
    try:
        clear_state(request.state_path)
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))
    report_progress(
        progress,
        ProgressEvent(
            "webcam_stop", "completed", "Webcam sink stopped", details=result.to_dict()
        ),
    )
    return OperationResult.success(result)


def render_webcam_start_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Webcam")
        print(f"Device      {result['device']} ({result['serial']})")
        print(f"Sink        {result['video_device']}")
    elif event.phase == "command":
        print(f"Command     {event.message}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        result = event.details
        if result.get("pid"):
            print(f"PID         {result['pid']}")


def render_webcam_stop_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Webcam Stop")
        print(f"PID         {result['pid']}")
        print(f"Sink        {result['video_device']}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        print("Stopped     webcam sink")


def webcam_start(arguments: Any) -> int:
    result = start_webcam(
        WebcamStartRequest(
            serial=arguments.serial,
            device=arguments.device,
            install=arguments.install,
            dry_run=arguments.dry_run,
        ),
        dependency_handler=ensure_webcam_dependencies,
        progress=None if arguments.json else render_webcam_start_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful webcam start did not return a value")
    if arguments.json:
        print(
            json.dumps(
                {"schema_version": JSON_SCHEMA_VERSION, "ok": True, "data": result.value.to_dict()},
                indent=2,
            )
        )
    return 0


def webcam_stop(arguments: Any) -> int:
    result = stop_webcam(
        WebcamStopRequest(dry_run=arguments.dry_run),
        progress=None if arguments.json else render_webcam_stop_progress,
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful webcam stop did not return a value")
    if arguments.json:
        print(
            json.dumps(
                {"schema_version": JSON_SCHEMA_VERSION, "ok": True, "data": result.value.to_dict()},
                indent=2,
            )
        )
    return 0
