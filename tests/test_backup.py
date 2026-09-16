import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkplane.backup import (
    BackupRequest,
    _backup_selected,
    MANIFEST_NAME,
    backup_photos,
    backup_selected,
    discover_remote_files,
    load_manifest,
    validate_source,
)
from linkplane.core import errors
from linkplane.transports import AdbTransport, BridgeError


class FakePhone:
    """A scripted phone for the backup service: files by relative path under the camera
    folder; every adb call is recorded and anything unexpected fails the test."""

    SOURCE = "/sdcard/DCIM/Camera"

    def __init__(self, files):
        self.files = dict(files)
        self.pulls = []
        self.calls = []

    def run(self, command, **_kwargs):
        self.calls.append(command)
        if command[:4] == ["adb", "-s", "serial-1", "shell"]:
            script = command[-1]
            if script.startswith("find "):
                return "".join(f"{self.SOURCE}/{name}\0" for name in sorted(self.files))
            for name, content in self.files.items():
                if script.startswith("stat ") and f"{self.SOURCE}/{name}" in script:
                    return f"{len(content)}\t1725000000\n"
                if script.startswith("sha256sum ") and f"{self.SOURCE}/{name}" in script:
                    return f"{hashlib.sha256(content).hexdigest()}  {self.SOURCE}/{name}\n"
        if command[:5] == ["adb", "-s", "serial-1", "pull", "-a"]:
            name = command[-2][len(self.SOURCE) + 1:]
            self.pulls.append(name)
            Path(command[-1]).write_bytes(self.files[name])
            return ""
        raise AssertionError(command)

    def backup(self, destination, **kwargs):
        return _backup_selected(AdbTransport("serial-1", self.run), {"model": "Phone"}, self.SOURCE, destination,
                                dry_run=kwargs.pop("dry_run", False), **kwargs)


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

    def test_insufficient_free_space_refuses_before_writing_anything(self):
        phone = FakePhone({"a.jpg": b"x" * 600, "b.jpg": b"y" * 500})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            with patch("linkplane.backup.free_bytes", return_value=1000) as free:
                with self.assertRaises(errors.LinkplaneError) as raised:
                    phone.backup(destination)
            self.assertEqual(raised.exception.code, errors.STORAGE_INSUFFICIENT)
            self.assertIn("nothing was downloaded", str(raised.exception))
            self.assertFalse(destination.exists(), "the destination is not even created")
            free.assert_called_once_with(destination)
        self.assertEqual(phone.pulls, [])

    def test_free_space_is_the_pending_bytes_plus_the_largest_replaced_file(self):
        phone = FakePhone({"a.jpg": b"x" * 600, "b.jpg": b"y" * 500})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            with patch("linkplane.backup.free_bytes", return_value=1100):
                result = phone.backup(destination)  # exactly enough for two new files
            self.assertEqual((result.downloaded, result.downloaded_bytes), (2, 1100))
            phone.files["b.jpg"] = b"z" * 400  # changed on the phone: replaces a local copy
            phone.pulls.clear()
            with patch("builtins.print"), patch("linkplane.backup.free_bytes", return_value=799):
                with self.assertRaises(errors.LinkplaneError):
                    phone.backup(destination)  # needs 400 + 400 beside the old copy
            with patch("linkplane.backup.free_bytes", return_value=800):
                self.assertEqual(phone.backup(destination).downloaded, 1)
        self.assertEqual(phone.pulls, ["b.jpg"])

    def test_nothing_pending_needs_no_free_space_check(self):
        phone = FakePhone({"a.jpg": b"x"})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            phone.backup(destination)
            with patch("linkplane.backup.free_bytes", side_effect=AssertionError("not needed")):
                result = phone.backup(destination)
        self.assertEqual((result.downloaded, result.skipped, result.verified_bytes), (0, 1, 1))

    def test_backup_service_reports_free_space_failure_with_its_code(self):
        with patch("linkplane.backup.AdbTransport") as transport_class, \
                patch("linkplane.backup.free_bytes", return_value=0), tempfile.TemporaryDirectory() as directory:
            phone = FakePhone({"a.jpg": b"x"})
            transport_class.return_value = AdbTransport("serial-1", phone.run)
            transport_class.return_value.select_device = lambda: {"model": "Phone"}
            result = backup_photos(BackupRequest(destination=str(Path(directory) / "b")))
        self.assertEqual((result.error.code, result.error.error_code), ("operation_failed", errors.STORAGE_INSUFFICIENT))
        self.assertTrue(result.error.hints)

    def test_existing_files_linkplane_did_not_create_are_never_overwritten(self):
        phone = FakePhone({"same.jpg": b"identical", "clash.jpg": b"phone version", "new.jpg": b"new"})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            destination.mkdir()
            (destination / "same.jpg").write_bytes(b"identical")
            (destination / "clash.jpg").write_bytes(b"the user's own file")
            preview = phone.backup(destination, dry_run=True)
            result = phone.backup(destination)
            again = phone.backup(destination)
            clash = (destination / "clash.jpg").read_bytes()
            manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))

        self.assertEqual(preview.preserved, ("clash.jpg", "same.jpg"))
        self.assertEqual(preview.pending_files, ("new.jpg",))
        self.assertEqual(clash, b"the user's own file")
        self.assertEqual(phone.pulls, ["new.jpg"])
        self.assertEqual((result.downloaded, result.adopted, result.preserved), (1, 1, ("clash.jpg",)))
        self.assertIn("same.jpg", manifest["files"])
        self.assertNotIn("clash.jpg", manifest["files"])
        self.assertEqual((again.skipped, again.downloaded, again.preserved), (2, 0, ("clash.jpg",)))

    def test_a_symbolic_link_in_the_destination_never_redirects_a_download(self):
        phone = FakePhone({"Trip/photo.jpg": b"photo"})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            elsewhere = Path(directory) / "elsewhere"
            destination.mkdir()
            elsewhere.mkdir()
            (destination / "Trip").symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaisesRegex(BridgeError, "symbolic link"):
                phone.backup(destination)
            self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(phone.pulls, [])

    def test_backup_is_additive_phone_deletions_never_delete_local_copies(self):
        phone = FakePhone({"a.jpg": b"a", "b.jpg": b"b"})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            phone.backup(destination)
            del phone.files["a.jpg"]
            result = phone.backup(destination)
            self.assertEqual((destination / "a.jpg").read_bytes(), b"a")
            self.assertEqual((result.discovered, result.skipped, result.downloaded), (1, 1, 0))
        destructive = [call for call in phone.calls if call[:4] == ["adb", "-s", "serial-1", "shell"]
                       and any(word in call[-1] for word in ("rm ", "mv ", "unlink"))]
        self.assertEqual(destructive, [], "backup never runs a destructive command on the phone")

    def test_result_carries_measurements_for_the_unchanged_file_strategy(self):
        phone = FakePhone({"a.jpg": b"a" * 10, "b.jpg": b"b" * 20})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            phone.backup(destination)
            result = phone.backup(destination).to_dict()
        for key in ("discovery_seconds", "unchanged_check_seconds", "duration_seconds"):
            self.assertIsInstance(result[key], float)
            self.assertGreaterEqual(result[key], 0.0)
        self.assertEqual((result["skipped"], result["verified_bytes"]), (2, 30))

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
