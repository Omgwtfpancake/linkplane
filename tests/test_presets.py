"""The automatic photo backup preset (v0.6): the rule it writes, enabling and disabling it,
and the whole path through a real daemon -- connect event, backup job, conditional
notification, audit -- with the phone, adb, and notify-send faked."""

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from linkplane import actions, daemon as daemond, presets
from linkplane.automations import load_rules, read_audit
from linkplane.backup import MANIFEST_NAME, BackupResult
from linkplane.cli import main
from linkplane.core import errors
from linkplane.core.automation import parse_automations
from linkplane.core.events import Event
from linkplane.core.state import CONNECTED, DeviceState
from linkplane.jobs import JobRunner
from linkplane.operations import CancellationToken, OperationResult


def backup_value(downloaded, destination="/b", **extra):
    return BackupResult("adb", "Phone", "S1", "/sdcard/DCIM/Camera", destination, 3, downloaded, downloaded * 10,
                        downloaded, 3 - downloaded, tuple(f"f{i}" for i in range(downloaded)), False,
                        downloaded_bytes=downloaded * 10, **extra)


class PresetRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.rules = str(self.base / "automations.json")
        self.photos = self.base / "Photos"

    def tearDown(self):
        self.directory.cleanup()

    def file(self):
        return json.loads(Path(self.rules).read_text(encoding="utf-8"))

    def test_the_preset_exists_and_its_rule_is_an_ordinary_valid_rule(self):
        self.assertIn(presets.PHOTO_BACKUP, presets.PRESETS)
        rule = presets.photo_backup_rule("phone", "/home/u/Pictures/Linkplane")
        parsed = parse_automations({"schema_version": 1, "automations": [rule]})[0]
        self.assertEqual((parsed.name, parsed.preset, parsed.when, parsed.device), ("photo-backup:phone", "photo-backup", "device.connected", "phone"))
        self.assertTrue(parsed.on_initial)
        self.assertEqual([step.action for step in parsed.do], ["backup", "notify-desktop"])
        self.assertEqual(parsed.do[1].conditions, {"downloaded": {"above": 0}})
        self.assertEqual(parsed.allow, (), "no privileged action: the preset never runs a shell command")
        self.assertNotIn("run", [step.action for step in parsed.do])
        self.assertEqual(parsed.blocked_actions, ())
        self.assertEqual(parsed.to_dict()["do"], rule["do"])

    def test_enable_creates_then_is_idempotent(self):
        first = presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules)
        text = Path(self.rules).read_text(encoding="utf-8")
        second = presets.enable_photo_backup("phone", None, device_id="S1", path=self.rules)
        self.assertEqual((first.change, second.change), ("created", "unchanged"))
        self.assertEqual(text, Path(self.rules).read_text(encoding="utf-8"))
        self.assertEqual(len(self.file()["automations"]), 1)
        self.assertEqual(oct(os.stat(self.rules).st_mode & 0o777), "0o600")
        self.assertFalse(self.photos.exists(), "enabling never creates or touches the backup folder")

    def test_disable_keeps_the_rule_and_every_backed_up_file_and_enable_restores(self):
        presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules)
        self.photos.mkdir()
        (self.photos / "IMG_1.jpg").write_bytes(b"kept")
        off = presets.disable_photo_backup("phone", path=self.rules)
        again = presets.disable_photo_backup("phone", path=self.rules)
        self.assertEqual((off.change, again.change), ("disabled", "unchanged"))
        self.assertEqual((self.photos / "IMG_1.jpg").read_bytes(), b"kept")
        stored = self.file()["automations"][0]
        self.assertFalse(stored["enabled"])
        self.assertEqual(presets.rule_destination(stored), str(self.photos.resolve()))
        loaded = load_rules(self.rules)
        self.assertEqual([rule.name for rule in loaded.active], ["photo-backup:phone"], "still inspectable as an ordinary rule")
        self.assertFalse(loaded.active[0].enabled)
        on = presets.enable_photo_backup("phone", None, device_id="S1", path=self.rules)
        self.assertEqual(on.change, "enabled")
        self.assertTrue(self.file()["automations"][0]["enabled"])

    def test_disable_without_a_rule_is_a_clear_error(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            presets.disable_photo_backup("phone", path=self.rules)
        self.assertIn("never enabled", str(raised.exception))
        self.assertFalse(Path(self.rules).exists())

    def test_hand_written_rules_and_unknown_keys_are_preserved(self):
        Path(self.rules).write_text(json.dumps({"schema_version": 1, "note": "mine", "automations": [
            {"name": "low", "when": "battery.low", "do": [{"action": "notify-desktop", "message": "Low"}]},
        ]}), encoding="utf-8")
        presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules)
        stored = self.file()
        self.assertEqual(stored["note"], "mine")
        self.assertEqual([rule["name"] for rule in stored["automations"]], ["low", "photo-backup:phone"])
        self.assertEqual(stored["automations"][0], {"name": "low", "when": "battery.low", "do": [{"action": "notify-desktop", "message": "Low"}]})

    def test_an_invalid_rules_file_is_never_overwritten(self):
        Path(self.rules).write_text("{broken", encoding="utf-8")
        with self.assertRaises(errors.LinkplaneError) as raised:
            presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules)
        self.assertEqual(raised.exception.code, errors.CONFIG_INVALID)
        self.assertEqual(Path(self.rules).read_text(encoding="utf-8"), "{broken")

    def test_a_hand_written_rule_with_the_preset_name_is_not_taken_over(self):
        Path(self.rules).write_text(json.dumps({"automations": [
            {"name": "photo-backup:phone", "when": "battery.low", "do": [{"action": "notify-desktop"}]},
        ]}), encoding="utf-8")
        before = Path(self.rules).read_text(encoding="utf-8")
        with self.assertRaises(errors.LinkplaneError):
            presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules)
        self.assertEqual(before, Path(self.rules).read_text(encoding="utf-8"))

    def test_unsafe_destinations_are_refused(self):
        home = self.base / "home"
        home.mkdir()
        for bad, fragment in [
            ("", "choose a folder"), ("Pictures", "absolute"), ("/", "cannot be the backup folder itself"),
            (str(home), "cannot be the backup folder itself"), ("/etc/photos", "system location"),
            ("/usr", "system location"), ("/proc/self", "system location"),
        ]:
            with self.subTest(bad=bad), self.assertRaises(errors.LinkplaneError) as raised:
                presets.validate_destination(bad, home=str(home))
            self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)
            self.assertIn(fragment, str(raised.exception))
        a_file = self.base / "file.txt"
        a_file.write_text("x")
        with self.assertRaisesRegex(errors.LinkplaneError, "not a folder"):
            presets.validate_destination(str(a_file), home=str(home))
        self.assertEqual(presets.validate_destination(str(home / "Pictures" / "Linkplane"), home=str(home)),
                         (home / "Pictures" / "Linkplane").resolve())

    def test_a_folder_holding_another_phones_backup_is_refused(self):
        self.photos.mkdir()
        (self.photos / MANIFEST_NAME).write_text(json.dumps({"schema_version": 1, "device_serial": "OTHER", "source": "/sdcard/DCIM/Camera", "files": {}}))
        with self.assertRaisesRegex(errors.LinkplaneError, "another phone"):
            presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules)
        (self.photos / MANIFEST_NAME).write_text(json.dumps({"schema_version": 1, "device_serial": "S1", "source": "/sdcard/DCIM/Camera", "files": {}}))
        self.assertEqual(presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules).change, "created",
                         "an earlier manual backup of the same phone is continued, not refused")

    def test_two_phones_cannot_share_one_folder(self):
        presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules)
        with self.assertRaisesRegex(errors.LinkplaneError, "each phone needs its own folder"):
            presets.enable_photo_backup("tablet", str(self.photos), device_id="T1", path=self.rules)
        self.assertEqual(presets.enable_photo_backup("tablet", str(self.base / "Tablet"), device_id="T1", path=self.rules).change, "created")

    def test_dry_run_writes_nothing(self):
        change = presets.enable_photo_backup("phone", str(self.photos), device_id="S1", path=self.rules, dry_run=True)
        self.assertEqual(change.change, "created")
        self.assertFalse(Path(self.rules).exists())


