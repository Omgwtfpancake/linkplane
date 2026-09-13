import json
import unittest
from unittest.mock import patch

from linkplane.core import errors
from linkplane.transports import (
    ADB_STATE_DEVICE,
    ADB_STATE_NO_PERMISSIONS,
    ADB_STATE_OFFLINE,
    ADB_STATE_UNAUTHORIZED,
    AdbTransport,
    BridgeError,
    SshTransport,
    parse_adb_devices,
    parse_battery,
    parse_df,
    parse_meminfo,
    parse_wifi_route,
)


class ParserTests(unittest.TestCase):
    def test_parse_adb_devices(self):
        devices = parse_adb_devices(
            "List of devices attached\n"
            "FAKESERIAL01 device usb:1-10 product:r0qsqw model:SM_S901U transport_id:4\n"
        )

        self.assertEqual(
            devices,
            [
                {
                    "serial": "FAKESERIAL01",
                    "state": "device",
                    "usb": "1-10",
                    "product": "r0qsqw",
                    "model": "SM_S901U",
                    "transport_id": "4",
                }
            ],
        )

    def test_parse_wifi_route_extracts_source_address(self):
        address = parse_wifi_route(
            "1.1.1.1 via 192.0.2.1 dev wlan0 table 1045 src 192.0.2.229 uid 2000 \n"
            "    cache \n"
        )

        self.assertEqual(address, "192.0.2.229")

    def test_parse_wifi_route_returns_none_without_a_route(self):
        address = parse_wifi_route("RTNETLINK answers: Network is unreachable\n")

        self.assertIsNone(address)

    def test_parse_battery(self):
        battery = parse_battery(
            "AC powered: false\n"
            "USB powered: true\n"
            "Wireless powered: false\n"
            "status: 2\n"
            "health: 2\n"
            "level: 64\n"
            "voltage: 3817\n"
            "temperature: 324\n"
        )

        self.assertEqual(battery["level"], 64)
        self.assertEqual(battery["status"], "charging")
        self.assertEqual(battery["health"], "good")
        self.assertEqual(battery["powered_by"], ["usb"])
        self.assertEqual(battery["temperature_c"], 32.4)

    def test_parse_meminfo(self):
        memory = parse_meminfo("MemTotal: 8000 kB\nMemAvailable: 2000 kB\n")

        self.assertEqual(memory["total_bytes"], 8_192_000)
        self.assertEqual(memory["available_bytes"], 2_048_000)
        self.assertEqual(memory["used_percent"], 75.0)

    def test_parse_df(self):
        storage = parse_df(
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/fuse 1000 750 250 75% /storage/emulated\n",
            "/sdcard",
        )

        self.assertEqual(storage["total_bytes"], 1_024_000)
        self.assertEqual(storage["available_bytes"], 256_000)
        self.assertEqual(storage["used_percent"], 75)


