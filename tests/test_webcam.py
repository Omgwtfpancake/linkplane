import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from linkplane.dependencies import DependencyPlan, ScrcpyCompatibility
from linkplane.webcam import (
    WebcamStartRequest,
    WebcamStopRequest,
    build_webcam_command,
    load_webcam_state,
    parse_v4l2loopback_devices,
    resolve_webcam_device,
    save_webcam_state,
    start_webcam,
    stop_webcam,
)


LOOPBACK_LIST_DEVICES = """Dummy video device (0x0000) (platform:v4l2loopback-000):
\t/dev/video4
\t/dev/video5

USB Camera: USB Camera (usb-0000:00:14.0-1):
\t/dev/video0
\t/dev/video1
"""


AVAILABLE_SCRCPY = DependencyPlan("scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None)
AVAILABLE_LOOPBACK = DependencyPlan("v4l2loopback", "v4l2loopback-dkms", True, None, None, None)
MISSING_LOOPBACK = DependencyPlan(
    "v4l2loopback", "v4l2loopback-dkms", False, None, "pacman",
    ("sudo", "pacman", "-S", "--needed", "v4l2loopback-dkms"),
)
FULL_COMPATIBILITY = ScrcpyCompatibility(True, True, webcam_supported=True)


class WebcamCommandTests(unittest.TestCase):
    def test_build_command_defaults_to_back_camera(self):
        command = build_webcam_command("serial-1", "/dev/video4")

        self.assertEqual(command[0:3], ["scrcpy", "--serial", "serial-1"])
        self.assertIn("--video-source=camera", command)
        self.assertIn("--v4l2-sink=/dev/video4", command)
        self.assertIn("--no-playback", command)
        self.assertIn("--camera-facing=back", command)

    def test_build_command_prefers_explicit_camera_id(self):
        command = build_webcam_command(
            "serial-1", "/dev/video4", facing="front", camera_id="2"
        )

        self.assertIn("--camera-id=2", command)
        self.assertNotIn("--camera-facing=front", command)

    def test_build_command_appends_extra_arguments(self):
        command = build_webcam_command(
            "serial-1", "/dev/video4", extra_arguments=["--camera-fps=30"]
        )

        self.assertEqual(command[-1], "--camera-fps=30")


class ParseLoopbackDevicesTests(unittest.TestCase):
    def test_only_loopback_devices_are_returned(self):
        devices = parse_v4l2loopback_devices(LOOPBACK_LIST_DEVICES)

        self.assertEqual(devices, ["/dev/video4", "/dev/video5"])

    def test_no_loopback_header_returns_empty(self):
        devices = parse_v4l2loopback_devices(
            "USB Camera: USB Camera (usb-0000:00:14.0-1):\n\t/dev/video0\n"
        )

        self.assertEqual(devices, [])


class ResolveWebcamDeviceTests(unittest.TestCase):
    def test_explicit_device_number_skips_probing(self):
        runner = Mock()

        device = resolve_webcam_device(7, command_runner=runner)

        self.assertEqual(device, "/dev/video7")
        runner.assert_not_called()

    def test_auto_detects_first_loopback_device(self):
        runner = Mock(return_value=LOOPBACK_LIST_DEVICES)

        device = resolve_webcam_device(None, command_runner=runner)

        self.assertEqual(device, "/dev/video4")
        runner.assert_called_once_with(["v4l2-ctl", "--list-devices"], timeout=10)

    def test_no_loopback_device_found_is_actionable(self):
        runner = Mock(return_value="USB Camera:\n\t/dev/video0\n")

        with self.assertRaisesRegex(Exception, "no v4l2loopback sink device"):
            resolve_webcam_device(None, command_runner=runner)


class WebcamStateFileTests(unittest.TestCase):
    def test_round_trips_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "webcam.json")

            save_webcam_state({"pid": 4321, "device": "/dev/video4"}, path)
            state = load_webcam_state(path)

            self.assertEqual(state, {"pid": 4321, "device": "/dev/video4"})
            mode = Path(path).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_missing_state_file_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "missing.json")

            self.assertIsNone(load_webcam_state(path))


