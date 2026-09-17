"""`linkplane setup`: every phase transition, without a phone, ADB, systemd, sudo, or a
package manager. The harness scripts what the outside world answers; the real pairing,
profile, client, and error code paths run against a temporary configuration."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import linkplane
from linkplane import setup as setup_module
from linkplane.core import errors
from linkplane.operations import CancellationToken, OperationCancelled
from linkplane.providers.base import BatteryReading, PingResult
from linkplane.setup import (
    API_CLIENT,
    AUTOMATIC_BACKUP,
    COMPLETE,
    DAEMON_INSTALLATION,
    DAEMON_START,
    DEPENDENCIES,
    DEVICE_AUTHORIZATION,
    DEVICE_DETECTION,
    DEVICE_REGISTRATION,
    FIRST_USE_VERIFICATION,
    OBSERVATION_VERIFICATION,
    PREFLIGHT,
    SetupOptions,
    SetupSeams,
    exec_start_of,
    run_setup,
)
from linkplane.core.telemetry import StatusResult
from linkplane.transports import BridgeError

SERIAL = "FAKESERIAL01"
OTHER = "OTHER1234"


def device(serial=SERIAL, state="device", model="SM_S901U"):
    return {"serial": serial, "state": state, "model": model, "usb": "1-2"}


class FakeDaemon:
    """What the control socket would answer, plus what installing/restarting does."""

    def __init__(self, *, running=False, version=None, observes=True, observe_after=0):
        self.running = running
        self.version = version if version is not None else linkplane.__version__
        self.observes = observes
        self.observe_after = observe_after
        self.polls = 0
        self.restarts = 0
        self.serial = None

    def status(self, socket_path):
        if not self.running:
            return None
        self.polls += 1
        devices = {}
        if self.observes and self.serial and self.polls > self.observe_after:
            devices[f"serial:{self.serial}"] = {"address": self.serial, "connection": "connected", "battery": {"level": 84}}
        return {"ok": True, "pid": 4242, "version": self.version, "devices": devices}

    def restart(self):
        self.restarts += 1
        self.running = True
        self.version = linkplane.__version__


class FakeProvider:
    def __init__(self, *, reachable=True, fail_status=False):
        self.reachable = reachable
        self.fail_status = fail_status

    def ping(self):
        return PingResult(self.reachable, "adb", SERIAL, 12.0 if self.reachable else None, None if self.reachable else "no ADB device connected")

    def status(self):
        if self.fail_status:
            raise errors.LinkplaneError(errors.PROVIDER_FAILED, "dumpsys failed")
        return StatusResult("adb", {"model": "SM-S901U"}, {"level": 84}, {"x": 1}, {"y": 1}, 10, ())

    def battery(self):
        return BatteryReading(84, "discharging", "good", (), "adb")


class Harness:
    def __init__(self, directory, *, tools=("adb", "systemctl", "pacman", "sudo"), devices=None,
                 daemon=None, provider=None, environ=None, answers=None, confirm=False, choice=None,
                 install_ok=True, service_error=None):
        self.directory = Path(directory)
        self.config = self.directory / "config.json"
        self.unit_dir = self.directory / "units"
        self.clients = self.directory / "clients.json"
        self.automations = self.directory / "automations.json"
        self.tools = set(tools)
        # A list of device lists; each poll pops the next, the last one repeats.
        self.devices = list(devices) if devices is not None else [[device()]]
        self.daemon = daemon or FakeDaemon()
        self.provider = provider or FakeProvider()
        self.environ = environ if environ is not None else {"XDG_RUNTIME_DIR": "/run/user/1000"}
        self.answers = list(answers or [])
        self.confirm_answer = confirm
        self.choice = choice
        self.install_ok = install_ok
        self.service_error = service_error
        self.installs = []
        self.service_installs = []
        self.prompts = []
        self.confirms = []
        self.choices = []
        self.sleeps = []
        self.now = 1000.0
        self.on_sleep = None

    # -- seams --
    def finder(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def list_adb_devices(self):
        current = self.devices[0] if len(self.devices) == 1 else self.devices.pop(0)
        return [dict(item) for item in current]

    def adb_factory(self, serial):
        self.daemon.serial = serial  # pairing selects the chosen phone; the daemon then observes it
        transport = Mock()
        transport.serial = serial
        listing = {item["serial"]: item for item in self.devices[0]}
        entry = listing.get(serial)
        if entry is None:
            transport.select_device.side_effect = BridgeError(f"ADB device {serial} was not found")
        else:
            transport.select_device.return_value = dict(entry)
        return transport

    def provider_factory(self, serial):
        self.daemon.serial = serial
        return self.provider

    def install_dependency(self, plan):
        self.installs.append(plan)
        if not self.install_ok:
            raise BridgeError("adb installation failed with exit code 1")
        self.tools.add("adb")

    def service_install(self, *, unit_dir=None, extra_arguments=(), dry_run=False):
        from linkplane import service

        self.service_installs.append((unit_dir, tuple(extra_arguments), dry_run))
        if self.service_error is not None:
            raise self.service_error
        unit_path = Path(unit_dir) / service.UNIT_NAME
        text = service.unit_text(self.daemon_command(tuple(extra_arguments)))
        if not dry_run:
            unit_path.parent.mkdir(parents=True, exist_ok=True)
            unit_path.write_text(text, encoding="utf-8")
            self.daemon.running = True  # `enable --now`
        return service.ServiceResult("install", str(unit_path), text, (), dry_run)

    @staticmethod
    def daemon_command(extra):
        return ["/home/u/.local/bin/linkplane", "daemon", "run", *extra]

    def daemon_status(self, socket_path):
        return self.daemon.status(socket_path)

    def restart_daemon(self):
        self.daemon.restart()

    start_ok = True

    def start_daemon(self):
        self.daemon.starts = getattr(self.daemon, "starts", 0) + 1
        if self.start_ok:
            self.daemon.running = True

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))

    def ask(self, question, default):
        self.prompts.append((question, default))
        return self.answers.pop(0) if self.answers else default

    def confirm(self, question):
        self.confirms.append(question)
        return self.confirm_answer

    def choose(self, question, choices):
        self.choices.append((question, list(choices)))
        return self.choice

    # -- run --
    def seams(self):
        return SetupSeams(
            finder=self.finder, environ=self.environ, list_adb_devices=self.list_adb_devices,
            adb_factory=self.adb_factory, provider_factory=self.provider_factory,
            install_dependency=self.install_dependency, service_install=self.service_install,
            daemon_command=self.daemon_command, daemon_status=self.daemon_status,
            restart_daemon=self.restart_daemon, start_daemon=self.start_daemon, clock=self.clock, sleep=self.sleep,
            ask=self.ask, confirm=self.confirm, choose=self.choose,
        )

    def options(self, **overrides):
        base = dict(config_path=str(self.config), unit_dir=str(self.unit_dir), clients_path=str(self.clients),
                    automations_path=str(self.automations), backup_destination=str(self.directory / "Photos"),
                    socket_path=str(self.directory / "daemon.sock"), device_wait=10.0, daemon_wait=5.0,
                    observe_wait=5.0, poll_interval=1.0, interactive=True)
        base.update(overrides)
        return SetupOptions(**base)

    def run(self, cancel=None, events=None, **overrides):
        self.daemon.serial = self.daemon.serial or SERIAL
        return run_setup(self.options(**overrides), self.seams(), progress=events.append if events is not None else None, cancel=cancel)

    def config_json(self):
        return json.loads(self.config.read_text(encoding="utf-8"))


def phases(result):
    return [step.phase for step in result.steps]


def by_name(result):
    return {step.check.name: step.check for step in result.steps}


class SetupHappyPathTests(unittest.TestCase):
    def test_fresh_machine_reaches_complete_with_every_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            result = h.run()
            config = h.config_json()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(result.status, "complete")
        expected = [PREFLIGHT, DEPENDENCIES, DEVICE_DETECTION, DEVICE_AUTHORIZATION, DEVICE_REGISTRATION,
                    DAEMON_INSTALLATION, DAEMON_START, OBSERVATION_VERIFICATION, FIRST_USE_VERIFICATION,
                    AUTOMATIC_BACKUP, COMPLETE]
        seen = []
        for phase in phases(result):
            if phase not in seen:
                seen.append(phase)
        self.assertEqual(seen, expected)
        self.assertTrue(all(step.check.status in {"ok", "warning"} for step in result.steps if step.phase != AUTOMATIC_BACKUP))
        self.assertEqual((by_name(result)["Automatic camera-photo backup"].status, by_name(result)["Automatic camera-photo backup"].summary), ("skipped", "off"))
        # Registered with the default name, as the default device, by the real pairing code.
        self.assertEqual(config["default_device"], "phone")
        self.assertEqual(config["devices"]["phone"]["device_id"], SERIAL)
        self.assertEqual(result.profile, {"name": "phone", "device_id": SERIAL, "created": True, "default": True})
        self.assertEqual(result.device, {"serial": SERIAL, "model": "SM S901U"})
        # Daemon installed once, running at the installed version, device observed, first use done.
        self.assertEqual(len(h.service_installs), 1)
        self.assertEqual((result.daemon["installed"], result.daemon["running"], result.daemon["version"]), (True, True, linkplane.__version__))
        self.assertEqual(by_name(result)["Device observed"].summary, f"serial:{SERIAL}: connected, battery 84%")
        self.assertEqual(by_name(result)["Battery"].summary, "84% (discharging)")
        self.assertIsNone(result.api_client)
        self.assertFalse(h.clients.exists(), "no API client without --api-client")
        self.assertEqual(result.next_steps[0], 'linkplane notify "Hello from Linkplane"')
        # The name prompt was offered with the default.
        self.assertEqual(h.prompts, [("Name this device", "phone")])

    def test_timing_records_start_first_use_and_elapsed_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.daemon.observe_after = 4  # several status polls before the daemon sees the phone
            result = h.run()

        self.assertIsNotNone(result.timing.first_use_succeeded)
        self.assertGreaterEqual(len(h.sleeps), 2)
        self.assertGreaterEqual(result.timing.elapsed_seconds, float(len(h.sleeps)))
        payload = result.to_dict()["timing"]
        self.assertEqual(set(payload), {"setup_started", "first_use_succeeded", "elapsed_seconds"})

    def test_optional_dependencies_missing_never_block_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, tools=("adb", "systemctl"))  # no scrcpy, notify-send, v4l2-ctl, ssh, ...
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        optional = by_name(result)["Optional features"]
        self.assertEqual(optional.status, "warning")
        self.assertIn("Screen control", optional.summary)
        self.assertIn("scrcpy not installed", optional.summary)
        self.assertIn("Webcam: v4l2-ctl not installed", optional.summary)
        self.assertNotIn("ssh", optional.summary.lower())

    def test_dry_run_writes_nothing_but_runs_every_check(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=False))
            result = h.run(dry_run=True)
            config_exists = h.config.exists()
            unit_exists = (h.unit_dir / "linkplaned.service").exists()

        self.assertTrue(result.ok, result.failure)
        self.assertFalse(config_exists)
        self.assertFalse(unit_exists)
        self.assertEqual(h.service_installs, [(str(h.unit_dir), (), True)])
        checks = by_name(result)
        self.assertIn("would be registered", checks["Device registered"].summary)
        self.assertIn("would install", checks["Daemon installed"].summary)
        self.assertEqual(checks["Daemon running"].status, "skipped")
        self.assertEqual(checks["Ping"].status, "ok")


class SetupIdempotenceTests(unittest.TestCase):
    def test_second_run_reuses_everything_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            first = h.run()
            config_before = h.config.read_text(encoding="utf-8")
            unit_before = (h.unit_dir / "linkplaned.service").read_text(encoding="utf-8")
            h.prompts.clear()
            second = h.run()
            config_after = h.config.read_text(encoding="utf-8")
            unit_after = (h.unit_dir / "linkplaned.service").read_text(encoding="utf-8")

        self.assertTrue(first.ok and second.ok)
        self.assertEqual(config_before, config_after)
        self.assertEqual(unit_before, unit_after)
        self.assertEqual(len(h.service_installs), 1, "no reinstall on the second run")
        self.assertEqual(h.daemon.restarts, 0)
        self.assertEqual(h.prompts, [], "no name prompt when the phone is already registered")
        checks = by_name(second)
        self.assertEqual(checks["Device registered"].summary, 'already registered as "phone"')
        self.assertIn("already installed", checks["Daemon installed"].summary)
        self.assertEqual(second.profile["created"], False)

    def test_same_device_existing_profile_under_another_name_is_reused_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, answers=["pixel"])
            h.run()
            h.answers = ["phone"]  # would be a second name for the same phone; must not be asked
            result = h.run()
            config = h.config_json()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(list(config["devices"]), ["pixel"])
        self.assertEqual(result.profile["name"], "pixel")

    def test_existing_profile_without_a_default_becomes_the_default(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.config.write_text(json.dumps({"devices": {"mine": {"device_id": SERIAL, "adb": {"serials": [SERIAL]}}}}), encoding="utf-8")
            result = h.run()
            config = h.config_json()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(config["default_device"], "mine")
        self.assertIn("now the default", by_name(result)["Device registered"].summary)

    def test_unrelated_configuration_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.config.write_text(json.dumps({"devices": {}, "ssh": {"host": "legacy.local", "user": "u0"}, "custom": {"keep": True}}), encoding="utf-8")
            result = h.run()
            config = h.config_json()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(config["ssh"], {"host": "legacy.local", "user": "u0"})
        self.assertEqual(config["custom"], {"keep": True})


class AutomaticBackupPhaseTests(unittest.TestCase):
    """v0.6: setup offers automatic photo backup once, after the phone is known-good."""

    def rules(self, h):
        return json.loads(h.automations.read_text(encoding="utf-8"))["automations"] if h.automations.exists() else []

    def test_harness_never_reaches_the_real_rules_file(self):
        """Regression (Slice 1): the harness once omitted automations_path, and an opt-in test
        wrote the developer's real ~/.config/linkplane/automations.json. Every rules-file
        resolution during a setup run must name an explicit path."""
        from linkplane import automations

        real = automations.resolve_automations_path

        def guarded(path=None):
            if path is None:
                raise AssertionError("setup resolved the default (real) automations.json")
            return real(path)

        with tempfile.TemporaryDirectory() as directory, patch("linkplane.presets.resolve_automations_path", guarded):
            h = Harness(directory, confirm=True)
            opted_in = h.run()
            again = h.run()
        self.assertTrue(opted_in.ok and again.ok)
        self.assertTrue(str(h.automations).startswith(directory))
        self.assertTrue(h.options().backup_destination.startswith(directory))

    def test_decline_is_the_default_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)  # confirm answers no, like pressing Enter at [y/N]
            events = []
            result = h.run(events=events)
            rules_exist = h.automations.exists()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(h.confirms, [setup_module.BACKUP_QUESTION])
        self.assertFalse(rules_exist, "declining writes no rule and no file")
        self.assertEqual(h.daemon.restarts, 0)
        step = by_name(result)["Automatic camera-photo backup"]
        self.assertEqual((step.status, step.summary), ("skipped", "off"))
        self.assertIn("linkplane automations enable photo-backup", step.fix)
        guidance = "\n".join(next(e.details["guidance"] for e in events if e.phase == AUTOMATIC_BACKUP and "guidance" in e.details))
        self.assertIn("one-way", guidance)
        self.assertIn("never deletes their backups", guidance)
        self.assertEqual(result.automatic_backup, {"state": "not set up", "destination": None, "changed": False})
        self.assertNotIn("linkplane automations jobs", result.next_steps)

    def test_question_comes_only_after_basic_setup_is_known_good(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, confirm=True)
            result = h.run()
        names = [step.check.name for step in result.steps]
        self.assertLess(names.index("Battery"), names.index("Automatic camera-photo backup"))
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, devices=[[device(state="unauthorized")]], confirm=True)
            failed = h.run(device_wait=2.0)
        self.assertFalse(failed.ok)
        self.assertNotIn(setup_module.BACKUP_QUESTION, h.confirms)
        self.assertFalse(h.automations.exists())

    def test_opt_in_writes_one_ordinary_rule_and_restarts_the_daemon_to_start_now(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, confirm=True)
            result = h.run()
            rules = self.rules(h)
            photos = str((Path(directory) / "Photos").resolve())

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(h.prompts[-1], (setup_module.BACKUP_FOLDER_QUESTION, str(Path(directory) / "Photos")))
        self.assertEqual(rules, [{
            "name": "photo-backup:phone", "preset": "photo-backup", "enabled": True, "when": "device.connected",
            "device": "phone", "on_initial": True,
            "do": [{"action": "backup", "source": "/sdcard/DCIM/Camera", "destination": photos},
                   {"action": "notify-desktop", "if": {"downloaded": {"above": 0}}, "title": "Linkplane camera-photo backup",
                    "message": "{downloaded} new camera photo(s) or video(s) backed up to {destination}"}],
            "on_error": [{"action": "notify-desktop", "if": {"failure_kind": {"not": "stopped"}},
                          "title": "Automatic camera-photo backup failed", "message": "{failure_reason}", "urgency": "normal"}],
        }])
        self.assertEqual(h.daemon.restarts, 1, "the restart re-observes the connected phone: the first backup starts")
        step = by_name(result)["Automatic camera-photo backup"]
        self.assertEqual(step.status, "ok")
        self.assertIn("first backup is starting now", step.summary)
        self.assertEqual(result.automatic_backup["state"], "enabled")
        self.assertTrue(result.automatic_backup["first_backup_started"])
        self.assertEqual(result.next_steps[-1], "linkplane automations jobs")

    def test_rerun_never_duplicates_never_asks_again_and_shows_the_state(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, confirm=True)
            h.run()
            before = h.automations.read_text(encoding="utf-8")
            h.confirms.clear()
            second = h.run()
            after = h.automations.read_text(encoding="utf-8")

        self.assertEqual(before, after)
        self.assertEqual(h.confirms, [])
        self.assertEqual(h.daemon.restarts, 1, "no second restart")
        step = by_name(second)["Automatic camera-photo backup"]
        self.assertEqual(step.status, "ok")
        self.assertTrue(step.summary.startswith("on: new camera photos go to "))

    def test_declined_then_rerun_is_not_asked_again(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.run()
            h.confirms.clear()
            second = h.run()
        self.assertEqual(h.confirms, [])
        step = by_name(second)["Automatic camera-photo backup"]
        self.assertEqual((step.status, step.summary), ("skipped", "off"))
        self.assertIn("automations enable photo-backup", step.fix)

    def test_disabled_rule_is_shown_off_and_left_alone(self):
        from linkplane import presets

        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, confirm=True)
            h.run()
            presets.disable_photo_backup("phone", path=str(h.automations))
            before = h.automations.read_text(encoding="utf-8")
            second = h.run()
            after = h.automations.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertEqual(by_name(second)["Automatic camera-photo backup"].status, "skipped")
        self.assertEqual(second.automatic_backup["state"], "disabled")

    def test_non_interactive_and_dry_run_never_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, confirm=True)
            quiet = h.run(interactive=False)
            self.assertFalse(h.automations.exists())
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, confirm=True)
            dry = h.run(dry_run=True)
            self.assertFalse(h.automations.exists())
        self.assertNotIn(setup_module.BACKUP_QUESTION, h.confirms)
        self.assertIn("non-interactive", by_name(quiet)["Automatic camera-photo backup"].summary)
        self.assertIn("default: no", by_name(dry)["Automatic camera-photo backup"].summary)

    def test_unsafe_folder_is_explained_and_asked_again(self):
        with tempfile.TemporaryDirectory() as directory:
            good = str(Path(directory) / "Mine")
            h = Harness(directory, confirm=True, answers=["phone", "/etc/photos", good])
            events = []
            result = h.run(events=events)
            rules = self.rules(h)

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(rules[0]["do"][0]["destination"], str(Path(good).resolve()))
        guidance = [line for e in events if e.phase == AUTOMATIC_BACKUP for line in e.details.get("guidance", [])]
        self.assertTrue(any("system location" in line for line in guidance))

    def test_repeatedly_unsafe_folder_leaves_it_off_but_setup_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, confirm=True, answers=["phone", "/", "/etc", "/usr/x"])
            result = h.run()
            exists = h.automations.exists()
        self.assertTrue(result.ok, result.failure)
        self.assertFalse(exists)
        step = by_name(result)["Automatic camera-photo backup"]
        self.assertEqual(step.status, "warning")
        self.assertIn("not turned on", step.summary)

    def test_without_a_daemon_the_rule_is_written_and_nothing_is_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, tools=("adb",), environ={}, daemon=FakeDaemon(running=False), confirm=True)
            result = h.run()
            rules = self.rules(h)
        self.assertTrue(result.ok, result.failure)
        self.assertEqual(len(rules), 1)
        self.assertEqual(h.daemon.restarts, 0)
        self.assertIn("once the Linkplane service is running", by_name(result)["Automatic camera-photo backup"].summary)


class DependencyPhaseTests(unittest.TestCase):
    def test_adb_missing_and_user_accepts_install(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, tools=("systemctl", "pacman", "sudo"), confirm=True)
            events = []
            result = h.run(events=events)

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(len(h.installs), 1)
        self.assertEqual(" ".join(h.installs[0].install_command), "sudo pacman -S --needed android-tools")
        self.assertEqual(h.confirms[0], "Install android-tools now?")
        guidance = next(e.details["guidance"] for e in events if "guidance" in e.details and e.phase == DEPENDENCIES)
        self.assertIn("  sudo pacman -S --needed android-tools", guidance)
        self.assertIn("installed", by_name(result)["Android platform tools"].summary)

    def test_adb_missing_and_user_declines(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, tools=("systemctl", "pacman", "sudo"), confirm=False)
            result = h.run()
            config_exists = h.config.exists()

        self.assertFalse(result.ok)
        self.assertEqual(h.installs, [])
        failure = result.failure
        self.assertEqual((failure.phase, failure.check.code), (DEPENDENCIES, errors.DEPENDENCY_MISSING))
        self.assertIn("declined", failure.check.summary)
        self.assertIn("sudo pacman -S --needed android-tools", failure.check.fix)
        self.assertFalse(config_exists)

    def test_adb_missing_non_interactive_never_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, tools=("systemctl", "pacman", "sudo"), confirm=True)
            result = h.run(interactive=False)

        self.assertFalse(result.ok)
        self.assertEqual(h.installs, [])
        self.assertEqual(h.confirms, [])
        self.assertEqual(result.failure.check.code, errors.DEPENDENCY_MISSING)

    def test_adb_install_failure_is_coded_and_rerunnable(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, tools=("systemctl", "pacman", "sudo"), confirm=True, install_ok=False)
            result = h.run()

        self.assertFalse(result.ok)
        self.assertEqual(result.failure.check.code, errors.DEPENDENCY_MISSING)
        self.assertIn("run `linkplane setup` again", result.failure.check.fix)


class DevicePhaseTests(unittest.TestCase):
    def test_no_device_shows_phone_guidance_then_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, devices=[[]])
            events = []
            result = h.run(events=events, device_wait=3.0)

        self.assertFalse(result.ok)
        self.assertEqual((result.failure.phase, result.failure.check.code), (DEVICE_DETECTION, errors.CONNECT_NO_DEVICE))
        guidance = [e for e in events if "guidance" in e.details]
        self.assertEqual(len(guidance), 1, "guidance is printed once, not every poll")
        self.assertIn("USB debugging", "\n".join(guidance[0].details["guidance"]))
        self.assertEqual(h.sleeps, [1.0, 1.0, 1.0])

    def test_unauthorized_then_authorized(self):
        with tempfile.TemporaryDirectory() as directory:
            listing = [[device(state="unauthorized")]] * 3 + [[device()]]
            h = Harness(directory, devices=listing)
            events = []
            result = h.run(events=events)

        self.assertTrue(result.ok, result.failure)
        guidance = [e.details["guidance"] for e in events if "guidance" in e.details and e.phase != AUTOMATIC_BACKUP]
        self.assertEqual(len(guidance), 1)
        self.assertIn("tap Allow", "\n".join(guidance[0]))
        self.assertNotIn("udev", "\n".join(guidance[0]))
        names = [step.check.name for step in result.steps]
        self.assertLess(names.index("Android device detected"), names.index("USB debugging authorized"))

    def test_unauthorized_forever_times_out_with_phone_side_code(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, devices=[[device(state="unauthorized")]])
            result = h.run(device_wait=3.0)

        self.assertFalse(result.ok)
        self.assertEqual((result.failure.phase, result.failure.check.code), (DEVICE_AUTHORIZATION, errors.AUTH_UNAUTHORIZED_DEVICE))

    def test_offline_then_device(self):
        with tempfile.TemporaryDirectory() as directory:
            listing = [[device(state="offline")]] * 2 + [[device()]]
            h = Harness(directory, devices=listing)
            events = []
            result = h.run(events=events)

        self.assertTrue(result.ok, result.failure)
        guidance = "\n".join(next(e.details["guidance"] for e in events if "guidance" in e.details))
        self.assertIn("cannot currently communicate", guidance)
        self.assertIn("plug it back in", guidance)
        self.assertNotIn("Allow", guidance)
        self.assertNotIn("reconnect", guidance.replace("Reconnect", ""))  # never `adb reconnect`

    def test_offline_forever_is_a_reconnect_problem_not_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, devices=[[device(state="offline")]])
            result = h.run(device_wait=2.0)

        self.assertFalse(result.ok)
        self.assertEqual(result.failure.check.code, errors.CONNECT_UNREACHABLE)
        self.assertIn("linkplane doctor", result.failure.check.fix)
        self.assertNotIn("Allow", result.failure.check.fix)

    def test_host_usb_permission_failure_stops_with_host_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, devices=[[device(state="no permissions")]])
            result = h.run()

        self.assertFalse(result.ok)
        failure = result.failure
        self.assertEqual((failure.phase, failure.check.code), (DEVICE_AUTHORIZATION, errors.AUTH_USB_PERMISSION))
        self.assertIn("android-udev", failure.check.fix)
        self.assertNotIn("Allow", failure.check.fix)
        self.assertEqual(h.sleeps, [], "no polling: the user must act on the computer first")

    def test_multiple_devices_are_chosen_and_the_choice_sticks(self):
        with tempfile.TemporaryDirectory() as directory:
            # Discovery order flips after the choice; setup must keep following OTHER.
            listing = [[device(SERIAL), device(OTHER, model="Pixel_9")], [device(OTHER, model="Pixel_9"), device(SERIAL)]]
            h = Harness(directory, devices=listing, choice=1)
            result = h.run()
            config = h.config_json()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(len(h.choices), 1)
        self.assertEqual(h.choices[0][1], [f"SM S901U ({SERIAL}, device)", f"Pixel 9 ({OTHER}, device)"])
        self.assertEqual(result.device["serial"], OTHER)
        self.assertEqual(config["devices"]["phone"]["device_id"], OTHER)

    def test_multiple_devices_non_interactive_needs_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, devices=[[device(SERIAL), device(OTHER)]])
            ambiguous = h.run(interactive=False)
            chosen = Harness(directory, devices=[[device(SERIAL), device(OTHER)]]).run(interactive=False, serial=OTHER)

        self.assertEqual(ambiguous.failure.check.code, errors.CONNECT_AMBIGUOUS)
        self.assertTrue(chosen.ok, chosen.failure)
        self.assertEqual(chosen.device["serial"], OTHER)

    def test_listing_failure_is_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.list_adb_devices = lambda: (_ for _ in ()).throw(BridgeError("ADB is not installed"))
            result = h.run()

        self.assertEqual(result.failure.check.code, errors.DEPENDENCY_MISSING)


class RegistrationPhaseTests(unittest.TestCase):
    def test_name_collision_with_another_phone_offers_the_next_free_name(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.config.write_text(json.dumps({"default_device": "phone", "devices": {"phone": {"device_id": OTHER, "adb": {"serials": [OTHER]}}}}), encoding="utf-8")
            events = []
            result = h.run(events=events)
            config = h.config_json()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(h.prompts, [("Name this device", "phone-2")])
        self.assertEqual(config["devices"]["phone"]["device_id"], OTHER, "the other phone's profile is untouched")
        self.assertEqual(config["devices"]["phone-2"]["device_id"], SERIAL)
        self.assertEqual(config["default_device"], "phone")

    def test_user_typed_name_of_another_phone_is_refused_and_reasked(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, answers=["phone", "mine"])
            h.config.write_text(json.dumps({"default_device": "phone", "devices": {"phone": {"device_id": OTHER}}}), encoding="utf-8")
            events = []
            result = h.run(events=events)
            config = h.config_json()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual([p[1] for p in h.prompts], ["phone-2", "phone-2"])
        self.assertEqual(config["devices"]["mine"]["device_id"], SERIAL)
        self.assertEqual(config["devices"]["phone"]["device_id"], OTHER)
        self.assertTrue(any("already belongs to another phone" in "\n".join(e.details.get("guidance", [])) for e in events))

    def test_explicit_name_of_another_phone_is_a_coded_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.config.write_text(json.dumps({"default_device": "phone", "devices": {"phone": {"device_id": OTHER}}}), encoding="utf-8")
            before = h.config.read_text(encoding="utf-8")
            result = h.run(name="phone")
            after = h.config.read_text(encoding="utf-8")

        self.assertFalse(result.ok)
        self.assertEqual((result.failure.phase, result.failure.check.code), (DEVICE_REGISTRATION, errors.REQUEST_INVALID))
        self.assertIn("phone-2", result.failure.check.fix)
        self.assertEqual(before, after)

    def test_invalid_configuration_stops_in_preflight_without_touching_it(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.config.write_text("{not json", encoding="utf-8")
            result = h.run()
            after = h.config.read_text(encoding="utf-8")

        self.assertFalse(result.ok)
        self.assertEqual((result.failure.phase, result.failure.check.code), (PREFLIGHT, errors.CONFIG_INVALID))
        self.assertEqual(after, "{not json")

    def test_unwritable_configuration_directory_is_config_004(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        with tempfile.TemporaryDirectory() as directory:
            locked = Path(directory) / "locked"
            locked.mkdir()
            locked.chmod(0o500)
            try:
                h = Harness(directory)
                result = h.run(config_path=str(locked / "sub" / "config.json"))
            finally:
                locked.chmod(0o700)

        self.assertEqual((result.failure.phase, result.failure.check.code), (PREFLIGHT, errors.CONFIG_UNWRITABLE))


class DaemonPhaseTests(unittest.TestCase):
    def test_daemon_absent_is_installed_and_started(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=False))
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(len(h.service_installs), 1)
        self.assertEqual(by_name(result)["Daemon running"].summary, f"pid 4242, version {linkplane.__version__}")

    def test_daemon_already_installed_at_the_correct_version_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=True))
            h.service_install(unit_dir=str(h.unit_dir))  # pre-existing matching unit
            h.service_installs.clear()
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(h.service_installs, [])
        self.assertEqual(h.daemon.restarts, 1, "a running daemon is restarted once after the profile was added")
        self.assertIn("already installed", by_name(result)["Daemon installed"].summary)

    def test_installed_but_inactive_unit_is_started_not_reported_dead(self):
        """Onboarding run 002 (Ubuntu 24.04): after `linkplane daemon stop`, a rerun of setup
        found the unit installed, waited, and stopped with LP-DAEMON-001 instead of starting
        the service it had installed."""
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=False))
            h.service_install(unit_dir=str(h.unit_dir))  # unit already on disk
            h.daemon.running = False                      # ...but the service is inactive
            h.service_installs.clear()
            h.config.write_text(json.dumps({"default_device": "phone", "devices": {"phone": {"device_id": SERIAL}}}), encoding="utf-8")
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(h.service_installs, [], "no reinstall")
        self.assertEqual(h.daemon.starts, 1)
        self.assertTrue(result.daemon["started"])
        self.assertIn("started; pid 4242", by_name(result)["Daemon running"].summary)
        self.assertEqual(h.daemon.restarts, 0)

    def test_daemon_running_an_older_version_is_restarted_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=True, version="0.3.0"))
            h.service_install(unit_dir=str(h.unit_dir))
            h.service_installs.clear()
            h.config.write_text(json.dumps({"default_device": "phone", "devices": {"phone": {"device_id": SERIAL}}}), encoding="utf-8")
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(h.daemon.restarts, 1)
        check = by_name(result)["Daemon running"]
        self.assertIn("restarted (was version 0.3.0)", check.summary)
        self.assertEqual(result.daemon["version"], linkplane.__version__)
        self.assertTrue(result.daemon["restarted"])

    def test_unit_pointing_at_a_vanished_installation_is_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            from linkplane import service

            h = Harness(directory, daemon=FakeDaemon(running=True))
            h.unit_dir.mkdir()
            (h.unit_dir / service.UNIT_NAME).write_text(service.unit_text(["/old/venv/bin/linkplane", "daemon", "run"]), encoding="utf-8")
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(len(h.service_installs), 1)
        self.assertIn("reinstalled (ExecStart points elsewhere)", by_name(result)["Daemon installed"].summary)
        self.assertEqual(h.daemon.restarts, 1)

    def test_daemon_start_failure_is_coded_with_journal_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=False))
            original = h.service_install

            def install_without_starting(**kwargs):
                result = original(**kwargs)
                h.daemon.running = False
                return result

            h.service_install = install_without_starting
            h.start_ok = False  # systemd accepts the start but the process never answers
            result = h.run()
            config = h.config_json()

        self.assertFalse(result.ok)
        self.assertEqual((result.failure.phase, result.failure.check.code), (DAEMON_START, errors.DAEMON_NOT_RUNNING))
        self.assertIn("journalctl", result.failure.check.fix)
        self.assertEqual(config["devices"]["phone"]["device_id"], SERIAL, "the registration survives")

    def test_service_install_error_is_coded(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=False), service_error=errors.LinkplaneError(errors.PROVIDER_FAILED, "systemctl --user failed: Failed to connect to bus"))
            result = h.run()

        self.assertEqual((result.failure.phase, result.failure.check.code), (DAEMON_INSTALLATION, errors.PROVIDER_FAILED))

    def test_no_systemd_skips_the_daemon_phases_but_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, tools=("adb",), environ={}, daemon=FakeDaemon(running=False))
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        checks = by_name(result)
        self.assertEqual(checks["Background service"].status, "warning")
        self.assertEqual(checks["Daemon installed"].status, "skipped")
        self.assertEqual(checks["Daemon running"].status, "skipped")
        self.assertEqual(checks["Device observed"].status, "skipped")
        self.assertEqual(checks["Ping"].status, "ok")

    def test_no_daemon_flag_verifies_an_already_running_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(running=True, version="0.3.0"))
            result = h.run(install_daemon=False)

        self.assertTrue(result.ok, result.failure)
        checks = by_name(result)
        self.assertEqual(checks["Daemon installed"].status, "skipped")
        self.assertEqual(checks["Daemon running"].status, "warning")
        self.assertIn("runs version 0.3.0", checks["Daemon running"].summary)
        self.assertEqual(h.daemon.restarts, 0, "cannot restart what it did not install")
        self.assertEqual(checks["Device observed"].status, "ok")

    def test_device_never_observed_stops_with_guidance_and_keeps_config(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(observes=False))
            result = h.run(observe_wait=3.0)
            config = h.config_json()

        self.assertFalse(result.ok)
        self.assertEqual((result.failure.phase, result.failure.check.code), (OBSERVATION_VERIFICATION, errors.CONNECT_NO_DEVICE))
        self.assertIn("events --follow", result.failure.check.fix)
        self.assertIn("doctor", result.failure.check.fix)
        self.assertEqual(config["devices"]["phone"]["device_id"], SERIAL)

    def test_observation_matches_by_profile_name_too(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            real_status = h.daemon.status

            def named_status(socket_path):
                info = real_status(socket_path)
                if info and info["devices"]:
                    info["devices"] = {"phone": {"address": None, "connection": "connected", "battery": {"level": 50}}}
                return info

            h.daemon_status = named_status
            result = h.run()

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(by_name(result)["Device observed"].summary, "phone: connected, battery 50%")


class FirstUseAndClientTests(unittest.TestCase):
    def test_unreachable_phone_at_first_use_is_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, provider=FakeProvider(reachable=False))
            result = h.run()

        self.assertFalse(result.ok)
        self.assertEqual((result.failure.phase, result.failure.check.code), (FIRST_USE_VERIFICATION, errors.CONNECT_NO_DEVICE))
        self.assertIsNone(result.timing.first_use_succeeded)

    def test_status_failure_at_first_use_is_coded(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, provider=FakeProvider(fail_status=True))
            result = h.run()

        self.assertEqual((result.failure.check.name, result.failure.check.code), ("Status", errors.PROVIDER_FAILED))

    def test_api_client_is_created_only_when_asked_with_read_scopes(self):
        from linkplane import clients as clients_module

        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            result = h.run(api_client="waybar")
            stored = clients_module.load_clients(str(h.clients))
            again = h.run(api_client="waybar")
            stored_again = clients_module.load_clients(str(h.clients))

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(result.api_client["client_id"], "waybar")
        self.assertTrue(result.api_client["created"])
        self.assertTrue(result.api_client["token"])
        self.assertEqual(tuple(result.api_client["scopes"]), clients_module.READ_SCOPES)
        for scope in ("audit.read", "rules.read", "rules.reload", "jobs.cancel", "shell.execute"):
            self.assertNotIn(scope, result.api_client["scopes"])
        self.assertEqual([client.client_id for client in stored], ["waybar"])
        # A rerun never mints a second token for the same id.
        self.assertTrue(again.ok, again.failure)
        self.assertFalse(again.api_client["created"])
        self.assertIsNone(again.api_client["token"])
        self.assertEqual(len(stored_again), 1)

    def test_api_client_invalid_id_is_coded(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            result = h.run(api_client="Not Valid!")

        self.assertEqual((result.failure.phase, result.failure.check.code), (API_CLIENT, errors.REQUEST_INVALID))


class InterruptTests(unittest.TestCase):
    def test_ctrl_c_while_waiting_for_the_phone_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, devices=[[]])
            cancel = CancellationToken()
            h.on_sleep = lambda count: cancel.cancel("interrupt") if count == 2 else None
            with self.assertRaises(OperationCancelled):
                h.run(cancel=cancel)
            leftovers = sorted(p.name for p in Path(directory).iterdir())

        self.assertEqual(leftovers, [])
        self.assertEqual(len(h.sleeps), 2)

    def test_ctrl_c_while_waiting_for_observation_keeps_the_valid_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, daemon=FakeDaemon(observes=False))
            cancel = CancellationToken()
            h.on_sleep = lambda count: cancel.cancel("interrupt")
            with self.assertRaises(OperationCancelled):
                h.run(cancel=cancel)
            config = h.config_json()
            leftovers = sorted(p.name for p in Path(directory).iterdir())

        self.assertEqual(config["devices"]["phone"]["device_id"], SERIAL)
        # `config.json.lock` is the standing flock file every profile write uses; the
        # only leftovers that would matter are the atomic-write temporaries.
        self.assertFalse(any(name.startswith(".config.json.") for name in leftovers), "no temp files")


class HelpersTests(unittest.TestCase):
    def test_exec_start_parsing(self):
        from linkplane import service

        text = service.unit_text(["/home/u/.local/bin/linkplane", "daemon", "run", "--no-api"])
        self.assertEqual(exec_start_of(text), ["/home/u/.local/bin/linkplane", "daemon", "run", "--no-api"])
        self.assertIsNone(exec_start_of("[Service]\nType=simple\n"))


class CompletionSuggestionTests(unittest.TestCase):
    """Alpha tester #1 (v0.5.0) was told `linkplane send <file>`, typed `linkplane send`, and
    got an argument error as the first thing after a successful setup. Every suggested
    command must run exactly as printed."""

    def test_suggestions_are_complete_commands_with_notify_first(self):
        from linkplane.setup import NEXT_STEPS, SEND_EXAMPLE_PATH

        self.assertEqual(NEXT_STEPS[0], 'linkplane notify "Hello from Linkplane"')
        for line in NEXT_STEPS:
            self.assertNotIn("<", line, line)
            self.assertNotIn(">file", line, line)
            self.assertNotRegex(line, r"linkplane send\s*$", line)
        send_lines = [line for line in NEXT_STEPS if "linkplane send" in line]
        self.assertEqual(len(send_lines), 1)
        self.assertIn(f"linkplane send {SEND_EXAMPLE_PATH}", send_lines[0])
        self.assertIn(f"> {SEND_EXAMPLE_PATH} &&", send_lines[0], "the file is created before it is sent")

    def test_send_suggestion_parses_as_a_valid_send_command(self):
        import shlex
        from linkplane.cli import build_parser
        from linkplane.setup import NEXT_STEPS

        send_line = next(line for line in NEXT_STEPS if "linkplane send" in line)
        argv = shlex.split(send_line.split("&&", 1)[1].strip())[1:]  # drop the leading "linkplane"
        arguments = build_parser().parse_args(argv)
        self.assertEqual(arguments.command, "send")
        self.assertEqual(arguments.paths, ["/tmp/linkplane-hello.txt"])

    def test_successful_setup_returns_the_suggestions(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Harness(directory).run()
        from linkplane.setup import NEXT_STEPS

        self.assertTrue(result.ok, result.failure)
        self.assertEqual(result.next_steps, NEXT_STEPS)