class TransportTests(unittest.TestCase):
    @patch("linkplane.transports.shutil.which", return_value="/usr/bin/adb")
    def test_adb_requires_serial_when_multiple_devices_are_connected(self, _which):
        def runner(command, **_kwargs):
            self.assertEqual(command, ["adb", "devices", "-l"])
            return "List of devices attached\none device\ntwo device\n"

        with self.assertRaisesRegex(BridgeError, "multiple ADB devices"):
            AdbTransport(runner=runner).select_device()

    @patch("linkplane.transports.shutil.which", return_value="/usr/bin/adb")
    def test_adb_status_keeps_successful_telemetry_when_battery_fails(self, _which):
        def runner(command, **_kwargs):
            if command == ["adb", "devices", "-l"]:
                return "List of devices attached\nserial-1 device product:test\n"
            shell = command[4:]
            responses = {
                ("getprop", "ro.product.manufacturer"): "Example\n",
                ("getprop", "ro.product.model"): "Phone\n",
                ("getprop", "ro.build.version.release"): "16\n",
                ("uname", "-r"): "6.1\n",
                ("dumpsys", "battery"): "status: 3\n",
                ("cat", "/proc/meminfo"): (
                    "MemTotal: 8000 kB\nMemAvailable: 2000 kB\n"
                ),
                ("df", "-Pk", "/sdcard"): (
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/fuse 1000 750 250 75% /storage/emulated\n"
                ),
                ("cat", "/proc/uptime"): "3600.5 100.0\n",
            }
            return responses[tuple(shell)]

        status = AdbTransport(runner=runner).status()

        self.assertIsNone(status["battery"])
        self.assertEqual(status["memory"]["used_percent"], 75.0)
        self.assertEqual(status["storage"]["used_percent"], 75)
        self.assertEqual(status["uptime_seconds"], 3600)
        self.assertEqual(status["issues"][0]["component"], "battery")

    @patch("linkplane.transports.shutil.which", return_value="/usr/bin/adb")
    def test_adb_status_fails_when_no_shell_probe_succeeds(self, _which):
        def runner(command, **_kwargs):
            if command == ["adb", "devices", "-l"]:
                return "List of devices attached\nserial-1 device product:test\n"
            raise BridgeError("device disconnected")

        with self.assertRaisesRegex(BridgeError, "unable to read any ADB telemetry"):
            AdbTransport(runner=runner).status()

    def test_ssh_normalizes_legacy_status(self):
        legacy = {
            "device": {
                "manufacturer": "samsung",
                "model": "SM-S901U",
                "android": "16",
                "kernel": "5.10",
            },
            "battery": {
                "percentage": 64,
                "status": "CHARGING",
                "health": "GOOD",
                "plugged": "PLUGGED_USB",
                "temperature": 32.4,
                "voltage": 3817,
            },
            "memory": {"total_kb": 8000, "available_kb": 2000},
            "storage": {
                "total_kb": 1000,
                "used_kb": 750,
                "free_kb": 250,
                "used_percent": 75,
            },
        }

        def runner(_command, **_kwargs):
            return json.dumps(legacy)

        status = SshTransport("phone.local", "termux", runner=runner).status()

        self.assertEqual(status["transport"], "ssh")
        self.assertEqual(status["battery"]["powered_by"], ["usb"])
        self.assertEqual(status["memory"]["used_percent"], 75.0)
        self.assertIsNone(status["uptime_seconds"])

    def test_ssh_status_keeps_memory_when_other_sections_are_invalid(self):
        legacy = {
            "device": {"manufacturer": "Example", "model": "Phone"},
            "memory": {"total_kb": 8000, "available_kb": 2000},
            "storage": "invalid",
        }

        status = SshTransport(
            "phone.local", "termux", runner=lambda _command, **_kwargs: json.dumps(legacy)
        ).status()

        self.assertIsNone(status["battery"])
        self.assertEqual(status["memory"]["used_percent"], 75.0)
        self.assertIsNone(status["storage"])
        self.assertEqual(
            [issue["component"] for issue in status["issues"]],
            ["battery", "storage"],
        )


if __name__ == "__main__":
    unittest.main()


# --- ADB device states (Slice 0, 2026-09-12) -------------------------------------------
# `adb devices -l` prints one state word per device -- except `no permissions`, which is
# two words followed by free text (and a URL with a colon in it). Linkplane must tell that
# Linux host-side condition apart from the phone declining authorization (`unauthorized`)
# and from a device that is present but not answering (`offline`); the guided setup gives
# a different instruction for each.

HEADER = "List of devices attached\n"
# Both phrasings adb has used for the udev failure (older and current platform-tools).
NO_PERMISSIONS_OLD = (
    "FAKESERIAL01    no permissions (user in plugdev group; are your udev rules wrong?); "
    "see [http://developer.android.com/tools/device.html] usb:1-2 transport_id:3\n"
)
NO_PERMISSIONS_NEW = (
    "FAKESERIAL01    no permissions (missing udev rules? user is in the plugdev group); "
    "see [https://developer.android.com/tools/device.html] usb:1-2 transport_id:3\n"
)


