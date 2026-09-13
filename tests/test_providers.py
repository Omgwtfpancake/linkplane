"""Provider contract tests.

`ProviderContract` states the behaviour every provider must satisfy (docs/core-v0.1-brief.md
"Provider Contract Tests"); `SSHProviderTests` and `ADBProviderTests` run that same contract
against each implementation with fake command runners, so a future provider (an Android
agent, say) only has to add one subclass supplying its own fakes. No test here touches a
phone.
"""

import json
import unittest

from linkplane.core import errors
from linkplane.core.capability import (
    CATALOGUE,
    PERMISSION_DENIED,
    STATUSES,
    SUPPORTED,
    UNAVAILABLE,
    UNSUPPORTED,
)
from linkplane.providers import ADBProvider, SSHProvider, select_provider
from linkplane.providers.base import BatteryReading, PingResult, Provider
from linkplane.status import StatusResult
from linkplane.transports import AdbTransport, BridgeError, SshTransport


class ProviderContract:
    """Mixed into a TestCase per provider. Subclasses build providers in three states."""

    def healthy_provider(self) -> Provider:
        raise NotImplementedError

    def unreachable_provider(self) -> Provider:
        raise NotImplementedError

    def malformed_provider(self) -> Provider:
        raise NotImplementedError

    def timing_out_provider(self) -> Provider:
        raise NotImplementedError

    def test_reports_every_catalogue_capability_in_order(self):
        reports = self.healthy_provider().capabilities()
        self.assertEqual(tuple(report.name for report in reports), CATALOGUE)
        for report in reports:
            self.assertIn(report.status, STATUSES)
        core = {report.name: report.status for report in reports[:4]}
        self.assertEqual(set(core.values()), {SUPPORTED})

    def test_local_api_capabilities_are_reported_per_provider(self):
        provider = self.healthy_provider()
        reports = {report.name: report for report in provider.capabilities()}
        if provider.name == "adb":
            self.assertEqual(reports["device.find"].status, SUPPORTED)
            self.assertEqual(reports["clipboard.sync"].status, UNSUPPORTED)
            # Capability truth (Slice 0 §7): say what would enable it, not "not implemented".
            from linkplane.providers.adb import TERMUX_API_DETAIL

            for name in ("clipboard.read", "clipboard.write", "clipboard.sync", "camera.capture"):
                self.assertEqual((reports[name].status, reports[name].detail), (UNSUPPORTED, TERMUX_API_DETAIL))
            self.assertIn("Termux:API", TERMUX_API_DETAIL)
        else:
            self.assertEqual(reports["clipboard.sync"].status, SUPPORTED)
            self.assertEqual(reports["device.find"].status, UNSUPPORTED)

    def test_returns_device_status(self):
        status = self.healthy_provider().status()
        self.assertIsInstance(status, StatusResult)
        self.assertEqual(status.battery["level"], 42)

    def test_returns_battery_reading(self):
        reading = self.healthy_provider().battery()
        self.assertIsInstance(reading, BatteryReading)
        self.assertEqual(reading.level, 42)
        self.assertEqual(reading.provider, self.healthy_provider().name)
        self.assertIn("level", reading.to_dict())

    def test_ping_reports_latency_when_reachable(self):
        result = self.healthy_provider().ping()
        self.assertIsInstance(result, PingResult)
        self.assertTrue(result.reachable)
        self.assertIsNotNone(result.latency_ms)

    def test_ping_never_raises_for_unreachable_device(self):
        result = self.unreachable_provider().ping()
        self.assertFalse(result.reachable)
        self.assertTrue(result.detail)

    def test_unreachable_device_is_a_structured_connect_error(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            self.unreachable_provider().battery()
        self.assertTrue(raised.exception.code.startswith("LP-CONNECT-"))
        self.assertTrue(raised.exception.hints)

    def test_malformed_remote_output_is_a_provider_error(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            self.malformed_provider().battery()
        self.assertTrue(raised.exception.code.startswith("LP-PROVIDER-"))

    def test_timeout_is_a_timeout_error(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            self.timing_out_provider().status()
        self.assertEqual(raised.exception.code, errors.TIMEOUT)

    def test_errors_are_still_bridge_errors_for_existing_callers(self):
        with self.assertRaises(BridgeError):
            self.unreachable_provider().status()


TERMUX_STATUS = {
    "device": {"manufacturer": "samsung", "model": "SM-S901U", "android": "16"},
    "battery": {"percentage": 42, "status": "CHARGING", "health": "GOOD", "plugged": "PLUGGED_USB"},
    "memory": {"total_kb": 8000000, "available_kb": 2000000},
    "storage": {"total_kb": 100, "used_kb": 60, "free_kb": 40, "used_percent": 60},
}
SSH_PROBE = (
    "manufacturer=samsung\nmodel=SM-S901U\nandroid=16\n"
    "command=phone-status-json.sh\ncommand=termux-notification\n"
    "command=termux-clipboard-get\ncommand=termux-clipboard-set\n"
)


def ssh_runner(*, status=TERMUX_STATUS, probe=SSH_PROBE, fail=None):
    def runner(command, **_kwargs):
        if fail is not None:
            raise BridgeError(fail)
        remote = command[-1]
        if remote == "printf ok":
            return "ok"
        if "phone-status-json.sh" in remote and remote.startswith("$HOME"):
            return status if isinstance(status, str) else json.dumps(status)
        if "getprop" in remote:
            return probe
        raise AssertionError(command)

    return runner


class SSHProviderTests(ProviderContract, unittest.TestCase):
    def _provider(self, **kwargs):
        return SSHProvider(SshTransport("phone.local", "u0", 8022, runner=ssh_runner(**kwargs)))

    def healthy_provider(self):
        return self._provider()

    def unreachable_provider(self):
        return self._provider(fail="ssh: connect to host phone.local port 8022: No route to host")

    def malformed_provider(self):
        return self._provider(status="not json at all")

    def timing_out_provider(self):
        return self._provider(fail="ssh timed out after 8 seconds")

    def test_address_is_the_ssh_endpoint(self):
        self.assertEqual(self.healthy_provider().address, "u0@phone.local:8022")

    def test_missing_remote_script_makes_status_capabilities_unavailable(self):
        provider = self._provider(probe="manufacturer=samsung\ncommand=termux-notification\n")
        reports = {report.name: report for report in provider.capabilities()}
        self.assertEqual(reports["device.ping"].status, SUPPORTED)
        self.assertEqual(reports["device.status"].status, UNAVAILABLE)
        self.assertIn("phone-status-json.sh", reports["battery.read"].detail)
        self.assertEqual(reports["notify.post"].status, SUPPORTED)
        self.assertEqual(reports["clipboard.read"].status, UNAVAILABLE)
        self.assertEqual(reports["files.send"].status, UNSUPPORTED)
        self.assertEqual(reports["screen.control"].status, UNSUPPORTED)

    def test_failed_probe_marks_core_capabilities_as_undetermined(self):
        reports = {r.name: r for r in self.unreachable_provider().capabilities()}
        self.assertEqual(reports["device.ping"].status, UNAVAILABLE)
        self.assertEqual(reports["battery.read"].status, "provider-error")

    def test_partial_status_without_battery_is_a_partial_provider_error(self):
        provider = self._provider(status={**TERMUX_STATUS, "battery": "broken"})
        with self.assertRaises(errors.LinkplaneError) as raised:
            provider.battery()
        self.assertEqual(raised.exception.code, errors.PROVIDER_PARTIAL)


DUMPSYS_BATTERY = """Current Battery Service state:
  AC powered: false
  USB powered: true
  status: 2
  health: 2
  level: 42
  temperature: 311
  voltage: 4245
"""


def adb_runner(*, devices="List of devices attached\nSERIAL1\tdevice product:x model:Phone\n",
               battery=DUMPSYS_BATTERY, fail=None):
    def runner(command, **_kwargs):
        if fail is not None:
            raise BridgeError(fail)
        if command[:3] == ["adb", "devices", "-l"]:
            return devices
        tail = command[4:]
        if tail == ["echo", "ok"]:
            return "ok\n"
        if tail == ["dumpsys", "battery"]:
            return battery
        if tail == ["getprop", "ro.product.manufacturer"]:
            return "samsung\n"
        if tail == ["getprop", "ro.product.model"]:
            return "SM-S901U\n"
        if tail == ["getprop", "ro.build.version.release"]:
            return "16\n"
        if tail == ["uname", "-r"]:
            return "6.1\n"
        if tail == ["cat", "/proc/meminfo"]:
            return "MemTotal: 8000000 kB\nMemAvailable: 2000000 kB\n"
        if tail == ["df", "-Pk", "/sdcard"]:
            return "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 100 60 40 60% /sdcard\n"
        if tail == ["cat", "/proc/uptime"]:
            return "100.0 200.0\n"
        raise AssertionError(command)

    return runner


class ADBProviderTests(ProviderContract, unittest.TestCase):
    def _provider(self, serial="SERIAL1", **kwargs):
        return ADBProvider(AdbTransport(serial, adb_runner(**kwargs)))

    def healthy_provider(self):
        with unittest.mock.patch("linkplane.transports.shutil.which", return_value="/usr/bin/adb"):
            return self._provider()

    def unreachable_provider(self):
        return self._provider(fail="no ADB device connected")

    def malformed_provider(self):
        return self._provider(battery="garbage without a level")

    def timing_out_provider(self):
        return self._provider(fail="adb timed out after 8 seconds")

    def setUp(self):
        patcher = unittest.mock.patch(
            "linkplane.transports.shutil.which", return_value="/usr/bin/adb"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unauthorized_device_is_permission_denied(self):
        provider = self._provider(
            devices="List of devices attached\nSERIAL1\tunauthorized\n"
        )
        reports = {report.name: report for report in provider.capabilities()}
        self.assertEqual(reports["battery.read"].status, PERMISSION_DENIED)
        with self.assertRaises(errors.LinkplaneError) as raised:
            provider.battery()
        self.assertEqual(raised.exception.code, errors.AUTH_UNAUTHORIZED_DEVICE)

    def test_battery_reads_dumpsys_directly(self):
        reading = self.healthy_provider().battery()
        self.assertEqual(reading.powered_by, ("usb",))
        self.assertEqual(reading.temperature_c, 31.1)
        self.assertEqual(reading.voltage_mv, 4245)


class SelectProviderTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch(
            "linkplane.transports.shutil.which", return_value="/usr/bin/adb"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_prefers_adb(self):
        provider = select_provider(
            "auto",
            adb_factory=lambda serial: AdbTransport(serial, adb_runner()),
            ssh_factory=lambda: SshTransport("phone.local", "u0", runner=ssh_runner()),
        )
        self.assertIsInstance(provider, ADBProvider)

    def test_auto_falls_back_to_ssh_when_adb_has_no_device(self):
        provider = select_provider(
            "auto",
            adb_factory=lambda serial: AdbTransport(serial, adb_runner(fail="no ADB device connected")),
            ssh_factory=lambda: SshTransport("phone.local", "u0", runner=ssh_runner()),
        )
        self.assertIsInstance(provider, SSHProvider)

    def test_explicit_serial_never_falls_back(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            select_provider(
                "auto",
                serial="SERIAL9",
                adb_factory=lambda serial: AdbTransport(serial, adb_runner(fail="ADB device SERIAL9 was not found")),
                ssh_factory=lambda: SshTransport("phone.local", "u0", runner=ssh_runner()),
            )
        self.assertEqual(raised.exception.code, errors.CONNECT_NO_DEVICE)

    def test_no_ssh_configured_is_a_config_error(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            select_provider(
                "ssh",
                ssh_factory=lambda: SshTransport(None, None, runner=ssh_runner()),
            )
        self.assertEqual(raised.exception.code, errors.CONFIG_MISSING)

    def test_both_unavailable_combines_hints(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            select_provider(
                "auto",
                adb_factory=lambda serial: AdbTransport(serial, adb_runner(fail="no ADB device connected")),
                ssh_factory=None,
            )
        self.assertEqual(raised.exception.code, errors.CONNECT_UNREACHABLE)
        self.assertIn("ADB unavailable", str(raised.exception))

    def test_invalid_transport_is_a_request_error(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            select_provider("carrier-pigeon")
        self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)


import unittest.mock  # noqa: E402  (used by the ADB fixtures above)

if __name__ == "__main__":
    unittest.main()
