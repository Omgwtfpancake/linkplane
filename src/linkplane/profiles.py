from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import shlex
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from linkplane.core import errors
from linkplane.dependencies import adb_dependency_plan
from linkplane.operations import (
    JSON_SCHEMA_VERSION,
    CancellationToken,
    OperationCancelled,
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    cancel_on_interrupt,
    check_cancelled,
    is_cancelled,
    report_progress,
)
from linkplane.transports import (
    AdbTransport,
    BridgeError,
    SshTransport,
    load_config,
    parse_adb_devices,
    parse_wifi_route,
    resolve_config_path,
    run_command,
)


PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ADB_NETWORK_SERIAL = re.compile(r"^(?P<host>[^:]+):(?P<port>\d{1,5})$")


class _ProfileNotFound(BridgeError):
    pass


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    device_id: str
    adb_serials: tuple[str, ...]
    preferred_adb_serial: str | None
    ssh: dict[str, Any] | None

    @property
    def adb_serial(self) -> str | None:
        return self.preferred_adb_serial or next(iter(self.adb_serials), None)


@dataclass(frozen=True)
class UsbPairingRequest:
    name: str
    serial: str | None = None
    make_default: bool = False
    dry_run: bool = False
    config_path: str | None = None


@dataclass(frozen=True)
class WirelessPairingRequest:
    name: str
    pair_address: str
    connect_address: str
    make_default: bool = False
    dry_run: bool = False
    config_path: str | None = None


@dataclass(frozen=True)
class SshPairingRequest:
    name: str
    host: str
    user: str
    port: int = 8022
    identity_file: str | None = None
    serial: str | None = None
    device_id: str | None = None
    make_default: bool = False
    dry_run: bool = False
    config_path: str | None = None


@dataclass(frozen=True)
class PairingResult:
    method: str
    profile: str
    device_id: str
    device: str | None
    adb_serial: str | None
    ssh_endpoint: str | None
    config_path: str
    commands: tuple[tuple[str, ...], ...]
    pairing_output: str | None = None
    connection_output: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileListRequest:
    config_path: str | None = None


@dataclass(frozen=True)
class ProfileListResult:
    default_device: str | None
    profiles: tuple[DeviceProfile, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_device": self.default_device,
            "devices": {
                profile.name: {
                    "device_id": profile.device_id,
                    "adb_serial": profile.adb_serial,
                    "adb_serials": profile.adb_serials,
                    "ssh": dict(profile.ssh) if profile.ssh is not None else None,
                }
                for profile in self.profiles
            },
        }


@dataclass(frozen=True)
class ProfileChangeRequest:
    name: str
    config_path: str | None = None


@dataclass(frozen=True)
class ProfileChangeResult:
    action: str
    profile: str
    config_path: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class EndpointRefreshRequest:
    name: str | None = None
    config_path: str | None = None
    dry_run: bool = False
    scan: bool = False


@dataclass(frozen=True)
class ProfileEndpointChange:
    field: str
    previous: str
    current: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileRefreshOutcome:
    profile: str
    discovered_address: str | None
    changes: tuple[ProfileEndpointChange, ...]
    note: str | None = None
    scanned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "discovered_address": self.discovered_address,
            "changes": [change.to_dict() for change in self.changes],
            "note": self.note,
            "scanned": self.scanned,
        }


@dataclass(frozen=True)
class EndpointRefreshResult:
    config_path: str
    dry_run: bool
    profiles: tuple[ProfileRefreshOutcome, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "dry_run": self.dry_run,
            "profiles": [outcome.to_dict() for outcome in self.profiles],
        }


def validate_profile_name(name: str) -> None:
    if not PROFILE_NAME.fullmatch(name):
        raise BridgeError(
            "device profile names must be 1-64 letters, numbers, dots, dashes, or underscores"
        )


