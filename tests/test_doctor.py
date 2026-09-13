import unittest

from linkplane.core.capability import CATALOGUE
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from linkplane.doctor import (
    DoctorRequest,
    build_checks,
    executable_version,
    run_diagnostics,
)
from linkplane.dependencies import ScrcpyCompatibility
from linkplane.models import Capability, Check, Device, DiscoveryResult, Endpoint
from linkplane.transports import BridgeError


class DoctorTests(unittest.TestCase):
    @patch("linkplane.doctor.run_command", side_effect=BridgeError("broken executable"))
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/tool")
    def test_broken_executable_is_not_reported_as_healthy(self, _which, _run):
        version, error = executable_version("tool", ["--version"])

        self.assertIsNone(version)
        self.assertEqual(error, "broken executable")

    @patch("linkplane.doctor.executable_version")
    @patch(
        "linkplane.doctor.scrcpy_compatibility",
        return_value=ScrcpyCompatibility(True, True),
    )
    @patch("linkplane.doctor.scrcpy_dependency_plan")
    def test_capability_check_reports_dependency_state(
        self, dependency, _support, version
    ):
        dependency.return_value.install_command = ("install", "scrcpy")
        version.side_effect = [
            ("adb 1.0", None),
            ("OpenSSH 10", None),
            (None, None),
            ("localsend-cli 1.0", None),
        ]
        endpoint = Endpoint(
            transport="adb",
            address="serial-1",
            state="connected",
            device_id="serial-1",
            capabilities=(
                Capability("status", "ready"),
                Capability("screen", "needs_dependency", "scrcpy missing"),
            ),
        )
        device = Device(
            id="serial-1",
            name="Phone",
            manufacturer=None,
            model=None,
            android=None,
            capabilities=endpoint.capabilities,
            endpoints=(endpoint,),
        )

        checks = build_checks(DiscoveryResult((device,)))

        capability_check = next(check for check in checks if check.name == "Capabilities")
        self.assertEqual(capability_check.status, "warning")
        self.assertIn("ready: status", capability_check.summary)
        self.assertIn("setup needed: screen", capability_check.summary)

    @patch("linkplane.doctor.executable_version", return_value=(None, None))
    @patch(
        "linkplane.dependencies.shutil.which",
        side_effect=lambda command: (
            f"/usr/bin/{command}" if command in {"apt-get", "sudo"} else None
        ),
    )
    def test_diagnostics_uses_distro_specific_dependency_fixes(self, _which, _version):
        with patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland"}, clear=True):
            checks = build_checks(DiscoveryResult(()))

        fixes = {check.name: check.fix for check in checks}
        self.assertEqual(fixes["ADB"], "install it with: sudo apt-get install adb")
        self.assertEqual(
            fixes["SSH client"],
            "install it with: sudo apt-get install openssh-client",
        )
        self.assertEqual(
            fixes["Screen"], "install it with: sudo apt-get install scrcpy"
        )
        self.assertEqual(
            fixes["Desktop clipboard"],
            "install it with: sudo apt-get install wl-clipboard",
        )
        self.assertEqual(
            fixes["LocalSend"],
            "automatic installation is not supported with apt-get",
        )

    @patch("linkplane.doctor.executable_version", return_value=("scrcpy 1.25", None))
    @patch(
        "linkplane.doctor.scrcpy_compatibility",
        return_value=ScrcpyCompatibility(
            False,
            False,
            missing_screen_options=("--video-bit-rate",),
        ),
    )
    def test_diagnostics_warns_when_scrcpy_lacks_required_options(
        self, _support, _version
    ):
        checks = build_checks(DiscoveryResult(()))

        screen = next(check for check in checks if check.name == "Screen")
        self.assertEqual(screen.status, "warning")
        self.assertIn("--video-bit-rate", screen.summary)
        self.assertEqual(screen.fix, "Update or repair scrcpy")

    @patch("linkplane.doctor.build_checks")
    @patch("builtins.print")
    def test_diagnostics_returns_immutable_result_without_printing(self, output, checks):
        checks.return_value = [Check("ADB", "error", "not installed", "Install ADB")]
        request = DoctorRequest(DiscoveryResult(()))

        result = run_diagnostics(request)

        self.assertTrue(result.ok)
        self.assertIsInstance(result.value.checks, tuple)
        self.assertEqual(
            result.value.to_dict(),
            {
                "checks": [
                    {
                        "name": "ADB",
                        "status": "error",
                        "summary": "not installed",
                        "fix": "Install ADB",
                        "code": None,
                    }
                ]
            },
        )
        with self.assertRaises(FrozenInstanceError):
            request.discovery = DiscoveryResult(())
        output.assert_not_called()

    @patch("linkplane.doctor.build_checks", side_effect=BridgeError("probe failed"))
    def test_diagnostics_returns_typed_operation_failure(self, _checks):
        result = run_diagnostics(DoctorRequest(DiscoveryResult(())))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "operation_failed")
        self.assertEqual(result.error.message, "probe failed")


