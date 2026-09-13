"""`linkplane.api.actions`: the declarative action table (design §8, §9)."""

import unittest

from linkplane.api import actions
from linkplane.api.actions import ACTIONS, JOB_ACTIONS, NOT_IMPLEMENTED, SEAM_FIELDS, ActionSpec, build_request, spec_for, validate_parameters
from linkplane.core import errors
from linkplane.core.capability import CATALOGUE
from linkplane.notification import NotificationRequest
from linkplane.transfer import SendRequest


class ActionTableTests(unittest.TestCase):
    def test_every_action_is_a_catalogue_capability(self):
        for name in ACTIONS:
            self.assertIn(name, CATALOGUE)

    def test_execution_mode_is_declared_not_inferred(self):
        self.assertEqual(JOB_ACTIONS, {"files.send": "send", "backup.photos": "backup", "clipboard.sync": "clipboard-sync"})
        for name, spec in ACTIONS.items():
            self.assertEqual(spec.execution, actions.JOB if name in JOB_ACTIONS else actions.IMMEDIATE)
            self.assertEqual(actions.execution_of(name), spec.execution)

    def test_job_action_names_are_the_rule_vocabulary(self):
        from linkplane.actions import JOB_ACTIONS as RULE_JOB_ACTIONS
        self.assertEqual(set(JOB_ACTIONS.values()), set(RULE_JOB_ACTIONS))

    def test_not_implemented_capabilities_are_the_known_gaps(self):
        self.assertEqual(NOT_IMPLEMENTED, ("storage.read", "screen.control"))

    def test_scope_is_the_action_name_and_seams_never_leak(self):
        for spec in ACTIONS.values():
            self.assertEqual(spec.scope, spec.name)
            self.assertFalse(set(spec.parameters) & SEAM_FIELDS)
            self.assertEqual(set(spec.to_dict()), {"action", "execution", "scope", "providers", "parameters", "required", "job_action"})

    def test_spec_validation_rejects_inconsistent_entries(self):
        with self.assertRaises(ValueError):
            ActionSpec("battery.read", "job", ("adb",))  # job without job_action
        with self.assertRaises(ValueError):
            ActionSpec("notify.post", "immediate", ("adb",), NotificationRequest, ("serial",))  # seam
        with self.assertRaises(ValueError):
            ActionSpec("notify.post", "immediate", ("adb",), NotificationRequest, ("nope",))
        with self.assertRaises(ValueError):
            ActionSpec("no.such", "immediate", ("adb",))


class LookupTests(unittest.TestCase):
    def test_unknown_name_is_400_and_unimplemented_capability_is_501(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            spec_for("files.backup")
        self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)
        with self.assertRaises(errors.LinkplaneError) as raised:
            spec_for("screen.control")
        self.assertEqual(raised.exception.code, errors.CAPABILITY_UNSUPPORTED)
        self.assertEqual(spec_for("backup.photos").job_action, "backup")


class ParameterTests(unittest.TestCase):
    def test_parameters_map_onto_the_request_dataclass(self):
        spec = spec_for("notify.post")
        request = build_request(spec, {"message": "hi", "title": "CI"}, transport="adb", serial="S1")
        self.assertEqual(request, NotificationRequest(message="hi", title="CI", transport="adb", serial="S1"))

    def test_required_unknown_seam_and_type_errors_are_request_invalid(self):
        spec = spec_for("notify.post")
        for bad in ({}, {"message": 5}, {"message": "x", "serial": "S1"}, {"message": "x", "install": True}, {"message": "x", "colour": "red"}):
            with self.assertRaises(errors.LinkplaneError) as raised:
                validate_parameters(spec, bad)
            self.assertEqual(raised.exception.code, errors.REQUEST_INVALID, bad)

    def test_lists_become_tuples_and_bools_are_not_ints(self):
        spec = spec_for("files.send")
        request = build_request(spec, {"paths": ["/tmp/a", "/tmp/b"], "dry_run": True}, serial="S1", transport="adb")
        self.assertEqual(request, SendRequest(paths=("/tmp/a", "/tmp/b"), transport="adb", serial="S1", dry_run=True))
        with self.assertRaises(errors.LinkplaneError):
            validate_parameters(spec, {"paths": "/tmp/a"})
        with self.assertRaises(errors.LinkplaneError):
            validate_parameters(spec_for("device.find"), {"duration": True})
        self.assertEqual(validate_parameters(spec_for("device.find"), {"duration": 3}), {"duration": 3})

    def test_fixed_values_pin_the_clipboard_action(self):
        read = build_request(spec_for("clipboard.read"), {})
        self.assertEqual(read.action, "get")
        write = build_request(spec_for("clipboard.write"), {"text": "hello"})
        self.assertEqual((write.action, write.text), ("set", "hello"))
        sync = build_request(spec_for("clipboard.sync"), {"interval": 2})
        self.assertEqual((sync.action, sync.foreground, sync.interval), ("sync", False, 2))
        with self.assertRaises(errors.LinkplaneError):
            validate_parameters(spec_for("clipboard.read"), {"text": "x"})

    def test_actions_without_a_request_type_take_no_parameters(self):
        self.assertIsNone(build_request(spec_for("battery.read"), None))
        with self.assertRaises(errors.LinkplaneError):
            validate_parameters(spec_for("battery.read"), {"anything": 1})
