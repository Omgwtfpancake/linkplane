import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from linkplane.cli import discovered_devices, get_status, main
from linkplane.doctor import DoctorResult
from linkplane.models import Check, DiscoveryResult
from linkplane.operations import OperationResult
from linkplane.transports import BridgeError


class RoutingTests(unittest.TestCase):
    @patch("linkplane.cli.configured_ssh")
    @patch("linkplane.cli.AdbTransport")
    def test_forced_adb_does_not_load_ssh_configuration(self, adb_class, configured_ssh):
        adb_class.return_value.status.return_value = {
            "transport": "adb",
            "device": {},
            "battery": None,
            "memory": None,
            "storage": None,
            "uptime_seconds": None,
            "issues": [],
        }
        arguments = Namespace(transport="adb", serial=None)

        status = get_status(arguments)

        self.assertEqual(status["transport"], "adb")
        configured_ssh.assert_not_called()

    @patch("linkplane.cli.discover_devices", return_value=DiscoveryResult(()))
    def test_invalid_ssh_port_does_not_hide_adb_discovery(self, discover):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"ssh": {"host": "phone", "user": "termux", "port": "bad"}}),
                encoding="utf-8",
            )
            arguments = Namespace(
                config=str(config_path),
                ssh_host=None,
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
                device_id=None,
            )

            result = discovered_devices(arguments)

        discover.assert_called_once()
        self.assertEqual(result.issues[0].backend, "ssh")
        self.assertIn("invalid SSH port", result.issues[0].error)

    @patch("linkplane.cli.discover_devices", return_value=DiscoveryResult(()))
    def test_ssh_override_does_not_inherit_configured_device_identity(self, discover):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ssh": {
                            "host": "saved-phone",
                            "user": "termux",
                            "device_id": "saved-serial",
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                config=str(config_path),
                ssh_host="different-phone",
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
                device_id=None,
            )

            discovered_devices(arguments)

        self.assertIsNone(discover.call_args.args[1])

    @patch("linkplane.cli.configured_ssh")
    @patch("linkplane.cli.AdbTransport")
    def test_explicit_serial_never_falls_back_to_ssh(self, adb_class, configured_ssh):
        adb_class.return_value.status.side_effect = BridgeError("device not found")
        arguments = Namespace(transport="auto", serial="missing")

        with self.assertRaisesRegex(BridgeError, "device not found"):
            get_status(arguments)

        configured_ssh.assert_not_called()

    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.cli.discovered_devices", return_value=DiscoveryResult(()))
    @patch("linkplane.cli.run_diagnostics")
    def test_doctor_json_preserves_schema_and_error_exit(
        self, diagnostics, _discovery, _profile
    ):
        diagnostics.return_value = OperationResult.success(
            DoctorResult((Check("ADB", "error", "not installed"),))
        )
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["doctor", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["schema_version"], 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["data"]["checks"][0]["name"], "ADB")

    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.cli.discovered_devices", return_value=DiscoveryResult(()))
    @patch("linkplane.cli.run_diagnostics")
    def test_doctor_warning_exits_successfully(
        self, diagnostics, _discovery, _profile
    ):
        diagnostics.return_value = OperationResult.success(
            DoctorResult((Check("Screen", "warning", "scrcpy is not installed"),))
        )
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            "Linkplane Doctor\n[WARN ] Screen: scrcpy is not installed\n",
        )

    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.cli.discovered_devices", return_value=DiscoveryResult(()))
    @patch("linkplane.cli.run_diagnostics")
    def test_doctor_renders_codes_and_problem_summary(self, diagnostics, _discovery, _profile):
        diagnostics.return_value = OperationResult.success(
            DoctorResult(
                (
                    Check("Configuration", "ok", "/tmp/config.json"),
                    Check("Phone reachable", "error", "Connection refused", "start sshd", "LP-CONNECT-001"),
                )
            )
        )
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["doctor"])

        self.assertEqual(exit_code, 1)
        text = output.getvalue()
        self.assertIn("[ERROR] Phone reachable: Connection refused  (LP-CONNECT-001)", text)
        self.assertIn("        Fix: start sshd", text)
        self.assertIn("\n1 problem found.\n", text)
        self.assertIn("Phone reachable: Connection refused  Error: LP-CONNECT-001", text)
        # The CLI hands doctor a provider resolver and the config path.
        request = diagnostics.call_args.args[0]
        self.assertIsNotNone(request.provider_resolver)

    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.cli.discovered_devices", return_value=DiscoveryResult(()))
    @patch("linkplane.cli.run_diagnostics")
    def test_doctor_json_checks_carry_code_field(self, diagnostics, _discovery, _profile):
        diagnostics.return_value = OperationResult.success(
            DoctorResult((Check("Provider", "error", "no ADB device connected", None, "LP-CONNECT-002"),))
        )
        output = StringIO()

        with redirect_stdout(output):
            main(["doctor", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["data"]["checks"][0]["code"], "LP-CONNECT-002")

    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.cli.discovered_devices", return_value=DiscoveryResult(()))
    @patch(
        "linkplane.cli.run_diagnostics",
        return_value=OperationResult.failure("operation_failed", "probe failed"),
    )
    def test_doctor_operation_failure_uses_json_error_path(
        self, _diagnostics, _discovery, _profile
    ):
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["doctor", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "BridgeError")
        self.assertEqual(payload["error"]["message"], "probe failed")


if __name__ == "__main__":
    unittest.main()
