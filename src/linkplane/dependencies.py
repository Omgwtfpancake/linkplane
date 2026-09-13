from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from linkplane.transports import BridgeError, run_command


class DependencyUnavailable(BridgeError):
    """A required host dependency cannot be used or installed."""


@dataclass(frozen=True)
class DependencyPlan:
    executable: str
    package: str | None
    available: bool
    executable_path: str | None
    package_manager: str | None
    install_command: tuple[str, ...] | None
    required_executables: tuple[str, ...] = ()
    missing_executables: tuple[str, ...] = ()
    requires_privilege: bool = False
    install_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PackageManager:
    executable: str
    install_prefix: tuple[str, ...]
    requires_privilege: bool


@dataclass(frozen=True)
class ScrcpyCompatibility:
    screen_supported: bool
    camera_supported: bool
    audio_supported: bool = True
    missing_screen_options: tuple[str, ...] = ()
    missing_camera_options: tuple[str, ...] = ()
    missing_audio_options: tuple[str, ...] = ()
    probe_error: str | None = None
    webcam_supported: bool = True
    missing_webcam_options: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PACKAGE_MANAGER_SPECS: tuple[PackageManager, ...] = (
    PackageManager("pacman", ("pacman", "-S", "--needed"), True),
    PackageManager("apt-get", ("apt-get", "install"), True),
    PackageManager("dnf", ("dnf", "install"), True),
    PackageManager("brew", ("brew", "install"), False),
)

PACKAGE_MANAGERS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (
        manager.executable,
        (
            ("sudo", *manager.install_prefix)
            if manager.requires_privilege
            else manager.install_prefix
        ),
    )
    for manager in _PACKAGE_MANAGER_SPECS
)

PackageArguments = str | tuple[str, ...] | None

ADB_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "android-tools",
    "apt-get": "adb",
    "dnf": "android-tools",
    "brew": ("--cask", "android-platform-tools"),
}
SSH_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "openssh",
    "apt-get": "openssh-client",
    "dnf": "openssh-clients",
    "brew": "openssh",
}
SCRCPY_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "scrcpy",
    "apt-get": "scrcpy",
    "dnf": None,
    "brew": "scrcpy",
}
WL_CLIPBOARD_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "wl-clipboard",
    "apt-get": "wl-clipboard",
    "dnf": "wl-clipboard",
    "brew": None,
}
XCLIP_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "xclip",
    "apt-get": "xclip",
    "dnf": "xclip",
    "brew": None,
}
LOCALSEND_PACKAGES: Mapping[str, PackageArguments] = {
    manager.executable: None for manager in _PACKAGE_MANAGER_SPECS
}
V4L2LOOPBACK_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "v4l2loopback-dkms",
    "apt-get": "v4l2loopback-dkms",
    "dnf": None,
    "brew": None,
}
V4L2_UTILS_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "v4l-utils",
    "apt-get": "v4l-utils",
    "dnf": "v4l-utils",
    "brew": None,
}
NOTIFY_SEND_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "libnotify",
    "apt-get": "libnotify-bin",
    "dnf": "libnotify",
    "brew": None,
}
# systemd is part of the base system or absent; never something Linkplane installs.
SYSTEMD_PACKAGES: Mapping[str, PackageArguments] = {
    manager.executable: None for manager in _PACKAGE_MANAGER_SPECS
}
# The udev rules that let an unprivileged user open the phone over USB. Arch ships them
# separately (`android-udev`); Debian/Ubuntu (`adb` → android-sdk-platform-tools-common)
# and Fedora (`android-tools`) install them with adb. There is no executable to detect:
# the condition shows up at runtime as adb's `no permissions` state (LP-AUTH-003).
ADB_USB_RULES_PACKAGES: Mapping[str, PackageArguments] = {
    "pacman": "android-udev",
    "apt-get": None,
    "dnf": None,
    "brew": None,
}
USB_RULES_BUNDLED_WITH_ADB: tuple[str, ...] = ("apt-get", "dnf")


