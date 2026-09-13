from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from linkplane.clipboard import desktop_clipboard, desktop_clipboard_dependency_plans
from linkplane.core import errors
from linkplane.core.capability import PROVIDER_ERROR, SUPPORTED, UNAVAILABLE
from linkplane.dependencies import (
    DependencyPlan,
    adb_dependency_plan,
    dependency_install_hint,
    dependency_plan,
    localsend_dependency_plan,
    notify_send_dependency_plan,
    scrcpy_compatibility,
    scrcpy_dependency_plan,
    ssh_dependency_plan,
    usb_rules_install_hint,
)
from linkplane.transports import ADB_STATE_NO_PERMISSIONS, ADB_STATE_OFFLINE, ADB_STATE_UNAUTHORIZED
from linkplane.models import Check, DiscoveryResult
from linkplane.operations import OperationResult
from linkplane.profiles import profiles_from_config
from linkplane.providers.base import Provider
from linkplane.transports import BridgeError, load_config, resolve_config_path, run_command


@dataclass(frozen=True)
class DoctorRequest:
    discovery: DiscoveryResult
    # Core v0.1 checks (configuration, default device, provider, reachability,
    # authentication, remote scripts, provider response, capability query) run only when a
    # provider resolver is supplied; the CLI always supplies one. Without it, diagnostics
    # cover the host environment only, as before.
    config_path: str | None = None
    provider_resolver: Callable[[], Provider] | None = None


@dataclass(frozen=True)
class DoctorResult:
    checks: tuple[Check, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"checks": [check.to_dict() for check in self.checks]}


def executable_version(
    command: str,
    arguments: list[str],
    *,
    plan: DependencyPlan | None = None,
) -> tuple[str | None, str | None]:
    dependency = plan or dependency_plan(command)
    if not dependency.available:
        return None, None
    try:
        output = run_command(
            [dependency.executable_path or command, *arguments], timeout=5
        )
    except BridgeError as error:
        return None, str(error)
    version = next((line.strip() for line in output.splitlines() if line.strip()), "installed")
    return version, None


def _hints_fix(error: errors.LinkplaneError) -> str | None:
    return "; ".join(error.hints) if error.hints else None


def configuration_checks(config_path: str | None) -> tuple[list[Check], dict[str, Any] | None]:
    """Configuration file and default-device checks. Returns the loaded config (or None)."""
    checks: list[Check] = []
    path = resolve_config_path(config_path)
    if not path.exists():
        checks.append(
            Check(
                "Configuration",
                "warning",
                f"no configuration file at {path}",
                "Run `linkplane pair usb`, `pair wireless`, or `pair ssh` to create one",
                errors.CONFIG_MISSING,
            )
        )
        return checks, {}
    try:
        config = load_config(config_path)
        profiles = profiles_from_config(config)
    except BridgeError as error:
        checks.append(
            Check(
                "Configuration",
                "error",
                str(error),
                "Fix or remove the invalid configuration file",
                errors.CONFIG_INVALID,
            )
        )
        return checks, None
    checks.append(Check("Configuration", "ok", str(path)))
    permissions = configuration_permission_check(path)
    if permissions is not None:
        checks.append(permissions)

    default = config.get("default_device")
    legacy_ssh = config.get("ssh")
    if default is None:
        if profiles:
            names = ", ".join(sorted(profiles))
            checks.append(
                Check(
                    "Default device",
                    "warning",
                    f"no default device set; profiles: {names}",
                    "Run `linkplane profiles default <name>`",
                    errors.CONFIG_MISSING,
                )
            )
        elif isinstance(legacy_ssh, dict) and legacy_ssh.get("host"):
            checks.append(
                Check("Default device", "ok", "legacy ssh configuration (no named profiles)")
            )
        else:
            checks.append(
                Check(
                    "Default device",
                    "warning",
                    "no device configured",
                    "Run `linkplane pair usb`, `pair wireless`, or `pair ssh`",
                    errors.CONFIG_MISSING,
                )
            )
    elif not isinstance(default, str) or default not in profiles:
        checks.append(
            Check(
                "Default device",
                "error",
                f"default device profile not found: {default}",
                "Run `linkplane profiles list` and set a valid default",
                errors.DEVICE_NOT_FOUND,
            )
        )
    else:
        checks.append(Check("Default device", "ok", default))
    return checks, config


