import unittest
from unittest.mock import Mock, patch

from linkplane.audio import (
    AUDIO_SOURCES,
    AudioRequest,
    build_audio_command,
    launch_audio,
)
from linkplane.dependencies import DependencyPlan, ScrcpyCompatibility
from linkplane.transports import BridgeError


class AudioCommandTests(unittest.TestCase):
    def test_no_video_and_audio_source_for_every_source(self):
        for source in AUDIO_SOURCES:
            command = build_audio_command("serial-1", source)
            self.assertEqual(command[0:3], ["scrcpy", "--serial", "serial-1"])
            self.assertIn("--no-video", command)
            self.assertIn(f"--audio-source={source}", command)

    def test_rejects_unsupported_source(self):
        with self.assertRaises(BridgeError):
            build_audio_command("serial-1", "bogus-source")

    def test_codec_option(self):
        command = build_audio_command("serial-1", "mic", codec="flac")
        self.assertIn("--audio-codec=flac", command)

    def test_rejects_unsupported_codec(self):
        with self.assertRaises(BridgeError):
            build_audio_command("serial-1", "playback", codec="mp3")

    def test_record_with_audio_extension_infers_format(self):
        command = build_audio_command("serial-1", "playback", record="~/clip.m4a")
        self.assertIn("--record", command)
        self.assertTrue(
            command[command.index("--record") + 1].endswith("/clip.m4a")
        )
        self.assertIn("--record-format=m4a", command)

    def test_record_with_explicit_format(self):
        command = build_audio_command(
            "serial-1", "playback", record="~/clip.bin", record_format="wav"
        )
        self.assertIn("--record-format=wav", command)

    def test_record_with_video_extension_and_no_explicit_format_rejected(self):
        with self.assertRaises(BridgeError):
            build_audio_command("serial-1", "playback", record="~/clip.mkv")

    def test_record_with_explicit_video_format_rejected(self):
        with self.assertRaises(BridgeError):
            build_audio_command(
                "serial-1", "playback", record="~/clip.mp4", record_format="mp4"
            )

    def test_extra_arguments_passthrough(self):
        command = build_audio_command(
            "serial-1", "playback", extra_arguments=["--turn-screen-off"]
        )
        self.assertEqual(command[-1], "--turn-screen-off")


class AudioServiceTests(unittest.TestCase):
    def test_audio_service_returns_typed_dry_run_without_dependency_install(self):
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

        result = launch_audio(
            AudioRequest(
                serial="serial-1",
                source="mic",
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
        self.assertEqual(result.value.source, "mic")
        self.assertEqual(result.value.command[-1], "--turn-screen-off")
        self.assertFalse(result.value.dependency.available)
        self.assertEqual(
            [event.phase for event in events], ["started", "command", "completed"]
        )
        dependency_handler.assert_not_called()

    def test_audio_service_returns_typed_missing_dependency(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan("scrcpy", "scrcpy", False, None, None, None)
        process_runner = Mock()

        result = launch_audio(
            AudioRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            process_runner=process_runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        process_runner.assert_not_called()

    def test_audio_service_returns_process_exit_code(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )
        process = Mock(returncode=7)

        result = launch_audio(
            AudioRequest(serial="serial-1", source="output"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            compatibility_factory=lambda _plan: ScrcpyCompatibility(True, True, True),
            process_runner=Mock(return_value=process),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.exit_code, 7)
        self.assertIn("--audio-source=output", result.value.command)

    def test_audio_service_rejects_incompatible_scrcpy(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        dependency = DependencyPlan(
            "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
        )
        process_runner = Mock()

        result = launch_audio(
            AudioRequest(serial="serial-1"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: dependency,
            compatibility_factory=lambda _plan: ScrcpyCompatibility(
                True,
                True,
                False,
                missing_audio_options=("--audio-source",),
            ),
            process_runner=process_runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        self.assertIn("--audio-source", result.error.message)
        process_runner.assert_not_called()

    def test_audio_service_rejects_invalid_source_before_transport_selection(self):
        adb_factory = Mock()

        result = launch_audio(
            AudioRequest(serial="serial-1", source="bogus"),
            adb_factory=adb_factory,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_request")
        adb_factory.assert_not_called()

    def test_audio_service_rejects_bad_record_extension(self):
        adb = Mock(serial="serial-1")
        adb.select_device.return_value = {"model": "Test_Phone"}

        result = launch_audio(
            AudioRequest(serial="serial-1", record="~/clip.mkv"),
            adb_factory=lambda _serial: adb,
            dependency_factory=lambda: DependencyPlan(
                "scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_request")


if __name__ == "__main__":
    unittest.main()