def dependency_plan(
    executable: str,
    *,
    package: str | None = None,
    packages: Mapping[str, PackageArguments] | None = None,
    additional_executables: tuple[str, ...] = (),
    finder: Callable[[str], str | None] | None = None,
    is_root: bool | None = None,
) -> DependencyPlan:
    locate = finder or shutil.which
    required_executables = (executable, *additional_executables)
    executable_paths = {name: locate(name) for name in required_executables}
    missing_executables = tuple(
        name for name, path in executable_paths.items() if path is None
    )

    selected_manager = next(
        (
            manager
            for manager in _PACKAGE_MANAGER_SPECS
            if locate(manager.executable)
        ),
        None,
    )
    package_name = package or executable
    install_error = None
    install_command = None
    requires_privilege = False
    if selected_manager is None:
        if missing_executables:
            install_error = "no supported package manager was found"
    else:
        package_arguments: tuple[str, ...]
        if packages is not None:
            configured_package = packages.get(selected_manager.executable)
        else:
            configured_package = package_name
        if configured_package is None:
            package_name = None
            install_error = (
                f"automatic installation is not supported with "
                f"{selected_manager.executable}"
            )
        else:
            package_arguments = (
                (configured_package,)
                if isinstance(configured_package, str)
                else configured_package
            )
            package_name = package_arguments[-1]
            requires_privilege = selected_manager.requires_privilege
            root = os.geteuid() == 0 if is_root is None else is_root
            elevation: tuple[str, ...] = ()
            if requires_privilege and not root:
                if locate("sudo") is None:
                    install_error = (
                        f"sudo is required to install {package_name} with "
                        f"{selected_manager.executable}"
                    )
                else:
                    elevation = ("sudo",)
            if install_error is None:
                install_command = (
                    *elevation,
                    *selected_manager.install_prefix,
                    *package_arguments,
                )

    return DependencyPlan(
        executable=executable,
        package=package_name,
        available=not missing_executables,
        executable_path=executable_paths[executable],
        package_manager=(
            selected_manager.executable if selected_manager is not None else None
        ),
        install_command=install_command,
        required_executables=required_executables,
        missing_executables=missing_executables,
        requires_privilege=requires_privilege,
        install_error=install_error,
    )


def dependency_install_hint(plan: DependencyPlan) -> str:
    if plan.install_command:
        return f"install it with: {shlex.join(plan.install_command)}"
    return plan.install_error or "install it with your system package manager"


def install_dependency(
    plan: DependencyPlan,
    *,
    runner: Callable[..., Any] | None = None,
    finder: Callable[[str], str | None] | None = None,
) -> None:
    if plan.available:
        return
    if plan.install_command is None:
        raise DependencyUnavailable(
            f"{plan.executable} is required; {dependency_install_hint(plan)}"
        )
    try:
        result = (runner or subprocess.run)(list(plan.install_command), check=False)
    except OSError as error:
        raise DependencyUnavailable(
            f"unable to install {plan.executable}: {error}"
        ) from error
    if result.returncode != 0:
        raise DependencyUnavailable(
            f"{plan.executable} installation failed with exit code {result.returncode}"
        )
    locate = finder or shutil.which
    required_executables = plan.required_executables or (plan.executable,)
    missing = [name for name in required_executables if locate(name) is None]
    if missing:
        raise DependencyUnavailable(
            f"{plan.executable} installation completed, but these commands are not "
            f"available: {', '.join(missing)}"
        )


def require_dependency(plan: DependencyPlan, *, install: bool = False) -> None:
    if plan.available:
        return
    if install:
        install_dependency(plan)
        return
    raise DependencyUnavailable(
        f"{plan.executable} is not installed; {dependency_install_hint(plan)}"
    )


def adb_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    return dependency_plan("adb", packages=ADB_PACKAGES, finder=finder)


def ssh_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    return dependency_plan("ssh", packages=SSH_PACKAGES, finder=finder)


def scrcpy_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    return dependency_plan("scrcpy", packages=SCRCPY_PACKAGES, finder=finder)


