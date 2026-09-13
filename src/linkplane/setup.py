"""`linkplane setup`: the guided first-run flow (Install & Onboarding v0.1, Slice 1).

Setup composes primitives that already exist -- the dependency catalogue, the ADB device
list and its state vocabulary, USB pairing, the systemd unit installer, the daemon control
socket, and the ADB provider -- into one rerunnable sequence of phases
(`docs/install-onboarding-design.md` §9). It owns no persistence of its own: every phase
reads its entry condition from reality (dependencies, configuration, unit file, socket,
observed state), which is what makes running it twice safe.

Everything external is a seam on `SetupSeams`, resolved at call time, so the whole flow is
testable without a phone, an ADB server, systemd, sudo, or a package manager.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from linkplane import __version__
from linkplane import clients as clients_module
from linkplane import daemon as daemond
from linkplane import service
from linkplane.core import errors
from linkplane.dependencies import (
    DependencyPlan,
    DependencyReport,
    dependency_install_hint,
    dependency_report,
    install_dependency,
    usb_rules_install_hint,
)
from linkplane.doctor import configuration_permission_check
from linkplane.models import Check
from linkplane.operations import (
    CancellationToken,
    ProgressCallback,
    ProgressEvent,
    check_cancelled,
    report_progress,
)
from linkplane.profiles import (
    ProfileChangeRequest,
    UsbPairingRequest,
    next_free_profile_name,
    pair_usb_device,
    profiles_from_config,
    select_default_profile,
)
from linkplane.providers.adb import ADBProvider
from linkplane.providers.base import Provider
from linkplane.transports import (
    ADB_STATE_DEVICE,
    ADB_STATE_NO_PERMISSIONS,
    ADB_STATE_OFFLINE,
    ADB_STATE_UNAUTHORIZED,
    AdbTransport,
    BridgeError,
    describe_blocked_adb_state,
    load_config,
    parse_adb_devices,
    resolve_config_path,
    run_command,
)

# Phase names, in order. `Check.name` on a step is the human line; `SetupStep.phase` is
# one of these.
PREFLIGHT = "preflight"
DEPENDENCIES = "dependencies"
DEVICE_DETECTION = "device_detection"
DEVICE_AUTHORIZATION = "device_authorization"
DEVICE_REGISTRATION = "device_registration"
DAEMON_INSTALLATION = "daemon_installation"
DAEMON_START = "daemon_start"
OBSERVATION_VERIFICATION = "observation_verification"
FIRST_USE_VERIFICATION = "first_use_verification"
API_CLIENT = "api_client"
COMPLETE = "complete"
PHASES = (
    PREFLIGHT, DEPENDENCIES, DEVICE_DETECTION, DEVICE_AUTHORIZATION, DEVICE_REGISTRATION,
    DAEMON_INSTALLATION, DAEMON_START, OBSERVATION_VERIFICATION, FIRST_USE_VERIFICATION,
    API_CLIENT, COMPLETE,
)

DEFAULT_PROFILE_NAME = "phone"
API_CLIENT_SCOPES = ("read",)  # expands to READ_SCOPES; never audit/rules/cancel

# Guidance the human sees while a wait is in progress. Phone-side steps only where the
# phone is the thing to act on; host-side steps only where the computer is.
CONNECT_GUIDANCE = (
    "On your phone:",
    "  1. Open Settings → About phone and tap \"Build number\" seven times",
    "     (this turns on Developer options)",
    "  2. Open Settings → System → Developer options and turn on \"USB debugging\"",
    "  3. Connect the phone to this computer with a USB data cable",
    "  4. Tap \"Allow\" when Android asks whether to allow USB debugging from this computer",
)
UNAUTHORIZED_GUIDANCE = (
    "Your phone sees this computer but has not authorized it yet.",
    "Check the phone for \"Allow USB debugging?\" and tap Allow",
    "(tick \"Always allow from this computer\" so you are not asked again).",
)
OFFLINE_GUIDANCE = (
    "ADB can see the phone but cannot currently communicate with it.",
    "Unlock the phone, or unplug the USB cable and plug it back in, then wait a moment.",
    "If this persists, run `linkplane doctor`.",
)
RECONNECT_GUIDANCE = (
    "The phone disappeared from USB. Reconnect the cable to continue.",
)
NEXT_STEPS = (
    "linkplane status",
    "linkplane notify \"Hello from Linkplane\"",
    "linkplane send <file>",
)
ADVANCED_HINT = "Wireless ADB or Termux/SSH later: linkplane pair wireless … / linkplane pair ssh …"

# Optional tools worth one line each on the happy path; everything else stays in doctor.
OPTIONAL_FEATURES = (
    ("scrcpy", "Screen control, camera preview, audio, webcam", "scrcpy not installed", "linkplane screen --install"),
    ("notify-send", "Desktop notifications from rules", "notify-send not installed", None),
    ("v4l2-ctl", "Webcam", "v4l2-ctl not installed", None),
)


@dataclass(frozen=True)
class SetupOptions:
    config_path: str | None = None
    name: str | None = None
    serial: str | None = None
    install_daemon: bool = True
    unit_dir: str | None = None
    socket_path: str | None = None
    daemon_arguments: tuple[str, ...] = ()
    api_client: str | None = None
    clients_path: str | None = None
    dry_run: bool = False
    interactive: bool = True
    device_wait: float = 120.0
    daemon_wait: float = 10.0
    observe_wait: float = 15.0
    poll_interval: float = 1.0


@dataclass(frozen=True)
class SetupSeams:
    """Every external touch point, resolved at call time (None → the real thing)."""

    finder: Callable[[str], str | None] | None = None
    environ: dict[str, str] | None = None
    list_adb_devices: Callable[[], list[dict[str, str]]] | None = None
    adb_factory: Callable[[str | None], AdbTransport] | None = None
    provider_factory: Callable[[str], Provider] | None = None
    install_dependency: Callable[[DependencyPlan], None] | None = None
    service_install: Callable[..., service.ServiceResult] | None = None
    daemon_command: Callable[[tuple[str, ...]], list[str]] | None = None
    daemon_status: Callable[[str | None], dict[str, Any] | None] | None = None
    restart_daemon: Callable[[], None] | None = None
    start_daemon: Callable[[], None] | None = None
    create_client: Callable[..., tuple[Any, str]] | None = None
    load_clients: Callable[[str | None], tuple[Any, ...]] | None = None
    clock: Callable[[], float] | None = None
    sleep: Callable[[float], None] | None = None
    ask: Callable[[str, str], str] | None = None
    confirm: Callable[[str], bool] | None = None
    choose: Callable[[str, list[str]], int | None] | None = None


@dataclass(frozen=True)
class SetupStep:
    phase: str
    check: Check

    def to_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, **self.check.to_dict()}


@dataclass(frozen=True)
class SetupTiming:
    started: str
    first_use_succeeded: str | None
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_started": self.started,
            "first_use_succeeded": self.first_use_succeeded,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass(frozen=True)
class SetupResult:
    ok: bool
    status: str  # complete | stopped
    steps: tuple[SetupStep, ...]
    timing: SetupTiming
    device: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    daemon: dict[str, Any] = field(default_factory=dict)
    api_client: dict[str, Any] | None = None
    next_steps: tuple[str, ...] = ()
    dry_run: bool = False

    @property
    def failure(self) -> SetupStep | None:
        return next((step for step in reversed(self.steps) if step.check.status == "error"), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "dry_run": self.dry_run,
            "steps": [step.to_dict() for step in self.steps],
            "device": self.device,
            "profile": self.profile,
            "daemon": self.daemon,
            "api_client": self.api_client,
            "timing": self.timing.to_dict(),
            "next_steps": list(self.next_steps),
        }


class SetupStopped(Exception):
    """A phase failed in a way setup cannot recover; carries the failing step."""

    def __init__(self, step: SetupStep):
        super().__init__(step.check.summary)
        self.step = step


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_list_adb_devices() -> list[dict[str, str]]:
    return parse_adb_devices(run_command(["adb", "devices", "-l"], timeout=15))


def _default_daemon_status(socket_path: str | None) -> dict[str, Any] | None:
    try:
        return daemond.request("status", socket_path)
    except (errors.LinkplaneError, OSError):
        return None


def _default_restart_daemon() -> None:
    run_command(["systemctl", "--user", "restart", service.UNIT_NAME], timeout=30)


def _default_start_daemon() -> None:
    run_command(["systemctl", "--user", "start", service.UNIT_NAME], timeout=30)


def exec_start_of(unit_text: str) -> list[str] | None:
    """The argv of the unit's `ExecStart=` line, or None when there is none."""
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            try:
                return shlex.split(line[len("ExecStart="):])
            except ValueError:
                return None
    return None


