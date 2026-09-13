import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from linkplane.notification import (
    NotificationRequest,
    adb_notification_command,
    notify,
    notify_phone,
    send_adb_notification,
    send_ssh_notification,
)
from linkplane.transports import BridgeError, SshTransport


class NotificationTests(unittest.TestCase):
    def test_adb_command_quotes_user_content(self):
        command = adb_notification_command(
            "serial-1",
            "Dinner's ready; enjoy!",
            title="Family phone",
            notification_id=42,
        )

        self.assertEqual(command[:4], ["adb", "-s", "serial-1", "shell"])
        self.assertIn("linkplane-42", command[4])
        self.assertIn("'Family phone'", command[4])
        self.assertIn("'Dinner'", command[4])

    @patch("linkplane.notification.run_command")
    @patch("builtins.print")
    def test_adb_notification_posts(self, _print, run_command):
        arguments = SimpleNamespace(
            serial=None,
            message="Hello",
            title="Linkplane",
            notification_id=7,
            dry_run=False,
        )
        transport = Mock()
        transport.serial = "serial-1"
        transport.select_device.return_value = {"model": "Test_Phone"}

        result = send_adb_notification(arguments, transport)

        self.assertEqual(result, 0)
        run_command.assert_called_once()

    @patch("builtins.print")
    def test_ssh_notification_uses_termux_api(self, _print):
        arguments = SimpleNamespace(
            message="Hello",
            title="Linkplane",
            notification_id=7,
            dry_run=False,
        )
        runner = Mock(return_value="")
        transport = SshTransport("phone.local", "termux", runner=runner)

        result = send_ssh_notification(arguments, transport)

        self.assertEqual(result, 0)
        remote_command = runner.call_args.args[0][-1]
        self.assertIn("termux-notification", remote_command)
        self.assertIn("--id 7", remote_command)

    @patch("linkplane.notification.AdbTransport")
    def test_explicit_serial_never_falls_back_to_ssh(self, adb_class):
        arguments = SimpleNamespace(
            transport="auto",
            serial="missing",
            message="Hello",
            title="Linkplane",
            notification_id=7,
            dry_run=False,
        )
        adb_class.return_value.select_device.side_effect = BridgeError("device not found")
        ssh_factory = Mock()

        with self.assertRaisesRegex(BridgeError, "device not found"):
            notify(arguments, ssh_factory)

        ssh_factory.assert_not_called()

    @patch("builtins.print")
    def test_notification_service_returns_typed_dry_run_and_events(self, output):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        events = []

        result = notify_phone(
            NotificationRequest(
                message="Hello",
                title="Build complete",
                notification_id=9,
                transport="adb",
                dry_run=True,
            ),
            adb_factory=lambda _serial: adb,
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.transport, "adb")
        self.assertEqual(result.value.device, "Test Phone")
        self.assertIn("linkplane-9", result.value.command[-1])
        self.assertEqual(
            [event.phase for event in events], ["started", "command", "completed"]
        )
        output.assert_not_called()

    @patch("linkplane.notification.run_command", side_effect=BridgeError("post failed"))
    def test_delivery_failure_never_falls_back_after_adb_selection(self, _run):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        ssh_factory = Mock()

        result = notify_phone(
            NotificationRequest(message="Hello", transport="auto"),
            adb_factory=lambda _serial: adb,
            ssh_factory=ssh_factory,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "operation_failed")
        self.assertEqual(result.error.message, "post failed")
        ssh_factory.assert_not_called()

    def test_profile_adb_endpoint_can_fallback_to_associated_ssh(self):
        adb = Mock()
        adb.select_device.side_effect = BridgeError("ADB offline")
        ssh = SshTransport("phone.local", "termux", runner=Mock(return_value=""))

        result = notify_phone(
            NotificationRequest(
                message="Hello",
                transport="auto",
                serial="serial-1",
                serial_from_profile=True,
                dry_run=True,
            ),
            adb_factory=lambda _serial: adb,
            ssh_factory=lambda: ssh,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.transport, "ssh")
        self.assertEqual(result.value.device, "termux@phone.local")


if __name__ == "__main__":
    unittest.main()