def configuration_permission_check(path: Path) -> Check | None:
    """A warning when the configuration is readable by other users (pairing writes 0600).

    Only the file's own mode is judged; a file written by hand or by an older version can
    be looser than what `linkplane pair` produces, and setup should say so rather than
    silently tighten it.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return Check(
            "Configuration permissions",
            "warning",
            f"{path} is mode {mode:04o}; other users on this computer can read it",
            f"Run `chmod 600 {path}`",
        )
    return None


def usb_access_checks(discovery: DiscoveryResult) -> list[Check]:
    """One check per ADB endpoint that is present but blocked, with the right remedy.

    `no permissions` is the Linux host (udev), `unauthorized` is the phone waiting for a
    tap, `offline` is a stale connection; they were one undifferentiated "no connected
    endpoint" before Slice 0.
    """
    checks: list[Check] = []
    for device in discovery.devices:
        for endpoint in device.endpoints:
            if endpoint.transport != "adb":
                continue
            if endpoint.state == ADB_STATE_NO_PERMISSIONS:
                checks.append(
                    Check(
                        "USB access",
                        "error",
                        f"{endpoint.address}: Linux denied ADB access to the USB device",
                        usb_rules_install_hint(),
                        errors.AUTH_USB_PERMISSION,
                    )
                )
            elif endpoint.state == ADB_STATE_UNAUTHORIZED:
                checks.append(
                    Check(
                        "USB access",
                        "warning",
                        f"{endpoint.address}: the phone has not authorized this computer",
                        "Tap Allow on the phone (tick 'Always allow from this computer')",
                        errors.AUTH_UNAUTHORIZED_DEVICE,
                    )
                )
            elif endpoint.state == ADB_STATE_OFFLINE:
                checks.append(
                    Check(
                        "USB access",
                        "warning",
                        f"{endpoint.address}: ADB reports the device offline",
                        "Unplug the cable and plug it back in",
                        errors.CONNECT_UNREACHABLE,
                    )
                )
    return checks


def provider_checks(resolver: Callable[[], Provider]) -> list[Check]:
    """Provider selection, reachability, authentication, remote scripts, response, capabilities."""
    checks: list[Check] = []
    try:
        provider = resolver()
    except errors.LinkplaneError as error:
        checks.append(Check("Provider", "error", str(error), _hints_fix(error), error.code))
        return checks
    except BridgeError as error:
        classified = errors.classify(error)
        checks.append(
            Check("Provider", "error", str(error), _hints_fix(classified), classified.code)
        )
        return checks
    checks.append(Check("Provider", "ok", f"{provider.name} ({provider.address})"))

    if provider.name == "ssh":
        identity = getattr(getattr(provider, "transport", None), "identity_file", None)
        if identity is None:
            checks.append(
                Check(
                    "SSH identity",
                    "warning",
                    "no identity file configured; ssh will use its defaults or an agent",
                    "Set ssh.identity_file in the configuration or re-run `linkplane pair ssh`",
                )
            )
        elif not os.path.exists(os.path.expanduser(identity)):
            checks.append(
                Check(
                    "SSH identity",
                    "error",
                    f"identity file not found: {identity}",
                    "Restore the key or re-run `linkplane pair ssh`",
                    errors.AUTH_FAILED,
                )
            )
        else:
            checks.append(Check("SSH identity", "ok", identity))

    ping = provider.ping()
    if not ping.reachable:
        classified = errors.classify(BridgeError(ping.detail or "unreachable"), provider=provider.name)
        checks.append(
            Check(
                "Phone reachable",
                "error",
                ping.detail or "no response",
                _hints_fix(classified),
                classified.code,
            )
        )
        checks.append(
            Check(
                "Provider responds",
                "error",
                "not attempted: phone unreachable",
                None,
                classified.code,
            )
        )
        return checks
    checks.append(Check("Phone reachable", "ok", f"{ping.latency_ms:g} ms via {ping.provider}"))
    if provider.name == "ssh":
        # A completed round trip over SSH means the key was accepted.
        checks.append(Check("Authentication", "ok", "ssh login accepted"))

    try:
        status = provider.status()
    except errors.LinkplaneError as error:
        checks.append(Check("Provider responds", "error", str(error), _hints_fix(error), error.code))
        status = None
    if status is not None:
        sections = [
            name for name in ("battery", "memory", "storage")
            if getattr(status, name) is not None
        ]
        if status.issues:
            components = ", ".join(f"{issue.component} ({issue.error})" for issue in status.issues)
            checks.append(
                Check(
                    "Provider responds",
                    "warning",
                    f"partial telemetry; failed: {components}",
                    "Run `linkplane status` for details",
                    errors.PROVIDER_PARTIAL,
                )
            )
        else:
            model = status.device.get("model") or "device"
            checks.append(
                Check("Provider responds", "ok", f"{model}: {', '.join(sections) or 'no telemetry'}")
            )

    try:
        reports = provider.capabilities()
    except errors.LinkplaneError as error:
        checks.append(Check("Capability query", "error", str(error), _hints_fix(error), error.code))
        return checks
    by_name = {report.name: report for report in reports}
    if provider.name == "ssh":
        remote = by_name.get("device.status")
        if remote is not None and remote.status == UNAVAILABLE:
            checks.append(
                Check(
                    "Remote scripts",
                    "error",
                    remote.detail or "phone-status-json.sh is not installed on the phone",
                    "Copy legacy/termux/phone-status-json.sh to $HOME on the phone and make it executable",
                    errors.CAPABILITY_UNAVAILABLE,
                )
            )
        elif remote is not None and remote.status == SUPPORTED:
            checks.append(Check("Remote scripts", "ok", "phone-status-json.sh"))
    supported = sum(1 for report in reports if report.status == SUPPORTED)
    undetermined = [report.name for report in reports if report.status == PROVIDER_ERROR]
    if undetermined:
        checks.append(
            Check(
                "Capability query",
                "warning",
                f"{supported} of {len(reports)} supported; undetermined: {', '.join(undetermined)}",
                "Run `linkplane capabilities` for details",
                errors.CAPABILITY_UNAVAILABLE,
            )
        )
    else:
        checks.append(
            Check("Capability query", "ok", f"{supported} of {len(reports)} capabilities supported")
        )
    return checks


def build_checks(discovery: DiscoveryResult) -> list[Check]:
    devices = discovery.devices
    checks = [Check("Python", "ok", sys.version.split()[0])]

    adb_dependency = adb_dependency_plan()
    adb_version, adb_probe_error = executable_version(
        "adb", ["version"], plan=adb_dependency
    )
    adb_discovery_error = next(
        (issue.error for issue in discovery.issues if issue.backend == "adb"), None
    )
    adb_error = adb_probe_error or adb_discovery_error
    adb_ready = bool(adb_version and not adb_error)
    adb_fix = None
    if not adb_ready:
        adb_fix = (
            dependency_install_hint(adb_dependency)
            if not adb_dependency.available
            else "Restart ADB and check device authorization and USB permissions"
        )
    checks.append(
        Check(
            "ADB",
            "ok" if adb_ready else "error",
            adb_error or adb_version or "not installed",
            adb_fix,
        )
    )

    ssh_dependency = ssh_dependency_plan()
    ssh_version, ssh_probe_error = executable_version(
        "ssh", ["-V"], plan=ssh_dependency
    )
    ssh_fix = None
    if not ssh_version:
        ssh_fix = (
            dependency_install_hint(ssh_dependency)
            if not ssh_dependency.available
            else "Repair or update the SSH client"
        )
    checks.append(
        Check(
            "SSH client",
            "ok" if ssh_version else "warning",
            ssh_probe_error or ssh_version or "not installed",
            ssh_fix,
        )
    )

    ssh_discovery_error = next(
        (issue.error for issue in discovery.issues if issue.backend == "ssh"), None
    )
    if ssh_discovery_error:
        checks.append(
            Check(
                "SSH configuration",
                "warning",
                ssh_discovery_error,
                "Correct or remove the invalid SSH configuration",
            )
        )

    connected = [
        endpoint
        for device in devices
        for endpoint in device.endpoints
        if endpoint.state == "connected"
    ]
    connected_summary = f"{len(connected)} connected endpoint"
    if len(connected) != 1:
        connected_summary += "s"
    checks.append(
        Check(
            "Device",
            "ok" if connected else "error",
            connected_summary if connected else "no connected endpoint",
            None if connected else "Connect the phone with ADB or start Termux sshd",
        )
    )
    checks.extend(usb_access_checks(discovery))

    scrcpy_dependency = scrcpy_dependency_plan()
    scrcpy_version, scrcpy_error = executable_version(
        "scrcpy", ["--version"], plan=scrcpy_dependency
    )
    scrcpy_support = scrcpy_compatibility(scrcpy_dependency)
    scrcpy_ready = bool(scrcpy_version and scrcpy_support.screen_supported)
    if scrcpy_support.missing_screen_options:
        scrcpy_summary = (
            "missing required options: "
            f"{', '.join(scrcpy_support.missing_screen_options)}"
        )
    else:
        scrcpy_summary = (
            scrcpy_error
            or scrcpy_support.probe_error
            or scrcpy_version
            or "scrcpy is not installed"
        )
    scrcpy_fix = None
    if not scrcpy_ready:
        scrcpy_fix = (
            dependency_install_hint(scrcpy_dependency)
            if not scrcpy_dependency.available
            else "Update or repair scrcpy"
        )
    checks.append(
        Check(
            "Screen",
            "ok" if scrcpy_ready else "warning",
            scrcpy_summary,
            scrcpy_fix,
        )
    )

    localsend_dependency = localsend_dependency_plan()
    localsend_version, localsend_error = executable_version(
        "localsend-cli", ["--version"], plan=localsend_dependency
    )
    localsend_fix = None
    if not localsend_version:
        localsend_fix = (
            dependency_install_hint(localsend_dependency)
            if not localsend_dependency.available
            else "Repair or update LocalSend CLI"
        )
    checks.append(
        Check(
            "LocalSend",
            "ok" if localsend_version else "warning",
            localsend_error or localsend_version or "localsend-cli is not installed",
            localsend_fix,
        )
    )

    clipboard = desktop_clipboard()
    clipboard_dependencies = desktop_clipboard_dependency_plans()
    clipboard_install = next(
        (
            dependency
            for dependency in clipboard_dependencies
            if dependency.install_command is not None
        ),
        clipboard_dependencies[0],
    )
    checks.append(
        Check(
            "Desktop clipboard",
            "ok" if clipboard else "warning",
            clipboard.name if clipboard else "no supported clipboard backend",
            None if clipboard else dependency_install_hint(clipboard_install),
        )
    )

    notify_dependency = notify_send_dependency_plan()
    checks.append(
        Check(
            "Desktop notifications",
            "ok" if notify_dependency.available else "warning",
            notify_dependency.executable_path or "notify-send is not installed (libnotify)",
            None if notify_dependency.available else dependency_install_hint(notify_dependency),
        )
    )

    ssh_endpoints = [
        endpoint
        for device in devices
        for endpoint in device.endpoints
        if endpoint.transport == "ssh"
    ]
    if ssh_endpoints:
        ssh_endpoint = ssh_endpoints[0]
        ssh_ready = ssh_endpoint.state == "connected"
        ssh_code = None
        if not ssh_ready:
            ssh_code = errors.classify(
                BridgeError(ssh_endpoint.error or "unreachable"), provider="ssh"
            ).code
        checks.append(
            Check(
                "Termux SSH",
                "ok" if ssh_ready else "warning",
                ssh_endpoint.address if ssh_ready else ssh_endpoint.error or "unreachable",
                None if ssh_ready else "Open Termux and run: sshd",
                ssh_code,
            )
        )

    capabilities = [capability for device in devices for capability in device.capabilities]
    ready = sorted({item.name for item in capabilities if item.status == "ready"})
    setup = sorted({item.name for item in capabilities if item.status == "needs_dependency"})
    unavailable = sorted({item.name for item in capabilities if item.status == "unavailable"})
    summary_parts: list[str] = []
    if ready:
        summary_parts.append(f"ready: {', '.join(ready)}")
    if setup:
        summary_parts.append(f"setup needed: {', '.join(setup)}")
    if unavailable:
        summary_parts.append(f"unavailable: {', '.join(unavailable)}")
    capability_status = "ok"
    if not ready:
        capability_status = "error"
    elif setup or unavailable:
        capability_status = "warning"
    checks.append(
        Check(
            "Capabilities",
            capability_status,
            "; ".join(summary_parts) if summary_parts else "none available",
        )
    )
    return checks


def run_diagnostics(request: DoctorRequest) -> OperationResult[DoctorResult]:
    try:
        checks: list[Check] = []
        if request.provider_resolver is not None:
            configuration, _config = configuration_checks(request.config_path)
            checks.extend(configuration)
            checks.extend(provider_checks(request.provider_resolver))
        checks.extend(build_checks(request.discovery))
    except (BridgeError, OSError, KeyError, TypeError, ValueError) as error:
        return OperationResult.failure("operation_failed", str(error))
    return OperationResult.success(DoctorResult(tuple(checks)))
