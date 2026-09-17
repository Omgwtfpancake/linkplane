import hashlib
import json
import shlex
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from linkplane.backup import (
    BATCH_LISTING_FORMAT,
    BackupRequest,
    discover,
    parse_batch_listing,
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
    folder. Shell scripts are split with shlex, exactly as the phone's shell would split the
    quoted words, so a path only matches when it was quoted correctly. `toybox` selects how
    `find -printf` behaves: "modern" (supported), "legacy" (unknown option), or "literal"
    (prints its escapes literally, like a very old implementation might)."""

    SOURCE = "/sdcard/DCIM/Camera"
    MTIME = "1725000000.123456789"

    def __init__(self, files, *, toybox="modern", source=None):
        self.files = dict(files)
        self.toybox = toybox
        self.source = source or self.SOURCE
        self.pulls = []
        self.calls = []

    def path(self, name):
        return f"{self.source}/{name}"

    def shell(self, script):
        words = shlex.split(script)
        if words[0] == "find":
            assert words[1] == self.source and words[2:4] == ["-type", "f"], words
            if words[4] == "-print0":
                return "".join(f"{self.path(name)}\0" for name in sorted(self.files))
            assert words[4] == "-printf" and len(words) == 6, words
            if self.toybox == "legacy":
                raise BridgeError("find: Unknown option '-printf'")
            fmt = words[5]
            if self.toybox == "modern":
                fmt = fmt.replace("\\t", "\t").replace("\\0", "\0")
            return "".join(fmt.replace("%s", str(len(content))).replace("%T@", self.MTIME).replace("%p", self.path(name))
                           for name, content in sorted(self.files.items()))
        if words[0] == "stat":
            assert words[1:3] == ["-c", "%s\t%Y"], words
            content = self.files[words[3][len(self.source) + 1:]]
            return f"{len(content)}\t{self.MTIME.split('.')[0]}\n"
        if words[0] == "sha256sum":
            content = self.files[words[1][len(self.source) + 1:]]
            return f"{hashlib.sha256(content).hexdigest()}  {words[1]}\n"
        raise AssertionError(script)

    def run(self, command, **_kwargs):
        self.calls.append(command)
        if command[:4] == ["adb", "-s", "serial-1", "shell"]:
            return self.shell(command[-1])
        if command[:5] == ["adb", "-s", "serial-1", "pull", "-a"]:
            name = command[-2][len(self.source) + 1:]
            self.pulls.append(name)
            Path(command[-1]).write_bytes(self.files[name])
            return ""
        raise AssertionError(command)

    def listing_calls(self):
        return [c for c in self.calls if c[:4] == ["adb", "-s", "serial-1", "shell"] and c[-1].split()[0] in ("find", "stat")]

    def backup(self, destination, **kwargs):
        return _backup_selected(AdbTransport("serial-1", self.run), {"model": "Phone"}, self.source, destination,
                                dry_run=kwargs.pop("dry_run", False), **kwargs)


# Names Android shared storage accepts, including ones a careless shell command would break
# on. (Android's emulated storage refuses `"`, `\\`, `<>|`, tabs and newlines in names;
# the parser is still tested with tabs and newlines below.)
DIFFICULT_NAMES = [
    "with space.jpg", "it's.jpg", "ünïcødé_写真.jpg", "$(echo pwned);`x`&.jpg", "-leading-dash.jpg",
    "100% %s %p.jpg", "a'b $HOME ~.jpg", "sub dir/nested.jpg", "IMG_0001.jpg",
]


class DiscoveryTests(unittest.TestCase):
    """v0.6 Slice 3: one `find -printf` call instead of one `stat` per file."""

    def discover(self, phone):
        return discover(AdbTransport("serial-1", phone.run), phone.source)

    def test_batch_listing_matches_the_file_by_file_listing_exactly(self):
        files = {name: name.encode() * 3 for name in DIFFICULT_NAMES}
        batch, method, calls = self.discover(FakePhone(files))
        legacy, legacy_method, legacy_calls = self.discover(FakePhone(files, toybox="legacy"))
        self.assertEqual((method, calls), ("batch", 1))
        self.assertEqual((legacy_method, legacy_calls), ("per-file", 2 + len(files)))
        self.assertEqual(batch, legacy)
        self.assertEqual([f.relative_path for f in batch], sorted(DIFFICULT_NAMES))
        self.assertTrue(all(f.modified == 1725000000 for f in batch), "nanoseconds never round the seconds")
        self.assertEqual({f.relative_path: f.size for f in batch}, {name: len(content) for name, content in files.items()})

    def test_zero_one_and_many_files_take_exactly_one_adb_call(self):
        for count in (0, 1, 100):
            with self.subTest(files=count):
                phone = FakePhone({f"IMG_{i:04d}.jpg": b"x" * (i + 1) for i in range(count)})
                listed, method, calls = self.discover(phone)
                self.assertEqual((len(listed), method, calls), (count, "batch", 1))
                self.assertEqual(len(phone.listing_calls()), 1, "no stat call per discovered file")
                self.assertFalse(any(c[-1].startswith("stat ") for c in phone.calls))

    def test_the_source_is_quoted_as_one_word(self):
        source = "/sdcard/DCIM/it's $(touch x) `y`; z"
        phone = FakePhone({"a.jpg": b"a"}, source=source)
        listed, method, _calls = self.discover(phone)  # FakePhone asserts shlex.split(script)[1] == source
        self.assertEqual((method, [f.path for f in listed]), ("batch", [f"{source}/a.jpg"]))
        self.assertEqual(shlex.split(phone.calls[0][-1]), ["find", source, "-type", "f", "-printf", BATCH_LISTING_FORMAT])

    def test_unsupported_printf_falls_back_to_a_complete_file_by_file_listing(self):
        phone = FakePhone({f"IMG_{i}.jpg": b"x" for i in range(5)}, toybox="legacy")
        listed, method, calls = self.discover(phone)
        self.assertEqual((len(listed), method, calls), (5, "per-file", 7))

    def test_literal_escapes_are_not_mistaken_for_a_listing(self):
        phone = FakePhone({"a.jpg": b"a", "b.jpg": b"bb"}, toybox="literal")
        listed, method, _calls = self.discover(phone)
        self.assertEqual((method, [f.relative_path for f in listed]), ("per-file", ["a.jpg", "b.jpg"]))

    def test_malformed_batch_output_is_never_a_partial_listing(self):
        source = PurePosixPath("/sdcard/DCIM/Camera")
        good = "12\t1725000000.5\t/sdcard/DCIM/Camera/a.jpg\0"
        for bad in [
            good + "7\t1725000000.5\t/sdcard/DCIM/Camera/b.jpg",       # truncated: no final NUL
            good + "x\t1725000000\t/sdcard/DCIM/Camera/b.jpg\0",     # size not a number
            good + "7\t1725000000\0",                                  # missing path
            good + "7 1725000000 /sdcard/DCIM/Camera/b.jpg\0",          # wrong separators
            good + "\0",                                                # empty record
        ]:
            with self.subTest(bad=bad), self.assertRaises(BridgeError):
                parse_batch_listing(bad, source)
        with self.assertRaisesRegex(BridgeError, "outside the backup source"):
            parse_batch_listing("1\t1\t/sdcard/Download/x.jpg\0", source)
        with self.assertRaisesRegex(BridgeError, "invalid backup path"):
            parse_batch_listing("1\t1\t/sdcard/DCIM/Camera/../x.jpg\0", source)
        parsed = parse_batch_listing("3\t-5.5\t/sdcard/DCIM/Camera/tab\there.jpg\0" "4\t9\t/sdcard/DCIM/Camera/new\nline.jpg\0", source)
        self.assertEqual([(f.relative_path, f.size, f.modified) for f in parsed],
                         [("tab\there.jpg", 3, -5), ("new\nline.jpg", 4, 9)])
        self.assertEqual(parse_batch_listing("", source), [])

    def test_garbled_batch_falls_back_and_fallback_errors_are_the_old_errors(self):
        class Garbled(FakePhone):
            def shell(self, script):
                if "-printf" in script:
                    return "12\t1725000000\t/sdcard/DCIM/Camera/a.jpg\0garbage"
                return super().shell(script)

        listed, method, _ = self.discover(Garbled({"a.jpg": b"a" * 12, "b.jpg": b"b"}))
        self.assertEqual((method, len(listed)), ("per-file", 2), "the garbled batch is discarded, not trimmed")

        class Broken(FakePhone):
            def shell(self, script):
                raise BridgeError("find: /sdcard/DCIM/Camera: No such file or directory")

        with self.assertRaisesRegex(BridgeError, "No such file"):
            self.discover(Broken({}))

    def test_backup_results_are_unchanged_and_a_no_change_run_is_one_adb_call(self):
        files = {name: name.encode() for name in DIFFICULT_NAMES}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup"
            modern, legacy = FakePhone(files), FakePhone(files, toybox="legacy")
            first = modern.backup(destination / "modern")
            legacy_first = legacy.backup(destination / "legacy")
            modern.calls.clear()
            again = modern.backup(destination / "modern")
            copied = sorted(str(p.relative_to(destination / "modern")) for p in (destination / "modern").rglob("*.jpg"))
        comparable = lambda r: {k: v for k, v in r.to_dict().items()
                                if k not in ("destination", "discovery_method", "discovery_calls") and not k.endswith("_seconds")}
        self.assertEqual(comparable(first), comparable(legacy_first))
        self.assertEqual((first.discovery_method, first.discovery_calls, legacy_first.discovery_method), ("batch", 1, "per-file"))
        self.assertEqual((first.downloaded, again.downloaded, again.skipped), (len(files), 0, len(files)))
        self.assertEqual(len(modern.calls), 1, "nothing new: one listing call, no stat, no pull")
        self.assertEqual(copied, sorted(DIFFICULT_NAMES))


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
