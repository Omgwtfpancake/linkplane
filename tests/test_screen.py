import unittest
from unittest.mock import Mock, patch

from linkplane.dependencies import DependencyPlan, ScrcpyCompatibility
from linkplane.screen import (
    ScreenRequest,
    build_screen_command,
    installation_command,
    launch_screen,
)


class ScreenTests(unittest.TestCase):
    def test_high_quality_command(self):
        command = build_screen_command("serial-1", "high")

        self.assertEqual(command[0:3], ["scrcpy", "--serial", "serial-1"])
        self.assertIn("--max-size=1920", command)
        self.assertIn("--max-fps=60", command)
        self.assertIn("--video-bit-rate=12M", command)

    def test_game_command_enables_low_latency_gamepad(self):
        command = build_screen_command("serial-1", "game")

        self.assertIn("--max-fps=120", command)
        self.assertIn("--video-buffer=0", command)
        self.assertIn("--gamepad=uhid", command)

    def test_options_and_passthrough(self):
        command = build_screen_command(
            "serial-1",
            "low",
            audio=False,
            record="~/recording.mkv",
            extra_arguments=["--turn-screen-off"],
        )

        self.assertIn("--no-audio", command)
        self.assertIn("--record", command)
        self.assertTrue(command[command.index("--record") + 1].endswith("/recording.mkv"))
        self.assertEqual(command[-1], "--turn-screen-off")

    @patch("linkplane.dependencies.shutil.which")
    def test_arch_installation_command(self, which):
        which.side_effect = lambda command: (
            f"/usr/bin/{command}" if command in {"pacman", "sudo"} else None
        )

        self.assertEqual(
            installation_command(),
            ["sudo", "pacman", "-S", "--needed", "scrcpy"],
        )

    def test_screen_service_returns_typed_dry_run_without_dependency_install(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan(
            "scrcpy",
            "scrcpy",
            False,
            None,
            "apt-get",
            ("sudo", "apt-get", "install", "scrcpy"),
        )
        dependency_handler = Mock()
        events = []

        result = launch_screen(
            ScreenRequest(
                serial="serial-1",
                quality="high",
                extra_arguments=("--", "--turn-screen-off"),
                dry_run=True,
            ),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            dependency_handler=dependency_handler,
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.device, "Test Phone")
        self.assertEqual(result.value.quality, "high")
        self.assertEqual(result.value.command[-1], "--turn-screen-off")
        self.assertFalse(result.value.dependency.available)
        self.assertEqual(
            [event.phase for event in events], ["started", "command", "completed"]
        )
        dependency_handler.assert_not_called()

    def test_screen_service_returns_typed_missing_dependency(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan("scrcpy", "scrcpy", False, None, None, None)
        process_runner = Mock()

        result = launch_screen(
            ScreenRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            process_runner=process_runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        process_runner.assert_not_called()

    def test_screen_service_returns_process_exit_code(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )
        process = Mock(returncode=7)

        result = launch_screen(
            ScreenRequest(serial="serial-1", audio=False),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            compatibility_factory=lambda _plan: ScrcpyCompatibility(True, True),
            process_runner=Mock(return_value=process),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.exit_code, 7)
        self.assertIn("--no-audio", result.value.command)

    def test_screen_service_rejects_incompatible_scrcpy(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )
        process_runner = Mock()

        result = launch_screen(
            ScreenRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            compatibility_factory=lambda _plan: ScrcpyCompatibility(
                False,
                False,
                missing_screen_options=("--video-bit-rate",),
            ),
            process_runner=process_runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        self.assertIn("--video-bit-rate", result.error.message)
        process_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
