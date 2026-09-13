"""Cooperative cancellation contract (`linkplane.operations.CancellationToken`).

Each long-running service accepts `cancel=` and, when the token is set, stops at its next
boundary, emits a `("<operation>", "cancelled")` progress event describing how far it got,
and returns `OperationResult.failure("cancelled", ...)`. Clipboard `sync` is the documented
exception: stopping is its normal completion, so a cancelled sync succeeds with its count.
The CLI maps the first Ctrl+C onto the token and exits 130.
"""

import hashlib
import json
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from linkplane.backup import MANIFEST_NAME, BackupRequest, backup_photos
from linkplane.cli import EXIT_CANCELLED, main
from linkplane.clipboard import (
    ClipboardRequest,
    DesktopClipboard,
    sync_clipboards,
    use_clipboard,
)
from linkplane.operations import (
    CancellationToken,
    OperationCancelled,
    OperationResult,
    cancel_on_interrupt,
    check_cancelled,
)
from linkplane.profiles import (
    DeviceProfile,
    EndpointRefreshRequest,
    refresh_profile_endpoints,
    save_config,
    scan_for_profile,
)
from linkplane.transfer import SendRequest, send_files
from linkplane.transports import BridgeError, SshTransport


class CancellationTokenTests(unittest.TestCase):
    def test_token_starts_clear_and_records_first_reason(self):
        token = CancellationToken()

        self.assertFalse(token.cancelled)
        self.assertIsNone(token.reason)
        check_cancelled(token)  # no-op while clear
        check_cancelled(None)  # and when no token was passed at all

        token.cancel("first")
        token.cancel("second")

        self.assertTrue(token.cancelled)
        self.assertEqual(token.reason, "first")
        with self.assertRaisesRegex(OperationCancelled, "first"):
            token.raise_if_cancelled()

    def test_wait_returns_early_once_cancelled(self):
        token = CancellationToken()
        self.assertFalse(token.wait(0.01))
        token.cancel()
        self.assertTrue(token.wait(30))

    def test_cancel_on_interrupt_routes_first_sigint_to_token_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGINT)
        token = CancellationToken()

        with cancel_on_interrupt(token) as yielded:
            self.assertIs(yielded, token)
            signal.raise_signal(signal.SIGINT)  # would raise KeyboardInterrupt by default
            self.assertTrue(token.cancelled)
            self.assertIn("Ctrl+C again", token.reason)
            # The first interrupt hands SIGINT back to the previous handler immediately,
            # so a second Ctrl+C behaves normally.
            self.assertIs(signal.getsignal(signal.SIGINT), previous)

        self.assertIs(signal.getsignal(signal.SIGINT), previous)


class SendCancellationTests(unittest.TestCase):
    def _sources(self, directory: str, count: int) -> tuple[str, ...]:
        paths = []
        for index in range(count):
            source = Path(directory) / f"file{index}.txt"
            source.write_text("x", encoding="utf-8")
            paths.append(str(source))
        return tuple(paths)

    @patch("linkplane.transfer.AdbTransport")
    @patch("linkplane.transfer.subprocess.run")
    def test_send_stops_between_items_after_cancel(self, run, transport_class):
        run.return_value.returncode = 0
        transport_class.return_value.select_device.return_value = {"model": "Test_Phone"}
        transport_class.return_value.serial = "serial-1"
        token = CancellationToken()
        events = []

        def progress(event):
            events.append(event)
            if event.phase == "item_completed":
                token.cancel("stop now")

        with tempfile.TemporaryDirectory() as directory:
            result = send_files(
                SendRequest(paths=self._sources(directory, 3), transport="adb"),
                progress=progress,
                cancel=token,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "cancelled")
        self.assertEqual(result.error.message, "stop now")
        self.assertEqual(
            [event.phase for event in events],
            ["started", "item_started", "item_completed", "cancelled"],
        )
        self.assertEqual((events[-1].current, events[-1].total), (1, 3))
        # mkdir + exactly one push; the remaining two pushes never started.
        self.assertEqual(run.call_count, 2)

    @patch("linkplane.transfer.AdbTransport")
    @patch("linkplane.transfer.subprocess.run")
    def test_failed_push_after_cancel_reports_cancelled_not_failure(
        self, run, transport_class
    ):
        # A terminal Ctrl+C also reaches the child `adb push`, which then exits non-zero.
        transport_class.return_value.select_device.return_value = {"model": "Test_Phone"}
        transport_class.return_value.serial = "serial-1"
        token = CancellationToken()

        def fake_run(command, **_kwargs):
            outcome = Mock()
            if command[3] == "push":
                token.cancel()
                outcome.returncode = 130
            else:
                outcome.returncode = 0
            return outcome

        run.side_effect = fake_run
        with tempfile.TemporaryDirectory() as directory:
            result = send_files(
                SendRequest(paths=self._sources(directory, 1), transport="adb"),
                cancel=token,
            )

        self.assertEqual(result.error.code, "cancelled")

    @patch("linkplane.transfer.AdbTransport")
    @patch("linkplane.transfer.subprocess.run")
    def test_send_without_token_is_unchanged(self, run, transport_class):
        run.return_value.returncode = 0
        transport_class.return_value.select_device.return_value = {"model": "Test_Phone"}
        transport_class.return_value.serial = "serial-1"
        with tempfile.TemporaryDirectory() as directory:
            result = send_files(SendRequest(paths=self._sources(directory, 2), transport="adb"))

        self.assertTrue(result.ok)
        self.assertEqual(run.call_count, 3)