if __name__ == "__main__":
    unittest.main()


class FakeProvider:
    """Just enough of the Provider surface for the coded doctor checks."""

    def __init__(
        self,
        *,
        name="adb",
        address="SERIAL1",
        reachable=True,
        status=None,
        status_error=None,
        capabilities=None,
        identity_file=None,
    ):
        from types import SimpleNamespace

        self.name = name
        self.address = address
        self._reachable = reachable
        self._status = status
        self._status_error = status_error
        self._capabilities = capabilities
        self.transport = SimpleNamespace(identity_file=identity_file)

    def ping(self):
        from linkplane.providers.base import PingResult

        if self._reachable:
            return PingResult(True, self.name, self.address, 12.0)
        return PingResult(False, self.name, self.address, detail="ssh: connect to host x port 8022: Connection refused")

    def status(self):
        if self._status_error is not None:
            raise self._status_error
        if self._status is not None:
            return self._status
        from linkplane.status import StatusResult

        return StatusResult("adb", {"model": "Phone"}, {"level": 1}, {"x": 1}, {"y": 1}, 1, ())

    def battery(self):
        raise AssertionError("not used")

    def capabilities(self):
        from linkplane.core.capability import SUPPORTED, CapabilityReport, complete, CATALOGUE

        if self._capabilities is not None:
            return self._capabilities
        return complete(
            {
                name: CapabilityReport(name, SUPPORTED, "fake")
                for name in ("device.ping", "device.status", "battery.read", "storage.read")
            },
            self.name,
        )