def profiles_from_config(config: dict[str, Any]) -> dict[str, DeviceProfile]:
    raw_profiles = config.get("devices", {})
    if not isinstance(raw_profiles, dict):
        raise BridgeError("devices configuration must be a JSON object")
    profiles: dict[str, DeviceProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not isinstance(raw_profile, dict):
            raise BridgeError("each device profile must be a named JSON object")
        validate_profile_name(name)
        device_id = raw_profile.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise BridgeError(f"device profile {name} requires a device_id")
        raw_adb = raw_profile.get("adb")
        if raw_adb is not None and not isinstance(raw_adb, dict):
            raise BridgeError(f"device profile {name}.adb must be a JSON object")
        adb_serials: list[str] = []
        preferred_adb_serial = None
        if raw_adb:
            legacy_serial = raw_adb.get("serial")
            raw_serials = raw_adb.get("serials", [])
            if legacy_serial is not None and not isinstance(legacy_serial, str):
                raise BridgeError(f"device profile {name}.adb.serial must be a string")
            if not isinstance(raw_serials, list) or not all(
                isinstance(serial, str) and serial for serial in raw_serials
            ):
                raise BridgeError(
                    f"device profile {name}.adb.serials must be a list of strings"
                )
            adb_serials = list(
                dict.fromkeys(
                    [*raw_serials, *([legacy_serial] if legacy_serial else [])]
                )
            )
            preferred_adb_serial = raw_adb.get("preferred_serial", legacy_serial)
            if preferred_adb_serial is not None and not isinstance(
                preferred_adb_serial, str
            ):
                raise BridgeError(
                    f"device profile {name}.adb.preferred_serial must be a string"
                )
            if preferred_adb_serial and preferred_adb_serial not in adb_serials:
                raise BridgeError(
                    f"device profile {name}.adb.preferred_serial is not in adb.serials"
                )
        raw_ssh = raw_profile.get("ssh")
        if raw_ssh is not None and not isinstance(raw_ssh, dict):
            raise BridgeError(f"device profile {name}.ssh must be a JSON object")
        if raw_ssh is not None:
            host = raw_ssh.get("host")
            user = raw_ssh.get("user")
            if not isinstance(host, str) or not host:
                raise BridgeError(f"device profile {name}.ssh.host must be a string")
            if not isinstance(user, str) or not user:
                raise BridgeError(f"device profile {name}.ssh.user must be a string")
            try:
                port = int(raw_ssh.get("port", 8022))
            except (TypeError, ValueError) as error:
                raise BridgeError(f"device profile {name}.ssh.port is invalid") from error
            if not 1 <= port <= 65535:
                raise BridgeError(
                    f"device profile {name}.ssh.port must be between 1 and 65535"
                )
            identity_file = raw_ssh.get("identity_file")
            if identity_file is not None and not isinstance(identity_file, str):
                raise BridgeError(
                    f"device profile {name}.ssh.identity_file must be a string"
                )
            raw_ssh = dict(raw_ssh)
            raw_ssh["port"] = port
        profiles[name] = DeviceProfile(
            name,
            device_id,
            tuple(adb_serials),
            preferred_adb_serial,
            raw_ssh,
        )
    adb_identities: dict[str, str] = {}
    ssh_identities: dict[tuple[Any, Any, Any], str] = {}
    for profile in profiles.values():
        for serial in profile.adb_serials:
            existing_id = adb_identities.setdefault(serial, profile.device_id)
            if existing_id != profile.device_id:
                raise BridgeError(
                    f"ADB endpoint {serial} belongs to multiple device identities"
                )
        if profile.ssh:
            endpoint = (
                profile.ssh.get("host"),
                profile.ssh.get("user"),
                profile.ssh.get("port", 8022),
            )
            existing_id = ssh_identities.setdefault(endpoint, profile.device_id)
            if existing_id != profile.device_id:
                raise BridgeError(
                    f"SSH endpoint {endpoint[1]}@{endpoint[0]}:{endpoint[2]} belongs to "
                    "multiple device identities"
                )
    return profiles


def selected_profile(
    config: dict[str, Any], name: str | None = None
) -> DeviceProfile | None:
    selected_name = name if name is not None else config.get("default_device")
    if selected_name is None:
        return None
    if not isinstance(selected_name, str):
        raise BridgeError("default_device must be a string")
    profiles = profiles_from_config(config)
    try:
        return profiles[selected_name]
    except KeyError as error:
        label = "device profile" if name is not None else "default device profile"
        raise BridgeError(f"{label} not found: {selected_name}") from error


def available_adb_serial(profile: DeviceProfile) -> str | None:
    preferred = profile.adb_serial
    if preferred is None or not adb_dependency_plan().available:
        return preferred
    try:
        devices = parse_adb_devices(run_command(["adb", "devices", "-l"]))
    except BridgeError:
        return preferred
    connected = {device["serial"] for device in devices if device["state"] == "device"}
    if preferred in connected:
        return preferred
    return next((serial for serial in profile.adb_serials if serial in connected), preferred)


def _device_wifi_address(
    serial: str, command_runner: Callable[..., str]
) -> str | None:
    try:
        output = command_runner(
            ["adb", "-s", serial, "shell", "ip", "route", "get", "1.1.1.1"], timeout=8
        )
    except BridgeError:
        return None
    return parse_wifi_route(output)


def hardware_serials(profile: DeviceProfile) -> tuple[str, ...]:
    """The profile's genuine (non `host:port`) ADB serials -- USB-pairing identity proof.

    A wireless `adb connect` succeeding proves only that this host's ADB key was
    authorized by *some* device before; it does not by itself prove which device. Only a
    hardware serial, learned once over USB, is a trustworthy anchor for later verifying
    an unknown wireless candidate is genuinely this profile's phone and not a different,
    previously-authorized device that happens to answer on the same port.
    """
    return tuple(
        serial for serial in profile.adb_serials if not ADB_NETWORK_SERIAL.fullmatch(serial)
    )


def stale_wireless_alias(profile: DeviceProfile) -> tuple[str, str] | None:
    """The host and port of the profile's last-known wireless ADB alias, if it has one."""
    for serial in profile.adb_serials:
        match = ADB_NETWORK_SERIAL.fullmatch(serial)
        if match:
            return match.group("host"), match.group("port")
    return None


def subnet_scan_candidates(anchor_host: str) -> list[str] | None:
    """Every other host address in `anchor_host`'s /24, or None if it isn't an IPv4 literal."""
    try:
        address = ipaddress.ip_address(anchor_host)
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    network = ipaddress.ip_network(f"{address}/24", strict=False)
    return [str(host) for host in network.hosts()]


def _probe_scan_candidate(
    candidate: str,
    expected_serials: tuple[str, ...],
    *,
    command_runner: Callable[..., str],
    timeout: float,
    cancel: CancellationToken | None = None,
) -> bool:
    if is_cancelled(cancel):
        # Queued probes short-circuit so the pool drains quickly after a cancel.
        return False
    try:
        command_runner(["adb", "connect", candidate], timeout=timeout)
        devices = parse_adb_devices(
            command_runner(["adb", "devices", "-l"], timeout=timeout)
        )
    except BridgeError:
        return False
    if not any(
        device["serial"] == candidate and device["state"] == "device"
        for device in devices
    ):
        return False
    try:
        serial = command_runner(
            ["adb", "-s", candidate, "shell", "getprop", "ro.serialno"], timeout=timeout
        ).strip()
    except BridgeError:
        serial = ""
    if serial in expected_serials:
        return True
    # Not the phone this profile is for -- don't leave an unrecognized connection (and
    # a possible pending authorization prompt on someone else's screen) behind.
    try:
        command_runner(["adb", "disconnect", candidate], timeout=timeout)
    except BridgeError:
        pass
    return False


def scan_for_profile(
    profile: DeviceProfile,
    *,
    command_runner: Callable[..., str] = run_command,
    timeout: float = 2.0,
    max_workers: int = 32,
    cancel: CancellationToken | None = None,
) -> str | None:
    """Bounded /24 sweep for a profile with no currently reachable alias.

    Anchors on the profile's last-known wireless ADB host:port and never guesses a
    different port. Scans every other host in that /24 at the same port and accepts a
    match only when the connecting device's own `ro.serialno` equals one of the
    profile's known hardware serials (see `hardware_serials`) -- a profile that has
    never been paired over USB has no trustworthy way to be verified this way and is
    skipped entirely (returns None) rather than trusting an unverified connection.
    """
    expected_serials = hardware_serials(profile)
    if not expected_serials:
        return None
    alias = stale_wireless_alias(profile)
    if alias is None:
        return None
    anchor_host, port = alias
    hosts = subnet_scan_candidates(anchor_host)
    if not hosts:
        return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _probe_scan_candidate,
                f"{host}:{port}",
                expected_serials,
                command_runner=command_runner,
                timeout=timeout,
                cancel=cancel,
            ): host
            for host in hosts
        }
        found: str | None = None
        for future in as_completed(futures):
            if is_cancelled(cancel):
                break
            if future.result():
                found = f"{futures[future]}:{port}"
                break
        for pending in futures:
            pending.cancel()
    check_cancelled(cancel)
    return found


