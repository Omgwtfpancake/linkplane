"""Pins the frozen application-service contract (`CONTRACT_VERSION` 2; 1 was the same surface under the PhoneBridge name).

docs/api-contracts.md promises external adapters (API, MCP, GUI) that every field listed
here keeps its name, and that every error code keeps its meaning. Fields may be *added*
(with defaults) freely -- this test only checks that the pinned ones still exist in order
-- and new codes may appear. Renaming/removing a field or a code is a breaking change:
update the snapshot here *and* bump `linkplane.operations.CONTRACT_VERSION`, and say so
in docs/api-contracts.md.

Internal helper dataclasses (`backup.RemoteFile`, `clipboard.DesktopClipboard`,
`dependencies.PackageManager`) are deliberately not pinned.
"""

import dataclasses
import importlib
import inspect
import re
import unittest
from pathlib import Path

from linkplane.operations import CONTRACT_VERSION

FROZEN_FIELDS = {
    "operations.OperationError": ("code", "message", "error_code", "hints"),
    "operations.OperationResult": (
        "value", "error", "operation", "resource_id", "provider", "warnings",
    ),
    "operations.ProgressEvent": (
        "operation", "phase", "message", "current", "total", "unit", "item", "details",
    ),
    "models.Capability": ("name", "status", "detail"),
    "models.Check": ("name", "status", "summary", "fix", "code"),
    "models.Device": (
        "id", "name", "manufacturer", "model", "android", "capabilities", "endpoints",
    ),
    "models.DiscoveryIssue": ("backend", "error"),
    "models.DiscoveryResult": ("devices", "issues"),
    "models.Endpoint": (
        "transport", "address", "state", "device_id", "capabilities", "details", "error",
    ),
    "dependencies.DependencyPlan": (
        "executable", "package", "available", "executable_path", "package_manager",
        "install_command", "required_executables", "missing_executables",
        "requires_privilege", "install_error",
    ),
    "dependencies.ScrcpyCompatibility": (
        "screen_supported", "camera_supported", "audio_supported",
        "missing_screen_options", "missing_camera_options", "missing_audio_options",
        "probe_error", "webcam_supported", "missing_webcam_options",
    ),
    "status.StatusRequest": ("transport", "serial", "serial_from_profile"),
    "status.StatusResult": (
        "transport", "device", "battery", "memory", "storage", "uptime_seconds", "issues",
    ),
    "status.TelemetryIssue": ("component", "error"),
    "screen.ScreenRequest": (
        "serial", "quality", "audio", "record", "extra_arguments", "install", "dry_run",
    ),
    "screen.ScreenResult": (
        "device", "serial", "quality", "command", "dependency", "dry_run", "exit_code",
    ),
    "audio.AudioRequest": (
        "serial", "source", "record", "record_format", "codec", "extra_arguments",
        "install", "dry_run",
    ),
    "audio.AudioResult": (
        "device", "serial", "source", "command", "dependency", "dry_run", "exit_code",
    ),
    "transfer.SendItem": ("path", "size"),
    "transfer.SendRequest": ("paths", "transport", "destination", "serial", "dry_run"),
    "transfer.SendResult": (
        "transport", "items", "total_bytes", "destination", "device", "serial", "dry_run",
    ),
    "backup.BackupRequest": ("destination", "source", "serial", "dry_run"),
    "backup.BackupResult": (
        "transport", "device", "serial", "source", "destination", "discovered", "pending",
        "pending_bytes", "downloaded", "skipped", "pending_files", "dry_run",
    ),
    "camera.CameraPreviewRequest": (
        "serial", "quality", "facing", "camera_id", "audio", "torch", "record",
        "extra_arguments", "install", "dry_run",
    ),
    "camera.CameraPreviewResult": (
        "device", "serial", "selection", "quality", "command", "dependency", "dry_run",
        "exit_code",
    ),
    "camera.CaptureRequest": ("output", "camera_id", "force", "foreground", "dry_run"),
    "camera.CaptureResult": (
        "transport", "endpoint", "camera_id", "output", "remote_path", "foreground_serial",
        "command", "bytes_written", "dry_run",
    ),
    "webcam.WebcamStartRequest": (
        "serial", "device", "facing", "camera_id", "extra_arguments", "install", "dry_run",
        "state_path",
    ),
    "webcam.WebcamStartResult": (
        "device", "serial", "video_device", "command", "scrcpy_dependency",
        "loopback_dependency", "pid", "dry_run",
    ),
    "webcam.WebcamStopRequest": ("state_path", "dry_run"),
    "webcam.WebcamStopResult": ("pid", "video_device", "serial", "dry_run"),
    "notification.NotificationRequest": (
        "message", "title", "notification_id", "transport", "serial", "dry_run",
        "serial_from_profile",
    ),
    "notification.NotificationResult": (
        "transport", "device", "serial", "title", "notification_id", "message", "command",
        "dry_run",
    ),
    "find.FindPhoneRequest": (
        "serial", "duration", "ring", "torch", "vibrate", "message", "install", "dry_run",
    ),
    "find.FindPhoneResult": (
        "device", "serial", "duration", "ring", "torch", "vibrate", "notification_command",
        "ring_get_command", "ring_set_command", "torch_command", "vibrate_command",
        "dry_run", "ring_applied", "ring_restored", "note",
    ),
    "devices.DiscoveryRequest": (
        "ssh", "ssh_device_id", "additional_ssh", "adb_identities", "allowed_adb_serials",
        "issues",
    ),
    "doctor.DoctorRequest": ("discovery",),
    "doctor.DoctorResult": ("checks",),
    "clipboard.ClipboardRequest": (
        "action", "text", "interval", "prefer", "foreground", "dry_run",
    ),
    "clipboard.ClipboardResult": (
        "action", "transport", "endpoint", "desktop_backend", "foreground_serial",
        "commands", "text", "bytes_transferred", "updates", "dry_run",
    ),
    "profiles.DeviceProfile": (
        "name", "device_id", "adb_serials", "preferred_adb_serial", "ssh",
    ),
    "profiles.EndpointRefreshRequest": ("name", "config_path", "dry_run", "scan"),
    "profiles.EndpointRefreshResult": ("config_path", "dry_run", "profiles"),
    "profiles.PairingResult": (
        "method", "profile", "device_id", "device", "adb_serial", "ssh_endpoint",
        "config_path", "commands", "pairing_output", "connection_output", "dry_run",
    ),
    "profiles.ProfileChangeRequest": ("name", "config_path"),
    "profiles.ProfileChangeResult": ("action", "profile", "config_path"),
    "profiles.ProfileEndpointChange": ("field", "previous", "current"),
    "profiles.ProfileListRequest": ("config_path",),
    "profiles.ProfileListResult": ("default_device", "profiles"),
    "profiles.ProfileRefreshOutcome": (
        "profile", "discovered_address", "changes", "note", "scanned",
    ),
    "profiles.SshPairingRequest": (
        "name", "host", "user", "port", "identity_file", "serial", "device_id",
        "make_default", "dry_run", "config_path",
    ),
    "profiles.UsbPairingRequest": ("name", "serial", "make_default", "dry_run", "config_path"),
    "profiles.WirelessPairingRequest": (
        "name", "pair_address", "connect_address", "make_default", "dry_run", "config_path",
    ),
    # Core v0.1 (pinned 2026-09-10 after real-device use; additive to contract version 1).
    "core.capability.CapabilityReport": ("name", "status", "detail", "requirements", "metadata"),
    "core.telemetry.StatusResult": (
        "transport", "device", "battery", "memory", "storage", "uptime_seconds", "issues",
    ),
    "providers.base.PingResult": ("reachable", "provider", "address", "latency_ms", "detail"),
    "providers.base.BatteryReading": (
        "level", "status", "health", "powered_by", "provider", "temperature_c", "voltage_mv",
    ),
    # Core v0.2 (pinned after the daemon used them).
    "core.events.Event": ("type", "device", "ts", "provider", "data", "seq", "event_id", "correlation_id", "source"),
    "core.state.DeviceState": (
        "device", "provider", "connection", "address", "battery", "wifi_ssid", "last_seen", "updated",
    ),
}