def scrcpy_compatibility(
    plan: DependencyPlan | None = None,
    *,
    runner: Callable[..., str] | None = None,
) -> ScrcpyCompatibility:
    runner = runner or run_command  # resolved at call time (patchable seam)
    dependency = plan or scrcpy_dependency_plan()
    if not dependency.available:
        return ScrcpyCompatibility(
            False,
            False,
            audio_supported=False,
            webcam_supported=False,
            probe_error="scrcpy is not installed",
        )
    try:
        help_output = runner(
            [dependency.executable_path or dependency.executable, "--help"], timeout=5
        )
    except (BridgeError, OSError) as error:
        return ScrcpyCompatibility(
            False, False, audio_supported=False, webcam_supported=False, probe_error=str(error)
        )

    screen_options = (
        "--max-size",
        "--max-fps",
        "--video-bit-rate",
        "--video-buffer",
        "--audio-buffer",
        "--gamepad",
        "--no-audio",
        "--record",
    )
    camera_options = (
        "--video-source",
        "--camera-facing",
        "--camera-id",
        "--camera-fps",
        "--camera-torch",
        "--no-audio",
        "--record",
    )
    audio_options = (
        "--no-video",
        "--audio-source",
        "--audio-codec",
        "--record",
        "--record-format",
    )
    webcam_options = (
        "--v4l2-sink",
        "--no-playback",
        "--video-source",
        "--camera-facing",
        "--camera-id",
    )
    missing_screen = tuple(
        option for option in screen_options if option not in help_output
    )
    missing_camera = tuple(
        option for option in camera_options if option not in help_output
    )
    missing_audio = tuple(
        option for option in audio_options if option not in help_output
    )
    missing_webcam = tuple(
        option for option in webcam_options if option not in help_output
    )
    return ScrcpyCompatibility(
        screen_supported=not missing_screen,
        camera_supported=not missing_camera,
        audio_supported=not missing_audio,
        missing_screen_options=missing_screen,
        missing_camera_options=missing_camera,
        missing_audio_options=missing_audio,
        webcam_supported=not missing_webcam,
        missing_webcam_options=missing_webcam,
    )


def v4l2loopback_module_loaded(
    *,
    module_path: str = "/sys/module/v4l2loopback",
    path_exists: Callable[[str], bool] = os.path.exists,
) -> bool:
    """Detect whether the v4l2loopback kernel module is currently loaded.

    v4l2loopback provides no command-line executable, so its readiness cannot be a
    `shutil.which` probe like other dependencies; the sysfs module directory is checked
    instead. `path_exists` is injectable so callers (and tests) never touch the real
    filesystem or load the kernel module.
    """
    return path_exists(module_path)


def v4l2loopback_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
    *,
    module_loaded: Callable[[], bool] | None = None,
) -> DependencyPlan:
    """Build a `DependencyPlan` for the v4l2loopback kernel module.

    `finder` locates the host's package manager exactly like other `*_dependency_plan`
    helpers; `module_loaded` overrides only the module-readiness check itself, since
    v4l2loopback has no executable for `finder` to find.
    """
    loaded = (module_loaded or v4l2loopback_module_loaded)()
    locate = finder or shutil.which

    def module_aware_finder(name: str) -> str | None:
        if name == "v4l2loopback":
            return "loaded" if loaded else None
        return locate(name)

    return dependency_plan(
        "v4l2loopback", packages=V4L2LOOPBACK_PACKAGES, finder=module_aware_finder
    )


def localsend_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    return dependency_plan("localsend-cli", packages=LOCALSEND_PACKAGES, finder=finder)


def notify_send_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    """`notify-send` (libnotify): desktop notifications from rules and `watch --notify`."""
    return dependency_plan("notify-send", packages=NOTIFY_SEND_PACKAGES, finder=finder)


def v4l2_ctl_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    """`v4l2-ctl` (v4l-utils): the webcam feature inspects the loopback device with it."""
    return dependency_plan("v4l2-ctl", packages=V4L2_UTILS_PACKAGES, finder=finder)


def systemd_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    """`systemctl`: needed only by `linkplane daemon install` (a `systemd --user` unit)."""
    return dependency_plan("systemctl", packages=SYSTEMD_PACKAGES, finder=finder)


def usb_rules_install_hint(
    finder: Callable[[str], str | None] | None = None,
) -> str:
    """How to get ADB's USB udev rules on this system, for the LP-AUTH-003 remediation.

    Text only: like every other plan this never runs anything, and the future setup flow
    shows it beside the exact package-manager command before asking.
    """
    locate = finder or shutil.which
    manager = next(
        (spec for spec in _PACKAGE_MANAGER_SPECS if locate(spec.executable)), None
    )
    if manager is None:
        return "install your distribution's ADB udev rules, then unplug and replug the phone"
    package = ADB_USB_RULES_PACKAGES.get(manager.executable)
    if package is not None:
        plan = dependency_plan(
            "adb-usb-rules", packages=ADB_USB_RULES_PACKAGES, finder=lambda name: None if name == "adb-usb-rules" else locate(name)
        )
        if plan.install_command:
            return f"install the udev rules with: {shlex.join(plan.install_command)}; then unplug and replug the phone"
        return f"install the {package} package; then unplug and replug the phone"
    if manager.executable in USB_RULES_BUNDLED_WITH_ADB:
        return (
            "the udev rules ship with the adb package on this system; reinstall it, then unplug "
            "and replug the phone (if it persists, add a udev rule for the phone's vendor id)"
        )
    return "install your distribution's ADB udev rules, then unplug and replug the phone"


