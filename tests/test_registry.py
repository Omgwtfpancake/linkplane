"""`linkplane.registry`: devices, aliases, canonical identity, targets (design §6, §24 slice 0)."""

import unittest

from linkplane import registry
from linkplane.core.state import DeviceState
from linkplane.transports import BridgeError

CONFIG = {
    "default_device": "phone",
    "devices": {
        "phone": {"device_id": "FAKESERIAL01", "adb": {"serials": ["FAKESERIAL01", "192.0.2.229:5555"], "preferred_serial": "FAKESERIAL01"},
                  "ssh": {"host": "192.0.2.229", "user": "u0_a123", "port": 8022, "identity_file": "/home/user/.ssh/phone"}},
        "tablet": {"device_id": "TAB123", "adb": {"serials": ["TAB123"]}},
    },
}
STATES = {
    "phone": DeviceState("phone", provider="adb", connection="connected", address="192.0.2.229:5555", battery={"level": 50}, last_seen="t"),
    "serial:UNPAIRED1": DeviceState("serial:UNPAIRED1", provider="adb", connection="connected", address="UNPAIRED1"),
}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = registry.Registry(CONFIG, STATES)

    def test_devices_merge_profiles_and_unpaired_observed_devices(self):
        devices = {device.name: device for device in self.registry.devices()}
        self.assertEqual(set(devices), {"phone", "tablet", "serial:UNPAIRED1"})
        phone = devices["phone"]
        self.assertEqual((phone.device_id, phone.name, phone.default, phone.observed), ("FAKESERIAL01", "phone", True, True))
        self.assertEqual([e.to_dict() for e in phone.providers], [
            {"provider": "adb", "address": "FAKESERIAL01", "observed": False},
            {"provider": "adb", "address": "192.0.2.229:5555", "observed": True},
            {"provider": "ssh", "address": "u0_a123@192.0.2.229:8022", "observed": False},
        ])
        tablet = devices["tablet"]
        self.assertEqual((tablet.device_id, tablet.observed, tablet.state), ("TAB123", False, None))
        unpaired = devices["serial:UNPAIRED1"]
        self.assertEqual((unpaired.device_id, unpaired.observed, unpaired.providers[0].address), ("UNPAIRED1", True, "UNPAIRED1"))

    def test_projection_never_contains_key_material(self):
        text = str([device.to_dict() for device in self.registry.devices()])
        self.assertNotIn("identity_file", text)
        self.assertNotIn(".ssh", text)
        phone = self.registry.resolve("phone").to_dict()
        self.assertEqual((phone["connection"], phone["address"], phone["state"]["battery"]["level"]), ("connected", "192.0.2.229:5555", 50))
        self.assertEqual(set(phone), {"device_id", "name", "observed", "default", "connection", "provider", "address", "last_seen", "providers", "state"})

    def test_resolution_prefers_canonical_id_then_name_then_serial_alias(self):
        for reference in ("FAKESERIAL01", "phone", "serial:FAKESERIAL01", "serial:192.0.2.229:5555"):
            self.assertEqual(self.registry.resolve(reference).device_id, "FAKESERIAL01", reference)
        self.assertEqual(self.registry.resolve("TAB123").name, "tablet")
        self.assertEqual(self.registry.resolve("UNPAIRED1").name, "serial:UNPAIRED1")
        self.assertEqual(self.registry.resolve("serial:UNPAIRED1").device_id, "UNPAIRED1")
        self.assertEqual(self.registry.resolve(None).name, "phone")  # the default device
        self.assertIsNone(self.registry.resolve("nope"))
        self.assertIsNone(self.registry.resolve("192.0.2.229:5555"))  # an address is never an id

    def test_identity_survives_a_rename(self):
        renamed = {**CONFIG, "devices": {**CONFIG["devices"]}}
        renamed["devices"]["work-phone"] = renamed["devices"].pop("phone")
        renamed["default_device"] = "work-phone"
        after = registry.Registry(renamed, {})
        self.assertEqual(after.resolve("FAKESERIAL01").name, "work-phone")
        self.assertEqual(after.resolve("FAKESERIAL01").device_id, self.registry.resolve("phone").device_id)
        self.assertIsNone(after.resolve("phone"))

    def test_target_uses_the_observed_serial_when_online_else_the_preferred_one(self):
        online = self.registry.target(self.registry.resolve("phone"))
        self.assertEqual((online.serial, online.ssh["host"], online.providers), ("192.0.2.229:5555", "192.0.2.229", ("adb", "ssh")))
        offline = registry.Registry(CONFIG, {}).target(registry.Registry(CONFIG, {}).resolve("phone"))
        self.assertEqual(offline.serial, "FAKESERIAL01")
        tablet = self.registry.target(self.registry.resolve("tablet"))
        self.assertEqual((tablet.serial, tablet.ssh, tablet.providers), ("TAB123", None, ("adb",)))
        unpaired = self.registry.target(self.registry.resolve("serial:UNPAIRED1"))
        self.assertEqual((unpaired.serial, unpaired.providers), ("UNPAIRED1", ("adb",)))

    def test_identities_map_serials_to_registry_names(self):
        self.assertEqual(self.registry.identities(), {"FAKESERIAL01": "phone", "192.0.2.229:5555": "phone", "TAB123": "tablet"})

    def test_legacy_ssh_block_is_a_device_and_a_fallback(self):
        legacy = {"ssh": {"host": "old.local", "user": "u0", "port": "8022", "device_id": "OLD1"}}
        reg = registry.Registry(legacy, {})
        self.assertEqual(registry.identities(legacy), {"OLD1": "OLD1"})
        devices = reg.devices()
        self.assertEqual((devices[0].device_id, devices[0].name, devices[0].providers[0].address), ("OLD1", "OLD1", "u0@old.local:8022"))
        self.assertEqual(reg.target(devices[0]).ssh["host"], "old.local")
        with_profile = {**legacy, "devices": {"phone": {"device_id": "OLD1", "adb": {"serials": ["OLD1"]}}}}
        profile = registry.Registry(with_profile, {}).profiles["phone"]
        self.assertEqual(registry.profile_ssh_config(with_profile, profile)["port"], 8022)
        self.assertEqual(len(registry.Registry(with_profile, {}).devices()), 1)  # not listed twice
        with self.assertRaises(BridgeError):
            registry.profile_ssh_config({**legacy, "ssh": {**legacy["ssh"], "port": "bad"}}, profile)

    def test_states_from_snapshot(self):
        snapshot = {"schema": "linkplane.state/1", "devices": {"phone": STATES["phone"].to_dict()}}
        states = registry.Registry.states_from_snapshot(snapshot)
        self.assertEqual(states["phone"].address, "192.0.2.229:5555")
