"""Device tier: the Core v0.1 release-gate smoke test against the paired phone over ADB.

Runs the core commands through the real CLI entry point and asserts exit codes and JSON
envelope shape. Skips (rather than fails) when no authorized ADB device is connected.
"""

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from linkplane.cli import main
from linkplane.core.capability import CATALOGUE
from linkplane.transports import AdbTransport, BridgeError


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CoreSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.serial = AdbTransport(None).select_device()["serial"]
        except BridgeError as error:
            raise unittest.SkipTest(f"no authorized ADB device: {error}")

    def assert_envelope(self, out):
        envelope = json.loads(out)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertTrue(envelope["ok"], envelope)
        return envelope["data"]

    def test_ping(self):
        code, out, _ = run(["ping", "--transport", "adb", "--json"])
        self.assertEqual(code, 0)
        data = self.assert_envelope(out)
        self.assertTrue(data["reachable"])
        self.assertEqual(data["address"], self.serial)

    def test_battery(self):
        code, out, _ = run(["battery", "--transport", "adb", "--json"])
        self.assertEqual(code, 0)
        data = self.assert_envelope(out)
        self.assertTrue(0 <= data["level"] <= 100)
        self.assertEqual(data["provider"], "adb")

    def test_status(self):
        code, out, _ = run(["status", "--transport", "adb", "--json"])
        self.assertEqual(code, 0)
        data = self.assert_envelope(out)
        self.assertEqual(data["transport"], "adb")
        self.assertEqual(data["device"]["serial"], self.serial)

    def test_capabilities(self):
        code, out, _ = run(["capabilities", "--transport", "adb", "--json"])
        self.assertEqual(code, 0)
        data = self.assert_envelope(out)
        names = [report["name"] for report in data["capabilities"]]
        self.assertEqual(names, list(CATALOGUE))
        supported = {r["name"] for r in data["capabilities"] if r["status"] == "supported"}
        self.assertTrue({"device.ping", "device.status", "battery.read", "storage.read"} <= supported)

    def test_devices(self):
        code, out, _ = run(["devices", "--json"])
        envelope = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn(self.serial, [e["address"] for d in envelope["data"]["devices"] for e in d["endpoints"]])

    def test_doctor_core_checks_pass_over_adb(self):
        code, out, _ = run(["doctor", "--transport", "adb", "--json"])
        envelope = json.loads(out)
        checks = {check["name"]: check for check in envelope["data"]["checks"]}
        for name in ("Configuration", "Provider", "Phone reachable", "Provider responds", "Capability query"):
            self.assertEqual(checks[name]["status"], "ok", checks[name])
        self.assertEqual(code, 0)

    def test_unknown_serial_is_a_coded_error(self):
        code, out, _ = run(["ping", "--transport", "adb", "--serial", "NOPE-SMOKE", "--json"])
        envelope = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "LP-CONNECT-002")


if __name__ == "__main__":
    unittest.main()