def wayland_clipboard_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    return dependency_plan(
        "wl-copy",
        packages=WL_CLIPBOARD_PACKAGES,
        additional_executables=("wl-paste",),
        finder=finder,
    )


def x11_clipboard_dependency_plan(
    finder: Callable[[str], str | None] | None = None,
) -> DependencyPlan:
    return dependency_plan("xclip", packages=XCLIP_PACKAGES, finder=finder)


def host_dependency_plans(
    finder: Callable[[str], str | None] | None = None,
) -> tuple[DependencyPlan, ...]:
    return (
        adb_dependency_plan(finder),
        ssh_dependency_plan(finder),
        scrcpy_dependency_plan(finder),
        localsend_dependency_plan(finder),
        wayland_clipboard_dependency_plan(finder),
        x11_clipboard_dependency_plan(finder),
        v4l2loopback_dependency_plan(finder),
        v4l2_ctl_dependency_plan(finder),
        notify_send_dependency_plan(finder),
        systemd_dependency_plan(finder),
    )


# --- the dependency catalogue (Install & Onboarding v0.1, Slice 0) -----------------------
#
# One place that says what each external thing is *for*, so `doctor`, the future
# `linkplane setup`, capability reporting, and the documentation agree. Plans above say
# how to detect and install; the catalogue says whether missing it may block onboarding.

CORE_REQUIRED = "core-required"          # without it Linkplane does not run at all
ANDROID_BASE = "android-base"            # without it no phone can be reached over USB
CAPABILITY_OPTIONAL = "capability-optional"  # one capability becomes unavailable
PROVIDER_OPTIONAL = "provider-optional"  # an alternative provider/fallback is unavailable
DEVELOPER_ONLY = "developer-only"        # tests and packaging, never a user's problem
PURPOSES = (CORE_REQUIRED, ANDROID_BASE, CAPABILITY_OPTIONAL, PROVIDER_OPTIONAL, DEVELOPER_ONLY)
# Purposes whose absence blocks the basic USB/ADB onboarding path.
BLOCKING_PURPOSES = (CORE_REQUIRED, ANDROID_BASE)

HOST = "host"
PHONE = "phone"


@dataclass(frozen=True)
class Dependency:
    name: str
    purpose: str
    where: str
    required_for: tuple[str, ...]
    summary: str
    plan_factory: Callable[[Callable[[str], str | None] | None], DependencyPlan] | None = None

    @property
    def blocking(self) -> bool:
        return self.purpose in BLOCKING_PURPOSES

    def plan(self, finder: Callable[[str], str | None] | None = None) -> DependencyPlan | None:
        """The install/detect plan, or None when nothing on this host can detect it."""
        return self.plan_factory(finder) if self.plan_factory is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "where": self.where,
            "required_for": list(self.required_for),
            "summary": self.summary,
            "blocking": self.blocking,
        }


DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency(
        "python", CORE_REQUIRED, HOST, ("core",),
        "Python 3.11 or newer; the only runtime Linkplane needs (standard library only)",
    ),
    Dependency(
        "adb", ANDROID_BASE, HOST,
        ("device.ping", "device.status", "battery.read", "storage.read", "files.send",
         "backup.photos", "notify.post", "device.find", "daemon.observe"),
        "Android Debug Bridge: the primary provider; every first-use action goes through it",
        adb_dependency_plan,
    ),
    Dependency(
        "adb-usb-rules", ANDROID_BASE, HOST, ("usb",),
        "udev rules that let your user open the phone over USB; missing rules surface as "
        "adb's `no permissions` state (LP-AUTH-003)",
    ),
    Dependency(
        "systemctl", CAPABILITY_OPTIONAL, HOST, ("daemon.install",),
        "systemd user services, for `linkplane daemon install`; one-shot commands and "
        "`linkplane daemon run` work without it",
        systemd_dependency_plan,
    ),
    Dependency(
        "ssh", PROVIDER_OPTIONAL, HOST, ("provider.ssh",),
        "OpenSSH client, for the Termux/SSH provider",
        ssh_dependency_plan,
    ),
    Dependency(
        "scrcpy", CAPABILITY_OPTIONAL, HOST,
        ("screen.control", "camera.preview", "audio.forward", "webcam"),
        "screen mirroring, camera preview, audio forwarding and the webcam feature",
        scrcpy_dependency_plan,
    ),
    Dependency(
        "v4l2loopback", CAPABILITY_OPTIONAL, HOST, ("webcam",),
        "kernel module that provides the virtual webcam device",
        v4l2loopback_dependency_plan,
    ),
    Dependency(
        "v4l2-ctl", CAPABILITY_OPTIONAL, HOST, ("webcam",),
        "v4l-utils, used to inspect the virtual webcam device",
        v4l2_ctl_dependency_plan,
    ),
    Dependency(
        "localsend-cli", PROVIDER_OPTIONAL, HOST, ("files.send",),
        "fallback for `send` when ADB is unavailable; no package on any supported manager",
        localsend_dependency_plan,
    ),
    Dependency(
        "wl-copy", CAPABILITY_OPTIONAL, HOST, ("clipboard.sync",),
        "desktop clipboard on Wayland (wl-clipboard)",
        wayland_clipboard_dependency_plan,
    ),
    Dependency(
        "xclip", CAPABILITY_OPTIONAL, HOST, ("clipboard.sync",),
        "desktop clipboard on X11",
        x11_clipboard_dependency_plan,
    ),
    Dependency(
        "notify-send", CAPABILITY_OPTIONAL, HOST, ("notify-desktop",),
        "desktop notifications from rules and `watch --notify` (libnotify)",
        notify_send_dependency_plan,
    ),
    Dependency(
        "termux", PROVIDER_OPTIONAL, PHONE, ("provider.ssh",),
        "Termux with the openssh package and legacy/termux/phone-status-json.sh, for the "
        "SSH provider; never needed on the USB/ADB path",
    ),
    Dependency(
        "termux-api", CAPABILITY_OPTIONAL, PHONE,
        ("clipboard.read", "clipboard.write", "clipboard.sync", "camera.capture"),
        "the Termux:API app and package on the phone; these capabilities run through the "
        "SSH provider, so a working USB/ADB setup does not enable them",
    ),
    Dependency(
        "make", DEVELOPER_ONLY, HOST, ("tests",),
        "runs the unit, integration and device test tiers",
    ),
)


def dependency_catalogue() -> tuple[Dependency, ...]:
    return DEPENDENCIES


def dependencies_for(capability: str) -> tuple[Dependency, ...]:
    """Every catalogue entry that names `capability` in `required_for`."""
    return tuple(item for item in DEPENDENCIES if capability in item.required_for)


@dataclass(frozen=True)
class DependencyStatus:
    dependency: Dependency
    plan: DependencyPlan | None
    # True/False when detectable on this host; None for phone-side entries and for
    # things only visible at runtime (the udev rules).
    available: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.dependency.to_dict(),
            "available": self.available,
            "plan": self.plan.to_dict() if self.plan is not None else None,
        }


@dataclass(frozen=True)
class DependencyReport:
    statuses: tuple[DependencyStatus, ...]

    @property
    def required_missing(self) -> tuple[str, ...]:
        """Blocking host dependencies that are detectably absent."""
        return tuple(
            status.dependency.name
            for status in self.statuses
            if status.dependency.blocking and status.available is False
        )

    @property
    def optional_missing(self) -> tuple[str, ...]:
        return tuple(
            status.dependency.name
            for status in self.statuses
            if not status.dependency.blocking and status.available is False
        )

    @property
    def base_ready(self) -> bool:
        """The USB/ADB onboarding path can proceed: nothing blocking is missing.

        Optional dependencies never affect this -- a missing scrcpy, notify-send, or
        Termux:API makes a capability unavailable, not the setup impossible.
        """
        return not self.required_missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ready": self.base_ready,
            "required_missing": list(self.required_missing),
            "optional_missing": list(self.optional_missing),
            "dependencies": [status.to_dict() for status in self.statuses],
        }


def dependency_report(
    finder: Callable[[str], str | None] | None = None,
    *,
    python_ok: bool | None = None,
) -> DependencyReport:
    """Detect every catalogue entry with `finder` (default `shutil.which`); never installs."""
    statuses: list[DependencyStatus] = []
    for dependency in DEPENDENCIES:
        if dependency.name == "python":
            available = (sys.version_info >= (3, 11)) if python_ok is None else python_ok
            statuses.append(DependencyStatus(dependency, None, available))
            continue
        plan = dependency.plan(finder)
        statuses.append(
            DependencyStatus(dependency, plan, plan.available if plan is not None else None)
        )
    return DependencyReport(tuple(statuses))