class BackupCancellationTests(unittest.TestCase):
    def _transport(self, transport_class, files):
        transport = transport_class.return_value
        transport.serial = "serial-1"
        transport.select_device.return_value = {"model": "Test_Phone"}
        pulled = []

        def runner(command, **_kwargs):
            if command[:4] == ["adb", "-s", "serial-1", "shell"]:
                script = command[-1]
                if script.startswith("find "):
                    return "".join(f"/sdcard/DCIM/Camera/{name}\0" for name in files)
                if script.startswith("stat "):
                    return "1\t1725000000\n"
                if script.startswith("sha256sum "):
                    return f"{hashlib.sha256(b'a').hexdigest()}  x\n"
            if command[:5] == ["adb", "-s", "serial-1", "pull", "-a"]:
                pulled.append(command[-2])
                Path(command[-1]).write_bytes(b"a")
                return ""
            raise AssertionError(command)

        transport.run.side_effect = runner
        return pulled

    @patch("linkplane.backup.AdbTransport")
    def test_backup_stops_between_files_and_keeps_manifest_resumable(
        self, transport_class
    ):
        pulled = self._transport(transport_class, ["one.jpg", "two.jpg", "three.jpg"])
        token = CancellationToken()
        events = []

        def progress(event):
            events.append(event)
            if event.phase == "item_completed":
                token.cancel()

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            result = backup_photos(
                BackupRequest(destination=str(destination)),
                progress=progress,
                cancel=token,
            )
            manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))

        self.assertEqual(result.error.code, "cancelled")
        self.assertEqual(pulled, ["/sdcard/DCIM/Camera/one.jpg"])
        self.assertEqual(list(manifest["files"]), ["one.jpg"])
        self.assertEqual(events[-1].phase, "cancelled")
        self.assertEqual(events[-1].details["downloaded"], 1)
        self.assertEqual(events[-1].details["pending"], 3)

    @patch("linkplane.backup.AdbTransport")
    def test_pre_cancelled_backup_never_touches_the_device(self, transport_class):
        self._transport(transport_class, ["one.jpg"])
        token = CancellationToken()
        token.cancel("already cancelled")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            result = backup_photos(BackupRequest(destination=str(destination)), cancel=token)
            self.assertFalse(destination.exists())

        self.assertEqual(result.error.code, "cancelled")
        self.assertEqual(result.error.message, "already cancelled")
        transport_class.return_value.run.assert_not_called()


