import json
import os
import stat
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from linkplane.cli import (
    apply_device_profile,
    configured_ssh_context,
    discovered_devices,
    get_status,
)
from linkplane.core import errors
from linkplane.models import DiscoveryResult
from linkplane.profiles import (
    DeviceProfile,
    EndpointRefreshRequest,
    ProfileChangeRequest,
    ProfileListRequest,
    SshPairingRequest,
    UsbPairingRequest,
    WirelessPairingRequest,
    available_adb_serial,
    hardware_serials,
    merge_profile,
    next_free_profile_name,
    pairing_code,
    pair_ssh,
    pair_ssh_device,
    pair_usb,
    pair_usb_device,
    pair_wireless,
    pair_wireless_device,
    profiles_from_config,
    read_profiles,
    refresh_profile_endpoints,
    remove_device_profile,
    save_config,
    scan_for_profile,
    select_default_profile,
    stale_wireless_alias,
    subnet_scan_candidates,
)
from linkplane.transports import BridgeError, SshTransport


class ProfileTests(unittest.TestCase):
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    @patch(
        "linkplane.profiles.run_command",
        return_value=(
            "List of devices attached\n"
            "usb-serial device model:Phone usb:1-2\n"
            "phone.local:5555 offline\n"
        ),
    )
    def test_available_adb_serial_uses_connected_alias(self, _run, _which):
        profile = DeviceProfile(
            "phone",
            "logical-1",
            ("usb-serial", "phone.local:5555"),
            "phone.local:5555",
            None,
        )

        self.assertEqual(available_adb_serial(profile), "usb-serial")

    def test_merge_profile_retains_multiple_adb_aliases(self):
        config = {}

        merge_profile(config, "phone", "logical-1", adb_serial="usb-serial")
        merge_profile(config, "phone", "logical-1", adb_serial="192.0.2.1:5555")
        profile = profiles_from_config(config)["phone"]

        self.assertEqual(
            profile.adb_serials,
            ("usb-serial", "192.0.2.1:5555"),
        )
        self.assertEqual(profile.adb_serial, "192.0.2.1:5555")

    def test_save_config_is_private_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "config.json"
            save_config({"other": {"enabled": True}}, str(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(saved, {"other": {"enabled": True}})
        self.assertEqual(mode, 0o600)

    def test_profile_listing_service_returns_sorted_typed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "default_device": "phone",
                    "devices": {
                        "tablet": {"device_id": "tablet-1"},
                        "phone": {
                            "device_id": "phone-1",
                            "adb": {"serials": ["usb-1"]},
                        },
                    },
                },
                str(path),
            )

            result = read_profiles(ProfileListRequest(str(path)))

        self.assertTrue(result.ok)
        self.assertEqual(result.value.default_device, "phone")
        self.assertEqual(
            [profile.name for profile in result.value.profiles], ["phone", "tablet"]
        )
        self.assertEqual(
            result.value.to_dict()["devices"]["phone"]["adb_serial"], "usb-1"
        )

    @patch("builtins.print")
    def test_profile_mutation_services_update_config_without_printing(self, output):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {"device_id": "phone-1"},
                        "tablet": {"device_id": "tablet-1"},
                    }
                },
                str(path),
            )

            selected = select_default_profile(
                ProfileChangeRequest("phone", str(path)), progress=events.append
            )
            removed = remove_device_profile(
                ProfileChangeRequest("tablet", str(path)), progress=events.append
            )
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(selected.ok)
        self.assertTrue(removed.ok)
        self.assertEqual(config["default_device"], "phone")
        self.assertNotIn("tablet", config["devices"])
        self.assertEqual(
            [event.phase for event in events],
            ["started", "completed", "started", "completed"],
        )
        output.assert_not_called()

    def test_profile_mutation_service_returns_typed_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config({"devices": {}}, str(path))

            result = remove_device_profile(ProfileChangeRequest("missing", str(path)))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "not_found")

    def test_refresh_updates_drifted_ssh_host_and_reports_progress(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "phone-1",
                            "adb": {"serials": ["usb-serial"]},
                            "ssh": {"host": "10.0.0.9", "user": "u0", "port": 8022},
                        }
                    }
                },
                str(path),
            )

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\nusb-serial\tdevice\n"
                if command == [
                    "adb", "-s", "usb-serial", "shell", "ip", "route", "get", "1.1.1.1",
                ]:
                    return "1.1.1.1 via 192.0.2.1 dev wlan0 src 192.0.2.5 uid 0\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path)),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
                progress=events.append,
            )
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.ok)
        outcome = result.value.profiles[0]
        self.assertEqual(outcome.discovered_address, "192.0.2.5")
        self.assertEqual(
            [(change.field, change.previous, change.current) for change in outcome.changes],
            [("ssh_host", "10.0.0.9", "192.0.2.5")],
        )
        self.assertEqual(config["devices"]["phone"]["ssh"]["host"], "192.0.2.5")
        self.assertEqual(
            [event.phase for event in events], ["started", "refreshed", "completed"]
        )

    def test_refresh_dry_run_reports_changes_without_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "phone-1",
                            "adb": {"serials": ["usb-serial"]},
                            "ssh": {"host": "10.0.0.9", "user": "u0", "port": 8022},
                        }
                    }
                },
                str(path),
            )
            original = path.read_text(encoding="utf-8")

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\nusb-serial\tdevice\n"
                if command == [
                    "adb", "-s", "usb-serial", "shell", "ip", "route", "get", "1.1.1.1",
                ]:
                    return "1.1.1.1 via 192.0.2.1 dev wlan0 src 192.0.2.5 uid 0\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path), dry_run=True),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )
            after = path.read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        self.assertTrue(result.value.dry_run)
        self.assertEqual(len(result.value.profiles[0].changes), 1)
        self.assertEqual(after, original)

    def test_refresh_skips_profile_without_reachable_adb_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config({"devices": {"phone": {"device_id": "phone-1"}}}, str(path))
            calls = []

            def command_runner(command, **_kwargs):
                calls.append(command)
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path)),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )

        self.assertTrue(result.ok)
        outcome = result.value.profiles[0]
        self.assertEqual(outcome.note, "no reachable ADB endpoint to query")
        self.assertEqual(outcome.changes, ())
        self.assertEqual(calls, [["adb", "devices", "-l"]])

    def test_refresh_verifies_wireless_alias_with_adb_connect_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "phone-1",
                            "adb": {
                                "serials": ["usb-serial", "10.0.0.9:5555"],
                                "preferred_serial": "10.0.0.9:5555",
                            },
                        }
                    }
                },
                str(path),
            )
            state = {"connected": False}

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    lines = ["List of devices attached", "usb-serial\tdevice"]
                    if state["connected"]:
                        lines.append("192.0.2.5:5555\tdevice")
                    return "\n".join(lines) + "\n"
                if command == [
                    "adb", "-s", "usb-serial", "shell", "ip", "route", "get", "1.1.1.1",
                ]:
                    return "1.1.1.1 via 192.0.2.1 dev wlan0 src 192.0.2.5 uid 0\n"
                if command == ["adb", "connect", "192.0.2.5:5555"]:
                    state["connected"] = True
                    return "connected to 192.0.2.5:5555\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path)),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.ok)
        outcome = result.value.profiles[0]
        self.assertEqual(
            [(change.field, change.previous, change.current) for change in outcome.changes],
            [("adb_serial", "10.0.0.9:5555", "192.0.2.5:5555")],
        )
        saved_adb = config["devices"]["phone"]["adb"]
        self.assertEqual(set(saved_adb["serials"]), {"usb-serial", "192.0.2.5:5555"})
        self.assertEqual(saved_adb["preferred_serial"], "192.0.2.5:5555")

    def test_refresh_leaves_wireless_alias_unchanged_when_connect_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "phone-1",
                            "adb": {"serials": ["usb-serial", "10.0.0.9:5555"]},
                        }
                    }
                },
                str(path),
            )
            original = path.read_text(encoding="utf-8")

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\nusb-serial\tdevice\n"
                if command == [
                    "adb", "-s", "usb-serial", "shell", "ip", "route", "get", "1.1.1.1",
                ]:
                    return "1.1.1.1 via 192.0.2.1 dev wlan0 src 192.0.2.5 uid 0\n"
                if command == ["adb", "connect", "192.0.2.5:5555"]:
                    raise BridgeError("adb: failed to connect to 192.0.2.5:5555")
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path)),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )
            after = path.read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        outcome = result.value.profiles[0]
        self.assertEqual(outcome.changes, ())
        self.assertEqual(outcome.note, "already up to date")
        self.assertEqual(after, original)

    def test_refresh_unknown_profile_returns_typed_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config({"devices": {}}, str(path))

            result = refresh_profile_endpoints(EndpointRefreshRequest("missing", str(path)))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "not_found")

    def test_refresh_all_profiles_when_name_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "phone-1",
                            "adb": {"serials": ["usb-serial"]},
                            "ssh": {"host": "10.0.0.9", "user": "u0", "port": 8022},
                        },
                        "tablet": {"device_id": "tablet-1"},
                    }
                },
                str(path),
            )

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\nusb-serial\tdevice\n"
                if command == [
                    "adb", "-s", "usb-serial", "shell", "ip", "route", "get", "1.1.1.1",
                ]:
                    return "1.1.1.1 via 192.0.2.1 dev wlan0 src 192.0.2.5 uid 0\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest(None, str(path)),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )

        self.assertTrue(result.ok)
        self.assertEqual(
            [outcome.profile for outcome in result.value.profiles], ["phone", "tablet"]
        )
        self.assertEqual(result.value.profiles[1].note, "no reachable ADB endpoint to query")

    def test_profile_removal_can_repair_invalid_legacy_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {"devices": {"invalid profile name": {"device_id": "phone-1"}}},
                str(path),
            )

            result = remove_device_profile(
                ProfileChangeRequest("invalid profile name", str(path))
            )
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.ok)
        self.assertEqual(config["devices"], {})

    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    @patch("linkplane.profiles.run_command", return_value="List of devices attached\n")
    def test_apply_explicit_profile_populates_endpoints(self, _run, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {
                            "phone": {
                                "device_id": "logical-1",
                                "adb": {
                                    "serials": ["usb-1", "phone.local:5555"],
                                    "preferred_serial": "phone.local:5555",
                                },
                                "ssh": {
                                    "host": "phone.local",
                                    "user": "termux",
                                    "port": 8022,
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="status",
                device_profile="phone",
                config=str(path),
                transport="auto",
                serial=None,
                ssh_host=None,
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
            )

            apply_device_profile(arguments)

        self.assertEqual(arguments.serial, "phone.local:5555")
        self.assertEqual(arguments.ssh_host, "phone.local")
        self.assertEqual(arguments.profile_device_id, "logical-1")
        self.assertTrue(arguments.serial_from_profile)

    def test_explicit_serial_suppresses_implicit_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "default_device": "phone",
                        "devices": {
                            "phone": {
                                "device_id": "logical-1",
                                "adb": {"serials": ["saved-serial"]},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="screen",
                device_profile=None,
                config=str(path),
                serial="explicit-serial",
            )

            apply_device_profile(arguments)

        self.assertEqual(arguments.serial, "explicit-serial")
        self.assertFalse(hasattr(arguments, "profile_device_id"))

    @patch("linkplane.cli.available_adb_serial", return_value="saved-serial")
    @patch.dict("os.environ", {"LINKPLANE_SSH_HOST": "unrelated.local"})
    def test_ssh_environment_does_not_disable_adb_profile(self, _available):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "default_device": "phone",
                        "devices": {
                            "phone": {
                                "device_id": "logical-1",
                                "adb": {"serials": ["saved-serial"]},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="backup",
                device_profile=None,
                config=str(path),
                serial=None,
            )

            apply_device_profile(arguments)

        self.assertEqual(arguments.serial, "saved-serial")

    def test_explicit_profile_rejects_conflicting_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {
                            "phone": {
                                "device_id": "logical-1",
                                "adb": {"serials": ["saved-serial"]},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="screen",
                device_profile="phone",
                config=str(path),
                serial="other-serial",
            )

            with self.assertRaisesRegex(BridgeError, "conflicts with device profile"):
                apply_device_profile(arguments)

    @patch.dict(
        "os.environ",
        {
            "LINKPLANE_SSH_HOST": "wrong.example",
            "LINKPLANE_SSH_USER": "wrong-user",
        },
    )
    def test_explicit_profile_ignores_environment_ssh_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {
                            "phone": {
                                "device_id": "logical-1",
                                "ssh": {
                                    "host": "phone.local",
                                    "user": "termux",
                                    "port": 8022,
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="status",
                device_profile="phone",
                config=str(path),
                transport="ssh",
                serial=None,
                ssh_host=None,
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
            )

            apply_device_profile(arguments)
            transport, device_id = configured_ssh_context(arguments)

        self.assertEqual(transport.host, "phone.local")
        self.assertEqual(transport.user, "termux")
        self.assertEqual(device_id, "logical-1")

    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    @patch("linkplane.profiles.run_command", return_value="List of devices attached\n")
    def test_matching_legacy_ssh_is_available_to_adb_only_profile(self, _run, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "default_device": "phone",
                        "devices": {
                            "phone": {
                                "device_id": "usb-1",
                                "adb": {"serials": ["usb-1"]},
                            }
                        },
                        "ssh": {
                            "device_id": "usb-1",
                            "host": "legacy-phone.local",
                            "user": "termux",
                        },
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="status",
                device_profile=None,
                config=str(path),
                transport="auto",
                serial=None,
                ssh_host=None,
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
            )

            apply_device_profile(arguments)
            transport, device_id = configured_ssh_context(arguments)

        self.assertEqual(transport.host, "legacy-phone.local")
        self.assertEqual(device_id, "usb-1")

    def test_matching_legacy_ssh_port_is_normalized_for_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {"phone": {"device_id": "logical-1"}},
                        "ssh": {
                            "device_id": "logical-1",
                            "host": "legacy-phone.local",
                            "user": "termux",
                            "port": "8022",
                        },
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="status",
                device_profile="phone",
                config=str(path),
                transport="ssh",
                serial=None,
                ssh_host=None,
                ssh_user=None,
                ssh_port=8022,
                ssh_key=None,
            )

            apply_device_profile(arguments)

        self.assertEqual(arguments.ssh_port, 8022)

    @patch("linkplane.cli.available_adb_serial", return_value="usb-1")
    def test_clipboard_foregrounds_profile_adb_alias(self, _available):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {
                            "phone": {
                                "device_id": "logical-1",
                                "adb": {"serials": ["usb-1"]},
                                "ssh": {"host": "phone.local", "user": "termux"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="clipboard",
                device_profile="phone",
                config=str(path),
                ssh_host=None,
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
            )

            apply_device_profile(arguments)

        self.assertEqual(arguments.profile_adb_serial, "usb-1")
        self.assertNotEqual(arguments.profile_adb_serial, arguments.profile_device_id)

    def test_explicit_profile_rejects_localsend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {
                            "phone": {
                                "device_id": "logical-1",
                                "adb": {"serials": ["usb-1"]},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="send",
                device_profile="phone",
                config=str(path),
                transport="localsend",
                serial=None,
            )

            with self.assertRaisesRegex(BridgeError, "LocalSend"):
                apply_device_profile(arguments)

    def test_duplicate_endpoint_identity_is_rejected(self):
        config = {
            "devices": {
                "phone": {
                    "device_id": "logical-1",
                    "adb": {"serials": ["shared-serial"]},
                },
                "tablet": {
                    "device_id": "logical-2",
                    "adb": {"serials": ["shared-serial"]},
                },
            }
        }

        with self.assertRaisesRegex(BridgeError, "multiple device identities"):
            profiles_from_config(config)

    def test_duplicate_ssh_ports_are_normalized(self):
        config = {
            "devices": {
                "phone": {
                    "device_id": "logical-1",
                    "ssh": {
                        "host": "phone.local",
                        "user": "termux",
                        "port": 8022,
                    },
                },
                "tablet": {
                    "device_id": "logical-2",
                    "ssh": {
                        "host": "phone.local",
                        "user": "termux",
                        "port": "8022",
                    },
                },
            }
        }

        with self.assertRaisesRegex(BridgeError, "multiple device identities"):
            profiles_from_config(config)

    @patch("getpass.getpass", return_value="１２３４５６")
    @patch("linkplane.profiles.sys.stdin.isatty", return_value=True)
    def test_pairing_code_requires_ascii_digits(self, _isatty, _getpass):
        with self.assertRaisesRegex(BridgeError, "six digits"):
            pairing_code()

    @patch("linkplane.cli.configured_ssh")
    @patch("linkplane.cli.AdbTransport")
    def test_profile_serial_can_fallback_to_its_ssh_endpoint(
        self, adb_class, configured_ssh
    ):
        adb_class.return_value.status.side_effect = BridgeError("offline")
        configured_ssh.return_value.status.return_value = {
            "transport": "ssh",
            "device": {},
        }
        arguments = Namespace(
            transport="auto",
            serial="saved-serial",
            serial_from_profile=True,
        )

        self.assertEqual(get_status(arguments)["transport"], "ssh")

    @patch("linkplane.cli.discover_devices", return_value=DiscoveryResult(()))
    def test_discovery_includes_all_profile_endpoints(self, discover):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {
                            "phone": {
                                "device_id": "logical-phone",
                                "adb": {"serials": ["usb-1", "phone.local:5555"]},
                                "ssh": {"host": "phone.local", "user": "termux"},
                            },
                            "tablet": {
                                "device_id": "logical-tablet",
                                "ssh": {"host": "tablet.local", "user": "termux"},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="devices",
                device_profile=None,
                config=str(path),
                serial=None,
                ssh_host=None,
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
            )

            apply_device_profile(arguments)
            discovered_devices(arguments)

        self.assertEqual(
            discover.call_args.kwargs["adb_identities"],
            {"usb-1": "logical-phone", "phone.local:5555": "logical-phone"},
        )
        self.assertEqual(len(discover.call_args.kwargs["additional_ssh"]), 2)

    @patch("linkplane.cli.discover_devices", return_value=DiscoveryResult(()))
    def test_discovery_honors_explicit_ssh_override(self, discover):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "devices": {
                            "saved": {
                                "device_id": "saved-id",
                                "ssh": {"host": "saved.local", "user": "termux"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            arguments = Namespace(
                command="devices",
                device_profile=None,
                config=str(path),
                serial=None,
                ssh_host="override.local",
                ssh_user="other",
                ssh_port=9022,
                ssh_key=None,
                device_id="override-id",
            )

            apply_device_profile(arguments)
            discovered_devices(arguments)

        transport, device_id = discover.call_args.args
        self.assertEqual(transport.host, "override.local")
        self.assertEqual(device_id, "override-id")

    @patch("linkplane.cli.discover_devices", return_value=DiscoveryResult(()))
    def test_discovery_honors_device_id_only_override(self, discover):
        arguments = Namespace(
            command="devices",
            device_profile=None,
            config="/definitely/missing/linkplane-config.json",
            serial=None,
            ssh_host=None,
            ssh_user=None,
            ssh_port=None,
            ssh_key=None,
            device_id="explicit-id",
        )

        apply_device_profile(arguments)
        discovered_devices(arguments)

        _transport, device_id = discover.call_args.args
        self.assertEqual(device_id, "explicit-id")
        self.assertNotIn("additional_ssh", discover.call_args.kwargs)

    @patch("linkplane.profiles.AdbTransport")
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    @patch("builtins.print")
    def test_usb_pairing_saves_profile(self, _print, _which, adb_class):
        adb_class.return_value.select_device.return_value = {"model": "Test_Phone"}
        adb_class.return_value.serial = "usb-1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            arguments = Namespace(
                name="phone",
                serial=None,
                default=False,
                dry_run=False,
                config=str(path),
            )

            self.assertEqual(pair_usb(arguments), 0)
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(config["default_device"], "phone")
        self.assertEqual(config["devices"]["phone"]["device_id"], "usb-1")

    @patch("linkplane.profiles.AdbTransport")
    @patch("linkplane.profiles.run_command")
    @patch("linkplane.profiles.pairing_code", return_value="123456")
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    @patch("builtins.print")
    def test_wireless_pairing_keeps_code_out_of_arguments(
        self, _print, _which, _code, run, adb_class
    ):
        run.side_effect = ["Successfully paired", "connected"]
        adb_class.return_value.select_device.return_value = {"model": "Phone"}
        adb_class.return_value.serial = "phone.local:5555"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            arguments = Namespace(
                name="phone",
                pair_address="phone.local:37123",
                connect_address="phone.local:5555",
                default=True,
                dry_run=False,
                config=str(path),
            )

            self.assertEqual(pair_wireless(arguments), 0)

        self.assertNotIn("123456", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[0].kwargs["input_text"], "123456\n")

    @patch("linkplane.profiles.run_command")
    @patch("linkplane.profiles.pairing_code")
    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    def test_wireless_pairing_validates_profile_before_adb(
        self, _which, code, run
    ):
        arguments = Namespace(
            name="invalid profile name",
            pair_address="phone.local:37123",
            connect_address="phone.local:5555",
            default=True,
            dry_run=False,
            config="/definitely/missing/linkplane-config.json",
        )

        with self.assertRaisesRegex(BridgeError, "profile names"):
            pair_wireless(arguments)

        code.assert_not_called()
        run.assert_not_called()

    @patch("linkplane.profiles.SshTransport")
    @patch("builtins.print")
    def test_ssh_pairing_verifies_and_saves_endpoint(self, _print, ssh_class):
        ssh_class.return_value.execute.return_value = "linkplane-ready"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            arguments = Namespace(
                name="phone",
                device_id="logical-1",
                serial=None,
                ssh_host="phone.local",
                ssh_user="termux",
                ssh_port=8022,
                ssh_key="~/.ssh/phone",
                default=True,
                dry_run=False,
                config=str(path),
            )

            self.assertEqual(pair_ssh(arguments), 0)
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["devices"]["phone"]["ssh"]["host"], "phone.local"
        )
        ssh_class.return_value.execute.assert_called_once_with(
            ["printf", "linkplane-ready"]
        )

    @patch("builtins.print")
    def test_usb_pairing_service_returns_typed_result_and_progress(self, output):
        adb = Mock(serial="usb-1")
        adb.select_device.return_value = {"model": "Test_Phone"}
        events = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            result = pair_usb_device(
                UsbPairingRequest("phone", config_path=str(path)),
                adb_factory=lambda _serial: adb,
                adb_locator=lambda _name: "/usr/bin/adb",
                progress=events.append,
            )
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.ok)
        self.assertEqual(result.value.method, "usb")
        self.assertEqual(result.value.device, "Test Phone")
        self.assertEqual(result.value.adb_serial, "usb-1")
        self.assertEqual([event.phase for event in events], ["started", "completed"])
        self.assertEqual(config["devices"]["phone"]["device_id"], "usb-1")
        output.assert_not_called()

    def test_pairing_services_return_typed_missing_adb_errors(self):
        usb = pair_usb_device(
            UsbPairingRequest("phone"), adb_locator=lambda _name: None
        )
        ssh = pair_ssh_device(
            SshPairingRequest("phone", "phone.local", "termux"),
            adb_locator=lambda _name: None,
        )

        self.assertEqual(usb.error.code, "dependency_missing")
        self.assertEqual(ssh.error.code, "dependency_missing")

    def test_pairing_adb_locator_retains_single_executable_contract(self):
        adb = Mock(serial="usb-1")
        adb.select_device.return_value = {"model": "Phone"}
        paths = {"adb": "/usr/bin/adb"}

        result = pair_usb_device(
            UsbPairingRequest(
                "phone",
                dry_run=True,
                config_path="/definitely/missing/linkplane-config.json",
            ),
            adb_factory=lambda _serial: adb,
            adb_locator=paths.__getitem__,
        )

        self.assertTrue(result.ok)

    def test_wireless_pairing_service_dry_run_has_safe_command_metadata(self):
        code_reader = Mock()
        command_runner = Mock()
        adb_factory = Mock()
        events = []

        result = pair_wireless_device(
            WirelessPairingRequest(
                "phone",
                "phone.local:37123",
                "phone.local:5555",
                dry_run=True,
                config_path="/definitely/missing/linkplane-config.json",
            ),
            adb_factory=adb_factory,
            command_runner=command_runner,
            code_reader=code_reader,
            adb_locator=lambda _name: "/usr/bin/adb",
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            result.value.commands,
            (
                ("adb", "pair", "phone.local:37123"),
                ("adb", "connect", "phone.local:5555"),
            ),
        )
        self.assertEqual([event.phase for event in events], ["started", "completed"])
        code_reader.assert_not_called()
        command_runner.assert_not_called()
        adb_factory.assert_not_called()

    def test_wireless_pairing_service_returns_typed_code_input_error(self):
        result = pair_wireless_device(
            WirelessPairingRequest(
                "phone",
                "phone.local:37123",
                "phone.local:5555",
                config_path="/definitely/missing/linkplane-config.json",
            ),
            code_reader=Mock(side_effect=BridgeError("interactive terminal required")),
            adb_locator=lambda _name: "/usr/bin/adb",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_request")
        self.assertEqual(result.error.message, "interactive terminal required")

    @patch("builtins.print")
    def test_wireless_pairing_service_keeps_code_out_of_result_and_events(self, output):
        adb = Mock(serial="phone.local:5555")
        adb.select_device.return_value = {"model": "Test Phone"}
        command_runner = Mock(
            side_effect=["Successfully paired with code 123456", "connected"]
        )
        events = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            result = pair_wireless_device(
                WirelessPairingRequest(
                    "phone",
                    "phone.local:37123",
                    "phone.local:5555",
                    config_path=str(path),
                ),
                adb_factory=lambda _serial: adb,
                command_runner=command_runner,
                code_reader=lambda: "123456",
                adb_locator=lambda _name: "/usr/bin/adb",
                progress=events.append,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.connection_output, "connected")
        self.assertEqual(
            result.value.pairing_output, "Successfully paired with code [redacted]"
        )
        self.assertEqual(
            command_runner.call_args_list[0].kwargs["input_text"], "123456\n"
        )
        self.assertNotIn("123456", str(result.value.to_dict()))
        self.assertTrue(
            all("123456" not in str(event.to_dict()) for event in events)
        )
        self.assertEqual(
            [event.phase for event in events],
            ["started", "paired", "connected", "completed"],
        )
        output.assert_not_called()

    @patch("builtins.print")
    def test_ssh_pairing_service_returns_typed_result_without_printing(self, output):
        runner = Mock(return_value="linkplane-ready")
        events = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            result = pair_ssh_device(
                SshPairingRequest(
                    "phone",
                    "phone.local",
                    "termux",
                    device_id="logical-1",
                    config_path=str(path),
                ),
                ssh_factory=lambda host, user, port, key: SshTransport(
                    host, user, port, key, runner=runner
                ),
                progress=events.append,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.method, "ssh")
        self.assertEqual(result.value.ssh_endpoint, "termux@phone.local:8022")
        self.assertEqual([event.phase for event in events], ["started", "completed"])
        self.assertIn("printf linkplane-ready", result.value.commands[0][-1])
        output.assert_not_called()

    def test_ssh_pairing_service_rejects_empty_explicit_device_id_before_probe(self):
        ssh_factory = Mock()

        result = pair_ssh_device(
            SshPairingRequest(
                "phone", "phone.local", "termux", device_id="", dry_run=True
            ),
            ssh_factory=ssh_factory,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_request")
        ssh_factory.assert_not_called()

    def test_hardware_serials_excludes_network_style_aliases(self):
        profile = DeviceProfile(
            "phone", "phone-1", ("usb-serial", "10.0.0.9:5555"), None, None
        )

        self.assertEqual(hardware_serials(profile), ("usb-serial",))

    def test_stale_wireless_alias_returns_host_and_port(self):
        profile = DeviceProfile(
            "phone", "phone-1", ("usb-serial", "10.0.0.9:5555"), None, None
        )

        self.assertEqual(stale_wireless_alias(profile), ("10.0.0.9", "5555"))

    def test_stale_wireless_alias_is_none_without_a_network_serial(self):
        profile = DeviceProfile("phone", "phone-1", ("usb-serial",), None, None)

        self.assertIsNone(stale_wireless_alias(profile))

    def test_subnet_scan_candidates_covers_the_24(self):
        hosts = subnet_scan_candidates("192.0.2.229")

        self.assertEqual(len(hosts), 254)
        self.assertIn("192.0.2.1", hosts)
        self.assertIn("192.0.2.254", hosts)
        self.assertNotIn("192.0.2.0", hosts)
        self.assertNotIn("192.0.2.255", hosts)

    def test_subnet_scan_candidates_rejects_non_ip_hosts(self):
        self.assertIsNone(subnet_scan_candidates("phone.local"))

    def test_scan_for_profile_finds_the_matching_hardware_serial(self):
        profile = DeviceProfile(
            "phone", "FAKESERIAL01", ("FAKESERIAL01", "10.0.0.9:5555"), None, None
        )
        winner = "10.0.0.77:5555"
        disconnected = []

        def command_runner(command, **_kwargs):
            if command[:2] == ["adb", "connect"]:
                candidate = command[2]
                if candidate != winner:
                    raise BridgeError("failed to connect")
                return f"connected to {candidate}"
            if command[:3] == ["adb", "devices", "-l"]:
                return f"List of devices attached\n{winner}\tdevice\n"
            if command[:2] == ["adb", "-s"] and command[3:6] == [
                "shell", "getprop", "ro.serialno",
            ]:
                return "FAKESERIAL01\n"
            if command[:2] == ["adb", "disconnect"]:
                disconnected.append(command[2])
                return ""
            raise AssertionError(f"unexpected command {command}")

        found = scan_for_profile(profile, command_runner=command_runner)

        self.assertEqual(found, winner)
        self.assertNotIn(winner, disconnected)

    def test_scan_for_profile_disconnects_a_device_with_the_wrong_serial(self):
        profile = DeviceProfile(
            "phone", "FAKESERIAL01", ("FAKESERIAL01", "10.0.0.9:5555"), None, None
        )
        imposter = "10.0.0.5:5555"
        disconnected = []

        def command_runner(command, **_kwargs):
            if command[:2] == ["adb", "connect"]:
                candidate = command[2]
                if candidate != imposter:
                    raise BridgeError("failed to connect")
                return f"connected to {candidate}"
            if command[:3] == ["adb", "devices", "-l"]:
                return f"List of devices attached\n{imposter}\tdevice\n"
            if command[:2] == ["adb", "-s"] and command[3:6] == [
                "shell", "getprop", "ro.serialno",
            ]:
                return "SOMEONE-ELSES-PHONE\n"
            if command[:2] == ["adb", "disconnect"]:
                disconnected.append(command[2])
                return ""
            raise AssertionError(f"unexpected command {command}")

        found = scan_for_profile(profile, command_runner=command_runner)

        self.assertIsNone(found)
        self.assertIn(imposter, disconnected)

    def test_scan_for_profile_skips_a_profile_with_no_usb_serial(self):
        profile = DeviceProfile("phone", "10.0.0.9:5555", ("10.0.0.9:5555",), None, None)
        command_runner = Mock()

        found = scan_for_profile(profile, command_runner=command_runner)

        self.assertIsNone(found)
        command_runner.assert_not_called()

    def test_scan_for_profile_skips_a_profile_with_no_wireless_alias(self):
        profile = DeviceProfile("phone", "usb-serial", ("usb-serial",), None, None)
        command_runner = Mock()

        found = scan_for_profile(profile, command_runner=command_runner)

        self.assertIsNone(found)
        command_runner.assert_not_called()

    def test_refresh_with_scan_finds_and_saves_a_new_wireless_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "FAKESERIAL01",
                            "adb": {"serials": ["FAKESERIAL01", "10.0.0.9:5555"]},
                        }
                    }
                },
                str(path),
            )
            winner = "10.0.0.77:5555"
            # The exact `["adb", "devices", "-l"]` call happens twice: once up front to
            # build the "currently connected" map (must report nothing, or scanning
            # never triggers), and again inside the scan probe after a successful
            # `connect` (must report the winner as connected). A plain call counter
            # distinguishes them; the very first call always happens synchronously,
            # before any concurrent scan probing starts.
            devices_calls = []

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    devices_calls.append(command)
                    if len(devices_calls) == 1:
                        return "List of devices attached\n"
                    return f"List of devices attached\n{winner}\tdevice\n"
                if command[:2] == ["adb", "connect"]:
                    candidate = command[2]
                    if candidate != winner:
                        raise BridgeError("failed to connect")
                    return f"connected to {candidate}"
                if command[:2] == ["adb", "-s"] and "getprop" in command:
                    return "FAKESERIAL01\n"
                if command[:2] == ["adb", "-s"] and "route" in command:
                    return "1.1.1.1 via 192.0.2.1 dev wlan0 src 10.0.0.77 uid 0\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path), scan=True),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.ok)
        outcome = result.value.profiles[0]
        self.assertTrue(outcome.scanned)
        self.assertIn(
            ("adb_serial", "10.0.0.9:5555", winner),
            [(c.field, c.previous, c.current) for c in outcome.changes],
        )
        saved_serials = config["devices"]["phone"]["adb"]["serials"]
        self.assertIn(winner, saved_serials)

    def test_refresh_without_scan_flag_does_not_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "FAKESERIAL01",
                            "adb": {"serials": ["FAKESERIAL01", "10.0.0.9:5555"]},
                        }
                    }
                },
                str(path),
            )

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\n"
                raise AssertionError(f"unexpected command {command}")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path)),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )

        self.assertTrue(result.ok)
        outcome = result.value.profiles[0]
        self.assertFalse(outcome.scanned)
        self.assertEqual(outcome.note, "no reachable ADB endpoint to query")

    def test_refresh_dry_run_with_scan_reports_intent_without_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {
                    "devices": {
                        "phone": {
                            "device_id": "FAKESERIAL01",
                            "adb": {"serials": ["FAKESERIAL01", "10.0.0.9:5555"]},
                        }
                    }
                },
                str(path),
            )

            def command_runner(command, **_kwargs):
                if command == ["adb", "devices", "-l"]:
                    return "List of devices attached\n"
                raise AssertionError(f"unexpected command {command}: dry run must not scan")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest("phone", str(path), dry_run=True, scan=True),
                command_runner=command_runner,
                adb_locator=lambda _: "/usr/bin/adb",
            )

        self.assertTrue(result.ok)
        outcome = result.value.profiles[0]
        self.assertTrue(outcome.scanned)
        self.assertEqual(outcome.note, "network scan would be attempted")

    @patch("linkplane.profiles.fcntl.flock", side_effect=OSError("lock failed"))
    def test_profile_service_returns_typed_lock_error(self, _flock):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(
                {"devices": {"phone": {"device_id": "phone-1"}}}, str(path)
            )

            result = remove_device_profile(ProfileChangeRequest("phone", str(path)))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "operation_failed")
        self.assertIn("unable to lock configuration", result.error.message)


