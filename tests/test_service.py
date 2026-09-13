import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkplane import service
from linkplane.core import errors
from linkplane.transports import BridgeError


class ServiceTests(unittest.TestCase):
    def test_unit_text_runs_daemon_with_restart(self):
        text = service.unit_text(["/usr/bin/linkplane", "daemon", "run", "--interval", "10"])
        self.assertIn("ExecStart=/usr/bin/linkplane daemon run --interval 10\n", text)
        self.assertIn("Restart=on-failure\n", text)
        self.assertIn("WantedBy=default.target\n", text)

    def test_daemon_command_prefers_installed_launcher(self):
        with patch("linkplane.service.shutil.which", return_value="/home/x/.local/bin/linkplane"):
            self.assertEqual(service.daemon_command(("--no-wifi",)), ["/home/x/.local/bin/linkplane", "daemon", "run", "--no-wifi"])
        with patch("linkplane.service.shutil.which", return_value=None):
            self.assertEqual(service.daemon_command()[1:], ["-m", "linkplane", "daemon", "run"])

    def test_install_writes_unit_and_runs_systemctl(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return ""

        with tempfile.TemporaryDirectory() as directory, patch("linkplane.service.shutil.which", return_value="/usr/bin/x"):
            result = service.install(unit_dir=directory, runner=runner)
            unit = Path(directory) / service.UNIT_NAME
            self.assertTrue(unit.exists())
            self.assertEqual(unit.read_text(), result.unit_text)
        self.assertEqual(calls, [["systemctl", "--user", "daemon-reload"],
                                 ["systemctl", "--user", "enable", "--now", "linkplaned.service"]])
        self.assertFalse(result.dry_run)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = service.install(unit_dir=directory, dry_run=True, runner=lambda *a, **k: self.fail("ran"))
            self.assertEqual(os.listdir(directory), [])
        self.assertTrue(result.dry_run)
        self.assertIn("[Service]", result.unit_text)

    def test_uninstall_disables_and_removes(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / service.UNIT_NAME
            unit.write_text("x")
            with patch("linkplane.service.shutil.which", return_value="/usr/bin/x"):
                service.uninstall(unit_dir=directory, runner=lambda c, **k: calls.append(c))
            self.assertFalse(unit.exists())
        self.assertEqual(calls[0], ["systemctl", "--user", "disable", "--now", "linkplaned.service"])

    def test_uninstall_without_unit_is_a_coded_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(errors.LinkplaneError) as raised:
                service.uninstall(unit_dir=directory, runner=lambda *a, **k: "")
        self.assertEqual(raised.exception.code, errors.DEVICE_NOT_FOUND)

    def test_missing_systemctl_is_a_dependency_error(self):
        with tempfile.TemporaryDirectory() as directory, patch("linkplane.service.shutil.which", return_value=None):
            with self.assertRaises(errors.LinkplaneError) as raised:
                service.install(unit_dir=directory, runner=lambda *a, **k: "")
        self.assertEqual(raised.exception.code, errors.DEPENDENCY_MISSING)

    def test_systemctl_failure_is_a_coded_error(self):
        def runner(command, **_kwargs):
            raise BridgeError("systemctl: Failed to connect to bus")

        with tempfile.TemporaryDirectory() as directory, patch("linkplane.service.shutil.which", return_value="/usr/bin/x"):
            with self.assertRaises(errors.LinkplaneError) as raised:
                service.install(unit_dir=directory, runner=runner)
        self.assertEqual(raised.exception.code, errors.PROVIDER_FAILED)


if __name__ == "__main__":
    unittest.main()


class ServiceSeamTests(unittest.TestCase):
    """`install`/`uninstall` resolve their runner at call time (setup composes them)."""

    def test_module_symbol_patch_reaches_install_and_uninstall(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("linkplane.service.shutil.which", return_value="/usr/bin/x"), \
                patch("linkplane.service.run_command", side_effect=lambda command, **k: calls.append(command) or ""):
            service.install(unit_dir=directory)
            service.uninstall(unit_dir=directory)

        self.assertEqual(calls[0], ["systemctl", "--user", "daemon-reload"])
        self.assertEqual(calls[1], ["systemctl", "--user", "enable", "--now", service.UNIT_NAME])
        self.assertEqual(calls[2], ["systemctl", "--user", "disable", "--now", service.UNIT_NAME])
        self.assertEqual(len(calls), 4)