class LastRunTests(unittest.TestCase):
    def test_last_run_is_derived_from_job_records_newest_finished_first(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = JobRunner(directory, retry_delays=())
            jobs.run(automation="photo-backup:phone", action="backup", device="phone",
                     call=lambda cancel, progress: OperationResult.success(backup_value(2)))
            jobs.run(automation="other", action="backup", device="phone",
                     call=lambda cancel, progress: OperationResult.success(backup_value(1)))
            time.sleep(1.1)  # job ids sort by their second-resolution timestamp
            jobs.run(automation="photo-backup:phone", action="backup", device="phone",
                     call=lambda cancel, progress: OperationResult.failure("operation_failed", "not enough free space", error_code=errors.STORAGE_INSUFFICIENT))
            latest = presets.last_run("photo-backup:phone", jobs_dir=directory)
            self.assertEqual(latest.state, "failed")
            self.assertIn("failed: not enough free space", presets.describe_run(latest))
            self.assertEqual(latest.error["error_code"], errors.STORAGE_INSUFFICIENT)
            for name in os.listdir(directory):
                if "failed" in Path(directory, name).read_text() or "not enough" in Path(directory, name).read_text():
                    os.unlink(Path(directory, name))
            previous = presets.last_run("photo-backup:phone", jobs_dir=directory)
        self.assertEqual(previous.state, "completed")
        self.assertIn("2 new file(s) copied (20 B)", presets.describe_run(previous))
        self.assertEqual(presets.describe_run(None), "never ran")

    def test_no_jobs_directory_means_never_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(presets.last_run("photo-backup:phone", jobs_dir=str(Path(directory) / "none")))


class InitialObserver:
    """A phone that is already plugged in when the daemon starts (an initial observation),
    then unplugged and plugged in again (a change)."""

    instances: list = []

    def __init__(self, sink, *, cancel: CancellationToken, **kwargs):
        self.sink = sink
        self.cancel = cancel
        self.states = {"phone": DeviceState("phone", "adb", CONNECTED, "S1", {"level": 80})}
        self.reconnect = threading.Event()
        InitialObserver.instances.append(self)

    def run(self):
        self.sink(Event("observer.started", "*", data={"sources": ["fake"], "interval": 1.0}))
        self.sink(Event("device.connected", "phone", provider="adb", data={"address": "S1", "transport": "adb", "initial": True}))
        while not self.reconnect.is_set() and not self.cancel.cancelled:
            time.sleep(0.01)
        if self.reconnect.is_set():
            self.sink(Event("device.disconnected", "phone", provider="adb", data={"address": "S1"}))
            self.sink(Event("device.connected", "phone", provider="adb", data={"address": "S1", "transport": "adb"}))
        self.cancel.wait(5)
        self.sink(Event("observer.stopped", "*"))


class AutomaticBackupDaemonTests(unittest.TestCase):
    """The preset through a real daemon: only the phone (backup service) and notify-send are fake."""

    def setUp(self):
        InitialObserver.instances.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.rules = str(self.base / "automations.json")
        self.audit = str(self.base / "audit.jsonl")
        self.jobs_dir = str(self.base / "jobs")
        self.photos = self.base / "Photos"
        self.backups = []      # BackupRequests the service received
        self.copies = [3, 0]   # what each backup run copies, in order
        self.notifications = []
        self.service_error = None

    def tearDown(self):
        if hasattr(self, "daemon"):
            self.daemon.stop()
            self.thread.join(5)
        self.directory.cleanup()

    def fake_backup(self, request, *, progress=None, cancel=None):
        self.backups.append(request)
        if self.service_error is not None:
            return self.service_error
        copied = self.copies.pop(0) if self.copies else 0
        return OperationResult.success(backup_value(copied, request.destination))

    def fake_notify_send(self, command, **_kwargs):
        self.notifications.append(command)
        return ""

    def start(self, rules):
        Path(self.rules).write_text(json.dumps({"schema_version": 1, "automations": rules}), encoding="utf-8")
        jobs = JobRunner(self.jobs_dir, retry_delays=())

        def step_runner(step, event, context, rule):
            if step.action == "backup":
                return actions.backup_job(step, event, context, rule, jobs, service=self.fake_backup)
            if step.action == "notify-desktop":
                with patch("linkplane.actions.shutil.which", return_value="/usr/bin/notify-send"):
                    return actions.notify_desktop(step, context, runner=self.fake_notify_send)
            raise AssertionError(f"unexpected action {step.action}")

        self.daemon = daemond.Daemon(socket_path=str(self.base / "d.sock"), state_path=str(self.base / "state.json"),
                                     write_history=False, observer_factory=InitialObserver, automations_path=self.rules,
                                     audit_path=self.audit, step_runner=step_runner, jobs=jobs)
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def fired(self):
        return [record for record in read_audit(self.audit) if record.get("kind") == "rule.fired"]

    def test_already_connected_phone_is_backed_up_notified_and_audited_then_a_reconnect_with_nothing_new_is_silent(self):
        rule = presets.photo_backup_rule("phone", str(self.photos))
        unrelated = {"name": "hello", "when": "device.connected", "do": [{"action": "notify-desktop", "message": "hi"}]}
        self.start([rule, unrelated])

        self.assertTrue(self.wait_for(lambda: len(self.fired()) == 1), "the initial connection fires the preset")
        self.assertEqual(len(self.backups), 1)
        self.assertEqual((self.backups[0].destination, self.backups[0].source, self.backups[0].serial),
                         (str(self.photos), "/sdcard/DCIM/Camera", "S1"))
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.notifications[0][-2:], ["Linkplane photo backup", f"3 new photo(s) or video(s) backed up to {self.photos}"])
        self.assertEqual(self.fired()[0]["rule"], "photo-backup:phone", "the unrelated rule ignores the initial observation")

        InitialObserver.instances[-1].reconnect.set()
        self.assertTrue(self.wait_for(lambda: len(self.fired()) == 3), "a real reconnect fires both rules")
        self.assertEqual(len(self.backups), 2)
        preset_firings = [r for r in self.fired() if r["rule"] == "photo-backup:phone"]
        silent = preset_firings[-1]["details"]["outcomes"]
        self.assertEqual([(o["action"], o["ok"], o["skipped"]) for o in silent],
                         [("backup", True, False), ("notify-desktop", True, True)])
        self.assertEqual(len(self.notifications), 2, "only the unrelated rule notified; nothing new means no backup notification")
        self.assertEqual(self.notifications[1][-1], "hi")

        audit = list(read_audit(self.audit))
        job_entries = [r for r in audit if r.get("kind", "").startswith("job.") and r.get("rule") == "photo-backup:phone"]
        self.assertEqual([r["kind"] for r in job_entries], ["job.started", "job.completed", "job.started", "job.completed"])
        self.assertEqual(job_entries[1]["details"]["result"]["downloaded"], 3)
        self.assertEqual(job_entries[1]["correlation_id"], preset_firings[0]["correlation_id"])
        last = presets.last_run("photo-backup:phone", jobs_dir=self.jobs_dir)
        self.assertEqual((last.state, last.result["downloaded"]), ("completed", 0))

    def test_a_disabled_preset_never_backs_up(self):
        self.start([presets.photo_backup_rule("phone", str(self.photos), enabled=False)])
        self.assertTrue(self.wait_for(lambda: any(r.get("kind") == "rules.loaded" for r in read_audit(self.audit))))
        InitialObserver.instances[-1].reconnect.set()
        time.sleep(0.3)
        self.assertEqual(self.backups, [])
        self.assertEqual(self.notifications, [])

    def test_a_full_disk_fails_visibly_in_jobs_and_audit_without_a_success_notification(self):
        self.service_error = OperationResult.failure("operation_failed", "not enough free space in /b: this backup needs 3 GB",
                                                     error_code=errors.STORAGE_INSUFFICIENT)
        self.start([presets.photo_backup_rule("phone", str(self.photos))])
        self.assertTrue(self.wait_for(lambda: len(self.fired()) == 1))
        firing = self.fired()[0]
        self.assertEqual(firing["decision"], "failed")
        self.assertEqual([o["action"] for o in firing["details"]["outcomes"]], ["backup"], "stops at the failed backup")
        self.assertEqual(self.notifications, [])
        last = presets.last_run("photo-backup:phone", jobs_dir=self.jobs_dir)
        self.assertEqual((last.state, last.error["error_code"]), ("failed", errors.STORAGE_INSUFFICIENT))
        self.assertIn("failed: not enough free space", presets.describe_run(last))


class PresetCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"default_device": "phone", "devices": {"phone": {"device_id": "S1", "adb": {"serials": ["S1"]}}}}))
        self.rules = str(self.base / "automations.json")
        self.common = ["--file", self.rules, "--config", str(self.config), "--socket", str(self.base / "none.sock")]

    def tearDown(self):
        self.directory.cleanup()

    def cli(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(argv))
        return code, output.getvalue()

    def test_enable_list_disable_through_the_cli(self):
        photos = str(self.base / "Photos")
        code, text = self.cli("automations", "enable", "photo-backup", "--destination", photos, *self.common)
        self.assertEqual(code, 0)
        self.assertIn("Automatic photo backup is now on for phone.", text)
        self.assertIn("deleting photos from the phone never deletes these backups", text)
        self.assertIn("The daemon is not running", text)
        code, text = self.cli("automations", "presets", "--file", self.rules, "--config", str(self.config), "--jobs-dir", str(self.base / "jobs"))
        self.assertIn("State       enabled", text)
        self.assertIn("Last run    never ran", text)
        code, text = self.cli("automations", "list", "--file", self.rules)
        self.assertIn("photo-backup:phone  [enabled]  (preset photo-backup)", text)
        self.assertIn('notify-desktop (if {"downloaded": {"above": 0}})', text)
        code, text = self.cli("automations", "disable", "photo-backup", *self.common)
        self.assertIn("is now off", text)
        self.assertIn("Photos already backed up stay where they are.", text)
        code, text = self.cli("automations", "presets", "--json", "--file", self.rules, "--config", str(self.config), "--jobs-dir", str(self.base / "jobs"))
        rows = json.loads(text)["data"]["presets"][0]["devices"]
        self.assertEqual((rows[0]["device"], rows[0]["state"]), ("phone", "disabled"))

    def test_enable_asks_a_running_daemon_to_reload(self):
        with patch("linkplane.daemon.request", return_value={"ok": True}) as request:
            code, text = self.cli("automations", "enable", "photo-backup", "--destination", str(self.base / "P"), *self.common)
        request.assert_called_once_with("reload", str(self.base / "none.sock"))
        self.assertIn("The running daemon reloaded its rules.", text)
        with patch("linkplane.daemon.request") as request:
            self.cli("automations", "enable", "photo-backup", *self.common)
        request.assert_not_called()  # nothing changed, nothing to reload

    def test_status_shows_the_automatic_backup_state_and_last_run(self):
        from argparse import Namespace
        from linkplane.cli import print_backup_status

        arguments = Namespace(device_profile=None, config=str(self.config))
        jobs_dir = str(self.base / "jobs")

        def render():
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                print_backup_status(arguments, jobs_dir=jobs_dir, rules_path=self.rules)
            return output.getvalue()

        self.assertEqual(render(), "", "nothing when the preset was never set up")
        presets.enable_photo_backup("phone", str(self.base / "Photos"), device_id="S1", path=self.rules)
        self.assertEqual(render(), f"Backup      automatic photo backup on, to {(self.base / 'Photos').resolve()}\nLast backup never ran\n")
        JobRunner(jobs_dir, retry_delays=()).run(automation="photo-backup:phone", action="backup", device="phone",
                                                 call=lambda cancel, progress: OperationResult.success(backup_value(4)))
        self.assertIn("4 new file(s) copied (40 B)", render())
        presets.disable_photo_backup("phone", path=self.rules)
        self.assertEqual(render(), "Backup      automatic photo backup off\n")
        Path(self.rules).write_text("{broken")
        self.assertEqual(render(), "", "a broken rules file never breaks status")

    def test_unknown_preset_and_unknown_device_are_coded(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, text = self.cli("automations", "enable", "dance", *self.common)
        self.assertNotEqual(code, 0)
        self.assertFalse(Path(self.rules).exists())
        with contextlib.redirect_stderr(io.StringIO()):
            code, text = self.cli("automations", "enable", "photo-backup", "--device", "nope", *self.common)
        self.assertNotEqual(code, 0)
        self.assertFalse(Path(self.rules).exists())


if __name__ == "__main__":
    unittest.main()