if __name__ == "__main__":
    unittest.main()

class RefreshSeamTests(unittest.TestCase):
    """`refresh_profile_endpoints` must resolve its command runner at call time.

    Regression: the default used to be bound at import time (`= run_command`), so the
    CLI-level `profiles refresh --json` test could not patch it and reached real ADB
    (2026-09-12). Patching the module symbol must be enough.
    """

    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    @patch("linkplane.profiles.run_command", return_value="List of devices attached\n")
    def test_module_symbol_patch_reaches_refresh(self, run_command, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"devices": {}}), encoding="utf-8")

            result = refresh_profile_endpoints(
                EndpointRefreshRequest(None, str(path), True, False)
            )

        self.assertIsNone(result.error)
        run_command.assert_called_once_with(["adb", "devices", "-l"])


def _usb_transport(serial: str, model: str = "Test_Phone"):
    transport = Mock()
    transport.serial = serial
    transport.select_device.return_value = {"model": model}
    return transport


@patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
class PairingIdentityTests(unittest.TestCase):
    """Profile names are human aliases; `device_id` is canonical (Slice 0, D-rule).

    Before 2026-09-12 a non-strict lookup let a second phone paired under an existing name
    inherit that profile's `device_id`, and the merge then appended the new serial as an
    alias of the *old* phone. Every row here must hold before `linkplane setup` exists.
    """

    def pair(self, path, name, serial, **kwargs):
        return pair_usb_device(
            UsbPairingRequest(name=name, config_path=str(path), **kwargs),
            adb_factory=lambda requested: _usb_transport(serial),
        )

    def test_same_device_same_name_is_a_repeatable_no_op(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            first = self.pair(path, "phone", "usb-1", make_default=True)
            second = self.pair(path, "phone", "usb-1")
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(first.ok and second.ok)
        self.assertEqual(config["devices"]["phone"]["device_id"], "usb-1")
        self.assertEqual(config["devices"]["phone"]["adb"]["serials"], ["usb-1"])
        self.assertEqual(config["default_device"], "phone")
        self.assertEqual(list(config["devices"]), ["phone"])

    def test_same_device_under_a_new_name_keeps_the_old_profile_intact(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.pair(path, "phone", "usb-1", make_default=True)
            renamed = self.pair(path, "pixel", "usb-1")
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(renamed.ok)
        self.assertEqual(config["devices"]["phone"]["device_id"], "usb-1")
        self.assertEqual(config["devices"]["pixel"]["device_id"], "usb-1")
        self.assertEqual(config["default_device"], "phone")

    def test_different_device_with_the_same_name_is_a_coded_conflict(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.pair(path, "phone", "usb-1", make_default=True)
            before = path.read_text(encoding="utf-8")
            result = self.pair(path, "phone", "usb-2")
            after = path.read_text(encoding="utf-8")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_request")
        self.assertEqual(result.error.error_code, errors.REQUEST_INVALID)
        self.assertIn("belongs to device usb-1, not usb-2", result.error.message)
        self.assertTrue(any("phone-2" in hint for hint in result.error.hints), result.error.hints)
        self.assertTrue(any("profiles remove phone" in hint for hint in result.error.hints))
        self.assertEqual(before, after, "a conflict must not touch the configuration")

    def test_conflict_reaches_the_cli_with_its_code_and_hints(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.pair(path, "phone", "usb-1", make_default=True)
            arguments = Namespace(name="phone", serial=None, default=False, dry_run=False, config=str(path))
            with patch("linkplane.profiles.AdbTransport", side_effect=lambda requested: _usb_transport("usb-2")), \
                    patch("builtins.print"):
                with self.assertRaises(errors.LinkplaneError) as raised:
                    pair_usb(arguments)

        self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)
        self.assertTrue(raised.exception.hints)

    def test_ssh_pairing_derived_from_a_usb_serial_is_strict_too(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.pair(path, "phone", "usb-1", make_default=True)
            result = pair_ssh_device(
                SshPairingRequest("phone", "phone.local", "termux", config_path=str(path), dry_run=True),
                adb_factory=lambda requested: _usb_transport("usb-2"),
                ssh_factory=Mock(),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.error_code, errors.REQUEST_INVALID)
        self.assertIn("belongs to device usb-1, not usb-2", result.error.message)

    def _wireless(self, path, getprop_serial, calls):
        def runner(command, **kwargs):
            calls.append(list(command))
            if command[:2] == ["adb", "pair"]:
                return "Successfully paired"
            if command[:2] == ["adb", "connect"]:
                return "connected to 10.0.0.9:5555"
            if command[-2:] == ["getprop", "ro.serialno"]:
                return getprop_serial + "\n"
            return ""

        return pair_wireless_device(
            WirelessPairingRequest("phone", "10.0.0.9:37123", "10.0.0.9:5555", config_path=str(path)),
            adb_factory=lambda requested: _usb_transport("10.0.0.9:5555", "Phone"),
            command_runner=runner,
            code_reader=lambda: "123456",
        )

    def test_wireless_alias_is_verified_against_the_profiles_hardware_serial(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.pair(path, "phone", "usb-1", make_default=True)
            calls = []
            result = self._wireless(path, "usb-1", calls)
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.ok, result.error)
        self.assertEqual(config["devices"]["phone"]["device_id"], "usb-1")
        self.assertEqual(config["devices"]["phone"]["adb"]["serials"], ["usb-1", "10.0.0.9:5555"])
        self.assertIn(["adb", "-s", "10.0.0.9:5555", "shell", "getprop", "ro.serialno"], calls)

    def test_wireless_pairing_refuses_and_disconnects_a_different_phone(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.pair(path, "phone", "usb-1", make_default=True)
            before = path.read_text(encoding="utf-8")
            calls = []
            result = self._wireless(path, "usb-2", calls)
            after = path.read_text(encoding="utf-8")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.error_code, errors.REQUEST_INVALID)
        self.assertIn("is not the phone profile phone belongs to (usb-1)", result.error.message)
        self.assertIn(["adb", "disconnect", "10.0.0.9:5555"], calls)
        self.assertEqual(before, after)

    def test_wireless_pairing_of_a_new_profile_needs_no_serial_proof(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            calls = []
            result = self._wireless(path, "whatever", calls)

        self.assertTrue(result.ok, result.error)
        self.assertFalse(any(call[-2:] == ["getprop", "ro.serialno"] for call in calls))

    def test_next_free_profile_name_is_deterministic(self, _which):
        self.assertEqual(next_free_profile_name("phone", []), "phone")
        self.assertEqual(next_free_profile_name("phone", ["phone"]), "phone-2")
        self.assertEqual(next_free_profile_name("phone", ["phone", "phone-2", "phone-3"]), "phone-4")


class ConfigWriteFailureTests(unittest.TestCase):
    """An unwritable configuration is LP-CONFIG-004, never 'the phone command failed'."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere; the read-only directory would not refuse")

    def test_save_config_into_a_read_only_directory_is_coded(self):
        with tempfile.TemporaryDirectory() as directory:
            locked = Path(directory) / "locked"
            locked.mkdir()
            locked.chmod(0o500)
            try:
                with self.assertRaises(errors.LinkplaneError) as raised:
                    save_config({"devices": {}}, str(locked / "config.json"))
            finally:
                locked.chmod(0o700)

        self.assertEqual(raised.exception.code, errors.CONFIG_UNWRITABLE)
        self.assertEqual(raised.exception.hints, errors.CONFIG_UNWRITABLE_HINTS)
        self.assertIn("unable to", str(raised.exception))

    @patch("linkplane.dependencies.shutil.which", return_value="/usr/bin/adb")
    def test_usb_pairing_reports_the_configuration_code_not_a_provider_failure(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            locked = Path(directory) / "locked"
            locked.mkdir()
            locked.chmod(0o500)
            try:
                result = pair_usb_device(
                    UsbPairingRequest(name="phone", config_path=str(locked / "config.json")),
                    adb_factory=lambda requested: _usb_transport("usb-1"),
                )
            finally:
                locked.chmod(0o700)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "operation_failed")
        self.assertEqual(result.error.error_code, errors.CONFIG_UNWRITABLE)
        self.assertEqual(result.error.hints, errors.CONFIG_UNWRITABLE_HINTS)
