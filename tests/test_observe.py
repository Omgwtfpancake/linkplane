"""Observer loop and sources with canned tracker frames and fake pollers."""

import threading
import unittest

from linkplane.core.state import CONNECTED, DISCONNECTED
from linkplane.observe import (
    Observer,
    connection_from_adb_state,
    parse_track_frames,
    parse_wifi_status,
)
from linkplane.operations import CancellationToken


class ParserTests(unittest.TestCase):
    def test_track_frames_split_on_length_prefix_and_keep_partials(self):
        buffer = bytearray()
        payload = b"FAKESERIAL01            device usb:1-10 product:r0qsqw model:SM_S901U device:r0q transport_id:1\n"
        frame = f"{len(payload):04x}".encode() + payload
        first, second = frame[:10], frame[10:] + frame
        self.assertEqual(parse_track_frames(first, buffer), [])
        frames = parse_track_frames(second, buffer)
        self.assertEqual(len(frames), 2)
        self.assertIn("SM_S901U", frames[0])
        self.assertEqual(buffer, bytearray())

    def test_empty_frame_means_no_devices(self):
        self.assertEqual(parse_track_frames(b"0000", bytearray()), [""])

    def test_garbage_is_discarded_not_fatal(self):
        buffer = bytearray()
        self.assertEqual(parse_track_frames(b"zzzz....", buffer), [])
        self.assertEqual(buffer, bytearray())

    def test_wifi_status(self):
        self.assertEqual(parse_wifi_status('Wifi is enabled\nWifi is connected to "Home Net"\n'), "Home Net")
        self.assertIsNone(parse_wifi_status("Wifi is enabled\nWifi is not connected\n"))
        self.assertIsNone(parse_wifi_status('Wifi is connected to ""\n'))

    def test_adb_states(self):
        self.assertEqual(connection_from_adb_state("device"), CONNECTED)
        self.assertEqual(connection_from_adb_state("unauthorized"), "unauthorized")
        self.assertEqual(connection_from_adb_state("offline"), "offline")
        self.assertEqual(connection_from_adb_state("weird"), DISCONNECTED)


def snapshot(*pairs):
    return {serial: {"serial": serial, "state": state} for serial, state in pairs}


class ObserverTests(unittest.TestCase):
    def make(self, frames, *, battery_levels=None, wifi=None, identities=None, **kwargs):
        events = []
        token = CancellationToken()
        levels = iter(battery_levels or [])

        def battery_poller(serial):
            level = next(levels, None)
            return None if level is None else {"level": level, "status": "ok", "health": "good", "powered_by": ["usb"]}

        def tracker():
            for frame in frames:
                yield frame
            token.cancel()

        observer = Observer(
            events.append,
            identities=identities or {"S1": "phone"},
            interval=0.01,
            cancel=token,
            tracker=tracker(),
            battery_poller=battery_poller,
            wifi_poller=lambda serial: wifi,
            **kwargs,
        )
        return observer, events

    def test_connect_poll_disconnect_sequence(self):
        observer, events = self.make(
            [snapshot(("S1", "device")), snapshot()], battery_levels=[77], wifi="Home"
        )
        observer.run()
        types = [event.type for event in events]
        self.assertEqual(types[0], "observer.started")
        self.assertEqual(types[-1], "observer.stopped")
        self.assertEqual(
            types[1:-1],
            ["device.connected", "battery.changed", "charging.started", "wifi.connected", "device.disconnected"],
        )
        connected = events[1]
        self.assertEqual(connected.device, "phone")
        self.assertTrue(all(event.initial for event in events[1:5]))  # opening observations
        self.assertFalse(events[5].initial)  # the disconnect is a real change
        self.assertEqual(connected.data["address"], "S1")
        self.assertEqual(observer.states["phone"].connection, DISCONNECTED)
        self.assertIsNone(observer.states["phone"].battery)

    def test_unnamed_serial_gets_a_serial_identity(self):
        observer, events = self.make([snapshot(("ZZ9", "device"))], identities={})
        observer.run()
        self.assertEqual(events[1].device, "serial:ZZ9")

    def test_failed_poll_keeps_last_battery_state(self):
        observer, events = self.make(
            [snapshot(("S1", "device")), snapshot(("S1", "device"))], battery_levels=[50], wifi=None
        )
        observer.run()
        self.assertEqual(observer.states["phone"].battery["level"], 50)
        self.assertEqual([e.type for e in events if e.type.startswith("battery")], ["battery.changed"])

    def test_unauthorized_then_authorized(self):
        observer, events = self.make(
            [snapshot(("S1", "unauthorized")), snapshot(("S1", "device"))], battery_levels=[9]
        )
        observer.run()
        types = [event.type for event in events][1:-1]
        self.assertEqual(
            types, ["device.unauthorized", "device.authorized", "device.connected", "battery.changed", "battery.low", "charging.started"]
        )

    def test_concurrent_polls_are_serialized_and_do_not_duplicate_observations(self):
        # Regression: seen on the real phone after `adb usb` -- the tracker thread and the
        # timer both polled a just-reconnected device and each emitted charging.started.
        import time

        events = []
        active = {"now": 0, "max": 0}
        lock = threading.Lock()

        def slow_battery(serial):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return {"level": 42, "status": "ok", "health": "good", "powered_by": ["usb"]}

        observer = Observer(events.append, identities={"S1": "phone"}, interval=60,
                            battery_poller=slow_battery, wifi_poller=lambda s: None, tracker=iter([]))
        observer.on_tracker_snapshot(snapshot(("S1", "device")))
        threads = [threading.Thread(target=observer.poll_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertEqual(active["max"], 1)
        self.assertEqual([e.type for e in events if e.type == "charging.started"], ["charging.started"])
        self.assertEqual([e.type for e in events if e.type == "battery.changed"], ["battery.changed"])

    def test_cancel_stops_a_blocked_tracker(self):
        token = CancellationToken()
        events = []
        gate = threading.Event()

        def tracker():
            yield snapshot()
            gate.wait(5)  # blocks like a real track-devices stream
            yield snapshot()

        observer = Observer(events.append, interval=0.01, cancel=token, tracker=tracker(),
                            battery_poller=lambda s: None, wifi_poller=lambda s: None)
        runner = threading.Thread(target=observer.run)
        runner.start()
        token.cancel()
        runner.join(timeout=5)
        gate.set()
        self.assertFalse(runner.is_alive())
        self.assertEqual([e.type for e in events], ["observer.started", "observer.stopped"])


if __name__ == "__main__":
    unittest.main()