class _Run:
    """One execution of the flow. Phases are methods; `run()` sequences them."""

    def __init__(
        self,
        options: SetupOptions,
        seams: SetupSeams,
        progress: ProgressCallback | None,
        cancel: CancellationToken | None,
    ) -> None:
        self.options = options
        self.progress = progress
        self.cancel = cancel
        s = seams
        self.finder = s.finder or shutil.which
        self.environ = s.environ if s.environ is not None else os.environ
        self.list_adb_devices = s.list_adb_devices or _default_list_adb_devices
        self.adb_factory = s.adb_factory or AdbTransport
        self.provider_factory = s.provider_factory or (lambda serial: ADBProvider(self.adb_factory(serial)))
        self.install_dependency = s.install_dependency or install_dependency
        self.service_install = s.service_install or service.install
        self.daemon_command = s.daemon_command or service.daemon_command
        self.daemon_status = s.daemon_status or _default_daemon_status
        self.restart_daemon = s.restart_daemon or _default_restart_daemon
        self.start_daemon = s.start_daemon or _default_start_daemon
        self.create_client = s.create_client or clients_module.create_client
        self.load_clients = s.load_clients or clients_module.load_clients
        self.clock = s.clock or time.monotonic
        self.sleep = s.sleep or time.sleep
        self.ask = s.ask or (lambda question, default: default)
        self.confirm = s.confirm or (lambda question: False)
        self.choose = s.choose or (lambda question, choices: None)

        self.steps: list[SetupStep] = []
        self.started_at = _now_iso()
        self.started_clock = self.clock()
        self.first_use_at: str | None = None
        self.serial: str | None = None
        self.model: str | None = None
        self.profile: dict[str, Any] | None = None
        self.daemon: dict[str, Any] = {
            "requested": options.install_daemon,
            "available": None,
            "unit_path": None,
            "installed": False,
            "running": False,
            "version": None,
            "restarted": False,
            "started": False,
        }
        self.daemon_was_running = False
        self.config_changed = False
        self.api_client_result: dict[str, Any] | None = None

    # -- helpers ---------------------------------------------------------------------

    def step(self, phase: str, name: str, status: str, summary: str, fix: str | None = None, code: str | None = None) -> SetupStep:
        record = SetupStep(phase, Check(name, status, summary, fix, code))
        self.steps.append(record)
        report_progress(self.progress, ProgressEvent("setup", phase, summary, details={"step": record.to_dict()}))
        return record

    def guidance(self, phase: str, lines: tuple[str, ...]) -> None:
        report_progress(self.progress, ProgressEvent("setup", phase, "\n".join(lines), details={"guidance": list(lines)}))

    def stop(self, phase: str, name: str, summary: str, fix: str | None, code: str | None) -> SetupStopped:
        return SetupStopped(self.step(phase, name, "error", summary, fix, code))

    def stop_with(self, phase: str, name: str, error: BridgeError) -> SetupStopped:
        classified = errors.classify(error, provider="adb")
        return self.stop(phase, name, str(classified), "; ".join(classified.hints) or None, classified.code)

    def wait(self) -> None:
        check_cancelled(self.cancel)
        self.sleep(self.options.poll_interval)

    @property
    def systemd_available(self) -> bool:
        return self.finder("systemctl") is not None and bool(self.environ.get("XDG_RUNTIME_DIR"))

    # -- phases ----------------------------------------------------------------------

    def preflight(self) -> None:
        phase = PREFLIGHT
        self.step(phase, "Linkplane", "ok", __version__)
        report = dependency_report(self.finder)
        python = next(status for status in report.statuses if status.dependency.name == "python")
        if not python.available:
            raise self.stop(phase, "Python", f"Python {sys.version.split()[0]} is too old", "Linkplane needs Python 3.11 or newer", errors.DEPENDENCY_MISSING)
        self.step(phase, "Python", "ok", sys.version.split()[0])

        path = resolve_config_path(self.options.config_path)
        if path.exists():
            try:
                config = load_config(str(path))
                profiles_from_config(config)
            except BridgeError as error:
                raise self.stop(phase, "Configuration", f"{path}: {error}", "Fix or move the file; setup never overwrites a configuration it cannot read", errors.CONFIG_INVALID)
            if not os.access(path, os.W_OK):
                raise self.stop(phase, "Configuration", f"{path} is not writable", "; ".join(errors.CONFIG_UNWRITABLE_HINTS), errors.CONFIG_UNWRITABLE)
            self.step(phase, "Configuration", "ok", f"{path} (existing)")
            permissions = configuration_permission_check(path)
            if permissions is not None:
                self.step(phase, permissions.name, permissions.status, permissions.summary, permissions.fix)
        else:
            parent = path.parent
            probe = parent
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            if not os.access(probe, os.W_OK):
                raise self.stop(phase, "Configuration", f"cannot create {parent}", "; ".join(errors.CONFIG_UNWRITABLE_HINTS), errors.CONFIG_UNWRITABLE)
            self.step(phase, "Configuration", "ok", f"{path} (will be created)")

        if self.options.install_daemon:
            self.daemon["available"] = self.systemd_available
            if not self.systemd_available:
                self.step(phase, "Background service", "warning", "systemd --user is not available in this session", "The daemon can still be run by hand: linkplane daemon run")
        self.daemon_was_running = self.daemon_status(self.options.socket_path) is not None
        self.dependency_report_value = report

    def dependencies(self) -> None:
        phase = DEPENDENCIES
        report: DependencyReport = self.dependency_report_value
        adb = next(status for status in report.statuses if status.dependency.name == "adb")
        plan = adb.plan
        if adb.available:
            self.step(phase, "Android platform tools", "ok", (plan.executable_path if plan else "adb"))
        else:
            hint = dependency_install_hint(plan) if plan else "install adb with your package manager"
            if plan is not None and plan.install_command and self.options.interactive and not self.options.dry_run:
                command = shlex.join(plan.install_command)
                self.guidance(phase, (
                    "ADB is required to connect an Android phone.",
                    "",
                    "Linkplane can run:",
                    f"  {command}",
                    "",
                    "Your package manager may ask for your password; Linkplane itself does not need root.",
                ))
                if self.confirm(f"Install {plan.package or 'adb'} now?"):
                    try:
                        self.install_dependency(plan)
                    except BridgeError as error:
                        raise self.stop(phase, "Android platform tools", str(error), f"Install it by hand: {command}, then run `linkplane setup` again", errors.DEPENDENCY_MISSING)
                    refreshed = dependency_report(self.finder)
                    adb = next(status for status in refreshed.statuses if status.dependency.name == "adb")
                    if not adb.available:
                        raise self.stop(phase, "Android platform tools", "adb is still not on PATH after installation", "Open a new terminal and run `linkplane setup` again", errors.DEPENDENCY_MISSING)
                    self.step(phase, "Android platform tools", "ok", f"installed ({adb.plan.executable_path if adb.plan else 'adb'})")
                else:
                    raise self.stop(phase, "Android platform tools", "adb is not installed (installation declined)", f"{hint}; then run `linkplane setup` again", errors.DEPENDENCY_MISSING)
            else:
                raise self.stop(phase, "Android platform tools", "adb is not installed", f"{hint}; then run `linkplane setup` again", errors.DEPENDENCY_MISSING)

        missing = set(report.optional_missing)
        unavailable = [
            f"{feature}: {reason}" + (f" ({fix})" if fix else "")
            for name, feature, reason, fix in OPTIONAL_FEATURES
            if name in missing
        ]
        if unavailable:
            self.step(phase, "Optional features", "warning", "unavailable: " + "; ".join(unavailable), "Optional; basic setup continues")

    def _devices(self) -> list[dict[str, str]]:
        try:
            devices = self.list_adb_devices()
        except BridgeError as error:
            raise self.stop_with(DEVICE_DETECTION, "Android device", error)
        if self.serial is not None:
            devices = [device for device in devices if device["serial"] == self.serial]
        elif self.options.serial is not None:
            devices = [device for device in devices if device["serial"] == self.options.serial]
        return devices

    @staticmethod
    def _describe(device: dict[str, str]) -> str:
        model = (device.get("model") or "Android device").replace("_", " ")
        return f"{model} ({device['serial']}, {device['state']})"

    def device_detection(self, deadline: float) -> dict[str, str]:
        phase = DEVICE_DETECTION
        announced = False
        while True:
            devices = self._devices()
            if devices:
                break
            if not announced:
                self.guidance(phase, CONNECT_GUIDANCE)
                announced = True
            if self.clock() >= deadline:
                raise self.stop(phase, "Android device", "no phone appeared on USB", "Check the cable (charge-only cables are the usual cause), USB debugging, and run `linkplane setup` again", errors.CONNECT_NO_DEVICE)
            self.wait()
        if len(devices) > 1:
            if not self.options.interactive:
                serials = ", ".join(device["serial"] for device in devices)
                raise self.stop(phase, "Android device", f"more than one phone is connected ({serials})", "Pass --serial <serial> or unplug the others", errors.CONNECT_AMBIGUOUS)
            index = self.choose("Which phone should Linkplane set up?", [self._describe(device) for device in devices])
            if index is None:
                serials = ", ".join(device["serial"] for device in devices)
                raise self.stop(phase, "Android device", f"more than one phone is connected ({serials})", "Pass --serial <serial> or unplug the others", errors.CONNECT_AMBIGUOUS)
            device = devices[index]
        else:
            device = devices[0]
        # From here on the flow follows this hardware identity, whatever discovery order does.
        self.serial = device["serial"]
        self.model = (device.get("model") or "").replace("_", " ") or None
        self.step(phase, "Android device detected", "ok", self._describe(device))
        return device

    def device_authorization(self, deadline: float) -> dict[str, str]:
        phase = DEVICE_AUTHORIZATION
        last_state: str | None = None
        while True:
            devices = self._devices()
            device = devices[0] if devices else None
            state = device["state"] if device else None
            if device is not None and state == ADB_STATE_DEVICE:
                if not self.model:
                    self.model = (device.get("model") or "").replace("_", " ") or None
                self.step(phase, "USB debugging authorized", "ok", self.serial or "")
                return device
            if state == ADB_STATE_NO_PERMISSIONS:
                message = describe_blocked_adb_state(self.serial or "", state)
                hints = (*errors.USB_PERMISSION_HINTS, usb_rules_install_hint(self.finder))
                raise self.stop(phase, "USB access", message, "; ".join(hints), errors.AUTH_USB_PERMISSION)
            if state != last_state:
                if state == ADB_STATE_UNAUTHORIZED:
                    self.guidance(phase, UNAUTHORIZED_GUIDANCE)
                elif state == ADB_STATE_OFFLINE:
                    self.guidance(phase, OFFLINE_GUIDANCE)
                elif state is None:
                    self.guidance(phase, RECONNECT_GUIDANCE)
                else:
                    self.guidance(phase, (describe_blocked_adb_state(self.serial or "", state),))
                last_state = state
            if self.clock() >= deadline:
                if state is None:
                    raise self.stop(phase, "USB debugging authorized", f"{self.serial} disconnected and did not come back", "Reconnect the phone and run `linkplane setup` again", errors.CONNECT_NO_DEVICE)
                classified = errors.classify(BridgeError(describe_blocked_adb_state(self.serial or "", state or "")), provider="adb")
                fix = "; ".join((*classified.hints, "if this persists, run `linkplane doctor`"))
                raise self.stop(phase, "USB debugging authorized", str(classified), fix, classified.code)
            self.wait()

    def device_registration(self) -> None:
        phase = DEVICE_REGISTRATION
        assert self.serial is not None
        config_path = self.options.config_path
        try:
            config = load_config(config_path)
            profiles = profiles_from_config(config)
        except BridgeError as error:
            raise self.stop(phase, "Device registered", str(error), "Fix the configuration file", errors.CONFIG_INVALID)
        default = config.get("default_device")
        existing = next((profile for profile in profiles.values() if profile.device_id == self.serial), None)
        if existing is not None:
            made_default = False
            if default is None and not self.options.dry_run:
                result = select_default_profile(ProfileChangeRequest(existing.name, config_path))
                if result.error is None:
                    made_default = True
            self.profile = {"name": existing.name, "device_id": existing.device_id, "created": False, "default": (default == existing.name) or made_default}
            self.step(phase, "Device registered", "ok", f"already registered as \"{existing.name}\"" + (" (now the default)" if made_default else ""))
            return

        requested = self.options.name
        suggested = next_free_profile_name(requested or DEFAULT_PROFILE_NAME, profiles)
        name = requested or suggested
        if requested is None and self.options.interactive:
            name = self.ask("Name this device", suggested).strip() or suggested
        attempts = 0
        while True:
            owner = profiles.get(name)
            if owner is None or owner.device_id == self.serial:
                break
            attempts += 1
            alternative = next_free_profile_name(name, profiles)
            if requested is not None or not self.options.interactive or attempts > 3:
                raise self.stop(phase, "Device registered", f"the name \"{name}\" already belongs to device {owner.device_id}", f"Use another name, for example {alternative} (or remove it: linkplane profiles remove {name})", errors.REQUEST_INVALID)
            self.guidance(phase, (f"\"{name}\" already belongs to another phone ({owner.device_id}).", f"Suggested: {alternative}"))
            name = self.ask("Name this device", alternative).strip() or alternative

        result = pair_usb_device(
            UsbPairingRequest(name=name, serial=self.serial, make_default=default is None, dry_run=self.options.dry_run, config_path=config_path),
            adb_factory=self.adb_factory,
            adb_locator=self.finder,
        )
        if result.error is not None:
            code = result.error.error_code or errors.OPERATION_CODE_MAP.get(result.error.code, errors.PROVIDER_FAILED)
            raise self.stop(phase, "Device registered", result.error.message, "; ".join(result.error.hints) or None, code)
        assert result.value is not None
        if result.value.device and not self.model:
            self.model = result.value.device
        self.config_changed = not self.options.dry_run
        self.profile = {"name": name, "device_id": result.value.device_id, "created": not self.options.dry_run, "default": default is None or default == name}
        verb = "would be registered" if self.options.dry_run else "registered"
        self.step(phase, "Device registered", "ok", f"{verb} as \"{name}\"" + (" (default)" if default is None else ""))

    def daemon_installation(self) -> None:
        phase = DAEMON_INSTALLATION
        if not self.options.install_daemon:
            self.step(phase, "Daemon installed", "skipped", "not requested (--no-daemon)")
            return
        if not self.systemd_available:
            self.step(phase, "Daemon installed", "skipped", "systemd --user unavailable", "Run `linkplane daemon run` under your own supervisor")
            return
        unit_path = service.resolve_unit_dir(self.options.unit_dir) / service.UNIT_NAME
        self.daemon["unit_path"] = str(unit_path)
        wanted = self.daemon_command(self.options.daemon_arguments)
        if unit_path.exists():
            current = exec_start_of(unit_path.read_text(encoding="utf-8"))
            if current == wanted:
                self.daemon["installed"] = True
                self.step(phase, "Daemon installed", "ok", f"already installed ({unit_path})")
                return
            reason = "ExecStart points elsewhere" if current else "unit has no ExecStart"
            if self.options.dry_run:
                self.step(phase, "Daemon installed", "ok", f"would rewrite {unit_path} ({reason})")
                return
            try:
                self.service_install(unit_dir=self.options.unit_dir, extra_arguments=self.options.daemon_arguments)
            except errors.LinkplaneError as error:
                raise self.stop(phase, "Daemon installed", str(error), "; ".join(error.hints) or "Check `systemctl --user status linkplaned`", error.code)
            self.daemon["installed"] = True
            self.daemon["restarted"] = True
            self.step(phase, "Daemon installed", "ok", f"reinstalled ({reason})")
            return
        try:
            result = self.service_install(unit_dir=self.options.unit_dir, extra_arguments=self.options.daemon_arguments, dry_run=self.options.dry_run)
        except errors.LinkplaneError as error:
            raise self.stop(phase, "Daemon installed", str(error), "; ".join(error.hints) or "Check `systemctl --user status linkplaned`", error.code)
        if self.options.dry_run:
            self.step(phase, "Daemon installed", "ok", f"would install {result.unit_path}")
            return
        self.daemon["installed"] = True
        self.step(phase, "Daemon installed", "ok", str(result.unit_path))

    def _status_with_version(self) -> tuple[dict[str, Any] | None, str | None]:
        info = self.daemon_status(self.options.socket_path)
        return info, (str(info.get("version")) if info and info.get("version") is not None else None)

    def _wait_for_daemon(self, deadline: float, *, want_version: bool) -> dict[str, Any] | None:
        while True:
            info, version = self._status_with_version()
            if info is not None and (not want_version or version == __version__):
                return info
            if self.clock() >= deadline:
                return info
            self.wait()

    def daemon_start(self) -> None:
        phase = DAEMON_START
        info, version = self._status_with_version()
        installed = self.daemon["installed"]
        if info is None and (not installed or self.options.dry_run):
            self.step(phase, "Daemon running", "skipped", "no daemon is running" + (" (dry run)" if self.options.dry_run and installed else ""), None if self.options.dry_run else "Install it with `linkplane daemon install`, or run `linkplane daemon run`")
            return
        started_here = False
        if info is None:
            # The unit exists (installed now or earlier) but nothing answers on the socket:
            # a fresh install was started by `enable --now`, an existing unit may simply be
            # inactive (e.g. after `linkplane daemon stop`; found in onboarding run 002).
            # Starting an installed unit is idempotent, so ask systemd either way.
            try:
                self.start_daemon()
                started_here = True
            except BridgeError as error:
                raise self.stop(phase, "Daemon running", f"could not start the service: {error}", "See `systemctl --user status linkplaned` and `journalctl --user -u linkplaned -n 20`", errors.DAEMON_NOT_RUNNING)
            deadline = self.clock() + self.options.daemon_wait
            info = self._wait_for_daemon(deadline, want_version=False)
            if info is None:
                raise self.stop(phase, "Daemon running", "the service did not start", "See `systemctl --user status linkplaned` and `journalctl --user -u linkplaned -n 20`", errors.DAEMON_NOT_RUNNING)
            version = str(info.get("version")) if info.get("version") is not None else None
        needs_restart = (version != __version__) or (self.config_changed and self.daemon_was_running) or self.daemon["restarted"]
        if needs_restart and installed and not self.options.dry_run:
            why = f"was version {version or 'unknown'}" if version != __version__ else "profile changed"
            try:
                self.restart_daemon()
            except BridgeError as error:
                raise self.stop(phase, "Daemon running", f"restart failed: {error}", "See `systemctl --user status linkplaned`", errors.DAEMON_NOT_RUNNING)
            deadline = self.clock() + self.options.daemon_wait
            info = self._wait_for_daemon(deadline, want_version=True)
            version = str(info.get("version")) if info and info.get("version") is not None else None
            if info is None or version != __version__:
                raise self.stop(phase, "Daemon running", f"after restart the daemon reports version {version or 'unknown'}, installed is {__version__}", "The unit may point at another installation; see `linkplane daemon install --dry-run`", errors.DAEMON_NOT_RUNNING)
            self.daemon["restarted"] = True
            self.daemon["running"] = True
            self.daemon["version"] = version
            self.step(phase, "Daemon running", "ok", f"restarted ({why}); pid {info.get('pid')}, version {version}")
            return
        self.daemon["running"] = True
        self.daemon["version"] = version
        if version != __version__:
            self.step(phase, "Daemon running", "warning", f"pid {info.get('pid')} runs version {version or 'unknown'}, installed is {__version__}", "Restart it: linkplane daemon stop, then linkplane daemon run (or systemctl --user restart linkplaned)")
            return
        self.daemon["started"] = started_here
        self.step(phase, "Daemon running", "ok", ("started; " if started_here else "") + f"pid {info.get('pid')}, version {version}")

    def observation_verification(self) -> None:
        phase = OBSERVATION_VERIFICATION
        if not self.daemon["running"]:
            self.step(phase, "Device observed", "skipped", "no daemon to observe with")
            return
        names = {f"serial:{self.serial}"}
        if self.profile:
            names.add(self.profile["name"])
        deadline = self.clock() + self.options.observe_wait
        while True:
            info = self.daemon_status(self.options.socket_path)
            devices = (info or {}).get("devices") or {}
            for name, state in devices.items():
                if (state.get("address") == self.serial or name in names) and state.get("connection") == "connected":
                    battery = (state.get("battery") or {}).get("level")
                    self.step(phase, "Device observed", "ok", f"{name}: connected" + (f", battery {battery}%" if battery is not None else ""))
                    return
            if self.clock() >= deadline:
                raise self.stop(phase, "Device observed", f"the daemon has not observed {self.serial} yet", "Unplug and replug the phone, then run `linkplane setup` again; `linkplane events --follow` shows what the daemon sees; `linkplane doctor` for diagnostics", errors.CONNECT_NO_DEVICE)
            self.wait()

    def first_use_verification(self) -> None:
        phase = FIRST_USE_VERIFICATION
        assert self.serial is not None
        try:
            provider = self.provider_factory(self.serial)
            ping = provider.ping()
        except BridgeError as error:
            raise self.stop_with(phase, "Ping", error)
        if not ping.reachable:
            raise self.stop_with(phase, "Ping", BridgeError(ping.detail or "no response"))
        latency = f"{ping.latency_ms:g} ms" if ping.latency_ms is not None else "answered"
        self.step(phase, "Ping", "ok", latency)
        try:
            status = provider.status()
        except BridgeError as error:
            raise self.stop_with(phase, "Status", error)
        model = status.device.get("model") or self.model or "device"
        sections = [name for name in ("battery", "memory", "storage") if getattr(status, name) is not None]
        if status.issues:
            components = ", ".join(issue.component for issue in status.issues)
            self.step(phase, "Status", "warning", f"{model}: partial ({components} unavailable)", "Run `linkplane status` for details")
        else:
            self.step(phase, "Status", "ok", f"{model}: {', '.join(sections)}")
        try:
            battery = provider.battery()
        except BridgeError as error:
            raise self.stop_with(phase, "Battery", error)
        self.step(phase, "Battery", "ok", f"{battery.level}% ({battery.status})")
        self.first_use_at = _now_iso()

    def api_client(self) -> None:
        phase = API_CLIENT
        client_id = self.options.api_client
        if not client_id:
            return
        try:
            existing = self.load_clients(self.options.clients_path)
        except errors.LinkplaneError as error:
            raise self.stop(phase, "API client", str(error), "; ".join(error.hints) or None, error.code)
        match = next((client for client in existing if client.client_id == client_id), None)
        if match is not None:
            self.api_client_result = {"client_id": client_id, "scopes": list(match.scopes), "created": False, "token": None}
            self.step(phase, "API client", "ok", f"\"{client_id}\" already exists (its token was shown once at creation)")
            return
        if self.options.dry_run:
            self.api_client_result = {"client_id": client_id, "scopes": list(API_CLIENT_SCOPES), "created": False, "token": None}
            self.step(phase, "API client", "ok", f"would create \"{client_id}\" with read scopes")
            return
        try:
            client, token = self.create_client(client_id, client_type="tool", scopes=API_CLIENT_SCOPES, path=self.options.clients_path)
        except errors.LinkplaneError as error:
            raise self.stop(phase, "API client", str(error), "; ".join(error.hints) or None, error.code)
        self.api_client_result = {"client_id": client.client_id, "scopes": list(client.scopes), "created": True, "token": token}
        self.step(phase, "API client", "ok", f"created \"{client_id}\" ({', '.join(client.scopes)})")

    # -- sequencing ------------------------------------------------------------------

    def run(self) -> SetupResult:
        try:
            self.preflight()
            self.dependencies()
            deadline = self.clock() + self.options.device_wait
            self.device_detection(deadline)
            self.device_authorization(deadline)
            self.device_registration()
            self.daemon_installation()
            self.daemon_start()
            self.observation_verification()
            self.first_use_verification()
            self.api_client()
            self.step(COMPLETE, "Ready", "ok", "Your phone is ready")
            return self.result(True, "complete")
        except SetupStopped:
            return self.result(False, "stopped")

    def result(self, ok: bool, status: str) -> SetupResult:
        elapsed = self.clock() - self.started_clock
        device = {"serial": self.serial, "model": self.model} if self.serial else None
        return SetupResult(
            ok=ok,
            status=status,
            steps=tuple(self.steps),
            timing=SetupTiming(self.started_at, self.first_use_at, elapsed),
            device=device,
            profile=self.profile,
            daemon=dict(self.daemon),
            api_client=self.api_client_result,
            next_steps=NEXT_STEPS if ok else (),
            dry_run=self.options.dry_run,
        )


def run_setup(
    options: SetupOptions,
    seams: SetupSeams | None = None,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> SetupResult:
    """Run the guided setup. Never raises for a phase failure (see `SetupResult.failure`);
    `OperationCancelled` propagates when `cancel` fires during a wait."""
    return _Run(options, seams or SetupSeams(), progress, cancel).run()
