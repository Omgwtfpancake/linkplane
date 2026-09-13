import unittest
from unittest.mock import Mock, patch

from linkplane.dependencies import DependencyPlan
from linkplane.find import (
    FindPhoneRequest,
    build_torch_command,
    locate_phone,
    parse_stream_volume,
    ring_volume_command,
    vibrate_command,
)
from linkplane.transports import BridgeError


class ParserTests(unittest.TestCase):
    def test_parse_stream_volume(self):
        self.assertEqual(
            parse_stream_volume("[V] volume is 3 in range [0..15]"), (3, 15)
        )

    def test_parse_stream_volume_returns_none_when_unparseable(self):
        self.assertIsNone(parse_stream_volume("unexpected output"))

    def test_ring_volume_command_shapes(self):
        self.assertEqual(
            ring_volume_command("serial-1", get=True),
            ["adb", "-s", "serial-1", "shell", "cmd", "media_session", "volume",
             "--stream", "2", "--get"],
        )
        self.assertEqual(
            ring_volume_command("serial-1", index=15, show=True),
            ["adb", "-s", "serial-1", "shell", "cmd", "media_session", "volume",
             "--stream", "2", "--set", "15", "--show"],
        )

    def test_build_torch_command_rounds_and_floors_duration(self):
        command = build_torch_command("serial-1", 0.2)

        self.assertIn("--camera-torch", command)
        self.assertIn("--no-playback", command)
        self.assertEqual(command[-1], "--time-limit=1")

    def test_vibrate_command_shape(self):
        command = vibrate_command("serial-1", 2)

        self.assertEqual(
            command,
            ["adb", "-s", "serial-1", "shell", "cmd", "vibrator_manager", "synced",
             "-f", "-B", "-d", "linkplane-find", "oneshot", "2000"],
        )

    def test_vibrate_command_converts_seconds_to_milliseconds(self):
        command = vibrate_command("serial-1", 0.05)

        self.assertEqual(command[-1], "50")


