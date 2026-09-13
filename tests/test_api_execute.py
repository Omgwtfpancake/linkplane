"""`linkplane.api.execute`: provider choice, capability gate, audit sanitising, effective capabilities."""

import unittest
from unittest.mock import patch

from linkplane.api import execute
from linkplane.api.actions import spec_for
from linkplane.core import errors
from linkplane.core.capability import CATALOGUE, PERMISSION_DENIED, SUPPORTED, UNAVAILABLE, UNSUPPORTED, CapabilityReport, complete
from linkplane.registry import Target


class SelectTests(unittest.TestCase):
    def _select(self, target, action):
        with patch("linkplane.api.execute.select_provider") as selector:
            selector.return_value = object()
            execute.select(target, spec_for(action))
        return selector.call_args

    def test_both_providers_use_auto_with_the_serial(self):
        call = self._select(Target("HW1", "phone", "S1", {"host": "h", "user": "u", "port": 8022}), "battery.read")
        self.assertEqual((call.args[0], call.kwargs["serial"], call.kwargs["serial_from_profile"]), ("auto", "S1", True))
        self.assertIsNotNone(call.kwargs["ssh_factory"])

    def test_no_serial_never_falls_through_to_any_phone(self):
        call = self._select(Target("HW1", "phone", None, {"host": "h", "user": "u", "port": 8022}), "battery.read")
        self.assertEqual((call.args[0], call.kwargs["serial"]), ("ssh", None))
        with self.assertRaises(errors.LinkplaneError) as raised:
            execute.select(Target("HW2", "tablet", None, None), spec_for("device.find"))
        self.assertEqual(raised.exception.code, errors.CONNECT_NO_DEVICE)

    def test_ssh_only_action_without_ssh_is_unavailable(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            execute.select(Target("HW2", "tablet", "S2", None), spec_for("clipboard.read"))
        self.assertEqual(raised.exception.code, errors.CAPABILITY_UNAVAILABLE)
        call = self._select(Target("HW1", "phone", "S1", {"host": "h", "user": "u"}), "clipboard.read")
        self.assertEqual((call.args[0], call.kwargs["serial"]), ("ssh", None))
        call = self._select(Target("HW1", "phone", "S1", None), "device.find")
        self.assertEqual((call.args[0], call.kwargs["serial"]), ("adb", "S1"))


class CapabilityGateTests(unittest.TestCase):
    class Provider:
        name = "adb"

        def __init__(self, status, detail=None):
            self.report = CapabilityReport("battery.read", status, detail)

        def capabilities(self):
            return complete({"battery.read": self.report}, "adb")

    def test_statuses_map_to_codes(self):
        target = Target("HW1", "phone", "S1")
        spec = spec_for("battery.read")
        execute.check_capability(self.Provider(SUPPORTED), spec, target)
        for status, code in ((UNSUPPORTED, errors.CAPABILITY_UNSUPPORTED), (UNAVAILABLE, errors.CAPABILITY_UNAVAILABLE),
                             (PERMISSION_DENIED, errors.AUTH_UNAUTHORIZED_DEVICE), ("provider-error", errors.CAPABILITY_UNAVAILABLE)):
            with self.assertRaises(execute.ActionError) as raised:
                execute.check_capability(self.Provider(status, "why"), spec, target)
            self.assertEqual(raised.exception.code, code, status)
            self.assertEqual(raised.exception.details["capability"]["status"], status)


class AuditParameterTests(unittest.TestCase):
    def test_clipboard_text_and_long_messages_never_reach_the_audit(self):
        self.assertEqual(execute.audit_parameters(spec_for("clipboard.write"), {"text": "hello"}), {"text_length": 5})
        kept = execute.audit_parameters(spec_for("notify.post"), {"message": "m" * 500, "title": "t"})
        self.assertEqual((len(kept["message"]), kept["title"]), (200, "t"))
        self.assertEqual(execute.audit_parameters(spec_for("files.send"), {"paths": ["/a"]}), {"paths": ["/a"]})


class EffectiveCapabilityTests(unittest.TestCase):
    def test_first_supported_provider_wins_in_auto_order(self):
        adb = complete({"battery.read": CapabilityReport("battery.read", UNAVAILABLE, "unplugged")}, "adb")
        ssh = complete({"battery.read": CapabilityReport("battery.read", SUPPORTED), "clipboard.read": CapabilityReport("clipboard.read", SUPPORTED)}, "ssh")
        effective = dict(execute.effective_capabilities({"ssh": ssh, "adb": adb}))
        chosen = {name: provider for provider, report in execute.effective_capabilities({"ssh": ssh, "adb": adb}) for name in [report.name]}
        self.assertEqual((chosen["battery.read"], chosen["clipboard.read"], chosen["screen.control"]), ("ssh", "ssh", "adb"))
        self.assertEqual(len(execute.effective_capabilities({"adb": adb})), len(CATALOGUE))
        self.assertEqual(execute.effective_capabilities({}), [])
