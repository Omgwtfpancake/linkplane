import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkplane.backup import (
    BackupRequest,
    MANIFEST_NAME,
    backup_photos,
    backup_selected,
    discover_remote_files,
    load_manifest,
    validate_source,
)
from linkplane.transports import AdbTransport, BridgeError


class BackupTests(unittest.TestCase):
    def test_validate_source_requires_safe_absolute_path(self):
        with self.assertRaisesRegex(BridgeError, "absolute Android path"):
            validate_source("sdcard/DCIM")
        with self.assertRaisesRegex(BridgeError, "absolute Android path"):
            validate_source("/sdcard/DCIM/../Download")

    def test_discovery_rejects_file_outside_source(self):
        def runner(command, **_kwargs):
            if command[-1].startswith("find "):
                return "/sdcard/Download/not-a-photo.jpg\0"
            raise AssertionError(command)

        transport = AdbTransport("serial-1", runner)

        with self.assertRaisesRegex(BridgeError, "outside the backup source"):
            discover_remote_files(transport, "/sdcard/DCIM/Camera")

    def test_backup_service_returns_typed_validation_error(self):
        result = backup_photos(BackupRequest(source="sdcard/DCIM"))

        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.error.code, "invalid_request")
        self.assertIn("absolute Android path", result.error.message)

    @patch("linkplane.backup.AdbTransport")
    def test_backup_service_returns_transport_error(self, transport_class):
        transport_class.return_value.select_device.side_effect = BridgeError(
            "device unavailable"
        )

        result = backup_photos(BackupRequest())

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "transport_unavailable")
        self.assertEqual(result.error.message, "device unavailable")

    @patch("linkplane.backup.AdbTransport")
    def test_backup_service_returns_plan_and_progress_without_printing(
        self, transport_class
    ):
        transport = transport_class.return_value
        transport.serial = "serial-1"
        transport.select_device.return_value = {"model": "Test_Phone"}

        def runner(command, **_kwargs):
            script = command[-1]
            if script.startswith("find "):
                return "/sdcard/DCIM/Camera/photo.jpg\0"
            if script.startswith("stat "):
                return "12\t1725000000\n"
            raise AssertionError(command)

        transport.run.side_effect = runner
        events = []
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            with patch("builtins.print") as print_output:
                result = backup_photos(
                    BackupRequest(destination=str(destination), dry_run=True),
                    progress=events.append,
                )

            self.assertFalse(destination.exists())

        self.assertTrue(result.ok)
        self.assertEqual(result.value.device, "Test Phone")
        self.assertEqual(result.value.discovered, 1)
        self.assertEqual(result.value.pending, 1)
        self.assertEqual(result.value.pending_bytes, 12)
        self.assertEqual(result.value.pending_files, ("photo.jpg",))
        self.assertEqual(
            [event.phase for event in events],
            ["discovering", "started", "item_pending", "completed"],
        )
        print_output.assert_not_called()

    @patch("builtins.print")
    def test_backup_verifies_download_and_skips_unchanged_file(self, _print):
        content = b"phone photo"
        checksum = hashlib.sha256(content).hexdigest()
        pulls = []

        def runner(command, **_kwargs):
            if command[:4] == ["adb", "-s", "serial-1", "shell"]:
                script = command[-1]
                if script.startswith("find "):
                    return "/sdcard/DCIM/Camera/Trip/photo one.jpg\0"
                if script.startswith("stat "):
                    return f"{len(content)}\t1725000000\n"
                if script.startswith("sha256sum "):
                    return f"{checksum}  /sdcard/DCIM/Camera/Trip/photo one.jpg\n"
            if command[:5] == ["adb", "-s", "serial-1", "pull", "-a"]:
                pulls.append(command)
                Path(command[-1]).write_bytes(content)
                return ""
            raise AssertionError(command)

        transport = AdbTransport("serial-1", runner)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            device = {"model": "Test_Phone"}

            self.assertEqual(
                backup_selected(
                    transport,
                    device,
                    "/sdcard/DCIM/Camera",
                    destination,
                    dry_run=False,
                ),
                0,
            )
            self.assertEqual(
                backup_selected(
                    transport,
                    device,
                    "/sdcard/DCIM/Camera",
                    destination,
                    dry_run=False,
                ),
                0,
            )

            local_photo = destination / "Trip" / "photo one.jpg"
            self.assertEqual(local_photo.read_bytes(), content)
            manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
            record = manifest["files"]["Trip/photo one.jpg"]
            self.assertEqual(record["sha256"], checksum)
            self.assertEqual(record["size"], len(content))
        self.assertEqual(len(pulls), 1)

    @patch("builtins.print")
    def test_checksum_failure_does_not_publish_partial_file(self, _print):
        content = b"corrupt transfer"

        def runner(command, **_kwargs):
            if command[:4] == ["adb", "-s", "serial-1", "shell"]:
                script = command[-1]
                if script.startswith("find "):
                    return "/sdcard/DCIM/Camera/photo.jpg\0"
                if script.startswith("stat "):
                    return f"{len(content)}\t1725000000\n"
                if script.startswith("sha256sum "):
                    return f"{'0' * 64}  /sdcard/DCIM/Camera/photo.jpg\n"
            if command[:5] == ["adb", "-s", "serial-1", "pull", "-a"]:
                Path(command[-1]).write_bytes(content)
                return ""
            raise AssertionError(command)

        transport = AdbTransport("serial-1", runner)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            with self.assertRaisesRegex(BridgeError, "checksum verification failed"):
                backup_selected(
                    transport,
                    {"model": "Phone"},
                    "/sdcard/DCIM/Camera",
                    destination,
                    dry_run=False,
                )
            self.assertFalse((destination / "photo.jpg").exists())
            self.assertFalse((destination / ".photo.jpg.linkplane-part").exists())

    @patch("builtins.print")
    def test_dry_run_does_not_create_destination(self, _print):
        def runner(command, **_kwargs):
            script = command[-1]
            if script.startswith("find "):
                return "/sdcard/DCIM/Camera/photo.jpg\0"
            if script.startswith("stat "):
                return "123\t1725000000\n"
            raise AssertionError(command)

        transport = AdbTransport("serial-1", runner)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            backup_selected(
                transport,
                {"model": "Phone"},
                "/sdcard/DCIM/Camera",
                destination,
                dry_run=True,
            )
            self.assertFalse(destination.exists())

    def test_manifest_cannot_be_reused_for_another_device(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / MANIFEST_NAME
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "device_serial": "phone-1",
                        "source": "/sdcard/DCIM/Camera",
                        "files": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BridgeError, "different device or source"):
                load_manifest(manifest_path, "phone-2", "/sdcard/DCIM/Camera")


if __name__ == "__main__":
    unittest.main()
