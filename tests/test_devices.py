import unittest
from unittest.mock import Mock, patch

from linkplane.devices import (
    DiscoveryRequest,
    discover_adb,
    discover_ssh,
    group_devices,
    scan_devices,
)
from linkplane.dependencies import ScrcpyCompatibility
from linkplane.models import Capability, DiscoveryIssue, Endpoint
from linkplane.transports import BridgeError, SshTransport


class DeviceDiscoveryTests(unittest.TestCase):
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/tool")
    @patch(
        "linkplane.devices.AdbTransport.status",
        return_value={
            "device": {
                "manufacturer": "Samsung",
                "model": "S22",
                "android": "16",
            }
        },
    )
    def test_adb_reports_backup_capability(self, _status, _which):
        def runner(command, **_kwargs):
            if command == ["adb", "devices", "-l"]:
                return "List of devices attached\nserial-1 device model:S22 usb:1-2\n"
            if "notification" in command:
                return "notification post usage"
            return ""

        endpoints = discover_adb(runner)

        backup = next(
            capability
            for capability in endpoints[0].capabilities
            if capability.name == "backup"
        )
        self.assertEqual(backup.status, "ready")

    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/tool")
    @patch(
        "linkplane.devices.AdbTransport.status",
        return_value={
            "device": {"manufacturer": "Samsung", "model": "S22"},
            "issues": [{"component": "battery", "error": "permission denied"}],
        },
    )
    def test_partial_adb_status_is_reported_as_unverified(self, _status, _which):
        def runner(command, **_kwargs):
            if command == ["adb", "devices", "-l"]:
                return "List of devices attached\nserial-1 device model:S22 usb:1-2\n"
            if "notification" in command:
                return "notification post usage"
            return ""

        endpoint = discover_adb(runner)[0]
        status = next(
            capability
            for capability in endpoint.capabilities
            if capability.name == "status"
        )

        self.assertEqual(status.status, "unverified")
        self.assertIn("battery", status.detail)

    @patch(
        "linkplane.devices.scrcpy_compatibility",
        return_value=ScrcpyCompatibility(True, False),
    )
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/tool")
    @patch(
        "linkplane.devices.AdbTransport.status",
        return_value={"device": {"model": "S22"}},
    )
    def test_discovery_distinguishes_scrcpy_camera_support(
        self, _status, _which, _support
    ):
        def runner(command, **_kwargs):
            if command == ["adb", "devices", "-l"]:
                return "List of devices attached\nserial-1 device model:S22 usb:1-2\n"
            if "notification" in command:
                return "notification post usage"
            return ""

        endpoint = discover_adb(runner)[0]
        capabilities = {
            capability.name: capability for capability in endpoint.capabilities
        }

        self.assertEqual(capabilities["screen"].status, "ready")
        self.assertEqual(capabilities["camera"].status, "needs_dependency")

    def test_group_devices_merges_only_matching_device_ids(self):
        adb = Endpoint(
            transport="adb",
            address="serial-1",
            state="connected",
            device_id="serial-1",
            capabilities=(Capability("status", "ready"), Capability("send", "ready")),
            details={"manufacturer": "Samsung", "model": "S22", "android": "16"},
        )
        matching_ssh = Endpoint(
            transport="ssh",
            address="termux@phone:8022",
            state="connected",
            device_id="serial-1",
            capabilities=(
                Capability("status", "ready"),
                Capability("clipboard", "ready"),
            ),
            details={"model": "S22"},
        )
        unrelated = Endpoint(
            transport="ssh",
            address="termux@tablet:8022",
            state="connected",
            device_id="tablet",
            capabilities=(Capability("status", "ready"),),
        )

        devices = group_devices([adb, matching_ssh, unrelated])

        self.assertEqual(len(devices), 2)
        phone = next(device for device in devices if device.id == "serial-1")
        self.assertEqual(len(phone.endpoints), 2)
        self.assertEqual(
            tuple(capability.name for capability in phone.capabilities),
            ("clipboard", "send", "status"),
        )

    def test_ssh_capabilities_come_from_remote_commands(self):
        output = (
            "manufacturer=samsung\n"
            "model=SM-S901U\n"
            "android=16\n"
            "command=phone-status-json.sh\n"
            "command=termux-notification\n"
            "command=termux-clipboard-get\n"
            "command=termux-clipboard-set\n"
        )
        runner = Mock(return_value=output)
        transport = SshTransport("phone.local", "termux", runner=runner)

        endpoint = discover_ssh(transport, "serial-1")

        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.device_id, "serial-1")
        self.assertEqual(endpoint.state, "connected")
        self.assertEqual(
            tuple(capability.name for capability in endpoint.capabilities),
            ("clipboard", "notify", "status"),
        )

    @patch("linkplane.devices.discover_ssh", return_value=None)
    @patch("linkplane.devices.discover_adb")
    @patch("builtins.print")
    def test_discovery_service_returns_typed_result_without_printing(
        self, output, adb_discovery, _ssh_discovery
    ):
        adb_discovery.return_value = [
            Endpoint(
                transport="adb",
                address="serial-1",
                state="connected",
                device_id="phone-1",
                details={"model": "Test Phone"},
            )
        ]
        request = DiscoveryRequest(
            SshTransport(None, None),
            adb_identities=(("serial-1", "phone-1"),),
        )

        result = scan_devices(request)

        self.assertTrue(result.ok)
        self.assertEqual(result.value.devices[0].id, "phone-1")
        self.assertEqual(result.value.devices[0].name, "Test Phone")
        adb_discovery.assert_called_once_with(
            identities={"serial-1": "phone-1"}, allowed_serials=None
        )
        output.assert_not_called()

    @patch("linkplane.devices.discover_ssh", return_value=None)
    @patch("linkplane.devices.discover_adb", side_effect=BridgeError("ADB unavailable"))
    def test_discovery_service_keeps_backend_failure_as_issue(
        self, _adb_discovery, _ssh_discovery
    ):
        existing = DiscoveryIssue("profiles", "invalid profile")

        result = scan_devices(
            DiscoveryRequest(SshTransport(None, None), issues=(existing,))
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            tuple(issue.backend for issue in result.value.issues), ("profiles", "adb")
        )

    @patch("linkplane.devices.discover_adb", side_effect=ValueError("invalid output"))
    def test_discovery_service_returns_typed_operation_failure(self, _adb_discovery):
        result = scan_devices(DiscoveryRequest(SshTransport(None, None)))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "operation_failed")
        self.assertEqual(result.error.message, "invalid output")


if __name__ == "__main__":
    unittest.main()


class DiscoverySeamTests(unittest.TestCase):
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/tool")
    @patch("linkplane.devices.scrcpy_compatibility", return_value=ScrcpyCompatibility(False, False))
    def test_discover_adb_resolves_its_runner_at_call_time(self, _support, _which):
        def runner(command, **_kwargs):
            if command == ["adb", "devices", "-l"]:
                return "List of devices attached\nFAKESERIAL01    no permissions (missing udev rules? user is in the plugdev group); see [https://developer.android.com/tools/device.html] usb:1-2\n"
            raise AssertionError(command)

        with patch("linkplane.devices.run_command", side_effect=runner):
            endpoints = discover_adb()

        self.assertEqual(len(endpoints), 1)
        # The blocked state is carried through verbatim so doctor can diagnose it.
        self.assertEqual(endpoints[0].state, "no permissions")
        self.assertEqual(endpoints[0].error, "ADB device is no permissions")
        self.assertEqual(endpoints[0].details["connection"], "usb")
