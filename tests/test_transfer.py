import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linkplane.dependencies import DependencyPlan
from linkplane.transfer import (
    SendRequest,
    adb_transfer,
    format_bytes,
    resolve_sources,
    send,
    send_files,
    source_size,
)
from linkplane.transports import BridgeError


class TransferTests(unittest.TestCase):
    def test_resolve_sources_rejects_missing_path(self):
        with self.assertRaisesRegex(BridgeError, "source does not exist"):
            resolve_sources(["/definitely/not/a/linkplane/file"])

    def test_source_size_includes_directory_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.txt").write_bytes(b"123")
            nested = root / "nested"
            nested.mkdir()
            (nested / "two.txt").write_bytes(b"4567")

            self.assertEqual(source_size(root), 7)

    @patch("linkplane.transfer.AdbTransport")
    def test_send_service_returns_typed_result_and_progress(self, transport_class):
        transport = transport_class.return_value
        transport.select_device.return_value = {"model": "Test_Phone"}
        transport.serial = "serial-1"
        events = []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hello.txt"
            source.write_text("hello", encoding="utf-8")

            with patch("builtins.print") as print_output:
                result = send_files(
                    SendRequest(
                        paths=(str(source),),
                        transport="adb",
                        dry_run=True,
                    ),
                    progress=events.append,
                )

        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertEqual(result.value.transport, "adb")
        self.assertEqual(result.value.total_bytes, 5)
        self.assertEqual(result.value.items[0].path, str(source))
        self.assertEqual(
            [event.phase for event in events],
            ["started", "command", "command", "completed"],
        )
        self.assertEqual(events[0].details["serial"], "serial-1")
        print_output.assert_not_called()

    def test_send_service_returns_typed_validation_error(self):
        result = send_files(
            SendRequest(
                paths=("/definitely/not/a/linkplane/file",),
                transport="adb",
            )
        )

        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.error.code, "invalid_request")
        self.assertIn("source does not exist", result.error.message)

    @patch("linkplane.transfer.AdbTransport")
    @patch("linkplane.transfer.subprocess.run")
    def test_send_service_reports_item_progress(self, run, transport_class):
        run.return_value.returncode = 0
        transport = transport_class.return_value
        transport.select_device.return_value = {"model": "Test_Phone"}
        transport.serial = "serial-1"
        events = []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hello.txt"
            source.write_text("hello", encoding="utf-8")

            result = send_files(
                SendRequest(paths=(str(source),), transport="adb"),
                progress=events.append,
            )

        self.assertTrue(result.ok)
        self.assertEqual(
            [event.phase for event in events],
            ["started", "item_started", "item_completed", "completed"],
        )
        self.assertEqual(events[2].current, 1)
        self.assertEqual(events[2].total, 1)

    def test_format_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1536), "1.5 KiB")

    @patch("linkplane.transfer.AdbTransport")
    def test_adb_dry_run_does_not_execute_transfer(self, transport_class):
        transport = transport_class.return_value
        transport.select_device.return_value = {"model": "Test_Phone"}
        transport.serial = "serial-1"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hello.txt"
            source.write_text("hello", encoding="utf-8")

            with patch("builtins.print"):
                result = adb_transfer(
                    [source],
                    "/sdcard/Download/Linkplane Test",
                    None,
                    dry_run=True,
                )

        self.assertEqual(result, 0)
        transport.select_device.assert_called_once_with()

    def test_adb_rejects_relative_destination(self):
        with self.assertRaisesRegex(BridgeError, "absolute Android path"):
            adb_transfer([], "Downloads", None, dry_run=True)

    @patch("linkplane.transfer.localsend_transfer")
    @patch("linkplane.transfer.AdbTransport")
    def test_explicit_serial_never_falls_back_to_localsend(
        self, adb_class, localsend_transfer
    ):
        adb_class.return_value.select_device.side_effect = BridgeError("device not found")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hello.txt"
            source.write_text("hello", encoding="utf-8")
            arguments = SimpleNamespace(
                paths=[str(source)],
                transport="auto",
                destination="/sdcard/Download",
                serial="missing",
                dry_run=False,
            )

            with self.assertRaisesRegex(BridgeError, "device not found"):
                send(arguments)

        localsend_transfer.assert_not_called()

    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/localsend-cli")
    @patch("linkplane.transfer.subprocess.run")
    @patch("builtins.print")
    def test_localsend_uses_supported_cli(self, _print, run, _which):
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hello.txt"
            source.write_text("hello", encoding="utf-8")

            from linkplane.transfer import localsend_transfer

            result = localsend_transfer([source], dry_run=False)

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/localsend-cli", "--file", str(source)],
        )

    @patch(
        "linkplane.transfer.localsend_dependency_plan",
        return_value=DependencyPlan(
            "localsend-cli",
            None,
            False,
            None,
            "apt-get",
            None,
            install_error="automatic installation is not supported with apt-get",
        ),
    )
    def test_localsend_service_returns_typed_missing_dependency(self, _dependency):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hello.txt"
            source.write_text("hello", encoding="utf-8")

            result = send_files(
                SendRequest(paths=(str(source),), transport="localsend")
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")
        self.assertIn("not supported with apt-get", result.error.message)


if __name__ == "__main__":
    unittest.main()