def _apply_endpoint_changes(
    config: dict[str, Any],
    changes_by_profile: dict[str, tuple[ProfileEndpointChange, ...]],
) -> None:
    raw_profiles = config.setdefault("devices", {})
    if not isinstance(raw_profiles, dict):
        raise BridgeError("devices configuration must be a JSON object")
    for name, changes in changes_by_profile.items():
        raw_profile = raw_profiles.get(name)
        if not isinstance(raw_profile, dict):
            continue
        for change in changes:
            if change.field == "ssh_host":
                raw_ssh = dict(raw_profile.get("ssh") or {})
                raw_ssh["host"] = change.current
                raw_profile["ssh"] = raw_ssh
            elif change.field == "adb_serial":
                raw_adb = dict(raw_profile.get("adb") or {})
                serials = [
                    change.current if serial == change.previous else serial
                    for serial in raw_adb.get("serials", [])
                ]
                if change.current not in serials:
                    serials.append(change.current)
                raw_adb["serials"] = serials
                if raw_adb.get("preferred_serial", raw_adb.get("serial")) == change.previous:
                    raw_adb["preferred_serial"] = change.current
                raw_adb.pop("serial", None)
                raw_profile["adb"] = raw_adb


def refresh_profile_endpoints(
    request: EndpointRefreshRequest,
    *,
    command_runner: Callable[..., str] | None = None,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> OperationResult[EndpointRefreshResult]:
    # Resolved at call time, not as a signature default: an import-time `= run_command`
    # is invisible to `patch("linkplane.profiles.run_command")`, so the CLI-level refresh
    # test reached real ADB (same defect as find.locate_phone, 2026-09-12).
    command_runner = command_runner or run_command
    if request.name is not None:
        try:
            validate_profile_name(request.name)
        except BridgeError as error:
            return OperationResult.failure("invalid_request", str(error))
    try:
        config = load_config(request.config_path)
        profiles = profiles_from_config(config)
    except BridgeError as error:
        return _failure("operation_failed", error)
    if request.name is not None and request.name not in profiles:
        return OperationResult.failure(
            "not_found", f"device profile not found: {request.name}"
        )
    targets = (
        [profiles[request.name]]
        if request.name is not None
        else sorted(profiles.values(), key=lambda profile: profile.name)
    )
    config_path = str(resolve_config_path(request.config_path))

    report_progress(
        progress,
        ProgressEvent(
            "profile_refresh",
            "started",
            "Endpoint refresh started",
            details={
                "profiles": [profile.name for profile in targets],
                "dry_run": request.dry_run,
            },
        ),
    )

    connected: dict[str, str] = {}
    if _adb_is_available(adb_locator):
        try:
            connected = {
                device["serial"]: device["state"]
                for device in parse_adb_devices(command_runner(["adb", "devices", "-l"]))
            }
        except BridgeError:
            connected = {}

    outcomes: list[ProfileRefreshOutcome] = []
    changes_by_profile: dict[str, tuple[ProfileEndpointChange, ...]] = {}
    try:
        _refresh_targets(
            request,
            targets,
            connected,
            outcomes,
            changes_by_profile,
            command_runner=command_runner,
            progress=progress,
            cancel=cancel,
        )
    except OperationCancelled as error:
        # Nothing has been written yet: config changes are applied only after every
        # profile has been examined, so a cancelled refresh leaves the config untouched.
        report_progress(
            progress,
            ProgressEvent(
                "profile_refresh",
                "cancelled",
                f"Endpoint refresh cancelled after {len(outcomes)} of {len(targets)} profile(s)",
                current=len(outcomes),
                total=len(targets),
                unit="profiles",
                details={"outcomes": [outcome.to_dict() for outcome in outcomes]},
            ),
        )
        return OperationResult.failure("cancelled", str(error))

    if changes_by_profile and not request.dry_run:
        try:
            path = update_config(
                request.config_path,
                lambda config: _apply_endpoint_changes(config, changes_by_profile),
            )
        except BridgeError as error:
            return _failure("operation_failed", error)
        config_path = str(path)

    result = EndpointRefreshResult(config_path, request.dry_run, tuple(outcomes))
    report_progress(
        progress,
        ProgressEvent(
            "profile_refresh", "completed", "Endpoint refresh completed", details=result.to_dict()
        ),
    )
    return OperationResult.success(result)


def _refresh_targets(
    request: EndpointRefreshRequest,
    targets: list[DeviceProfile],
    connected: dict[str, str],
    outcomes: list[ProfileRefreshOutcome],
    changes_by_profile: dict[str, tuple[ProfileEndpointChange, ...]],
    *,
    command_runner: Callable[..., str],
    progress: ProgressCallback | None,
    cancel: CancellationToken | None,
) -> None:
    for profile in targets:
        check_cancelled(cancel)
        reachable = next(
            (serial for serial in profile.adb_serials if connected.get(serial) == "device"),
            None,
        )
        scanned_from: str | None = None
        if reachable is None and request.scan:
            alias = stale_wireless_alias(profile)
            if alias is not None and hardware_serials(profile):
                if request.dry_run:
                    scanned_from = f"{alias[0]}:{alias[1]}"
                    # Scanning connects to other hosts; it isn't read-only, so dry runs
                    # only report that a scan would be attempted, without doing it.
                else:
                    report_progress(
                        progress,
                        ProgressEvent(
                            "profile_refresh",
                            "scanning",
                            f"Scanning {alias[0]}/24 for {profile.name}",
                            details={"profile": profile.name},
                        ),
                    )
                    found = scan_for_profile(
                        profile, command_runner=command_runner, cancel=cancel
                    )
                    if found is not None:
                        reachable = found
                        scanned_from = f"{alias[0]}:{alias[1]}"
        if request.dry_run and scanned_from is not None:
            # A speculative report: a real run might find a different address, or none.
            # (reachable is still None here -- dry runs never actually scan.)
            outcome = ProfileRefreshOutcome(
                profile.name,
                None,
                (ProfileEndpointChange("adb_serial", scanned_from, "(scan result)"),),
                "network scan would be attempted",
                scanned=True,
            )
            outcomes.append(outcome)
            report_progress(
                progress,
                ProgressEvent(
                    "profile_refresh", "scan_planned", outcome.note, details=outcome.to_dict()
                ),
            )
            continue
        if reachable is None:
            note = (
                "no reachable ADB endpoint found after network scan"
                if request.scan
                else "no reachable ADB endpoint to query"
            )
            outcome = ProfileRefreshOutcome(
                profile.name, None, (), note, scanned=request.scan
            )
            outcomes.append(outcome)
            report_progress(
                progress,
                ProgressEvent(
                    "profile_refresh", "skipped", outcome.note, details=outcome.to_dict()
                ),
            )
            continue
        address = _device_wifi_address(reachable, command_runner)
        if address is None:
            outcome = ProfileRefreshOutcome(
                profile.name,
                None,
                (),
                "device has no active Wi-Fi route",
                scanned=scanned_from is not None,
            )
            outcomes.append(outcome)
            report_progress(
                progress,
                ProgressEvent(
                    "profile_refresh", "skipped", outcome.note, details=outcome.to_dict()
                ),
            )
            continue

        changes: list[ProfileEndpointChange] = []
        if scanned_from is not None and reachable not in profile.adb_serials:
            changes.append(ProfileEndpointChange("adb_serial", scanned_from, reachable))
        if profile.ssh and profile.ssh.get("host") != address:
            changes.append(
                ProfileEndpointChange("ssh_host", str(profile.ssh.get("host")), address)
            )
        for serial in profile.adb_serials:
            match = ADB_NETWORK_SERIAL.fullmatch(serial)
            if match is None or match.group("host") == address:
                continue
            candidate = f"{address}:{match.group('port')}"
            if candidate == reachable or candidate in profile.adb_serials:
                continue
            if request.dry_run:
                changes.append(ProfileEndpointChange("adb_serial", serial, candidate))
                continue
            try:
                command_runner(["adb", "connect", candidate], timeout=10)
                refreshed = parse_adb_devices(command_runner(["adb", "devices", "-l"]))
            except BridgeError:
                continue
            if any(
                device["serial"] == candidate and device["state"] == "device"
                for device in refreshed
            ):
                changes.append(ProfileEndpointChange("adb_serial", serial, candidate))

        if changes:
            changes_by_profile[profile.name] = tuple(changes)
        outcome = ProfileRefreshOutcome(
            profile.name,
            address,
            tuple(changes),
            None if changes else "already up to date",
            scanned=scanned_from is not None,
        )
        outcomes.append(outcome)
        report_progress(
            progress,
            ProgressEvent(
                "profile_refresh",
                "refreshed" if changes else "unchanged",
                outcome.note or f"refreshed {len(changes)} endpoint(s)",
                details=outcome.to_dict(),
            ),
        )


def render_profile_refresh_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        print("Linkplane Endpoint Refresh")
        if event.details.get("dry_run"):
            print("(dry run; no changes will be saved)")
    elif event.phase == "scanning":
        print(f"{event.details['profile']:<12} {event.message}")
    elif event.phase == "scan_planned":
        print(f"{event.details['profile']:<12} {event.message}")
    elif event.phase == "skipped":
        print(f"{event.details['profile']:<12} {event.message}")
    elif event.phase in {"refreshed", "unchanged"}:
        outcome = event.details
        address = outcome["discovered_address"] or "unknown"
        prefix = "Found via scan, " if outcome.get("scanned") else ""
        print(f"{outcome['profile']:<12} {prefix}Wi-Fi address {address}")
        for change in outcome["changes"]:
            print(f"  {change['field']:<12} {change['previous']} -> {change['current']}")
        if not outcome["changes"]:
            print("  up to date")
    elif event.phase == "completed":
        result = event.details
        if not result["dry_run"] and any(profile["changes"] for profile in result["profiles"]):
            print(f"Config      {result['config_path']}")


def refresh_profiles(arguments: Any) -> int:
    with cancel_on_interrupt(CancellationToken()) as cancel:
        result = refresh_profile_endpoints(
            EndpointRefreshRequest(
                getattr(arguments, "name", None),
                arguments.config,
                arguments.dry_run,
                arguments.scan,
            ),
            progress=None if arguments.json else render_profile_refresh_progress,
            cancel=cancel,
        )
    if result.error is not None:
        if result.error.code == "cancelled":
            raise OperationCancelled(result.error.message)
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful endpoint refresh did not return a value")
    if arguments.json:
        print(
            json.dumps(
                {"schema_version": JSON_SCHEMA_VERSION, "ok": True, "data": result.value.to_dict()},
                indent=2,
            )
        )
    return 0


def save_config(config: dict[str, Any], path: str | None = None) -> Path:
    config_path = resolve_config_path(path)
    temporary: Path | None = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            dir=config_path.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            os.fchmod(config_file.fileno(), 0o600)
            json.dump(config, config_file, indent=2, sort_keys=True)
            config_file.write("\n")
            config_file.flush()
            os.fsync(config_file.fileno())
        temporary.replace(config_path)
    except OSError as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise errors.LinkplaneError(
            errors.CONFIG_UNWRITABLE,
            f"unable to save configuration at {config_path}: {error.strerror or error}",
            errors.CONFIG_UNWRITABLE_HINTS,
        ) from error
    return config_path


def update_config(
    path: str | None,
    update: Callable[[dict[str, Any]], None],
) -> Path:
    config_path = resolve_config_path(path)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(f"{config_path}.lock", flags, 0o600)
    except OSError as error:
        raise errors.LinkplaneError(
            errors.CONFIG_UNWRITABLE,
            f"unable to lock configuration at {config_path}: {error.strerror or error}",
            errors.CONFIG_UNWRITABLE_HINTS,
        ) from error
    try:
        lock_file = os.fdopen(descriptor, "r+")
        descriptor = -1
        with lock_file:
            os.fchmod(lock_file.fileno(), 0o600)
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            config = load_config(str(config_path))
            update(config)
            return save_config(config, str(config_path))
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise errors.LinkplaneError(
            errors.CONFIG_UNWRITABLE,
            f"unable to lock configuration at {config_path}: {error.strerror or error}",
            errors.CONFIG_UNWRITABLE_HINTS,
        ) from error


def merge_profile(
    config: dict[str, Any],
    name: str,
    device_id: str,
    *,
    adb_serial: str | None = None,
    ssh: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_profile_name(name)
    raw_profiles = config.setdefault("devices", {})
    if not isinstance(raw_profiles, dict):
        raise BridgeError("devices configuration must be a JSON object")
    existing = raw_profiles.get(name, {})
    if not isinstance(existing, dict):
        raise BridgeError(f"device profile {name} must be a JSON object")
    existing_id = existing.get("device_id")
    if existing_id is not None and existing_id != device_id:
        raise BridgeError(
            f"device profile {name} belongs to {existing_id}; remove it before reusing the name"
        )
    updated = dict(existing)
    updated["device_id"] = device_id
    if adb_serial is not None:
        existing_adb = updated.get("adb", {})
        if not isinstance(existing_adb, dict):
            raise BridgeError(f"device profile {name}.adb must be a JSON object")
        serials = existing_adb.get("serials", [])
        legacy_serial = existing_adb.get("serial")
        if not isinstance(serials, list) or not all(
            isinstance(serial, str) and serial for serial in serials
        ):
            raise BridgeError(
                f"device profile {name}.adb.serials must be a list of strings"
            )
        if legacy_serial is not None and not isinstance(legacy_serial, str):
            raise BridgeError(f"device profile {name}.adb.serial must be a string")
        combined = [*serials, *([legacy_serial] if legacy_serial else []), adb_serial]
        updated["adb"] = {
            "serials": list(dict.fromkeys(combined)),
            "preferred_serial": adb_serial,
        }
    if ssh is not None:
        updated["ssh"] = ssh
    raw_profiles[name] = updated
    return config


def read_profiles(request: ProfileListRequest) -> OperationResult[ProfileListResult]:
    try:
        config = load_config(request.config_path)
        profiles = profiles_from_config(config)
        default = config.get("default_device")
    except BridgeError as error:
        return _failure("operation_failed", error)
    if default is not None and not isinstance(default, str):
        return OperationResult.failure(
            "operation_failed", "default_device must be a string"
        )
    return OperationResult.success(
        ProfileListResult(default, tuple(profile for _, profile in sorted(profiles.items())))
    )


def list_profiles(arguments: Any) -> int:
    result = read_profiles(ProfileListRequest(arguments.config))
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful profile listing did not return a value")
    if arguments.json:
        print(
            json.dumps(
                {
                    "schema_version": JSON_SCHEMA_VERSION,
                    "ok": True,
                    "data": result.value.to_dict(),
                },
                indent=2,
            )
        )
        return 0

    print("Linkplane Device Profiles")
    if not result.value.profiles:
        print("No profiles configured")
        return 0
    for profile in result.value.profiles:
        marker = " (default)" if profile.name == result.value.default_device else ""
        endpoints: list[str] = []
        if profile.adb_serials:
            endpoints.append(f"ADB {', '.join(profile.adb_serials)}")
        if profile.ssh:
            host = profile.ssh.get("host", "?")
            user = profile.ssh.get("user", "?")
            port = profile.ssh.get("port", 8022)
            endpoints.append(f"SSH {user}@{host}:{port}")
        print(f"{profile.name}{marker}")
        print(f"  ID         {profile.device_id}")
        print(f"  Endpoints  {', '.join(endpoints) or 'none'}")
    return 0


def remove_device_profile(
    request: ProfileChangeRequest,
    *,
    progress: ProgressCallback | None = None,
) -> OperationResult[ProfileChangeResult]:
    def remove(config: dict[str, Any]) -> None:
        profiles = config.get("devices", {})
        if not isinstance(profiles, dict):
            raise BridgeError("devices configuration must be a JSON object")
        if request.name not in profiles:
            raise _ProfileNotFound(f"device profile not found: {request.name}")
        del profiles[request.name]
        if config.get("default_device") == request.name:
            config.pop("default_device", None)

    report_progress(
        progress,
        ProgressEvent(
            "profile_remove",
            "started",
            "Profile removal started",
            details={"profile": request.name},
        ),
    )
    try:
        path = update_config(request.config_path, remove)
    except _ProfileNotFound as error:
        return OperationResult.failure("not_found", str(error))
    except BridgeError as error:
        return _failure("operation_failed", error)
    result = ProfileChangeResult("remove", request.name, str(path))
    report_progress(
        progress,
        ProgressEvent(
            "profile_remove",
            "completed",
            "Profile removed",
            details=result.to_dict(),
        ),
    )
    return OperationResult.success(result)


def remove_profile(arguments: Any) -> int:
    result = remove_device_profile(
        ProfileChangeRequest(arguments.name, arguments.config)
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful profile removal did not return a value")
    print(f"Removed     {result.value.profile}")
    print(f"Config      {result.value.config_path}")
    return 0


def select_default_profile(
    request: ProfileChangeRequest,
    *,
    progress: ProgressCallback | None = None,
) -> OperationResult[ProfileChangeResult]:
    try:
        validate_profile_name(request.name)
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))

    def select(config: dict[str, Any]) -> None:
        profiles = profiles_from_config(config)
        if request.name not in profiles:
            raise _ProfileNotFound(f"device profile not found: {request.name}")
        config["default_device"] = request.name

    report_progress(
        progress,
        ProgressEvent(
            "profile_default",
            "started",
            "Default profile selection started",
            details={"profile": request.name},
        ),
    )
    try:
        path = update_config(request.config_path, select)
    except _ProfileNotFound as error:
        return OperationResult.failure("not_found", str(error))
    except BridgeError as error:
        return _failure("operation_failed", error)
    result = ProfileChangeResult("default", request.name, str(path))
    report_progress(
        progress,
        ProgressEvent(
            "profile_default",
            "completed",
            "Default profile selected",
            details=result.to_dict(),
        ),
    )
    return OperationResult.success(result)


def set_default_profile(arguments: Any) -> int:
    result = select_default_profile(
        ProfileChangeRequest(arguments.name, arguments.config)
    )
    if result.error is not None:
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful default selection did not return a value")
    print(f"Default     {result.value.profile}")
    print(f"Config      {result.value.config_path}")
    return 0


def save_paired_profile(
    name: str,
    config_path: str | None,
    make_default: bool,
    device_id: str,
    *,
    adb_serial: str | None = None,
    ssh: dict[str, Any] | None = None,
) -> Path:
    def save(config: dict[str, Any]) -> None:
        merge_profile(
            config,
            name,
            device_id,
            adb_serial=adb_serial,
            ssh=ssh,
        )
        profiles_from_config(config)
        if make_default or config.get("default_device") is None:
            config["default_device"] = name

    return update_config(config_path, save)


def next_free_profile_name(name: str, taken: Iterable[str]) -> str:
    """`name`, or the first of `name-2`, `name-3`, … not in `taken`. Deterministic, so a
    guided setup can propose it and a user can predict it."""
    names = set(taken)
    if name not in names:
        return name
    counter = 2
    while f"{name}-{counter}" in names:
        counter += 1
    return f"{name}-{counter}"


def paired_device_id(
    name: str,
    config_path: str | None,
    fallback: str,
    *,
    strict: bool = False,
) -> str:
    """The canonical `device_id` a pairing under `name` must use.

    `strict` means `fallback` is a proven hardware identity (a USB serial, or an explicit
    `--device-id`): an existing profile of that name must belong to that same device, or
    the pairing is refused with a conflict -- a different phone never inherits another
    phone's profile just because the human picked the same word. Non-strict callers pass
    an endpoint that is *not* an identity (a wireless `host:port`) and legitimately attach
    it to the existing profile; they must verify the device themselves afterwards.
    """
    profiles = profiles_from_config(load_config(config_path))
    existing = profiles.get(name)
    if existing and strict and existing.device_id != fallback:
        raise errors.LinkplaneError(
            errors.REQUEST_INVALID,
            f"device profile {name} belongs to device {existing.device_id}, not {fallback}",
            (
                f"pair this phone under another name, for example "
                f"{next_free_profile_name(name, profiles)}",
                f"or remove the old profile first: linkplane profiles remove {name}",
            ),
        )
    return existing.device_id if existing else fallback


def _failure(code: str, error: BridgeError) -> OperationResult[Any]:
    """`OperationResult.failure` that keeps a `LinkplaneError`'s stable code and hints."""
    if isinstance(error, errors.LinkplaneError):
        return OperationResult.failure(code, str(error), error_code=error.code, hints=error.hints)
    return OperationResult.failure(code, str(error))


def _raise_failure(error: Any) -> None:
    """Turn a failed `OperationResult` back into the exception the CLI renders, keeping the
    stable code and hints when the service recorded them."""
    if error.error_code is not None:
        raise errors.LinkplaneError(error.error_code, error.message, error.hints)
    raise BridgeError(error.message)


def _adb_is_available(locator: Callable[[str], str | None] | None) -> bool:
    if locator is not None:
        return locator("adb") is not None
    return adb_dependency_plan().available


def pair_usb_device(
    request: UsbPairingRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[PairingResult]:
    adb_factory = adb_factory or AdbTransport  # resolved at call time (setup seam)
    try:
        validate_profile_name(request.name)
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))
    if not _adb_is_available(adb_locator):
        return OperationResult.failure("dependency_missing", "ADB is not installed")
    try:
        transport = adb_factory(request.serial)
        device = transport.select_device()
    except BridgeError as error:
        return OperationResult.failure("transport_unavailable", str(error))
    serial = transport.serial
    if serial is None:
        return OperationResult.failure(
            "transport_unavailable", "unable to determine the ADB device serial"
        )
    try:
        # A USB serial is the hardware identity: an existing profile of this name must be
        # this very phone (found 2026-09-12: non-strict lookup aliased a second phone).
        device_id = paired_device_id(request.name, request.config_path, serial, strict=True)
    except BridgeError as error:
        return _failure("invalid_request", error)
    model = device.get("model", "Android device").replace("_", " ")
    result = PairingResult(
        method="usb",
        profile=request.name,
        device_id=device_id,
        device=model,
        adb_serial=serial,
        ssh_endpoint=None,
        config_path=str(resolve_config_path(request.config_path)),
        commands=(),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "pair_usb", "started", "USB pairing prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress, ProgressEvent("pair_usb", "completed", "Dry run completed")
        )
        return OperationResult.success(result)
    try:
        path = save_paired_profile(
            request.name,
            request.config_path,
            request.make_default,
            device_id,
            adb_serial=serial,
        )
    except BridgeError as error:
        return _failure("operation_failed", error)
    result = replace(result, config_path=str(path))
    report_progress(
        progress,
        ProgressEvent(
            "pair_usb", "completed", "USB pairing saved", details=result.to_dict()
        ),
    )
    return OperationResult.success(result)


