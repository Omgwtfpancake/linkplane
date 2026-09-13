import unittest
from unittest.mock import Mock, patch

from linkplane.cli import print_status
from linkplane.status import StatusRequest, StatusResult, read_status
from linkplane.transports import BridgeError


def raw_status(transport="adb"):
    return {
        "transport": transport,
        "device": {
            "serial": "serial-1" if transport == "adb" else None,
            "manufacturer": "Example",
            "model": "Phone",
            "android": "16",
            "kernel": "6.1",
            "product": "example",
        },
        "battery": None,
        "memory": {
            "total_bytes": 8192000,
            "available_bytes": 2048000,
            "used_percent": 75.0,
        },
        "storage": None,
        "uptime_seconds": 3600,
        "issues": [{"component": "battery", "error": "permission denied"}],
    }


class StatusServiceTests(unittest.TestCase):
    def test_status_service_returns_typed_partial_result(self):
        adb = Mock()
        adb.status.return_value = raw_status()

        result = read_status(StatusRequest(transport="adb"), adb_factory=lambda _serial: adb)

        self.assertTrue(result.ok)
        self.assertIsInstance(result.value, StatusResult)
        self.assertIsNone(result.value.battery)
        self.assertEqual(result.value.memory["used_percent"], 75.0)
        self.assertEqual(result.value.issues[0].component, "battery")
        self.assertIsNone(result.error)
        # Provenance from the provider layer (additive fields).
        self.assertEqual(result.operation, "device.status")
        self.assertEqual(result.provider, "adb")

    def test_profile_serial_can_fallback_but_explicit_serial_cannot(self):
        adb = Mock()
        adb.status.side_effect = BridgeError("ADB disconnected")
        ssh = Mock()
        ssh.status.return_value = raw_status("ssh")
        ssh_factory = Mock(return_value=ssh)

        strict = read_status(
            StatusRequest(transport="auto", serial="serial-1"),
            adb_factory=lambda _serial: adb,
            ssh_factory=ssh_factory,
        )
        fallback = read_status(
            StatusRequest(
                transport="auto", serial="serial-1", serial_from_profile=True
            ),
            adb_factory=lambda _serial: adb,
            ssh_factory=ssh_factory,
        )

        self.assertFalse(strict.ok)
        self.assertEqual(strict.error.code, "transport_unavailable")
        self.assertTrue(strict.error.error_code.startswith("LP-"))
        self.assertTrue(fallback.ok)
        self.assertEqual(fallback.value.transport, "ssh")
        self.assertEqual(fallback.provider, "ssh")
        ssh_factory.assert_called_once_with()

    def test_malformed_provider_output_keeps_frozen_category_with_precise_code(self):
        adb = Mock()
        adb.status.return_value = {"transport": "adb"}  # missing every required key

        result = read_status(StatusRequest(transport="adb"), adb_factory=lambda _serial: adb)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "transport_unavailable")
        self.assertEqual(result.error.error_code, "LP-PROVIDER-002")

    def test_auto_with_nothing_available_combines_both_reasons(self):
        adb = Mock()
        adb.status.side_effect = BridgeError("no ADB device connected")

        result = read_status(StatusRequest(transport="auto"), adb_factory=lambda _serial: adb)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "transport_unavailable")
        self.assertIn("ADB unavailable (no ADB device connected)", result.error.message)
        self.assertIn("SSH unavailable (SSH transport is not configured)", result.error.message)
        self.assertEqual(result.error.error_code, "LP-CONNECT-001")
        self.assertTrue(result.error.hints)

    @patch("builtins.print")
    def test_status_renderer_shows_unavailable_sections_and_warning(self, output):
        print_status(raw_status())

        lines = [call.args[0] for call in output.call_args_list]
        self.assertIn("Battery     unavailable", lines)
        self.assertIn("Storage     unavailable", lines)
        self.assertIn("Warning     battery: permission denied", lines)


if __name__ == "__main__":
    unittest.main()
