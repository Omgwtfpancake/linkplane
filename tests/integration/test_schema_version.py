"""Regression coverage for the shared `--json` envelope's `schema_version`.

Every command that prints its own JSON envelope must tag it with the single shared
`linkplane.operations.JSON_SCHEMA_VERSION` constant rather than a hardcoded literal --
otherwise the envelope version silently drifts out of sync across modules. This once
actually happened (find.py, profiles.py, and webcam.py each hardcoded `1` locally instead
of importing the constant) and went unnoticed because every literal happened to still
equal 1; these tests exercise the real `--json` code path of every command that builds
its own envelope so a future drift shows up as a real, specific assertion failure instead
of silent inconsistency.
"""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linkplane.cli import main
from linkplane.operations import JSON_SCHEMA_VERSION
from linkplane.transports import BridgeError


def run_cli(argv: list[str]) -> dict:
    output = StringIO()
    with redirect_stdout(output):
        main(argv)
    return json.loads(output.getvalue())


class SchemaVersionTests(unittest.TestCase):
    def test_profiles_list_envelope_uses_shared_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"devices": {}}), encoding="utf-8")

            envelope = run_cli(["profiles", "list", "--json", "--config", str(path)])

        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertTrue(envelope["ok"])

    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    @patch("linkplane.profiles.run_command", return_value="List of devices attached\n")
    def test_profiles_refresh_envelope_uses_shared_schema_version(self, _run, _which):
        # Refresh lists ADB devices before deciding there is nothing to do; fake that
        # listing so the test never depends on adb or a phone (found 2026-09-12).
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"devices": {}}), encoding="utf-8")

            envelope = run_cli(
                ["profiles", "refresh", "--dry-run", "--json", "--config", str(path)]
            )

        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertTrue(envelope["ok"])

    def test_profiles_refresh_error_envelope_uses_shared_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"devices": {}}), encoding="utf-8")

            envelope = run_cli(
                ["profiles", "refresh", "missing", "--json", "--config", str(path)]
            )

        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertFalse(envelope["ok"])

    @patch("linkplane.find.AdbTransport")
    def test_find_error_envelope_uses_shared_schema_version(self, adb_class):
        # Device selection fails deterministically through a faked transport (it used to
        # rely on an invalid --serial against real ADB, which "worked" with or without a
        # phone and so hid a live-device dependency; found 2026-09-12). This exercises the
        # shared error envelope in cli.main() -- itself a real regression target since
        # every command's error path funnels through that one site.
        adb_class.return_value.select_device.side_effect = BridgeError(
            "device not found: no-such-device"
        )

        envelope = run_cli(
            ["find", "--serial", "no-such-device", "--dry-run", "--json"]
        )

        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertFalse(envelope["ok"])

    @patch("linkplane.find.run_command", return_value="7\n")
    @patch("linkplane.find.AdbTransport")
    def test_find_success_envelope_uses_shared_schema_version(self, adb_class, _run):
        # `run_command` is patched too: even a dry run reads the current ring volume, and
        # without the patch this test silently depended on a live phone (found 2026-09-10).
        # A hardware-independent stand-in for device selection: this is the only way to
        # reach find.py's own success-path json.dumps() (dry-run still selects a device
        # before its dry-run branch), which the --serial-based failure test above never
        # touches -- find.py's success envelope and cli.py's shared error envelope are
        # two different call sites and must each be covered separately.
        adb_class.return_value.serial = "fake-serial"
        adb_class.return_value.select_device.return_value = {"model": "Test_Phone"}

        envelope = run_cli(["find", "--dry-run", "--json"])

        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertTrue(envelope["ok"])

    @patch("linkplane.webcam.AdbTransport")
    def test_webcam_start_success_envelope_uses_shared_schema_version(self, adb_class):
        adb_class.return_value.serial = "fake-serial"
        adb_class.return_value.select_device.return_value = {"model": "Test_Phone"}

        # --device 0 skips the real v4l2-ctl auto-detect probe entirely.
        envelope = run_cli(
            ["webcam", "start", "--device", "0", "--dry-run", "--json"]
        )

        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertTrue(envelope["ok"])

    def test_webcam_stop_error_envelope_uses_shared_schema_version(self):
        # No state file at this path, so nothing is tracked -- a deterministic,
        # environment-independent "not_found" error regardless of what this machine's
        # real ~/.config/linkplane/webcam.json currently contains.
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "webcam.json"

            with patch.dict("os.environ", {"LINKPLANE_WEBCAM_STATE": str(state_path)}):
                envelope = run_cli(["webcam", "stop", "--json"])

        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertFalse(envelope["ok"])


if __name__ == "__main__":
    unittest.main()
