"""`linkplane uninstall` and `--purge`: service integration goes, user data stays, and no
path outside the fixed Linkplane-owned roots can ever be touched. No systemctl, no daemon,
no real HOME: every root is a temporary directory selected through the XDG variables the
product itself resolves."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linkplane import service
from linkplane.cli import main
from linkplane.core import errors
from linkplane.operations import CancellationToken, OperationCancelled
from linkplane.uninstall import (
    UninstallOptions,
    UninstallSeams,
    owned_roots,
    refuse_reason,
    run_uninstall,
)


class Sandbox:
    """A fake home with Linkplane's three roots, a unit dir, and a scripted daemon."""

    def __init__(self, directory):
        self.home = Path(directory) / "home"
        self.config = self.home / ".config" / "linkplane"
        self.state = self.home / ".local" / "state" / "linkplane"
        self.runtime = Path(directory) / "run" / "linkplane"
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        self.outside = Path(directory) / "outside"
        for path in (self.config, self.state, self.runtime, self.unit_dir, self.outside):
            path.mkdir(parents=True, exist_ok=True)
        self.env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_RUNTIME_DIR": str(Path(directory) / "run"),
        }
        self.daemon_running = False
        self.daemon_pid = 4242
        self.managed_pid = None
        self.uninstall_calls = []
        self.uninstall_error = None
        self.confirm_answer = False
        self.confirm_questions = []
        self.sleeps = []
        self.now = 0.0
        self.on_sleep = None

    def populate(self):
        (self.config / "config.json").write_text(json.dumps({"default_device": "phone", "devices": {"phone": {"device_id": "HW1"}}}), encoding="utf-8")
        (self.config / "clients.json").write_text('{"schema": "linkplane.clients/1", "clients": []}', encoding="utf-8")
        (self.config / "automations.json").write_text("[]", encoding="utf-8")
        (self.state / "events.jsonl").write_text('{"type": "x"}\n', encoding="utf-8")
        (self.state / "audit.jsonl").write_text('{"kind": "x"}\n', encoding="utf-8")
        (self.state / "jobs").mkdir(exist_ok=True)
        (self.state / "jobs" / "j1.json").write_text("{}", encoding="utf-8")
        (self.runtime / "daemon.sock").write_text("", encoding="utf-8")
        (self.runtime / "api.json").write_text("{}", encoding="utf-8")

    def install_unit(self):
        (self.unit_dir / service.UNIT_NAME).write_text(service.unit_text(["/x/linkplane", "daemon", "run"]), encoding="utf-8")

    # seams
    def daemon_status(self, socket_path):
        return {"ok": True, "pid": self.daemon_pid, "socket": str(self.runtime / "daemon.sock"), "version": "0.4.0"} if self.daemon_running else None

    def unit_main_pid(self):
        return self.managed_pid

    def service_uninstall(self, *, unit_dir=None, dry_run=False):
        self.uninstall_calls.append(unit_dir)
        if self.uninstall_error is not None:
            raise self.uninstall_error
        unit = Path(unit_dir) / service.UNIT_NAME
        if unit.exists():
            unit.unlink()
        if self.managed_pid == self.daemon_pid:
            self.daemon_running = False  # `disable --now`
        return service.ServiceResult("uninstall", str(unit), "", (), dry_run)

    def confirm(self, question):
        self.confirm_questions.append(question)
        return self.confirm_answer

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))

    def run(self, cancel=None, **overrides):
        options = UninstallOptions(unit_dir=str(self.unit_dir), socket_path=str(self.runtime / "daemon.sock"), **overrides)
        seams = UninstallSeams(finder=lambda name: f"/usr/bin/{name}", daemon_status=self.daemon_status, unit_main_pid=self.unit_main_pid,
                               service_uninstall=self.service_uninstall, confirm=self.confirm, clock=self.clock, sleep=self.sleep)
        with patch.dict(os.environ, self.env, clear=False):
            return run_uninstall(options, seams, cancel=cancel)

    def by_name(self, result):
        return {step.name: step for step in result.steps}