def pair_usb(arguments: Any) -> int:
    result = pair_usb_device(
        UsbPairingRequest(
            name=arguments.name,
            serial=arguments.serial,
            make_default=arguments.default,
            dry_run=arguments.dry_run,
            config_path=arguments.config,
        ),
        adb_factory=AdbTransport,
        progress=render_usb_pairing_progress,
    )
    if result.error is not None:
        _raise_failure(result.error)
    return 0


def render_usb_pairing_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane USB Pairing")
        print(f"Device      {result['device']} ({result['adb_serial']})")
        print(f"Profile     {result['profile']}")
        if result["dry_run"]:
            print(f"Would save  {result['config_path']}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        print(f"Config      {event.details['config_path']}")


def validate_network_endpoint(value: str, label: str) -> None:
    host, separator, raw_port = value.rpartition(":")
    if (
        not separator
        or not host
        or any(character.isspace() for character in value)
        or value.startswith("-")
    ):
        raise BridgeError(f"{label} must use HOST:PORT")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise BridgeError(f"{label} must use HOST:PORT") from error
    if not 1 <= port <= 65535:
        raise BridgeError(f"{label} port must be between 1 and 65535")


def pairing_code() -> str:
    if not sys.stdin.isatty():
        raise BridgeError("wireless ADB pairing requires an interactive terminal")
    import getpass

    code = getpass.getpass("Android wireless pairing code: ").strip()
    if not re.fullmatch(r"[0-9]{6}", code):
        raise BridgeError("wireless ADB pairing code must contain exactly six digits")
    return code


def pair_wireless_device(
    request: WirelessPairingRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    command_runner: Callable[..., str] | None = None,
    code_reader: Callable[[], str] | None = None,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[PairingResult]:
    adb_factory = adb_factory or AdbTransport
    command_runner = command_runner or run_command
    code_reader = code_reader or pairing_code
    try:
        validate_profile_name(request.name)
        validate_network_endpoint(request.pair_address, "pairing address")
        validate_network_endpoint(request.connect_address, "connection address")
        device_id = paired_device_id(
            request.name, request.config_path, request.connect_address
        )
        candidate_config = load_config(request.config_path)
        existing_profile = profiles_from_config(candidate_config).get(request.name)
        # A `host:port` is an endpoint, not an identity. If the profile already knows the
        # phone's hardware serial (learned over USB), the connected device must present
        # that serial before its address is attached to the profile.
        expected_serials = hardware_serials(existing_profile) if existing_profile else ()
        merge_profile(
            candidate_config,
            request.name,
            device_id,
            adb_serial=request.connect_address,
        )
        profiles_from_config(candidate_config)
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))
    if not _adb_is_available(adb_locator):
        return OperationResult.failure("dependency_missing", "ADB is not installed")

    pair_command = ("adb", "pair", request.pair_address)
    connect_command = ("adb", "connect", request.connect_address)
    result = PairingResult(
        method="wireless",
        profile=request.name,
        device_id=device_id,
        device=None,
        adb_serial=request.connect_address,
        ssh_endpoint=None,
        config_path=str(resolve_config_path(request.config_path)),
        commands=(pair_command, connect_command),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "pair_wireless",
            "started",
            "Wireless pairing prepared",
            details=result.to_dict(),
        ),
    )
    if request.dry_run:
        report_progress(
            progress,
            ProgressEvent("pair_wireless", "completed", "Dry run completed"),
        )
        return OperationResult.success(result)

    try:
        code = code_reader()
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))
    if not re.fullmatch(r"[0-9]{6}", code):
        return OperationResult.failure(
            "invalid_request",
            "wireless ADB pairing code must contain exactly six digits",
        )
    try:
        pair_output = command_runner(
            list(pair_command), timeout=60, input_text=f"{code}\n"
        ).strip()
        safe_pair_output = pair_output.replace(code, "[redacted]")
        report_progress(
            progress,
            ProgressEvent("pair_wireless", "paired", safe_pair_output or "completed"),
        )
        connect_output = command_runner(list(connect_command), timeout=30).strip()
        report_progress(
            progress,
            ProgressEvent(
                "pair_wireless", "connected", connect_output or "connected"
            ),
        )
        transport = adb_factory(request.connect_address)
        device = transport.select_device()
        if expected_serials:
            actual = command_runner(
                ["adb", "-s", request.connect_address, "shell", "getprop", "ro.serialno"],
                timeout=30,
            ).strip()
            if actual not in expected_serials:
                try:
                    command_runner(["adb", "disconnect", request.connect_address], timeout=30)
                except BridgeError:
                    pass
                raise errors.LinkplaneError(
                    errors.REQUEST_INVALID,
                    f"the device at {request.connect_address} (serial {actual or 'unknown'}) "
                    f"is not the phone profile {request.name} belongs to ({expected_serials[0]})",
                    (
                        "pair it under another name, for example "
                        f"{next_free_profile_name(request.name, profiles_from_config(candidate_config))}",
                        f"or remove the old profile first: linkplane profiles remove {request.name}",
                    ),
                )
        path = save_paired_profile(
            request.name,
            request.config_path,
            request.make_default,
            device_id,
            adb_serial=request.connect_address,
        )
    except BridgeError as error:
        failure = _failure("operation_failed", error)
        return OperationResult.failure(
            "operation_failed",
            failure.error.message.replace(code, "[redacted]"),
            error_code=failure.error.error_code,
            hints=failure.error.hints,
        )
    model = device.get("model", "Android device").replace("_", " ")
    result = replace(
        result,
        device=model,
        config_path=str(path),
        pairing_output=safe_pair_output or "completed",
        connection_output=connect_output or "connected",
    )
    report_progress(
        progress,
        ProgressEvent(
            "pair_wireless",
            "completed",
            "Wireless pairing saved",
            details=result.to_dict(),
        ),
    )
    return OperationResult.success(result)