# Event types (docs/adr/0007). Append-only.
FROZEN_EVENT_TYPES = (
    "observer.started", "observer.stopped",
    "device.connected", "device.disconnected", "device.authorized", "device.unauthorized",
    "battery.changed", "battery.low", "battery.ok",
    "charging.started", "charging.stopped",
    "wifi.connected", "wifi.disconnected",
)

# The public capability vocabulary (docs/adr/0004). Names may be appended; never renamed.
FROZEN_CAPABILITY_CATALOGUE = (
    "device.ping", "device.status", "battery.read", "storage.read",
    "files.send", "backup.photos", "notify.post", "clipboard.read", "clipboard.write",
    "screen.control", "camera.capture",
    "device.find", "clipboard.sync",  # local API design pass (2026-09-11)
)
FROZEN_CAPABILITY_STATUSES = (
    "supported", "unsupported", "unavailable", "permission-denied", "provider-error",
)

# Stable PB codes (docs/adr/0005). Never renumbered or reused; new ones may be added.
FROZEN_PB_CODES = {
    "LP-CONFIG-001", "LP-CONFIG-002", "LP-CONFIG-003",
    "LP-CONNECT-001", "LP-CONNECT-002", "LP-CONNECT-003",
    "LP-AUTH-001", "LP-AUTH-002",
    "LP-TIMEOUT-001",
    "LP-PROVIDER-001", "LP-PROVIDER-002", "LP-PROVIDER-003",
    "LP-CAPABILITY-001", "LP-CAPABILITY-002",
    "LP-DEPENDENCY-001",
    "LP-REQUEST-001",
    "LP-CANCELLED-001",
    "LP-STATE-001",
    "LP-DAEMON-001", "LP-DAEMON-002", "LP-DAEMON-003",
    # Local API (docs/local-api-design.md §15), Slice 0.
    "LP-CLIENT-001", "LP-CLIENT-002", "LP-RESOURCE-001", "LP-INTERNAL-001", "LP-API-001",
}