class ProfileRefreshCancellationTests(unittest.TestCase):
    def test_refresh_stops_between_profiles_and_leaves_config_untouched(self):
        token = CancellationToken()
        events = []

        def progress(event):
            events.append(event)
            if event.phase == "refreshed":
                token.cancel()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        name: {
                            "device_id": f"{name}-id",
                            "adb": {"serials": [f"{name}-usb"]},
                            "ssh": {"host": "10.0.0.9", "user": name, "port": 8022},
                        }
                        for name in ("alpha", "beta")
                    }
                },
                str(path),
            )
            before = path.read_text(encoding="utf-8")

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\nalpha-usb\tdevice\nbeta-usb\tdevice\n"
                if command[:2] == ["adb", "-s"] and "route" in command:
                    return "1.1.1.1 via 192.0.2.1 dev wlan0 src 192.0.2.5 uid 0\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest(None, str(path)),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
                progress=progress,
                cancel=token,
            )
            after = path.read_text(encoding="utf-8")

        self.assertEqual(result.error.code, "cancelled")
        self.assertEqual(
            [event.phase for event in events], ["started", "refreshed", "cancelled"]
        )
        self.assertEqual((events[-1].current, events[-1].total), (1, 2))
        self.assertEqual(events[-1].details["outcomes"][0]["profile"], "alpha")
        self.assertEqual(before, after)

    def test_scan_stops_probing_once_cancelled(self):
        profile = DeviceProfile(
            "phone", "FAKESERIAL01", ("FAKESERIAL01", "10.0.0.9:5555"), None, None
        )
        token = CancellationToken()
        probed = []

        def command_runner(command, **_kwargs):
            if command[:2] == ["adb", "connect"]:
                probed.append(command[2])
                token.cancel()
                raise BridgeError("failed to connect")
            raise AssertionError(f"unexpected command {command}")

        with self.assertRaises(OperationCancelled):
            scan_for_profile(profile, command_runner=command_runner, cancel=token, max_workers=1)

        # With a single worker, exactly one probe ran before every queued probe
        # short-circuited on the cancelled token.
        self.assertEqual(len(probed), 1)


class ClipboardSyncCancellationTests(unittest.TestCase):
    @patch("linkplane.clipboard.get_desktop_clipboard", return_value="same")
    @patch("linkplane.clipboard.get_phone_clipboard", return_value="same")
    def test_sync_returns_count_when_token_is_cancelled_during_wait(
        self, _get_phone, _get_desktop
    ):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        token = CancellationToken()
        events = []

        def progress(event):
            events.append(event)
            if event.phase == "watching":
                token.cancel()

        updates = sync_clipboards(
            Mock(), backend, interval=60, prefer="desktop", progress=progress, cancel=token
        )

        self.assertEqual(updates, 0)
        self.assertEqual([event.phase for event in events], ["watching"])

    @patch("linkplane.clipboard.get_desktop_clipboard", return_value="same")
    @patch("linkplane.clipboard.get_phone_clipboard", return_value="same")
    def test_cancelled_sync_service_completes_successfully(self, _get_phone, _get_desktop):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        transport = SshTransport("phone.local", "termux", runner=Mock())
        token = CancellationToken()
        token.cancel()

        result = use_clipboard(
            ClipboardRequest("sync", interval=60, foreground=False),
            transport,
            backend_factory=lambda: backend,
            cancel=token,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.updates, 0)


class CliCancellationTests(unittest.TestCase):
    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.backup.backup_photos")
    def test_cancelled_backup_exits_130_with_message(self, backup_photos_mock, _profile):
        backup_photos_mock.return_value = OperationResult.failure("cancelled", "interrupted")
        stderr = StringIO()

        with redirect_stderr(stderr):
            exit_code = main(["backup"])

        self.assertEqual(exit_code, EXIT_CANCELLED)
        self.assertEqual(stderr.getvalue().strip(), "linkplane: interrupted")
        self.assertIsInstance(
            backup_photos_mock.call_args.kwargs["cancel"], CancellationToken
        )

    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.profiles.refresh_profile_endpoints")
    def test_cancelled_json_command_prints_error_envelope(self, refresh_mock, _profile):
        refresh_mock.return_value = OperationResult.failure("cancelled", "interrupted")
        stdout = StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["profiles", "refresh", "--json"])

        envelope = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, EXIT_CANCELLED)
        self.assertFalse(envelope["ok"])
        self.assertEqual(
            envelope["error"],
            {"type": "OperationCancelled", "message": "interrupted", "code": "LP-CANCELLED-001"},
        )

    @patch("linkplane.cli.apply_device_profile")
    @patch("linkplane.transfer.send_files", side_effect=KeyboardInterrupt)
    def test_second_interrupt_exits_130_without_traceback(self, _send, _profile):
        stderr = StringIO()

        with redirect_stderr(stderr):
            exit_code = main(["send", "somefile"])

        self.assertEqual(exit_code, EXIT_CANCELLED)
        self.assertEqual(stderr.getvalue().strip(), "linkplane: interrupted")


if __name__ == "__main__":
    unittest.main()
