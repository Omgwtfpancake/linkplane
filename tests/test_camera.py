import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from linkplane.camera import (
    CameraPreviewRequest,
    CaptureRequest,
    build_preview_command,
    capture_camera_photo,
    capture_photo,
    decode_photo,
    preview_phone_camera,
    resolve_capture_path,
)
from linkplane.dependencies import DependencyPlan, ScrcpyCompatibility
from linkplane.transports import BridgeError, SshTransport


class CameraTests(unittest.TestCase):
    def test_preview_command_selects_facing_and_preset(self):
        command = build_preview_command(
            "serial-1",
            "motion",
            facing="front",
            camera_id=None,
            audio=False,
            torch=True,
            record="~/camera.mp4",
            extra_arguments=["--orientation=90"],
        )

        self.assertEqual(command[:4], ["scrcpy", "--serial", "serial-1", "--video-source=camera"])
        self.assertIn("--camera-facing=front", command)
        self.assertIn("--camera-fps=60", command)
        self.assertIn("--no-audio", command)
        self.assertIn("--camera-torch", command)
        self.assertEqual(command[-1], "--orientation=90")

    def test_preview_command_uses_id_instead_of_facing(self):
        command = build_preview_command(
            "serial-1",
            "balanced",
            facing=None,
            camera_id="2",
            audio=True,
            torch=False,
            record=None,
        )

        self.assertIn("--camera-id=2", command)
        self.assertFalse(any(value.startswith("--camera-facing") for value in command))

    def test_decode_photo_requires_complete_jpeg(self):
        photo = b"\xff\xd8linkplane\xff\xd9"

        self.assertEqual(decode_photo(base64.b64encode(photo).decode()), photo)
        with self.assertRaisesRegex(BridgeError, "complete JPEG"):
            decode_photo(base64.b64encode(b"not a jpeg").decode())

    @patch("builtins.print")
    def test_capture_transfers_photo_and_cleans_remote_file(self, _print):
        photo = b"\xff\xd8linkplane\xff\xd9"
        runner = Mock(side_effect=["", base64.b64encode(photo).decode(), ""])
        transport = SshTransport("phone.local", "termux", runner=runner)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jpg"
            arguments = SimpleNamespace(
                camera_id=0,
                output=str(output),
                force=False,
                dry_run=False,
                no_foreground=True,
            )

            self.assertEqual(capture_photo(arguments, transport, None), 0)
            self.assertEqual(output.read_bytes(), photo)

        self.assertIn("termux-camera-photo", runner.call_args_list[0].args[0][-1])
        self.assertIn("base64", runner.call_args_list[1].args[0][-1])
        self.assertIn("rm -f", runner.call_args_list[2].args[0][-1])

    @patch("linkplane.camera.foreground_termux")
    @patch("builtins.print")
    def test_capture_dry_run_does_not_use_camera(self, _print, foreground):
        runner = Mock()
        transport = SshTransport("phone.local", "termux", runner=runner)
        arguments = SimpleNamespace(
            camera_id=1,
            output=None,
            force=False,
            dry_run=True,
            no_foreground=False,
        )

        self.assertEqual(capture_photo(arguments, transport, "serial-1"), 0)

        runner.assert_not_called()
        foreground.assert_not_called()

    def test_capture_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jpg"
            output.write_bytes(b"existing")

            with self.assertRaisesRegex(BridgeError, "already exists"):
                resolve_capture_path(str(output), force=False)

    def test_capture_service_returns_typed_result_and_progress(self):
        photo = b"\xff\xd8linkplane\xff\xd9"
        runner = Mock(side_effect=["", base64.b64encode(photo).decode(), ""])
        transport = SshTransport("phone.local", "termux", runner=runner)
        events = []

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jpg"
            result = capture_camera_photo(
                CaptureRequest(output=str(output), foreground=False),
                transport,
                None,
                progress=events.append,
            )

            self.assertEqual(output.read_bytes(), photo)

        self.assertTrue(result.ok)
        self.assertEqual(result.value.transport, "ssh")
        self.assertEqual(result.value.bytes_written, len(photo))
        self.assertEqual(
            [event.phase for event in events],
            ["started", "capturing", "transferring", "completed"],
        )

    @patch("builtins.print")
    def test_capture_service_dry_run_is_silent_and_has_command_metadata(self, output):
        runner = Mock()
        transport = SshTransport("phone.local", "termux", runner=runner)
        events = []

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "capture.jpg"
            result = capture_camera_photo(
                CaptureRequest(output=str(destination), camera_id=2, dry_run=True),
                transport,
                "serial-1",
                progress=events.append,
            )

            self.assertFalse(destination.exists())

        self.assertTrue(result.ok)
        self.assertEqual(result.value.camera_id, 2)
        self.assertIn("termux-camera-photo", result.value.command[-1])
        self.assertEqual(
            [event.phase for event in events],
            ["started", "command", "foreground", "completed"],
        )
        runner.assert_not_called()
        output.assert_not_called()

    def test_preview_service_returns_typed_dry_run(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan("scrcpy", "scrcpy", False, None, None, None)
        events = []

        result = preview_phone_camera(
            CameraPreviewRequest(
                serial="serial-1",
                quality="motion",
                facing="front",
                torch=True,
                dry_run=True,
            ),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.device, "Test Phone")
        self.assertEqual(result.value.selection, "front")
        self.assertIn("--camera-torch", result.value.command)
        self.assertEqual(
            [event.phase for event in events], ["started", "command", "completed"]
        )

    def test_preview_service_returns_typed_missing_dependency(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan("scrcpy", "scrcpy", False, None, None, None)

        result = preview_phone_camera(
            CameraPreviewRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")

    def test_preview_service_rejects_scrcpy_without_camera_support(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )
        process_runner = Mock()

        result = preview_phone_camera(
            CameraPreviewRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            compatibility_factory=lambda _plan: ScrcpyCompatibility(
                True,
                False,
                missing_camera_options=("--video-source",),
            ),
            process_runner=process_runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        self.assertIn("--video-source", result.error.message)
        process_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