# The Provider interface every provider implements (docs/adr/0002).
FROZEN_PROVIDER_METHODS = {"address", "ping", "status", "battery", "capabilities"}

FROZEN_ERROR_CODES = {
    "invalid_request",
    "transport_unavailable",
    "dependency_missing",
    "not_found",
    "already_running",
    "operation_failed",
    "cancelled",
}

# Every service entry point that accepts a cancellation token. Signature-level pin so the
# keyword cannot be silently renamed or dropped.
CANCELLABLE_SERVICES = (
    "transfer.send_files",
    "backup.backup_photos",
    "profiles.refresh_profile_endpoints",
    "profiles.scan_for_profile",
    "clipboard.use_clipboard",
    "clipboard.sync_clipboards",
)

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "linkplane"


def _resolve(dotted: str):
    module_name, attribute = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(f"linkplane.{module_name}"), attribute)


class ContractFreezeTests(unittest.TestCase):
    def test_operation_result_renders_with_provenance(self):
        from types import SimpleNamespace
        from linkplane.operations import OperationResult

        result = OperationResult.success_with(SimpleNamespace(to_dict=lambda: {"level": 1}), operation="battery.read",
                                              resource_id="phone", provider="adb", warnings=("w",))
        self.assertEqual(result.to_dict(), {"value": {"level": 1}, "error": None, "operation": "battery.read",
                                            "resource_id": "phone", "provider": "adb", "warnings": ["w"]})
        failed = OperationResult.failure("invalid_request", "bad", error_code="LP-REQUEST-001")
        self.assertEqual(failed.to_dict()["error"]["error_code"], "LP-REQUEST-001")
        self.assertIsNone(failed.to_dict()["value"])

    def test_contract_version_is_one(self):
        self.assertEqual(CONTRACT_VERSION, 2)  # bumped once: the rename (ADR 0010)

    def test_every_pinned_field_still_exists_in_order(self):
        for dotted, expected in FROZEN_FIELDS.items():
            with self.subTest(contract=dotted):
                cls = _resolve(dotted)
                self.assertTrue(dataclasses.is_dataclass(cls), f"{dotted} is not a dataclass")
                actual = tuple(field.name for field in dataclasses.fields(cls))
                # Additive growth is allowed: the pinned fields must be a prefix-preserving
                # subsequence of the real ones, and every pinned name must still be present.
                missing = [name for name in expected if name not in actual]
                self.assertEqual(missing, [], f"{dotted} lost frozen field(s): {missing}")
                positions = [actual.index(name) for name in expected]
                self.assertEqual(
                    positions, sorted(positions), f"{dotted} reordered frozen fields"
                )

    def test_added_fields_must_have_defaults(self):
        # A new *required* field on a frozen Request breaks every existing caller that
        # constructs it -- that is a breaking change, not additive growth.
        for dotted, expected in FROZEN_FIELDS.items():
            if not dotted.endswith("Request"):
                continue
            cls = _resolve(dotted)
            for field in dataclasses.fields(cls):
                if field.name in expected:
                    continue
                with self.subTest(contract=dotted, field=field.name):
                    self.assertFalse(
                        field.default is dataclasses.MISSING
                        and field.default_factory is dataclasses.MISSING,
                        f"{dotted}.{field.name} was added without a default",
                    )

    def test_error_codes_are_exactly_the_documented_set(self):
        found = set()
        for source in SOURCE_ROOT.glob("*.py"):
            found.update(
                re.findall(r'OperationResult\.failure\(\s*"([a-z_]+)"', source.read_text())
            )
        self.assertEqual(found, FROZEN_ERROR_CODES)

    def test_capability_catalogue_and_statuses_are_pinned(self):
        from linkplane.core import capability

        self.assertEqual(capability.CATALOGUE[: len(FROZEN_CAPABILITY_CATALOGUE)], FROZEN_CAPABILITY_CATALOGUE)
        self.assertEqual(set(capability.STATUSES), set(FROZEN_CAPABILITY_STATUSES))

    def test_event_types_are_pinned_in_order(self):
        from linkplane.core.events import EVENT_TYPES

        self.assertEqual(EVENT_TYPES[: len(FROZEN_EVENT_TYPES)], FROZEN_EVENT_TYPES)

    def test_pb_codes_are_exactly_the_pinned_set_plus_additions(self):
        from linkplane.core import errors

        codes = {
            value for name, value in vars(errors).items()
            if name.isupper() and isinstance(value, str) and value.startswith("LP-")
        }
        missing = FROZEN_PB_CODES - codes
        self.assertEqual(missing, set(), f"pinned PB codes removed: {missing}")

    def test_provider_interface_is_pinned(self):
        from linkplane.providers.base import Provider

        self.assertEqual(set(Provider.__abstractmethods__), FROZEN_PROVIDER_METHODS)

    def test_cancellable_services_accept_a_cancel_keyword(self):
        for dotted in CANCELLABLE_SERVICES:
            with self.subTest(service=dotted):
                parameters = inspect.signature(_resolve(dotted)).parameters
                self.assertIn("cancel", parameters)
                self.assertIs(parameters["cancel"].kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIsNone(parameters["cancel"].default)


if __name__ == "__main__":
    unittest.main()