class StartWebcamTests(unittest.TestCase):
    def _adb(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        return adb

    def test_dry_run_reports_command_without_touching_state_or_process(self):
        adb = self._adb()
        events = []
        state_saver = Mock()
        process_factory = Mock()

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "webcam.json")
            result = start_webcam(
                WebcamStartRequest(serial="serial-1", device=4, dry_run=True, state_path=state_path),
                adb_factory=lambda _serial: adb,
                scrcpy_dependency_factory=lambda: AVAILABLE_SCRCPY,
                loopback_dependency_factory=lambda: AVAILABLE_LOOPBACK,
                state_saver=state_saver,
                process_factory=process_factory,
                progress=events.append,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.video_device, "/dev/video4")
        self.assertIn("--v4l2-sink=/dev/video4", result.value.command)
        self.assertIsNone(result.value.pid)
        self.assertEqual(
            [event.phase for event in events], ["started", "command", "completed"]
        )
        state_saver.assert_not_called()
        process_factory.assert_not_called()

    def test_missing_loopback_dependency_is_reported_without_launching(self):
        adb = self._adb()
        process_factory = Mock()

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "webcam.json")
            result = start_webcam(
                WebcamStartRequest(serial="serial-1", device=4, state_path=state_path),
                adb_factory=lambda _serial: adb,
                scrcpy_dependency_factory=lambda: AVAILABLE_SCRCPY,
                loopback_dependency_factory=lambda: MISSING_LOOPBACK,
                process_factory=process_factory,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        self.assertIn("v4l2loopback", result.error.message)
        process_factory.assert_not_called()

    def test_start_persists_pid_and_device_to_state_file(self):
        adb = self._adb()
        process = Mock(pid=54321)
        process_factory = Mock(return_value=process)

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "webcam.json")
            result = start_webcam(
                WebcamStartRequest(serial="serial-1", device=4, state_path=state_path),
                adb_factory=lambda _serial: adb,
                scrcpy_dependency_factory=lambda: AVAILABLE_SCRCPY,
                loopback_dependency_factory=lambda: AVAILABLE_LOOPBACK,
                compatibility_factory=lambda _plan: FULL_COMPATIBILITY,
                process_factory=process_factory,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.value.pid, 54321)
            process_factory.assert_called_once()
            _, call_kwargs = process_factory.call_args
            self.assertTrue(call_kwargs.get("start_new_session"))

            stopped = stop_webcam(
                WebcamStopRequest(state_path=state_path),
                process_alive=lambda _pid: False,
            )

        self.assertTrue(stopped.ok)
        self.assertEqual(stopped.value.pid, 54321)
        self.assertEqual(stopped.value.video_device, "/dev/video4")

    def test_second_start_while_running_is_rejected(self):
        adb = self._adb()
        process_factory = Mock()

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "webcam.json")
            save_webcam_state({"pid": 111, "device": "/dev/video4", "serial": "serial-1"}, state_path)

            result = start_webcam(
                WebcamStartRequest(serial="serial-1", device=4, state_path=state_path),
                adb_factory=lambda _serial: adb,
                scrcpy_dependency_factory=lambda: AVAILABLE_SCRCPY,
                loopback_dependency_factory=lambda: AVAILABLE_LOOPBACK,
                process_alive=lambda _pid: True,
                process_factory=process_factory,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "already_running")
        self.assertIn("111", result.error.message)
        process_factory.assert_not_called()

    def test_stale_state_does_not_block_a_new_start(self):
        adb = self._adb()
        process = Mock(pid=222)
        process_factory = Mock(return_value=process)

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "webcam.json")
            save_webcam_state({"pid": 111, "device": "/dev/video4", "serial": "serial-1"}, state_path)

            result = start_webcam(
                WebcamStartRequest(serial="serial-1", device=4, state_path=state_path),
                adb_factory=lambda _serial: adb,
                scrcpy_dependency_factory=lambda: AVAILABLE_SCRCPY,
                loopback_dependency_factory=lambda: AVAILABLE_LOOPBACK,
                compatibility_factory=lambda _plan: FULL_COMPATIBILITY,
                process_alive=lambda _pid: False,
                process_factory=process_factory,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.pid, 222)


class StopWebcamTests(unittest.TestCase):
    def test_stop_with_no_tracked_instance_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "missing.json")

            result = stop_webcam(WebcamStopRequest(state_path=state_path))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "not_found")

    def test_stop_terminates_the_tracked_pid_and_clears_state(self):
        terminator = Mock()

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "webcam.json")
            save_webcam_state(
                {"pid": 999, "device": "/dev/video4", "serial": "serial-1"}, state_path
            )

            result = stop_webcam(
                WebcamStopRequest(state_path=state_path),
                process_alive=lambda _pid: True,
                process_terminator=terminator,
            )

            self.assertTrue(result.ok)
            terminator.assert_called_once_with(999)
            self.assertIsNone(load_webcam_state(state_path))

    def test_dry_run_stop_does_not_terminate_or_clear_state(self):
        terminator = Mock()

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "webcam.json")
            save_webcam_state(
                {"pid": 999, "device": "/dev/video4", "serial": "serial-1"}, state_path
            )

            result = stop_webcam(
                WebcamStopRequest(state_path=state_path, dry_run=True),
                process_terminator=terminator,
            )

            self.assertTrue(result.ok)
            terminator.assert_not_called()
            self.assertIsNotNone(load_webcam_state(state_path))


if __name__ == "__main__":
    unittest.main()
