"""`core.state.diff`: the whole event catalogue, from canned states, no phone."""

import unittest

from linkplane.core.events import EVENT_TYPES, Event
from linkplane.core.state import CONNECTED, DISCONNECTED, OFFLINE, UNAUTHORIZED, DeviceState, diff


def battery(level, powered=("usb",), status="charging"):
    return {"level": level, "status": status, "health": "good", "powered_by": list(powered)}


def types(events):
    return [event.type for event in events]


class DiffTests(unittest.TestCase):
    def test_opening_snapshot_marks_events_initial(self):
        current = DeviceState("phone", "adb", CONNECTED, "S1", battery(80), "Home")
        events = diff(None, current)
        self.assertEqual(
            types(events), ["device.connected", "battery.changed", "charging.started", "wifi.connected"]
        )
        self.assertTrue(all(event.initial for event in events))
        self.assertEqual(events[0].data["address"], "S1")
        self.assertEqual(events[0].provider, "adb")

    def test_opening_snapshot_of_absent_device_is_silent(self):
        self.assertEqual(diff(None, DeviceState("phone")), [])

    def test_connect_and_disconnect(self):
        gone = DeviceState("phone", "adb", DISCONNECTED, "S1")
        here = gone.touch(connection=CONNECTED)
        self.assertEqual(types(diff(gone, here)), ["device.connected"])
        self.assertEqual(types(diff(here, gone)), ["device.disconnected"])
        self.assertEqual(diff(here, gone)[0].data["address"], "S1")
        self.assertEqual(types(diff(here, here.touch(connection=OFFLINE))), ["device.disconnected"])

    def test_authorization_transitions(self):
        pending = DeviceState("phone", "adb", UNAUTHORIZED, "S1")
        self.assertEqual(types(diff(None, pending)), ["device.unauthorized"])
        self.assertEqual(
            types(diff(pending, pending.touch(connection=CONNECTED))),
            ["device.authorized", "device.connected"],
        )
        self.assertEqual(types(diff(pending, pending.touch(connection=DISCONNECTED))), [])

    def test_battery_level_change_and_threshold_edges(self):
        base = DeviceState("phone", "adb", CONNECTED, "S1", battery(25))
        low = base.touch(battery=battery(19))
        lower = low.touch(battery=battery(10))
        back = lower.touch(battery=battery(21))
        self.assertEqual(types(diff(base, low)), ["battery.changed", "battery.low"])
        self.assertEqual(diff(base, low)[1].data, {"level": 19, "threshold": 20})
        self.assertEqual(types(diff(low, lower)), ["battery.changed"])  # still low: no repeat
        self.assertEqual(types(diff(lower, back)), ["battery.changed", "battery.ok"])
        self.assertEqual(diff(base, base.touch(battery=battery(25))), [])  # same level: nothing

    def test_custom_threshold(self):
        base = DeviceState("phone", "adb", CONNECTED, "S1", battery(60))
        self.assertEqual(
            types(diff(base, base.touch(battery=battery(49)), low_battery=50)),
            ["battery.changed", "battery.low"],
        )

    def test_charging_transitions(self):
        plugged = DeviceState("phone", "adb", CONNECTED, "S1", battery(50, ("usb",)))
        unplugged = plugged.touch(battery=battery(50, ()))
        self.assertEqual(types(diff(plugged, unplugged)), ["charging.stopped"])
        self.assertEqual(types(diff(unplugged, plugged)), ["charging.started"])
        self.assertEqual(diff(unplugged, plugged)[0].data["powered_by"], ["usb"])

    def test_wifi_transitions(self):
        home = DeviceState("phone", "adb", CONNECTED, "S1", None, "Home")
        away = home.touch(wifi_ssid=None)
        office = home.touch(wifi_ssid="Office")
        self.assertEqual(types(diff(home, away)), ["wifi.disconnected"])
        self.assertEqual(diff(home, away)[0].data["previous"], "Home")
        self.assertEqual(types(diff(away, home)), ["wifi.connected"])
        self.assertEqual(diff(home, office)[0].data, {"ssid": "Office", "previous": "Home"})

    def test_every_emitted_type_is_in_the_catalogue(self):
        for name in EVENT_TYPES:
            Event(name, "x")  # constructible
        with self.assertRaises(ValueError):
            Event("made.up", "x")

    def test_event_round_trips_through_dict(self):
        event = Event("battery.low", "phone", provider="adb", data={"level": 5}, seq=7)
        record = event.to_dict()
        self.assertEqual(record["schema"], "linkplane.event/1")
        self.assertEqual(Event.from_dict(record), event)


if __name__ == "__main__":
    unittest.main()