class CoreDoctorChecksTests(unittest.TestCase):
    """Coded configuration and provider checks (docs/core-v0.1-continuation.md, Task 1)."""

    def by_name(self, checks):
        return {check.name: check for check in checks}

    def test_missing_configuration_file_is_a_coded_warning(self):
        import tempfile
        from pathlib import Path

        from linkplane.doctor import configuration_checks

        with tempfile.TemporaryDirectory() as directory:
            checks, config = configuration_checks(str(Path(directory) / "missing.json"))

        self.assertEqual(config, {})
        check = self.by_name(checks)["Configuration"]
        self.assertEqual((check.status, check.code), ("warning", "LP-CONFIG-001"))
        self.assertIn("pair", check.fix)

    def test_invalid_configuration_is_a_coded_error(self):
        import tempfile
        from pathlib import Path

        from linkplane.doctor import configuration_checks

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            checks, config = configuration_checks(str(path))

        self.assertIsNone(config)
        check = self.by_name(checks)["Configuration"]
        self.assertEqual((check.status, check.code), ("error", "LP-CONFIG-002"))

    def test_default_device_states(self):
        import json
        import tempfile
        from pathlib import Path

        from linkplane.doctor import configuration_checks

        cases = [
            ({"devices": {"a": {"device_id": "x"}}, "default_device": "a"}, "ok", None),
            ({"devices": {"a": {"device_id": "x"}}, "default_device": "zzz"}, "error", "LP-CONFIG-003"),
            ({"devices": {"a": {"device_id": "x"}}}, "warning", "LP-CONFIG-001"),
            ({"ssh": {"host": "h", "user": "u"}}, "ok", None),
            ({}, "warning", "LP-CONFIG-001"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for config, status, code in cases:
                with self.subTest(config=config):
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    checks, _ = configuration_checks(str(path))
                    check = self.by_name(checks)["Default device"]
                    self.assertEqual((check.status, check.code), (status, code))

    def test_healthy_provider_passes_every_check(self):
        from linkplane.doctor import provider_checks

        checks = self.by_name(provider_checks(lambda: FakeProvider()))

        self.assertEqual(
            [check.status for check in checks.values()], ["ok"] * len(checks)
        )
        self.assertEqual(
            list(checks), ["Provider", "Phone reachable", "Provider responds", "Capability query"]
        )
        self.assertEqual(checks["Capability query"].summary, f"4 of {len(CATALOGUE)} capabilities supported")
        self.assertTrue(all(check.code is None for check in checks.values()))

    def test_unresolvable_provider_carries_its_code_and_hints(self):
        from linkplane.core import errors
        from linkplane.doctor import provider_checks

        def resolver():
            raise errors.LinkplaneError(errors.CONFIG_MISSING, "SSH transport is not configured", ("run pair ssh",))

        checks = provider_checks(resolver)

        self.assertEqual(len(checks), 1)
        self.assertEqual((checks[0].status, checks[0].code), ("error", "LP-CONFIG-001"))
        self.assertEqual(checks[0].fix, "run pair ssh")

    def test_unreachable_phone_stops_after_reachability(self):
        from linkplane.doctor import provider_checks

        checks = self.by_name(provider_checks(lambda: FakeProvider(name="ssh", reachable=False, identity_file=None)))

        self.assertEqual(checks["Phone reachable"].status, "error")
        self.assertEqual(checks["Phone reachable"].code, "LP-CONNECT-001")
        self.assertIn("sshd", checks["Phone reachable"].fix)
        self.assertEqual(checks["Provider responds"].status, "error")
        self.assertNotIn("Capability query", checks)
        self.assertEqual(checks["SSH identity"].status, "warning")

    def test_missing_ssh_identity_is_an_auth_error(self):
        from linkplane.doctor import provider_checks

        checks = self.by_name(
            provider_checks(lambda: FakeProvider(name="ssh", identity_file="/nonexistent/key"))
        )

        self.assertEqual((checks["SSH identity"].status, checks["SSH identity"].code), ("error", "LP-AUTH-001"))
        self.assertEqual(checks["Authentication"].status, "ok")

    def test_partial_status_is_a_coded_warning(self):
        from linkplane.doctor import provider_checks
        from linkplane.status import StatusResult, TelemetryIssue

        partial = StatusResult("adb", {"model": "Phone"}, None, {"x": 1}, None, 1, (TelemetryIssue("battery", "denied"),))
        checks = self.by_name(provider_checks(lambda: FakeProvider(status=partial)))

        check = checks["Provider responds"]
        self.assertEqual((check.status, check.code), ("warning", "LP-PROVIDER-003"))
        self.assertIn("battery (denied)", check.summary)

    def test_provider_status_failure_is_a_coded_error(self):
        from linkplane.core import errors
        from linkplane.doctor import provider_checks

        failure = errors.LinkplaneError(errors.PROVIDER_BAD_OUTPUT, "Termux returned invalid status JSON")
        checks = self.by_name(provider_checks(lambda: FakeProvider(status_error=failure)))

        self.assertEqual((checks["Provider responds"].status, checks["Provider responds"].code), ("error", "LP-PROVIDER-002"))
        self.assertEqual(checks["Capability query"].status, "ok")

    def test_missing_remote_script_is_reported_for_ssh(self):
        from linkplane.core.capability import SUPPORTED, UNAVAILABLE, CapabilityReport, complete
        from linkplane.doctor import provider_checks

        reports = complete(
            {
                "device.ping": CapabilityReport("device.ping", SUPPORTED, "x"),
                "device.status": CapabilityReport("device.status", UNAVAILABLE, "phone-status-json.sh is not installed on the phone"),
            },
            "ssh",
        )
        checks = self.by_name(
            provider_checks(lambda: FakeProvider(name="ssh", identity_file=None, capabilities=reports))
        )

        self.assertEqual((checks["Remote scripts"].status, checks["Remote scripts"].code), ("error", "LP-CAPABILITY-002"))
        self.assertIn("legacy/termux/phone-status-json.sh", checks["Remote scripts"].fix)

    def test_undetermined_capabilities_are_a_coded_warning(self):
        from linkplane.core.capability import PROVIDER_ERROR, SUPPORTED, CapabilityReport, complete
        from linkplane.doctor import provider_checks

        reports = complete(
            {
                "device.ping": CapabilityReport("device.ping", SUPPORTED, "x"),
                "battery.read": CapabilityReport("battery.read", PROVIDER_ERROR, "probe failed"),
            },
            "adb",
        )
        checks = self.by_name(provider_checks(lambda: FakeProvider(capabilities=reports)))

        check = checks["Capability query"]
        self.assertEqual((check.status, check.code), ("warning", "LP-CAPABILITY-002"))
        self.assertIn("undetermined: battery.read", check.summary)

    @patch("linkplane.doctor.build_checks", return_value=[])
    def test_run_diagnostics_includes_core_checks_only_with_a_resolver(self, _build):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "config.json")
            without = run_diagnostics(DoctorRequest(DiscoveryResult(()), config_path=path))
            with_resolver = run_diagnostics(
                DoctorRequest(DiscoveryResult(()), config_path=path, provider_resolver=lambda: FakeProvider())
            )

        self.assertEqual(without.value.checks, ())
        names = [check.name for check in with_resolver.value.checks]
        # The temporary config path does not exist, so only the Configuration check runs
        # before the provider checks.
        self.assertEqual(names[:2], ["Configuration", "Provider"])
        self.assertIn("Capability query", names)


class OnboardingDoctorChecksTests(unittest.TestCase):
    """Additive Slice 0 checks doctor and the future setup share."""

    @staticmethod
    def discovery(state):
        endpoint = Endpoint(transport="adb", address="FAKESERIAL01", state=state, device_id="FAKESERIAL01", error=f"ADB device is {state}")
        device = Device(id="FAKESERIAL01", name="phone", manufacturer=None, model=None, android=None, capabilities=(), endpoints=(endpoint,))
        return DiscoveryResult((device,))

    def test_usb_access_distinguishes_host_permission_phone_authorization_and_offline(self):
        from linkplane.core import errors
        from linkplane.doctor import usb_access_checks

        with patch("linkplane.dependencies.shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in {"pacman", "sudo"} else None):
            denied = usb_access_checks(self.discovery("no permissions"))
        self.assertEqual([(c.name, c.status, c.code) for c in denied], [("USB access", "error", errors.AUTH_USB_PERMISSION)])
        self.assertIn("android-udev", denied[0].fix)
        self.assertNotIn("Allow", denied[0].fix)

        waiting = usb_access_checks(self.discovery("unauthorized"))
        self.assertEqual((waiting[0].status, waiting[0].code), ("warning", errors.AUTH_UNAUTHORIZED_DEVICE))
        self.assertIn("Tap Allow", waiting[0].fix)

        offline = usb_access_checks(self.discovery("offline"))
        self.assertEqual((offline[0].status, offline[0].code), ("warning", errors.CONNECT_UNREACHABLE))

        self.assertEqual(usb_access_checks(self.discovery("connected")), [])
        self.assertEqual(usb_access_checks(DiscoveryResult(())), [])

    def test_loose_configuration_permissions_are_a_warning(self):
        import os
        import tempfile
        from pathlib import Path

        from linkplane.doctor import configuration_permission_check

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            os.chmod(path, 0o644)
            loose = configuration_permission_check(path)
            os.chmod(path, 0o600)
            tight = configuration_permission_check(path)

        self.assertEqual((loose.name, loose.status), ("Configuration permissions", "warning"))
        self.assertIn("chmod 600", loose.fix)
        self.assertIsNone(tight)
        self.assertIsNone(configuration_permission_check(Path("/nonexistent/config.json")))

    @patch("linkplane.doctor.executable_version", return_value=(None, None))
    @patch("linkplane.doctor.scrcpy_compatibility", return_value=ScrcpyCompatibility(False, False))
    @patch(
        "linkplane.dependencies.shutil.which",
        side_effect=lambda command: f"/usr/bin/{command}" if command in {"apt-get", "sudo"} else None,
    )
    def test_desktop_notifications_are_an_optional_warning_with_a_fix(self, _which, _support, _version):
        with patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland"}, clear=True):
            checks = {check.name: check for check in build_checks(DiscoveryResult(()))}

        self.assertEqual(checks["Desktop notifications"].status, "warning")
        self.assertEqual(checks["Desktop notifications"].fix, "install it with: sudo apt-get install libnotify-bin")
        # Optional tools are warnings; only ADB and the device are errors on a bare host.
        errors_named = sorted(name for name, check in checks.items() if check.status == "error")
        self.assertEqual(errors_named, ["ADB", "Capabilities", "Device"])
