from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from linkplane.dependencies import (
    adb_dependency_plan,
    scrcpy_compatibility,
    scrcpy_dependency_plan,
)
from linkplane.models import Capability, Device, DiscoveryIssue, DiscoveryResult, Endpoint
from linkplane.operations import OperationResult
from linkplane.transports import (
    AdbTransport,
    BridgeError,
    SshTransport,
    parse_adb_devices,
    run_command,
)


CommandRunner = Callable[..., str]


@dataclass(frozen=True)
class DiscoveryRequest:
    ssh: SshTransport
    ssh_device_id: str | None = None
    additional_ssh: tuple[tuple[SshTransport, str], ...] = ()
    adb_identities: tuple[tuple[str, str], ...] = ()
    allowed_adb_serials: frozenset[str] | None = None
    issues: tuple[DiscoveryIssue, ...] = ()


def discover_adb(
    runner: CommandRunner | None = None,
    identities: dict[str, str] | None = None,
    allowed_serials: frozenset[str] | None = None,
) -> list[Endpoint]:
    runner = runner or run_command  # resolved at call time (doctor/setup seam)
    if not adb_dependency_plan().available:
        return []
    raw_devices = parse_adb_devices(runner(["adb", "devices", "-l"]))
    scrcpy_dependency = scrcpy_dependency_plan()
    scrcpy_support = scrcpy_compatibility(scrcpy_dependency, runner=runner)
    endpoints: list[Endpoint] = []
    for raw in raw_devices:
        serial = raw["serial"]
        if allowed_serials is not None and serial not in allowed_serials:
            continue
        state = raw["state"]
        details = {
            "model": raw.get("model", "").replace("_", " ") or None,
            "product": raw.get("product"),
            "connection": "usb" if raw.get("usb") else "network",
        }
        capabilities: list[Capability] = []
        endpoint_error = None
        if state == "device":
            try:
                status = AdbTransport(serial, runner).status()
                details.update(status["device"])
                telemetry_issues = status.get("issues", ())
                if telemetry_issues:
                    components = ", ".join(
                        str(issue["component"]) for issue in telemetry_issues
                    )
                    capabilities.append(
                        Capability(
                            "status",
                            "unverified",
                            f"partial ADB telemetry; unavailable: {components}",
                        )
                    )
                else:
                    capabilities.append(
                        Capability("status", "ready", "ADB telemetry available")
                    )
            except BridgeError as probe_error:
                capabilities.append(Capability("status", "unavailable", str(probe_error)))
            try:
                runner(["adb", "-s", serial, "shell", "test", "-w", "/sdcard/Download"])
                capabilities.append(Capability("send", "ready", "/sdcard/Download is writable"))
            except BridgeError as probe_error:
                capabilities.append(Capability("send", "unavailable", str(probe_error)))
            try:
                runner(
                    [
                        "adb",
                        "-s",
                        serial,
                        "shell",
                        "test -r /sdcard/DCIM && command -v find stat sha256sum >/dev/null",
                    ]
                )
                capabilities.append(
                    Capability("backup", "ready", "/sdcard/DCIM is readable")
                )
            except BridgeError as probe_error:
                capabilities.append(Capability("backup", "unavailable", str(probe_error)))
            try:
                notification_help = runner(
                    [
                        "adb",
                        "-s",
                        serial,
                        "shell",
                        "cmd",
                        "notification",
                        "post",
                        "--help",
                    ]
                )
                if "notification post" in notification_help:
                    capabilities.append(Capability("notify", "ready", "Android shell API"))
                else:
                    capabilities.append(
                        Capability("notify", "unavailable", "notification post is unsupported")
                    )
            except BridgeError as probe_error:
                capabilities.append(Capability("notify", "unavailable", str(probe_error)))
            capabilities.append(
                Capability(
                    "screen",
                    (
                        "ready"
                        if scrcpy_support.screen_supported
                        else "unverified"
                        if scrcpy_support.probe_error and scrcpy_dependency.available
                        else "needs_dependency"
                    ),
                    (
                        "scrcpy screen options available"
                        if scrcpy_support.screen_supported
                        else scrcpy_support.probe_error
                        or "scrcpy must be updated for required screen options"
                    ),
                )
            )
            capabilities.append(
                Capability(
                    "camera",
                    (
                        "ready"
                        if scrcpy_support.camera_supported
                        else "unverified"
                        if scrcpy_support.probe_error and scrcpy_dependency.available
                        else "needs_dependency"
                    ),
                    (
                        "scrcpy camera source"
                        if scrcpy_support.camera_supported
                        else scrcpy_support.probe_error
                        or "scrcpy must be updated for camera support"
                    ),
                )
            )
        else:
            endpoint_error = f"ADB device is {state}"
        endpoints.append(
            Endpoint(
                transport="adb",
                address=serial,
                state="connected" if state == "device" else state,
                device_id=(identities or {}).get(serial, serial),
                capabilities=tuple(capabilities),
                details=details,
                error=endpoint_error,
            )
        )
    return endpoints


SSH_PROBE_SCRIPT = """printf 'manufacturer=%s\\n' "$(getprop ro.product.manufacturer)"
printf 'model=%s\\n' "$(getprop ro.product.model)"
printf 'android=%s\\n' "$(getprop ro.build.version.release)"
if [ -x "$HOME/phone-status-json.sh" ]; then printf 'command=phone-status-json.sh\\n'; fi
for name in termux-notification termux-clipboard-get termux-clipboard-set termux-camera-photo; do
  if command -v "$name" >/dev/null 2>&1; then printf 'command=%s\\n' "$name"; fi
done"""


