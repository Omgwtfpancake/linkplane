"""Device tier: reversible clipboard round trip between the configured phone and desktop.

Requires the phone's Termux SSH daemon to be running and a desktop clipboard backend.
Restores both clipboards afterwards.
"""

import time
import unittest

from linkplane.clipboard import (
    desktop_clipboard,
    get_desktop_clipboard,
    get_phone_clipboard,
    set_desktop_clipboard,
    set_phone_clipboard,
)
from linkplane.transports import BridgeError, load_config, ssh_from_config


def wait_for(read, expected: str) -> str:
    value = ""
    for _ in range(20):
        value = read()
        if value == expected:
            return value
        time.sleep(0.05)
    return value


class ClipboardRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.backend = desktop_clipboard()
        if self.backend is None:
            self.skipTest("no desktop clipboard backend available")
        self.phone = ssh_from_config(load_config())
        if not self.phone.host or not self.phone.user:
            self.skipTest("no SSH endpoint configured")
        try:
            self.original_phone = get_phone_clipboard(self.phone)
        except BridgeError as error:
            self.skipTest(f"phone SSH unreachable: {error}")
        self.original_desktop = get_desktop_clipboard(self.backend)

    def tearDown(self):
        if hasattr(self, "original_phone"):
            set_phone_clipboard(self.phone, self.original_phone)
            set_desktop_clipboard(self.backend, self.original_desktop)

    def test_round_trip_in_both_directions(self):
        set_phone_clipboard(self.phone, "linkplane-phone-test")
        set_desktop_clipboard(self.backend, get_phone_clipboard(self.phone))
        self.assertEqual(
            wait_for(lambda: get_desktop_clipboard(self.backend), "linkplane-phone-test"),
            "linkplane-phone-test",
        )

        set_desktop_clipboard(self.backend, "linkplane-desktop-test")
        set_phone_clipboard(self.phone, get_desktop_clipboard(self.backend))
        self.assertEqual(
            wait_for(lambda: get_phone_clipboard(self.phone), "linkplane-desktop-test"),
            "linkplane-desktop-test",
        )


if __name__ == "__main__":
    unittest.main()
