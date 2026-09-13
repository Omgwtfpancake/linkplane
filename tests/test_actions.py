import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from linkplane import actions
from linkplane.core.automation import Step, parse_automation
from linkplane.core.events import Event
from linkplane.operations import OperationResult
from linkplane.transports import BridgeError


def rule(allow=()):
    return parse_automation({"name": "r", "when": "battery.low", "do": [{"action": "run", "command": "x"}], "allow": list(allow)})


class NotifyDesktopTests(unittest.TestCase):
    @patch("linkplane.actions.shutil.which", return_value="/usr/bin/notify-send")
    def test_fills_placeholders_and_calls_notify_send(self, _which):
        calls = []
        outcome = actions.notify_desktop(
            Step("notify-desktop", {"message": "Battery {level}%", "urgency": "critical", "title": "{device}"}),
            {"level": 7, "device": "phone"}, runner=lambda cmd, **k: calls.append(cmd) or "",
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(calls, [["notify-send", "--app-name", "Linkplane", "--urgency", "critical", "phone", "Battery 7%"]])
        self.assertEqual(outcome.data["message"], "Battery 7%")

    @patch("linkplane.actions.shutil.which", return_value=None)
    def test_missing_notify_send(self, _which):
        outcome = actions.notify_desktop(Step("notify-desktop", {"message": "x"}), {})
        self.assertFalse(outcome.ok)
        self.assertIn("notify-send", outcome.detail)

    @patch("linkplane.actions.shutil.which", return_value="/usr/bin/notify-send")
    def test_bad_urgency_and_runner_failure(self, _which):
        self.assertFalse(actions.notify_desktop(Step("notify-desktop", {"message": "x", "urgency": "loud"}), {}).ok)
        def boom(cmd, **k):
            raise BridgeError("notify-send: no bus")
        outcome = actions.notify_desktop(Step("notify-desktop", {"message": "x"}), {}, runner=boom)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "notify-send: no bus")


class NotifyPhoneTests(unittest.TestCase):
    def test_uses_the_notification_service_with_the_event_address(self):
        seen = {}

        def sender(request):
            seen["request"] = request
            return OperationResult.success(SimpleNamespace())

        outcome = actions.notify_phone(Step("notify-phone", {"message": "Hi {ssid}"}), {"ssid": "Home", "provider": "adb", "address": "S1"}, sender=sender)
        self.assertTrue(outcome.ok)
        self.assertEqual(seen["request"].message, "Hi Home")
        self.assertEqual(seen["request"].serial, "S1")

    def test_service_failure_is_reported(self):
        outcome = actions.notify_phone(Step("notify-phone", {"message": "x"}), {}, sender=lambda r: OperationResult.failure("transport_unavailable", "no phone"))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "no phone")


class RunShellTests(unittest.TestCase):
    def test_refused_without_allow(self):
        outcome = actions.run_shell(Step("run", {"command": "echo hi"}), Event("battery.low", "phone"), {}, rule())
        self.assertFalse(outcome.ok)
        self.assertIn('"allow": ["run"]', outcome.detail)

    def test_runs_with_event_environment_and_no_shell(self):
        seen = {}

        def process_runner(argv, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs["env"]
            return SimpleNamespace(returncode=0, stdout="done\n")

        event = Event("battery.low", "phone", data={"level": 5})
        outcome = actions.run_shell(Step("run", {"command": "~/scripts/x.sh --flag 'a b'"}), event, {}, rule(allow=["run"]), process_runner=process_runner)
        self.assertTrue(outcome.ok)
        self.assertEqual(seen["argv"][1:], ["--flag", "a b"])
        self.assertTrue(seen["argv"][0].endswith("/scripts/x.sh"))
        self.assertEqual(seen["env"]["LINKPLANE_EVENT"], "battery.low")
        self.assertEqual(seen["env"]["LINKPLANE_DEVICE"], "phone")
        self.assertEqual(seen["env"]["LINKPLANE_DATA"], '{"level": 5}')
        self.assertEqual(outcome.data["exit_code"], 0)

    def test_nonzero_exit_missing_command_and_timeout(self):
        event = Event("battery.low", "phone")
        fail = actions.run_shell(Step("run", {"command": "x"}), event, {}, rule(allow=["run"]), process_runner=lambda *a, **k: SimpleNamespace(returncode=3, stdout=""))
        self.assertFalse(fail.ok)
        self.assertIn("exit 3", fail.detail)

        def missing(*a, **k):
            raise FileNotFoundError

        self.assertIn("command not found", actions.run_shell(Step("run", {"command": "nope"}), event, {}, rule(allow=["run"]), process_runner=missing).detail)

        def slow(*a, **k):
            raise subprocess.TimeoutExpired("x", 1)

        self.assertIn("timed out", actions.run_shell(Step("run", {"command": "x", "timeout": 1}), event, {}, rule(allow=["run"]), process_runner=slow).detail)

    def test_real_subprocess_receives_environment(self):
        event = Event("wifi.connected", "phone", data={"ssid": "Home"})
        outcome = actions.run_shell(Step("run", {"command": "sh -c 'echo $LINKPLANE_EVENT/$LINKPLANE_DEVICE'"}), event, {}, rule(allow=["run"]))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.data["stdout"].strip(), "wifi.connected/phone")