class ParseAdbDevicesTests(unittest.TestCase):
    def test_ready_device_keeps_key_value_details(self):
        devices = parse_adb_devices(
            HEADER + "FAKESERIAL01    device usb:1-2 product:r0q model:SM_S901U device:r0q transport_id:3\n"
        )
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["state"], ADB_STATE_DEVICE)
        self.assertEqual(devices[0]["model"], "SM_S901U")
        self.assertEqual(devices[0]["usb"], "1-2")

    def test_single_word_states_are_taken_verbatim(self):
        devices = parse_adb_devices(
            HEADER
            + "A1 unauthorized usb:1-2 transport_id:3\n"
            + "B2 offline usb:1-3 transport_id:4\n"
            + "192.0.2.9:5555 device product:x model:y transport_id:5\n"
        )
        self.assertEqual([d["state"] for d in devices], [ADB_STATE_UNAUTHORIZED, ADB_STATE_OFFLINE, ADB_STATE_DEVICE])
        self.assertEqual(devices[2]["serial"], "192.0.2.9:5555")

    def test_no_permissions_is_one_state_and_the_url_is_not_a_detail(self):
        for text in (NO_PERMISSIONS_OLD, NO_PERMISSIONS_NEW):
            with self.subTest(text=text[:40]):
                devices = parse_adb_devices(HEADER + text)
                self.assertEqual(devices[0]["state"], ADB_STATE_NO_PERMISSIONS)
                self.assertEqual(devices[0]["usb"], "1-2")
                self.assertEqual(devices[0]["transport_id"], "3")
                self.assertNotIn("[http", "".join(devices[0]))
                self.assertNotIn("[https", "".join(devices[0]))

    def test_track_devices_payload_parses_the_same_way(self):
        # observe.py prefixes the header itself; the line format is identical.
        devices = parse_adb_devices(HEADER + "FAKESERIAL01\tno permissions (missing udev rules? user is in the plugdev group); see [https://developer.android.com/tools/device.html]\n")
        self.assertEqual(devices[0]["state"], ADB_STATE_NO_PERMISSIONS)

    def test_blank_and_short_lines_are_ignored(self):
        self.assertEqual(parse_adb_devices(HEADER + "\n* daemon started successfully\n"), [])


@patch("linkplane.transports.shutil.which", return_value="/usr/bin/adb")
class SelectDeviceTests(unittest.TestCase):
    def select(self, listing, serial=None):
        transport = AdbTransport(serial, runner=lambda command, **kwargs: HEADER + listing)
        return transport, transport.select_device()

    def classify(self, listing, serial=None):
        transport = AdbTransport(serial, runner=lambda command, **kwargs: HEADER + listing)
        with self.assertRaises(BridgeError) as raised:
            transport.select_device()
        return errors.classify(raised.exception, provider="adb")

    def test_ready_device_is_selected_and_its_serial_learned(self, _which):
        transport, device = self.select("S1 device model:Phone\n")
        self.assertEqual((transport.serial, device["model"]), ("S1", "Phone"))

    def test_no_permissions_is_a_host_usb_permission_error(self, _which):
        classified = self.classify(NO_PERMISSIONS_OLD)
        self.assertEqual(classified.code, errors.AUTH_USB_PERMISSION)
        self.assertEqual(classified.hints, errors.USB_PERMISSION_HINTS)
        # Never the phone-side advice: the phone is not asking anything.
        self.assertNotIn("authorized", classified.title.lower())
        self.assertFalse(any("Allow" in hint for hint in classified.hints))

    def test_unauthorized_is_android_side_authorization(self, _which):
        classified = self.classify("S1 unauthorized usb:1-2\n")
        self.assertEqual(classified.code, errors.AUTH_UNAUTHORIZED_DEVICE)
        self.assertIn("authorized", classified.hints[0])

    def test_offline_is_a_reconnect_problem_not_authorization(self, _which):
        classified = self.classify("S1 offline usb:1-2\n")
        self.assertEqual(classified.code, errors.CONNECT_UNREACHABLE)
        self.assertEqual(classified.hints, errors.OFFLINE_HINTS)

    def test_host_permission_problem_is_diagnosed_before_other_blocked_states(self, _which):
        classified = self.classify("A1 unauthorized usb:1-2\n" + NO_PERMISSIONS_OLD.replace("FAKESERIAL01", "B2"))
        self.assertEqual(classified.code, errors.AUTH_USB_PERMISSION)
        self.assertIn("also found A1 (unauthorized)", str(classified))

    def test_explicit_serial_in_a_blocked_state_gets_the_same_diagnosis(self, _which):
        classified = self.classify(NO_PERMISSIONS_NEW, serial="FAKESERIAL01")
        self.assertEqual(classified.code, errors.AUTH_USB_PERMISSION)
        classified = self.classify("S1 unauthorized usb:1-2\n", serial="S1")
        self.assertEqual(classified.code, errors.AUTH_UNAUTHORIZED_DEVICE)

    def test_nothing_listed_is_no_device(self, _which):
        self.assertEqual(self.classify("").code, errors.CONNECT_NO_DEVICE)

    def test_two_ready_devices_are_ambiguous(self, _which):
        self.assertEqual(self.classify("A1 device\nB2 device\n").code, errors.CONNECT_AMBIGUOUS)

    def test_runner_is_resolved_at_call_time(self, _which):
        with patch("linkplane.transports.run_command", return_value=HEADER + "S1 device\n") as run:
            transport = AdbTransport()
            transport.select_device()
        run.assert_called_once_with(["adb", "devices", "-l"])
        self.assertEqual(transport.serial, "S1")