def pair_wireless(arguments: Any) -> int:
    result = pair_wireless_device(
        WirelessPairingRequest(
            name=arguments.name,
            pair_address=arguments.pair_address,
            connect_address=arguments.connect_address,
            make_default=arguments.default,
            dry_run=arguments.dry_run,
            config_path=arguments.config,
        ),
        adb_factory=AdbTransport,
        command_runner=run_command,
        code_reader=pairing_code,
        progress=render_wireless_pairing_progress,
    )
    if result.error is not None:
        _raise_failure(result.error)
    return 0


def render_wireless_pairing_progress(event: ProgressEvent) -> None:
    if event.phase == "started" and event.details["dry_run"]:
        result = event.details
        print("Linkplane Wireless ADB Pairing")
        print(f"Pair        {shlex.join(result['commands'][0])}")
        print(f"Connect     {shlex.join(result['commands'][1])}")
        print(f"Profile     {result['profile']}")
        print(f"Would save  {result['config_path']}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        result = event.details
        print("Linkplane Wireless ADB Pairing")
        print(f"Pairing     {result['pairing_output']}")
        print(f"Connection  {result['connection_output']}")
        print(f"Device      {result['device']} ({result['adb_serial']})")
        print(f"Profile     {result['profile']}")
        print(f"Config      {result['config_path']}")


def pair_ssh_device(
    request: SshPairingRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    ssh_factory: Callable[[str, str, int, str | None], SshTransport] = SshTransport,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[PairingResult]:
    adb_factory = adb_factory or AdbTransport  # resolved at call time (setup seam)
    try:
        validate_profile_name(request.name)
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))
    if not request.host or not request.user:
        return OperationResult.failure(
            "invalid_request", "Termux SSH pairing requires a host and user"
        )
    if not 1 <= request.port <= 65535:
        return OperationResult.failure(
            "invalid_request", "Termux SSH port must be between 1 and 65535"
        )
    if request.serial is not None and request.device_id is not None:
        return OperationResult.failure(
            "invalid_request", "ADB serial and explicit device ID are mutually exclusive"
        )
    if request.device_id is not None and not request.device_id:
        return OperationResult.failure(
            "invalid_request", "explicit device ID must not be empty"
        )

    device_id = request.device_id
    adb_serial = request.serial
    if device_id is None:
        if not _adb_is_available(adb_locator):
            return OperationResult.failure("dependency_missing", "ADB is not installed")
        try:
            adb = adb_factory(adb_serial)
            adb.select_device()
        except BridgeError as error:
            return OperationResult.failure("transport_unavailable", str(error))
        adb_serial = adb.serial
        device_id = adb.serial
    if device_id is None:
        return OperationResult.failure(
            "transport_unavailable",
            "unable to associate SSH with a device; connect ADB or pass --device-id",
        )
    try:
        # Strict in both cases: an explicit --device-id and a serial read over USB are
        # each a claimed hardware identity, never a mere endpoint.
        device_id = paired_device_id(
            request.name,
            request.config_path,
            device_id,
            strict=True,
        )
        transport = ssh_factory(
            request.host, request.user, request.port, request.identity_file
        )
        command = tuple(transport.command("printf linkplane-ready"))
    except BridgeError as error:
        return _failure("invalid_request", error)

    endpoint = f"{request.user}@{request.host}:{request.port}"
    result = PairingResult(
        method="ssh",
        profile=request.name,
        device_id=device_id,
        device=None,
        adb_serial=adb_serial,
        ssh_endpoint=endpoint,
        config_path=str(resolve_config_path(request.config_path)),
        commands=(command,),
        dry_run=request.dry_run,
    )
    report_progress(
        progress,
        ProgressEvent(
            "pair_ssh", "started", "SSH pairing prepared", details=result.to_dict()
        ),
    )
    if request.dry_run:
        report_progress(
            progress, ProgressEvent("pair_ssh", "completed", "Dry run completed")
        )
        return OperationResult.success(result)

    try:
        probe = transport.execute(["printf", "linkplane-ready"]).strip()
        if probe != "linkplane-ready":
            return OperationResult.failure(
                "operation_failed", "Termux SSH returned an unexpected pairing response"
            )
        ssh: dict[str, Any] = {
            "host": request.host,
            "user": request.user,
            "port": request.port,
        }
        if request.identity_file:
            ssh["identity_file"] = request.identity_file
        path = save_paired_profile(
            request.name,
            request.config_path,
            request.make_default,
            device_id,
            adb_serial=adb_serial,
            ssh=ssh,
        )
    except BridgeError as error:
        return _failure("operation_failed", error)
    result = replace(result, config_path=str(path))
    report_progress(
        progress,
        ProgressEvent(
            "pair_ssh", "completed", "SSH pairing saved", details=result.to_dict()
        ),
    )
    return OperationResult.success(result)