class LocatePhoneTests(unittest.TestCase):
    def _adb(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        return adb

    def test_rejects_non_positive_duration(self):
        result = locate_phone(FindPhoneRequest(duration=0))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_request")

    def test_rejects_none_of_ring_torch_or_vibrate(self):
        result = locate_phone(
            FindPhoneRequest(ring=False, torch=False, vibrate=False)
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_request")

    def test_dry_run_reports_commands_without_executing(self):
        adb = self._adb()
        command_runner = Mock(return_value="[V] volume is 0 in range [0..15]")
        process_runner = Mock()
        events = []

        result = locate_phone(
            FindPhoneRequest(serial="serial-1", dry_run=True),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=process_runner,
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.value.dry_run)
        self.assertEqual(result.value.device, "Test Phone")
        # The read-only ring-volume probe is allowed to run in dry-run mode...
        command_runner.assert_called_once()
        # ...but nothing that mutates the phone or launches scrcpy is.
        process_runner.assert_not_called()
        self.assertEqual(
            [event.phase for event in events], ["started", "completed"]
        )

    def test_real_run_notifies_raises_ring_flashes_torch_and_restores_volume(self):
        adb = self._adb()
        calls = []

        def command_runner(command, **_kwargs):
            calls.append(command)
            if "--get" in command:
                return "[V] volume is 3 in range [0..15]"
            return ""

        process = Mock(returncode=0)
        process_runner = Mock(return_value=process)
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )
        events = []

        result = locate_phone(
            FindPhoneRequest(serial="serial-1", duration=2),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=process_runner,
            dependency_factory=lambda: dependency,
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.value.ring_applied)
        self.assertTrue(result.value.ring_restored)
        process_runner.assert_called_once()
        torch_command = process_runner.call_args[0][0]
        self.assertIn("--camera-torch", torch_command)
        # notification, ring get, ring set(max=15), vibrate, ring restore(=3)
        self.assertEqual(len(calls), 5)
        self.assertIn(["adb", "-s", "serial-1", "shell", "cmd", "media_session",
                        "volume", "--stream", "2", "--set", "15", "--show"], calls)
        self.assertIn(["adb", "-s", "serial-1", "shell", "cmd", "media_session",
                        "volume", "--stream", "2", "--set", "3"], calls)
        self.assertIn(["adb", "-s", "serial-1", "shell", "cmd", "vibrator_manager",
                        "synced", "-f", "-B", "-d", "linkplane-find", "oneshot", "2000"],
                       calls)
        self.assertEqual(
            [event.phase for event in events],
            ["started", "notified", "ring", "vibrate", "torch", "completed"],
        )

    def test_ring_only_skips_scrcpy_dependency_entirely(self):
        adb = self._adb()

        def command_runner(command, **_kwargs):
            if "--get" in command:
                return "[V] volume is 0 in range [0..15]"
            return ""

        process_runner = Mock()
        dependency_factory = Mock()

        result = locate_phone(
            FindPhoneRequest(serial="serial-1", torch=False),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=process_runner,
            dependency_factory=dependency_factory,
        )

        self.assertTrue(result.ok)
        process_runner.assert_not_called()
        dependency_factory.assert_not_called()
        self.assertIsNone(result.value.torch_command)

    def test_torch_only_skips_ring_volume_probe(self):
        adb = self._adb()
        command_runner = Mock(return_value="")
        process_runner = Mock(return_value=Mock(returncode=0))
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )

        result = locate_phone(
            FindPhoneRequest(serial="serial-1", ring=False, vibrate=False),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=process_runner,
            dependency_factory=lambda: dependency,
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.value.ring_applied)
        self.assertIsNone(result.value.ring_get_command)
        # Only the notification command runs through command_runner.
        command_runner.assert_called_once()

    def test_vibrate_only_skips_ring_and_torch(self):
        adb = self._adb()
        command_runner = Mock(return_value="")
        process_runner = Mock()
        dependency_factory = Mock()

        result = locate_phone(
            FindPhoneRequest(serial="serial-1", ring=False, torch=False),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=process_runner,
            dependency_factory=dependency_factory,
        )

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.value.vibrate_command)
        process_runner.assert_not_called()
        dependency_factory.assert_not_called()
        # notification, then vibrate; no ring probe.
        self.assertEqual(command_runner.call_count, 2)
        vibrate_calls = [
            call for call in command_runner.call_args_list
            if "vibrator_manager" in call.args[0]
        ]
        self.assertEqual(len(vibrate_calls), 1)

    def test_ring_probe_failure_is_non_fatal_and_skips_ring(self):
        adb = self._adb()

        def command_runner(command, **_kwargs):
            if "--get" in command:
                raise BridgeError("device offline")
            return ""

        process_runner = Mock(return_value=Mock(returncode=0))
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )

        result = locate_phone(
            FindPhoneRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=process_runner,
            dependency_factory=lambda: dependency,
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.value.ring_applied)
        self.assertIn("skipping ring", result.value.note)
        process_runner.assert_called_once()

    def test_ring_is_restored_even_when_torch_step_fails(self):
        adb = self._adb()
        set_calls = []

        def command_runner(command, **_kwargs):
            if "--get" in command:
                return "[V] volume is 3 in range [0..15]"
            if "--set" in command:
                set_calls.append(command)
            return ""

        dependency = DependencyPlan("scrcpy", "scrcpy", False, None, None, None)

        result = locate_phone(
            FindPhoneRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=Mock(),
            dependency_factory=lambda: dependency,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        # Raised to max, then restored to the original level, despite the failure.
        self.assertEqual(
            set_calls,
            [
                ["adb", "-s", "serial-1", "shell", "cmd", "media_session", "volume",
                 "--stream", "2", "--set", "15", "--show"],
                ["adb", "-s", "serial-1", "shell", "cmd", "media_session", "volume",
                 "--stream", "2", "--set", "3"],
            ],
        )

    def test_missing_scrcpy_returns_typed_dependency_error(self):
        adb = self._adb()
        command_runner = Mock(return_value="[V] volume is 0 in range [0..15]")
        dependency = DependencyPlan("scrcpy", "scrcpy", False, None, None, None)

        result = locate_phone(
            FindPhoneRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            command_runner=command_runner,
            process_runner=Mock(),
            dependency_factory=lambda: dependency,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")

    def test_transport_unavailable_when_device_cannot_be_selected(self):
        adb = Mock()
        adb.select_device.side_effect = BridgeError("no ADB device connected")

        result = locate_phone(
            FindPhoneRequest(),
            adb_factory=lambda _serial: adb,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "transport_unavailable")


if __name__ == "__main__":
    unittest.main()


class DependencySeamTests(unittest.TestCase):
    """`locate_phone` must resolve its transport and runner at call time.

    Regression: the defaults used to be bound at import time (`= AdbTransport`,
    `= run_command`), so patching the module symbols -- the seam every CLI-level test
    relies on -- was a no-op and the find `--json` envelope test reached real ADB. It
    passed with the phone plugged in and failed without it (2026-09-12).
    """

    @patch("linkplane.find.run_command")
    @patch("linkplane.find.AdbTransport")
    def test_module_symbol_patches_reach_locate_phone(self, adb_class, run_command):
        adb_class.return_value.serial = "fake-serial"
        adb_class.return_value.select_device.return_value = {"model": "Test_Phone"}
        run_command.return_value = "[V] volume is 7 in range [0..15]"

        result = locate_phone(FindPhoneRequest(dry_run=True))

        self.assertIsNone(result.error)
        self.assertEqual(result.value.serial, "fake-serial")
        self.assertEqual(result.value.device, "Test Phone")
        adb_class.assert_called_once_with(None)
        run_command.assert_called_once_with(ring_volume_command("fake-serial", get=True))

    @patch("linkplane.find.run_command")
    @patch("linkplane.find.AdbTransport")
    def test_explicit_injection_still_wins_over_module_symbols(self, adb_class, run_command):
        transport = Mock()
        transport.serial = "injected-serial"
        transport.select_device.return_value = {"model": "Injected"}
        runner = Mock(return_value="[V] volume is 3 in range [0..15]")

        result = locate_phone(
            FindPhoneRequest(dry_run=True), adb_factory=lambda serial: transport,
            command_runner=runner,
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.value.serial, "injected-serial")
        adb_class.assert_not_called()
        run_command.assert_not_called()
