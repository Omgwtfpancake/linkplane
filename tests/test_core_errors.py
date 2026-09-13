"""`linkplane.core.errors`: stable codes, friendly rendering, and classification."""

import unittest

from linkplane.cli import render_error
from linkplane.core import errors
from linkplane.transports import BridgeError


class ClassifyTests(unittest.TestCase):
    def assert_code(self, message, code, provider=None):
        classified = errors.classify(BridgeError(message), provider=provider)
        self.assertEqual(classified.code, code, message)
        self.assertEqual(str(classified), message)
        return classified

    def test_transport_messages_map_to_stable_codes(self):
        self.assert_code("ssh timed out after 8 seconds", errors.TIMEOUT)
        self.assert_code("ssh: Permission denied (publickey).", errors.AUTH_FAILED)
        self.assert_code("no authorized ADB device; found X (unauthorized)", errors.AUTH_UNAUTHORIZED_DEVICE)
        self.assert_code("multiple ADB devices connected (a, b); use --serial", errors.CONNECT_AMBIGUOUS)
        self.assert_code("scrcpy is not installed", errors.DEPENDENCY_MISSING)
        self.assert_code("no ADB device connected", errors.CONNECT_NO_DEVICE)
        self.assert_code("ssh: connect to host x port 8022: Connection refused", errors.CONNECT_UNREACHABLE)
        self.assert_code("ssh: connect to host x port 8022: Connection timed out", errors.CONNECT_UNREACHABLE)
        self.assert_code("Termux returned invalid status JSON", errors.PROVIDER_BAD_OUTPUT)
        self.assert_code("the ssh configuration must be a JSON object", errors.CONFIG_INVALID)
        self.assert_code("something else entirely", errors.PROVIDER_FAILED)

    def test_host_usb_permission_offline_and_config_write_have_their_own_codes(self):
        # Slice 0 (2026-09-12): `no permissions` used to fall into LP-AUTH-002 via the
        # "no authorized" pattern, sending the user to the phone for a udev problem.
        classified = self.assert_code(
            "this computer has no permissions to access ADB device X over USB", errors.AUTH_USB_PERMISSION
        )
        self.assertEqual(classified.hints, errors.USB_PERMISSION_HINTS)
        self.assertEqual(classified.title, "This computer is not allowed to access the phone over USB.")
        self.assert_code("no authorized ADB device; found X (no permissions)", errors.AUTH_USB_PERMISSION)
        self.assert_code("ADB device X is unauthorized", errors.AUTH_UNAUTHORIZED_DEVICE)
        classified = self.assert_code("ADB device X is offline", errors.CONNECT_UNREACHABLE)
        self.assertEqual(classified.hints, errors.OFFLINE_HINTS)
        for message in (
            "unable to save configuration at /x/config.json: Permission denied",
            "unable to lock configuration at /x/config.json: Read-only file system",
        ):
            classified = self.assert_code(message, errors.CONFIG_UNWRITABLE)
            self.assertEqual(classified.hints, errors.CONFIG_UNWRITABLE_HINTS)
            self.assertEqual(classified.title, "Linkplane could not write its configuration.")
        # Still an invalid configuration, not an unwritable one.
        self.assert_code("the ssh configuration must be a JSON object", errors.CONFIG_INVALID)

    def test_provider_specific_hints(self):
        classified = self.assert_code("ssh timed out after 8 seconds", errors.TIMEOUT, provider="ssh")
        self.assertEqual(classified.hints, errors.SSH_HINTS)
        classified = self.assert_code("no ADB device connected", errors.CONNECT_NO_DEVICE, provider="adb")
        self.assertEqual(classified.hints, errors.ADB_HINTS)

    def test_linkplane_error_passes_through_unchanged(self):
        original = errors.LinkplaneError(errors.CONFIG_MISSING, "no config", ("run setup",))
        self.assertIs(errors.classify(original), original)

    def test_every_code_has_a_title_and_the_map_covers_every_operation_code(self):
        codes = {
            value for name, value in vars(errors).items()
            if name.isupper() and isinstance(value, str) and value.startswith("LP-")
        }
        self.assertEqual(codes, set(errors.TITLES))
        self.assertEqual(
            set(errors.OPERATION_CODE_MAP),
            {"invalid_request", "transport_unavailable", "dependency_missing", "not_found",
             "already_running", "operation_failed", "cancelled"},
        )

    def test_to_dict_is_the_json_error_shape(self):
        error = errors.LinkplaneError(errors.CONNECT_UNREACHABLE, "unreachable", ("a", "b"))
        self.assertEqual(
            error.to_dict(),
            {"type": "LinkplaneError", "message": "unreachable",
             "code": "LP-CONNECT-001", "hints": ["a", "b"]},
        )


class RenderTests(unittest.TestCase):
    def test_friendly_rendering_has_title_hints_and_code(self):
        text = render_error(
            errors.LinkplaneError(errors.CONNECT_UNREACHABLE, "ssh: No route to host", errors.SSH_HINTS)
        )
        self.assertTrue(text.startswith("Phone unreachable.\n"))
        self.assertIn("Check that:\n  • the phone is on the same network", text)
        self.assertTrue(text.endswith("Error: LP-CONNECT-001"))
        self.assertIn("ssh: No route to host", text)

    def test_plain_bridge_error_keeps_the_legacy_one_liner(self):
        self.assertEqual(render_error(BridgeError("boom")), "linkplane: boom")


if __name__ == "__main__":
    unittest.main()