class DispatchTests(unittest.TestCase):
    def test_job_actions_need_a_runner(self):
        outcome = actions.run_step(Step("backup", {}), Event("wifi.connected", "phone"), {}, rule())
        self.assertFalse(outcome.ok)
        self.assertIn("needs a job runner", outcome.detail)


class JobActionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from linkplane.jobs import JobRunner

        self.directory = tempfile.TemporaryDirectory()
        self.jobs = JobRunner(self.directory.name, retry_delays=(), sleep=lambda s: None)

    def tearDown(self):
        self.directory.cleanup()

    def test_backup_runs_through_the_job_runner_and_exposes_result_keys(self):
        seen = {}

        def service(request, *, progress, cancel):
            seen["request"] = request
            progress(SimpleNamespace(phase="completed", message="Backup completed", current=3, total=3, unit="files", item=None, details={}))
            return OperationResult.success(SimpleNamespace(to_dict=lambda: {"downloaded": 3, "skipped": 1}))

        outcome = actions.backup_job(Step("backup", {"destination": "~/Pictures/{device}"}), Event("device.connected", "phone", provider="adb", data={"address": "S1"}),
                                     {"device": "phone", "provider": "adb", "address": "S1"}, rule(), self.jobs, service=service)
        self.assertTrue(outcome.ok)
        self.assertEqual(seen["request"].destination, "~/Pictures/phone")
        self.assertEqual(seen["request"].serial, "S1")
        self.assertEqual(outcome.data["downloaded"], 3)
        self.assertEqual(outcome.data["job_state"], "completed")
        self.assertIn("Backup completed", outcome.detail)

    def test_send_requires_paths_and_forwards_them(self):
        missing = actions.send_job(Step("send", {}), Event("wifi.connected", "phone"), {}, rule(), self.jobs)
        self.assertFalse(missing.ok)
        seen = {}

        def service(request, *, progress, cancel):
            seen["request"] = request
            return OperationResult.success(SimpleNamespace(to_dict=lambda: {"total_bytes": 5}))

        outcome = actions.send_job(Step("send", {"paths": ["~/a.txt", "/b"], "destination": "/sdcard/x"}), Event("wifi.connected", "phone"), {}, rule(), self.jobs, service=service)
        self.assertTrue(outcome.ok)
        self.assertTrue(seen["request"].paths[0].endswith("/a.txt"))
        self.assertEqual(seen["request"].paths[1], "/b")
        self.assertEqual(seen["request"].destination, "/sdcard/x")

    def test_failed_job_is_a_failed_outcome(self):
        outcome = actions.backup_job(Step("backup", {}), Event("device.connected", "phone"), {}, rule(), self.jobs,
                                     service=lambda request, *, progress, cancel: OperationResult.failure("operation_failed", "pull failed"))
        self.assertFalse(outcome.ok)
        self.assertIn("failed: pull failed", outcome.detail)

    def test_clipboard_sync_uses_the_transport_factory(self):
        from types import SimpleNamespace as NS

        def service(request, transport, serial, *, progress, cancel):
            self.assertEqual(request.action, "sync")
            self.assertEqual(transport.host, "phone.local")
            return OperationResult.success(NS(to_dict=lambda: {"updates": 2}))

        outcome = actions.clipboard_sync_job(Step("clipboard-sync", {"interval": 2}), Event("device.connected", "phone"), {}, rule(), self.jobs,
                                             service=service, transport_factory=lambda: NS(host="phone.local"))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.data["updates"], 2)


if __name__ == "__main__":
    unittest.main()
