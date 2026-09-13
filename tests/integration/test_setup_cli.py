"""`linkplane setup` through `cli.main()`: flags, JSON envelope, exit codes, and the stable
codes it stops with. The phases themselves are covered in `tests/test_setup.py`; here only
the outermost seams are faked (`shutil.which` and the `adb devices` runner)."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linkplane.cli import main
from linkplane.core import errors
from linkplane.operations import JSON_SCHEMA_VERSION


def run_cli(argv):
    output = StringIO()
    with redirect_stdout(output):
        code = main(argv)
    return code, output.getvalue()


def which_only(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


class SetupCliTests(unittest.TestCase):
    def test_adb_missing_non_interactive_stops_with_dependency_code_in_json(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("linkplane.setup.shutil.which", side_effect=which_only("systemctl", "pacman", "sudo")), \
                patch("linkplane.dependencies.shutil.which", side_effect=which_only("systemctl", "pacman", "sudo")):
            code, output = run_cli(["setup", "--json", "--non-interactive", "--config", str(Path(directory) / "config.json")])

        envelope = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(envelope["schema_version"], JSON_SCHEMA_VERSION)
        self.assertFalse(envelope["ok"])
        failing = [step for step in envelope["data"]["steps"] if step["status"] == "error"]
        self.assertEqual(failing[-1]["code"], errors.DEPENDENCY_MISSING)
        self.assertIn("sudo pacman -S --needed android-tools", failing[-1]["fix"])
        self.assertEqual(envelope["data"]["status"], "stopped")
        self.assertIsNone(envelope["data"]["api_client"])

    def test_host_usb_permission_state_stops_with_auth_003_and_host_guidance(self):
        listing = ("List of devices attached\n"
                   "FAKESERIAL01    no permissions (missing udev rules? user is in the plugdev group); "
                   "see [https://developer.android.com/tools/device.html] usb:1-2\n")
        with tempfile.TemporaryDirectory() as directory, \
                patch("linkplane.setup.shutil.which", side_effect=which_only("adb", "systemctl", "pacman", "sudo")), \
                patch("linkplane.dependencies.shutil.which", side_effect=which_only("adb", "systemctl", "pacman", "sudo")), \
                patch("linkplane.setup.run_command", return_value=listing):
            code, output = run_cli(["setup", "--non-interactive", "--config", str(Path(directory) / "config.json")])

        self.assertEqual(code, 1)
        self.assertIn("Error: LP-AUTH-003", output)
        self.assertIn("android-udev", output)
        self.assertNotIn("Tap Allow", output)
        self.assertIn("linkplane doctor", output)

    def test_help_lists_the_minimal_flag_set(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
            main(["setup", "--help"])
        self.assertEqual(exit_info.exception.code, 0)
        text = output.getvalue()
        for flag in ("--name", "--serial", "--config", "--no-daemon", "--api-client", "--dry-run", "--non-interactive", "--json"):
            self.assertIn(flag, text)
        self.assertNotIn("--yes", text)