def probe_ssh_endpoint(
    transport: SshTransport, *, timeout: int = 8
) -> tuple[dict[str, str], set[str]]:
    """Run the one-shot SSH probe and return (device details, available remote commands).

    Shared by discovery and the SSH provider so both see the same phone-side facts.
    Raises `BridgeError` when the phone cannot be reached.
    """
    output = transport.run(transport.command(SSH_PROBE_SCRIPT), timeout=timeout)
    details: dict[str, str] = {}
    commands: set[str] = set()
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key == "command":
            commands.add(value)
        else:
            details[key] = value
    return details, commands


def discover_ssh(transport: SshTransport, device_id: str | None = None) -> Endpoint | None:
    if not transport.host or not transport.user:
        return None
    address = f"{transport.user}@{transport.host}:{transport.port}"
    logical_id = device_id or f"ssh:{address}"
    try:
        details, commands = probe_ssh_endpoint(transport)
    except BridgeError as error:
        return Endpoint(
            transport="ssh",
            address=address,
            state="disconnected",
            device_id=logical_id,
            error=str(error),
        )

    capabilities: list[Capability] = []
    if "phone-status-json.sh" in commands:
        capabilities.append(Capability("status", "ready", "Termux status script"))
    if "termux-notification" in commands:
        capabilities.append(Capability("notify", "ready", "Termux:API"))
    if {"termux-clipboard-get", "termux-clipboard-set"}.issubset(commands):
        capabilities.append(Capability("clipboard", "ready", "Termux:API"))
    if "termux-camera-photo" in commands:
        capabilities.append(Capability("camera", "ready", "Termux:API"))
    details["commands"] = ",".join(sorted(commands))
    return Endpoint(
        transport="ssh",
        address=address,
        state="connected",
        device_id=logical_id,
        capabilities=tuple(sorted(capabilities, key=lambda capability: capability.name)),
        details=details,
    )


def group_devices(endpoints: list[Endpoint]) -> list[Device]:
    grouped: dict[str, list[Endpoint]] = {}
    for endpoint in endpoints:
        grouped.setdefault(endpoint.device_id, []).append(endpoint)

    devices: list[Device] = []
    for device_id, device_endpoints in grouped.items():
        details = next(
            (endpoint.details for endpoint in device_endpoints if endpoint.details.get("model")),
            {},
        )
        manufacturer = details.get("manufacturer")
        model = details.get("model")
        name = " ".join(value for value in (manufacturer, model) if value) or device_id
        capability_states: dict[str, Capability] = {}
        rank = {"ready": 3, "needs_dependency": 2, "unverified": 1, "unavailable": 0}
        for endpoint in device_endpoints:
            for capability in endpoint.capabilities:
                current = capability_states.get(capability.name)
                if current is None or rank[capability.status] > rank[current.status]:
                    capability_states[capability.name] = capability
        capabilities = tuple(capability_states[name] for name in sorted(capability_states))
        devices.append(
            Device(
                id=device_id,
                name=name,
                manufacturer=manufacturer,
                model=model,
                android=details.get("android"),
                capabilities=capabilities,
                endpoints=tuple(device_endpoints),
            )
        )
    return sorted(devices, key=lambda device: device.name.lower())


def discover_devices(
    ssh: SshTransport,
    ssh_device_id: str | None = None,
    *,
    additional_ssh: tuple[tuple[SshTransport, str], ...] = (),
    adb_identities: dict[str, str] | None = None,
    allowed_adb_serials: frozenset[str] | None = None,
) -> DiscoveryResult:
    result = scan_devices(
        DiscoveryRequest(
            ssh=ssh,
            ssh_device_id=ssh_device_id,
            additional_ssh=additional_ssh,
            adb_identities=tuple((adb_identities or {}).items()),
            allowed_adb_serials=allowed_adb_serials,
        )
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful discovery did not return a value")
    return result.value


def scan_devices(request: DiscoveryRequest) -> OperationResult[DiscoveryResult]:
    issues = list(request.issues)
    try:
        endpoints = discover_adb(
            identities=dict(request.adb_identities),
            allowed_serials=request.allowed_adb_serials,
        )
    except BridgeError as error:
        endpoints = []
        issues.append(DiscoveryIssue("adb", str(error)))
    except (KeyError, TypeError, ValueError) as error:
        return OperationResult.failure("operation_failed", str(error))
    try:
        known_ssh_addresses = {
            endpoint.address for endpoint in endpoints if endpoint.transport == "ssh"
        }
        for additional_transport, device_id in request.additional_ssh:
            endpoint = discover_ssh(additional_transport, device_id)
            if endpoint and endpoint.address not in known_ssh_addresses:
                endpoints.append(endpoint)
                known_ssh_addresses.add(endpoint.address)
        ssh_endpoint = discover_ssh(request.ssh, request.ssh_device_id)
        if ssh_endpoint and ssh_endpoint.address not in known_ssh_addresses:
            endpoints.append(ssh_endpoint)
        devices = tuple(group_devices(endpoints))
    except (BridgeError, KeyError, TypeError, ValueError) as error:
        return OperationResult.failure("operation_failed", str(error))
    return OperationResult.success(DiscoveryResult(devices, tuple(issues)))
