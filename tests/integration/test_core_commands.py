"""CLI tests for the v0.1 core commands (`battery`, `ping`, `capabilities`).

The provider is faked at the `resolve_provider` seam, so these cover argument handling,
human rendering, the JSON envelope, exit codes, and the coded error path -- not the
transports (see tests/test_providers.py).
"""

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from linkplane.cli import main
from linkplane.core import errors
from linkplane.core.capability import CATALOGUE, SUPPORTED, UNSUPPORTED, CapabilityReport, complete
from linkplane.providers.base import BatteryReading, PingResult, Provider


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, *, reachable=True, battery_error=None):
        self.reachable = reachable
        self.battery_error = battery_error

    @property
    def address(self):
        return "fake-1"

    def ping(self):
        if self.reachable:
            return PingResult(True, self.name, self.address, 12.5)
        return PingResult(False, self.name, self.address, detail="no route")

    def status(self):
        raise AssertionError("not used")

    def battery(self):
        if self.battery_error is not None:
            raise self.battery_error
        return BatteryReading(87, "charging", "good", ("usb",), self.name, 30.5, 4200)

    def capabilities(self):
        return complete(
            {
                "device.ping": CapabilityReport("device.ping", SUPPORTED, "fake"),
                "battery.read": CapabilityReport("battery.read", SUPPORTED, "fake"),
            },
            self.name,
        )


def run(argv, provider):
    out, err = StringIO(), StringIO()
    with patch("linkplane.cli.apply_device_profile"), patch(
        "linkplane.cli.resolve_provider", return_value=provider
    ), redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class BatteryCommandTests(unittest.TestCase):
    def test_human_output(self):
        code, out, _ = run(["battery"], FakeProvider())
        self.assertEqual(code, 0)
        self.assertIn("Level       87%", out)
        self.assertIn("Powered by  usb", out)
        self.assertIn("Provider    fake (fake-1)", out)

    def test_json_envelope(self):
        code, out, _ = run(["battery", "--json"], FakeProvider())
        envelope = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["level"], 87)
        self.assertEqual(envelope["data"]["provider"], "fake")
        self.assertEqual(envelope["data"]["address"], "fake-1")

    def test_coded_error_renders_friendly_text_and_exits_1(self):
        failure = errors.LinkplaneError(
            errors.CONNECT_UNREACHABLE, "ssh: No route to host", errors.SSH_HINTS
        )
        code, out, err = run(["battery"], FakeProvider(battery_error=failure))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Phone unreachable.", err)
        self.assertIn("• the phone is on the same network", err)
        self.assertIn("Error: LP-CONNECT-001", err)

    def test_coded_error_json_envelope(self):
        failure = errors.LinkplaneError(errors.TIMEOUT, "ssh timed out after 8 seconds")
        code, out, _ = run(["battery", "--json"], FakeProvider(battery_error=failure))
        envelope = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "LP-TIMEOUT-001")
        self.assertEqual(envelope["error"]["type"], "LinkplaneError")
        self.assertEqual(envelope["error"]["message"], "ssh timed out after 8 seconds")


class PingCommandTests(unittest.TestCase):
    def test_reachable_exits_0(self):
        code, out, _ = run(["ping"], FakeProvider())
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "Phone reachable via fake (fake-1) in 12.5 ms")

    def test_unreachable_exits_1_without_raising(self):
        code, out, err = run(["ping"], FakeProvider(reachable=False))
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "Phone unreachable via fake (fake-1): no route")
        self.assertEqual(err, "")

    def test_json_ok_mirrors_reachability(self):
        code, out, _ = run(["ping", "--json"], FakeProvider(reachable=False))
        envelope = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(envelope["ok"])
        self.assertFalse(envelope["data"]["reachable"])
        self.assertEqual(envelope["data"]["detail"], "no route")


class CapabilitiesCommandTests(unittest.TestCase):
    def test_human_table_lists_full_catalogue(self):
        code, out, _ = run(["capabilities"], FakeProvider())
        self.assertEqual(code, 0)
        for name in CATALOGUE:
            self.assertIn(name, out)
        self.assertIn("battery.read", out)
        self.assertIn("✓  supported  (linkplane battery)", out)
        self.assertIn("✗  unsupported", out)

    def test_json_lists_every_capability_with_status(self):
        code, out, _ = run(["capabilities", "--json"], FakeProvider())
        envelope = json.loads(out)
        self.assertEqual(code, 0)
        reports = envelope["data"]["capabilities"]
        self.assertEqual([report["name"] for report in reports], list(CATALOGUE))
        by_name = {report["name"]: report for report in reports}
        self.assertEqual(by_name["battery.read"]["status"], SUPPORTED)
        self.assertEqual(by_name["files.send"]["status"], UNSUPPORTED)
        self.assertEqual(envelope["data"]["provider"], "fake")
        self.assertIn("requirements", by_name["device.ping"])


class BrokenPipeTests(unittest.TestCase):
    def test_closed_stdout_exits_141_without_a_traceback(self):
        from linkplane.cli import EXIT_BROKEN_PIPE

        provider = FakeProvider()
        err = StringIO()
        with patch("linkplane.cli.apply_device_profile"), patch(
            "linkplane.cli.resolve_provider", return_value=provider
        ), patch("linkplane.commands.ping.print", side_effect=BrokenPipeError), patch(
            "linkplane.cli.os.dup2"
        ), redirect_stderr(err):
            code = main(["ping"])
        self.assertEqual(code, EXIT_BROKEN_PIPE)
        self.assertEqual(err.getvalue(), "")


class GlobalFlagTests(unittest.TestCase):
    def test_help_lists_core_commands(self):
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            main(["--help"])
        for name in ("battery", "ping", "capabilities", "devices", "status", "doctor"):
            self.assertIn(name, out.getvalue())

    def test_debug_flag_is_accepted(self):
        code, out, _ = run(["--debug", "ping"], FakeProvider())
        self.assertEqual(code, 0)
        self.assertIn("reachable", out)


if __name__ == "__main__":
    unittest.main()