def pair_ssh(arguments: Any) -> int:
    result = pair_ssh_device(
        SshPairingRequest(
            name=arguments.name,
            host=arguments.ssh_host,
            user=arguments.ssh_user,
            port=arguments.ssh_port,
            identity_file=arguments.ssh_key,
            serial=arguments.serial,
            device_id=arguments.device_id,
            make_default=arguments.default,
            dry_run=arguments.dry_run,
            config_path=arguments.config,
        ),
        adb_factory=AdbTransport,
        ssh_factory=SshTransport,
        progress=render_ssh_pairing_progress,
    )
    if result.error is not None:
        _raise_failure(result.error)
    return 0


def render_ssh_pairing_progress(event: ProgressEvent) -> None:
    if event.phase == "started" and event.details["dry_run"]:
        result = event.details
        print("Linkplane Termux SSH Pairing")
        print(f"Endpoint    {result['ssh_endpoint']}")
        print(f"Device ID   {result['device_id']}")
        print(f"Profile     {result['profile']}")
        print(f"Command     {shlex.join(result['commands'][0])}")
        print(f"Would save  {result['config_path']}")
    elif event.phase == "completed" and event.message != "Dry run completed":
        result = event.details
        print("Linkplane Termux SSH Pairing")
        print(f"Endpoint    {result['ssh_endpoint']}")
        print(f"Device ID   {result['device_id']}")
        print(f"Profile     {result['profile']}")
        print(f"Config      {result['config_path']}")