class UninstallTests(unittest.TestCase):
    def test_normal_uninstall_removes_service_and_runtime_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.install_unit()
            box.daemon_running = True
            box.managed_pid = box.daemon_pid
            result = box.run()
            unit_exists = (box.unit_dir / service.UNIT_NAME).exists()
            runtime_left = sorted(p.name for p in box.runtime.iterdir()) if box.runtime.exists() else []
            config_left = sorted(p.name for p in box.config.iterdir())
            state_left = sorted(p.name for p in box.state.iterdir())

        self.assertTrue(result.ok, result.steps)
        self.assertEqual((result.daemon_was_running, result.daemon_stopped, result.unit_removed), (True, True, True))
        self.assertIsNone(result.unmanaged_daemon)
        self.assertFalse(unit_exists)
        self.assertEqual(runtime_left, [])
        self.assertEqual(config_left, ["automations.json", "clients.json", "config.json"])
        self.assertEqual(state_left, ["audit.jsonl", "events.jsonl", "jobs"])
        self.assertTrue(result.configuration_retained)
        self.assertFalse(result.purge_performed)
        self.assertEqual(sorted(result.retained), sorted([str(box.config), str(box.state)]))
        self.assertIn("pipx uninstall linkplane", result.software_hint)
        self.assertEqual(box.uninstall_calls, [str(box.unit_dir)])

    def test_repeat_uninstall_is_a_quiet_success(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.install_unit()
            box.daemon_running = True
            box.managed_pid = box.daemon_pid
            first = box.run()
            second = box.run()

        self.assertTrue(first.ok and second.ok)
        self.assertEqual((second.daemon_was_running, second.daemon_stopped, second.unit_removed), (False, None, False))
        names = box.by_name(second)
        self.assertIn("not installed", names["Service unit"].summary)
        self.assertEqual(names["Daemon"].summary, "not running")
        self.assertIn("nothing to remove", names["Runtime files"].summary)
        self.assertEqual(len(box.uninstall_calls), 1)

    def test_empty_runtime_directory_left_by_a_stopped_daemon_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)  # runtime dir exists but the daemon already removed its files
            result = box.run()
            runtime_exists = box.runtime.exists()

        self.assertTrue(result.ok)
        self.assertFalse(runtime_exists)
        self.assertEqual(result.runtime_removed, (str(box.runtime),))
        self.assertIn("removed the empty", box.by_name(result)["Runtime files"].summary)

    def test_daemon_already_stopped_unit_missing_runtime_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            for root in (box.config, box.state, box.runtime):
                root.rmdir()  # a machine that never had Linkplane
            result = box.run()

        self.assertTrue(result.ok)
        self.assertEqual(result.removed, ())
        self.assertEqual(box.uninstall_calls, [])
        self.assertEqual(box.by_name(result)["Configuration and state"].summary, "kept: nothing present")

    def test_unmanaged_daemon_is_reported_and_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.daemon_running = True  # no unit, so nobody manages it
            result = box.run()
            runtime_left = sorted(p.name for p in box.runtime.iterdir())

        self.assertTrue(result.ok)
        self.assertEqual(result.unmanaged_daemon["pid"], box.daemon_pid)
        self.assertEqual(runtime_left, ["api.json", "daemon.sock"], "a live daemon's socket is never removed")
        self.assertIn("linkplane daemon stop", box.by_name(result)["Daemon"].fix)
        self.assertTrue(box.daemon_running)

    def test_unit_present_but_daemon_pid_differs_is_unmanaged(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.install_unit()
            box.daemon_running = True
            box.managed_pid = 999  # the unit's process is not the one answering the socket
            result = box.run()

        self.assertTrue(result.ok)
        self.assertEqual(result.unmanaged_daemon["pid"], box.daemon_pid)
        self.assertTrue(result.unit_removed)
        self.assertTrue(box.daemon_running, "never killed")

    def test_daemon_that_will_not_stop_is_a_warning_not_a_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.install_unit()
            box.daemon_running = True
            box.managed_pid = box.daemon_pid
            original = box.service_uninstall

            def uninstall_without_stopping(**kwargs):
                result = original(**kwargs)
                box.daemon_running = True
                return result

            box.service_uninstall = uninstall_without_stopping
            result = box.run(stop_wait=1.0)

        self.assertTrue(result.ok)
        self.assertFalse(result.daemon_stopped)
        self.assertIn("still answering", box.by_name(result)["Daemon"].summary)
        self.assertGreater(len(box.sleeps), 0)

    def test_service_failure_is_coded_and_stops_nothing_else_from_being_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.install_unit()
            box.uninstall_error = errors.LinkplaneError(errors.PROVIDER_FAILED, "systemctl --user failed: Failed to connect to bus")
            result = box.run()
            unit_exists = (box.unit_dir / service.UNIT_NAME).exists()

        self.assertFalse(result.ok)
        self.assertEqual(box.by_name(result)["Service unit"].code, errors.PROVIDER_FAILED)
        self.assertTrue(unit_exists)
        self.assertTrue(result.configuration_retained)

    def test_ctrl_c_while_waiting_for_the_daemon_to_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.install_unit()
            box.daemon_running = True
            box.managed_pid = box.daemon_pid
            original = box.service_uninstall

            def uninstall_without_stopping(**kwargs):
                result = original(**kwargs)
                box.daemon_running = True
                return result

            box.service_uninstall = uninstall_without_stopping
            cancel = CancellationToken()
            box.on_sleep = lambda count: cancel.cancel("interrupt") if count == 2 else None
            with self.assertRaises(OperationCancelled):
                box.run(cancel=cancel)
            config_left = sorted(p.name for p in box.config.iterdir())

        self.assertEqual(config_left, ["automations.json", "clients.json", "config.json"])

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.install_unit()
            result = box.run(dry_run=True, purge=True)
            unit_exists = (box.unit_dir / service.UNIT_NAME).exists()
            config_exists = box.config.exists()

        self.assertTrue(result.ok)
        self.assertTrue(unit_exists and config_exists)
        self.assertEqual(box.uninstall_calls, [])
        self.assertIn("would remove", box.by_name(result)["Purge"].summary)
        self.assertFalse(result.purge_performed)


class PurgeTests(unittest.TestCase):
    def test_purge_with_confirmation_removes_only_the_owned_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.confirm_answer = True
            (box.outside / "keep.txt").write_text("mine", encoding="utf-8")
            result = box.run(purge=True)
            left = {"config": box.config.exists(), "state": box.state.exists(), "runtime": box.runtime.exists(),
                    "outside": (box.outside / "keep.txt").exists(), "home": box.home.exists(), "unit_dir": box.unit_dir.exists()}

        self.assertTrue(result.ok, result.steps)
        self.assertTrue(result.purge_performed)
        self.assertFalse(result.configuration_retained)
        self.assertEqual(left, {"config": False, "state": False, "runtime": False, "outside": True, "home": True, "unit_dir": True})
        roots = {str(box.config), str(box.state), str(box.runtime)}
        # The runtime directory is emptied and dropped by the runtime step before purge runs.
        self.assertTrue({str(box.config), str(box.state)} <= set(result.removed), result.removed)
        for path in result.removed:  # runtime files first, then the three roots; nothing else
            self.assertTrue(any(path == root or path.startswith(root + "/") for root in roots), path)
        self.assertEqual(len(box.confirm_questions), 1)
        self.assertIn(str(box.config), box.confirm_questions[0])
        self.assertIn("cannot be undone", box.confirm_questions[0])

    def test_purge_declined_removes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.confirm_answer = False
            result = box.run(purge=True)
            config_exists = (box.config / "config.json").exists()

        self.assertTrue(result.ok)
        self.assertFalse(result.purge_performed)
        self.assertTrue(config_exists)
        self.assertIn("declined", box.by_name(result)["Purge"].summary)

    def test_repeat_purge_is_a_quiet_success(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.confirm_answer = True
            first = box.run(purge=True)
            second = box.run(purge=True)

        self.assertTrue(first.purge_performed)
        self.assertTrue(second.ok)
        self.assertFalse(second.purge_performed)
        self.assertEqual(box.by_name(second)["Purge"].summary, "nothing to remove")
        self.assertEqual(len(box.confirm_questions), 1)

    def test_non_interactive_purge_needs_force(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            refused = box.run(purge=True, interactive=False)
            still_there = box.config.exists()
            forced = box.run(purge=True, interactive=False, force=True)
            gone = not box.config.exists()

        self.assertFalse(refused.ok)
        self.assertEqual(box.by_name(refused)["Purge"].code, errors.REQUEST_INVALID)
        self.assertTrue(still_there)
        self.assertTrue(forced.ok and forced.purge_performed and gone)
        self.assertEqual(box.confirm_questions, [])

    def test_purge_refuses_while_a_daemon_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.daemon_running = True
            box.confirm_answer = True
            result = box.run(purge=True)
            config_exists = box.config.exists()

        self.assertFalse(result.ok)
        self.assertEqual(box.by_name(result)["Purge"].code, errors.STATE_CONFLICT)
        self.assertTrue(config_exists)

    def test_backup_and_transfer_paths_in_config_are_never_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            other_user = Path(directory) / "home-other"
            other_user.mkdir()
            (other_user / "secret.txt").write_text("theirs", encoding="utf-8")
            photos = box.home / "Pictures" / "PhoneBackup"
            photos.mkdir(parents=True)
            (photos / "IMG_0001.jpg").write_bytes(b"jpeg")
            (photos / ".linkplane-manifest.json").write_text("{}", encoding="utf-8")
            hostile = {
                "default_device": "phone",
                "devices": {"phone": {"device_id": "HW1"}},
                "backup": {"destination": str(box.home)},
                "backups": [str(box.home), "/", str(other_user), str(photos)],
                "send": {"destination": str(other_user)},
            }
            (box.config / "config.json").write_text(json.dumps(hostile), encoding="utf-8")
            with patch.dict(os.environ, box.env):
                roots = owned_roots()
            box.confirm_answer = True
            result = box.run(purge=True)
            survivors = {
                "home": box.home.exists(),
                "other_user": (other_user / "secret.txt").exists(),
                "photo": (photos / "IMG_0001.jpg").exists(),
                "manifest": (photos / ".linkplane-manifest.json").exists(),
                "root": Path("/").exists(),
            }

        self.assertTrue(result.purge_performed)
        self.assertEqual(set(map(str, roots)), {str(box.config), str(box.state), str(box.runtime)})
        self.assertEqual(survivors, {"home": True, "other_user": True, "photo": True, "manifest": True, "root": True})
        for removed in result.removed:
            self.assertIn("/linkplane", removed)
            self.assertFalse(removed.startswith(str(other_user)) or removed.startswith(str(photos)), removed)

    def test_symlink_inside_a_root_is_unlinked_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            treasure = box.outside / "treasure"
            treasure.mkdir()
            (treasure / "gold.txt").write_text("gold", encoding="utf-8")
            (box.state / "escape").symlink_to(treasure, target_is_directory=True)
            (box.config / "escape-file").symlink_to(treasure / "gold.txt")
            box.confirm_answer = True
            result = box.run(purge=True)
            gold = (treasure / "gold.txt").exists()
            state_gone = not box.state.exists()

        self.assertTrue(result.purge_performed)
        self.assertTrue(gold, "purge followed a symlink out of its root")
        self.assertTrue(state_gone)

    def test_a_root_that_is_itself_a_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            treasure = box.outside / "treasure"
            treasure.mkdir()
            (treasure / "gold.txt").write_text("gold", encoding="utf-8")
            import shutil
            shutil.rmtree(box.state)
            box.state.symlink_to(treasure, target_is_directory=True)
            box.confirm_answer = True
            result = box.run(purge=True)
            gold = (treasure / "gold.txt").exists()
            link_still_there = box.state.is_symlink()

        self.assertTrue(gold)
        self.assertTrue(link_still_there)
        refused = [t for t in result.purge_targets if t.refused]
        self.assertEqual([t.path for t in refused], [str(box.state)])
        self.assertIn("symbolic link", refused[0].refused)
        self.assertNotIn(str(box.state), result.removed)

    def test_refuse_reason_bounds(self):
        home = Path(os.path.expanduser("~"))
        self.assertIsNotNone(refuse_reason(Path("/")))
        self.assertIsNotNone(refuse_reason(home))
        self.assertIsNotNone(refuse_reason(home.parent))
        self.assertIsNotNone(refuse_reason(home / "Pictures"))
        self.assertIsNotNone(refuse_reason(Path("/linkplane")))
        self.assertIsNone(refuse_reason(home / ".config" / "linkplane"))
        self.assertIsNone(refuse_reason(home / ".config" / "phonebridge"))

    def test_permission_failure_is_coded(self):
        if os.geteuid() == 0:
            self.skipTest("root can delete anything")
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            box.confirm_answer = True
            box.config.parent.chmod(0o500)
            try:
                result = box.run(purge=True)
            finally:
                box.config.parent.chmod(0o700)

        self.assertFalse(result.ok)
        purge_steps = [s for s in result.steps if s.name == "Purge"]
        self.assertTrue(any(s.code == errors.CONFIG_UNWRITABLE for s in purge_steps), purge_steps)

    def test_json_result_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            result = box.run()
            payload = result.to_dict()

        self.assertEqual(set(payload), {"ok", "dry_run", "steps", "daemon", "unit", "runtime_removed", "configuration_retained", "purge", "removed", "retained", "warnings", "software_hint"})
        self.assertEqual(payload["daemon"], {"was_running": False, "stopped": None, "unmanaged": None})
        self.assertFalse(payload["purge"]["performed"])
        self.assertTrue(all("token" not in json.dumps(step) for step in payload["steps"]))


class UninstallCliTests(unittest.TestCase):
    def test_dry_run_json_through_the_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Sandbox(directory)
            box.populate()
            with patch.dict(os.environ, box.env), patch("linkplane.uninstall.daemond.request", side_effect=errors.LinkplaneError(errors.DAEMON_NOT_RUNNING, "not running")):
                output = StringIO()
                with redirect_stdout(output):
                    code = main(["uninstall", "--purge", "--dry-run", "--json", "--unit-dir", str(box.unit_dir)])
            config_exists = box.config.exists()

        envelope = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(envelope["ok"])
        self.assertTrue(envelope["data"]["dry_run"])
        self.assertTrue(config_exists)
        self.assertEqual([t["path"] for t in envelope["data"]["purge"]["targets"]][:2], [str(box.config), str(box.state)])

    def test_help_has_purge_and_force_but_no_yes(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            main(["uninstall", "--help"])
        text = output.getvalue()
        self.assertIn("--purge", text)
        self.assertIn("--force", text)
        self.assertNotIn("--yes", text)
