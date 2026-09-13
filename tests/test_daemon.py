"""linkplaned over a real Unix socket in a temp dir, with a fake observer. No phone."""

import json
import os
import tempfile
import threading
import time
import unittest

import linkplane
from pathlib import Path

from linkplane import daemon as daemond
from linkplane.automations import read_audit
from linkplane.core import errors
from linkplane.core.events import Event
from linkplane.core.state import CONNECTED, DeviceState
from linkplane.operations import CancellationToken, OperationResult


class FakeObserver:
    """Emits a scripted sequence once `release` is set, then waits for cancellation."""

    instances: list = []

    def __init__(self, sink, *, cancel: CancellationToken, **kwargs):
        self.sink = sink
        self.cancel = cancel
        self.kwargs = kwargs
        self.states = {"phone": DeviceState("phone", "adb", CONNECTED, "S1", {"level": 55}, "Home")}
        self.release = threading.Event()
        FakeObserver.instances.append(self)

    def run(self):
        self.sink(Event("observer.started", "*", data={"sources": ["fake"], "interval": 1.0}))
        while not self.release.is_set() and not self.cancel.cancelled:
            time.sleep(0.01)
        if self.release.is_set():
            self.sink(Event("device.connected", "phone", provider="adb", data={"address": "S1"}))
            self.sink(Event("battery.low", "phone", provider="adb", data={"level": 5, "threshold": 20}))
            self.sink(Event("battery.changed", "other", provider="adb", data={"level": 50}))
        self.cancel.wait(5)
        self.sink(Event("observer.stopped", "*"))


class DaemonTests(unittest.TestCase):
    def setUp(self):
        FakeObserver.instances.clear()
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        # Unix socket paths are length-limited; keep it short.
        self.socket = str(base / "d.sock")
        self.state = str(base / "state.json")
        self.history = str(base / "events.jsonl")
        self.daemon = daemond.Daemon(
            socket_path=self.socket, state_path=self.state, history_path=self.history,
            identities={"S1": "phone"}, interval=1.0, observer_factory=FakeObserver,
            audit_path=str(base / "audit.jsonl"), automations_path=str(base / "rules.json"),
        )
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if os.path.exists(self.socket) and daemond.is_running(self.socket):
                break
            time.sleep(0.02)
        else:
            self.fail("daemon did not start")

    def tearDown(self):
        self.daemon.stop("test teardown")
        self.thread.join(5)
        self.directory.cleanup()

    def observer(self) -> FakeObserver:
        return FakeObserver.instances[-1]

    def test_status_reports_pid_socket_and_observed_devices(self):
        info = daemond.request("status", self.socket)
        self.assertTrue(info["ok"])
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(info["socket"], self.socket)
        self.assertEqual(info["protocol"], daemond.PROTOCOL)
        # Slice 0: the package version rides along so setup can detect a stale daemon.
        self.assertEqual(info["version"], linkplane.__version__)
        self.assertEqual(info["devices"]["phone"]["battery"]["level"], 55)
        self.assertEqual(self.observer().kwargs["identities"], {"S1": "phone"})

    def test_subscribe_receives_filtered_events_with_history_seq(self):
        received = []
        token = CancellationToken()

        def consume():
            for event in daemond.subscribe(self.socket, types=("battery.low", "battery.changed"), devices=("phone",), cancel=token):
                received.append(event)
                if event.type == "battery.low":
                    token.cancel()

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        time.sleep(0.2)  # let the subscription register before the script fires
        self.observer().release.set()
        consumer.join(5)

        self.assertEqual([e.type for e in received], ["battery.low"])  # filtered by type and device
        self.assertIsNotNone(received[0].seq)  # stamped by the daemon's history writer
        lines = Path(self.history).read_text().splitlines()
        self.assertEqual([json.loads(l)["type"] for l in lines][:4],
                         ["observer.started", "device.connected", "battery.low", "battery.changed"])

    def test_departed_subscriber_is_released_without_an_event(self):
        sock = daemond.connect(self.socket)
        daemond._send_line(sock, {"op": "subscribe"})
        next(daemond._read_lines(sock))  # the acknowledgement
        for _ in range(50):
            if daemond.request("status", self.socket)["subscribers"] == 1:
                break
            time.sleep(0.02)
        self.assertEqual(daemond.request("status", self.socket)["subscribers"], 1)
        sock.close()
        for _ in range(100):
            if daemond.request("status", self.socket)["subscribers"] == 0:
                break
            time.sleep(0.02)
        self.assertEqual(daemond.request("status", self.socket)["subscribers"], 0)

    def test_state_snapshot_is_written_and_marks_daemon_on_stop(self):
        snapshot = json.loads(Path(self.state).read_text())
        self.assertEqual(snapshot["schema"], daemond.STATE_SCHEMA)
        self.assertEqual(snapshot["daemon"]["pid"], os.getpid())
        self.assertEqual(snapshot["devices"]["phone"]["wifi_ssid"], "Home")

        daemond.request("stop", self.socket)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(os.path.exists(self.socket))
        final = json.loads(Path(self.state).read_text())
        self.assertIsNone(final["daemon"])
        self.assertEqual(self.daemon.stop_reason, "stop requested over the control socket")

    def test_second_daemon_on_the_same_socket_is_refused(self):
        second = daemond.Daemon(socket_path=self.socket, state_path=self.state, history_path=self.history,
                                observer_factory=FakeObserver, write_history=False,
                                audit_path=str(Path(self.directory.name) / "a2.jsonl"), automations_path=str(Path(self.directory.name) / "r2.json"))
        with self.assertRaises(errors.LinkplaneError) as raised:
            second.run()
        self.assertEqual(raised.exception.code, errors.DAEMON_ALREADY_RUNNING)

    def test_unknown_op_is_a_coded_error(self):
        with self.assertRaises(errors.LinkplaneError) as raised:
            daemond.request("dance", self.socket)
        self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)


class DaemonRulesTests(unittest.TestCase):
    """Rules from the file fire on the daemon's worker thread and are audited."""

    def setUp(self):
        FakeObserver.instances.clear()
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.socket, self.audit, self.rules = str(base / "d.sock"), str(base / "audit.jsonl"), str(base / "rules.json")
        Path(self.rules).write_text(json.dumps({"automations": [
            {"name": "low", "when": "battery.low", "do": [{"action": "notify-desktop", "message": "Low {level}"}]},
            {"name": "blocked", "when": "battery.low", "do": [{"action": "run", "command": "x"}]},
        ]}))
        self.steps = []

        def step_runner(step, event, context, rule):
            from linkplane.core.automation import StepOutcome

            self.steps.append((rule.name, step.action, context.get("level")))
            return StepOutcome(step.action, True, "fake")

        self.daemon = daemond.Daemon(socket_path=self.socket, state_path=str(base / "state.json"), write_history=False,
                                     observer_factory=FakeObserver, automations_path=self.rules, audit_path=self.audit,
                                     step_runner=step_runner)
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if daemond.is_running(self.socket):
                break
            time.sleep(0.02)

    def tearDown(self):
        self.daemon.stop()
        self.thread.join(5)
        self.directory.cleanup()

    def wait_for_steps(self, count):
        for _ in range(100):
            if len(self.steps) >= count:
                return
            time.sleep(0.02)

    def test_rules_fire_off_the_observer_thread_and_are_audited(self):
        status = daemond.request("status", self.socket)["automations"]
        self.assertEqual((status["loaded"], status["blocked"], status["fired"]), (1, {"blocked": ["run"]}, 0))
        FakeObserver.instances[-1].release.set()
        self.wait_for_steps(1)
        self.assertEqual(self.steps, [("low", "notify-desktop", 5)])
        self.assertEqual(daemond.request("status", self.socket)["automations"]["fired"], 1)
        kinds = [r["kind"] for r in read_audit(self.audit)]
        self.assertEqual(kinds[:3], ["daemon.started", "rules.loaded", "rule.blocked"])
        self.assertIn("rule.fired", kinds)

    def test_reload_picks_up_file_changes(self):
        Path(self.rules).write_text(json.dumps({"automations": [
            {"name": "a", "when": "battery.low", "do": [{"action": "notify-desktop"}]},
            {"name": "b", "when": "battery.changed", "do": [{"action": "notify-desktop"}]},
        ]}))
        info = daemond.request("reload", self.socket)
        self.assertEqual((info["loaded"], info["active"], info["blocked"]), (2, ["a", "b"], {}))
        FakeObserver.instances[-1].release.set()
        self.wait_for_steps(2)
        self.assertEqual(sorted(s[0] for s in self.steps), ["a", "b"])

    def test_reload_with_a_broken_file_keeps_old_rules(self):
        Path(self.rules).write_text("{broken")
        with self.assertRaises(errors.LinkplaneError) as raised:
            daemond.request("reload", self.socket)
        self.assertEqual(raised.exception.code, errors.CONFIG_INVALID)
        self.assertEqual(daemond.request("status", self.socket)["automations"]["loaded"], 1)


class DaemonJobsTests(unittest.TestCase):
    """A disconnect cancels that device's running jobs; firings run off the rules thread."""

    def setUp(self):
        FakeObserver.instances.clear()
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.socket = str(base / "d.sock")
        Path(base / "rules.json").write_text(json.dumps({"automations": [
            {"name": "bk", "when": "device.connected", "do": [{"action": "backup"}]},
        ]}))
        from linkplane.jobs import JobRunner
        self.jobs = JobRunner(str(base / "jobs"), retry_delays=())
        self.job_started = threading.Event()
        self.job_result = {}

        def step_runner(step, event, context, rule, **_):
            from linkplane.core.automation import StepOutcome

            def call(cancel, progress):
                self.job_started.set()
                cancel.wait(5)
                return OperationResult.failure("cancelled", cancel.reason or "cancelled")

            record = self.jobs.run(automation=rule.name, action="backup", device=event.device, call=call)
            self.job_result["record"] = record
            return StepOutcome(step.action, record.ok, record.state)

        self.daemon = daemond.Daemon(socket_path=self.socket, state_path=str(base / "state.json"), write_history=False,
                                     observer_factory=FakeObserver, automations_path=str(base / "rules.json"),
                                     audit_path=str(base / "audit.jsonl"), step_runner=step_runner, jobs=self.jobs)
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if daemond.is_running(self.socket):
                break
            time.sleep(0.02)

    def tearDown(self):
        self.daemon.stop()
        self.thread.join(5)
        self.directory.cleanup()

    def test_disconnect_cancels_the_running_backup(self):
        FakeObserver.instances[-1].release.set()  # emits device.connected -> backup job starts
        self.assertTrue(self.job_started.wait(3))
        status = daemond.request("status", self.socket)
        self.assertEqual(status["jobs"], [{"device": "phone", "action": "backup"}])
        self.assertIn("last_seq", status)  # None here: write_history=False
        self.assertIsNone(status["api"])
        self.daemon._sink(Event("device.disconnected", "phone", provider="adb", data={"address": "S1"}))
        for _ in range(100):
            if "record" in self.job_result:
                break
            time.sleep(0.02)
        record = self.job_result["record"]
        self.assertEqual(record.state, "cancelled")
        self.assertIn("disconnected", record.error["message"])
        kinds = [r["kind"] for r in read_audit(str(Path(self.directory.name) / "audit.jsonl"))]
        self.assertIn("jobs.cancelled", kinds)
        self.assertIn("job.cancelled", kinds)
        self.assertIn("job.started", kinds)
        self.assertIn("rule.fired", kinds)


class DaemonClientWithoutServerTests(unittest.TestCase):
    def test_request_without_daemon_is_not_running(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(errors.LinkplaneError) as raised:
                daemond.request("status", str(Path(directory) / "none.sock"))
        self.assertEqual(raised.exception.code, errors.DAEMON_NOT_RUNNING)
        self.assertIn("daemon run", raised.exception.hints[0])

    def test_stale_socket_file_is_removed_on_start(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "s.sock"
            socket_path.write_text("stale")
            daemon = daemond.Daemon(socket_path=str(socket_path), state_path=str(Path(directory) / "st.json"),
                                    observer_factory=FakeObserver, write_history=False,
                                    audit_path=str(Path(directory) / "a.jsonl"), automations_path=str(Path(directory) / "r.json"))
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            for _ in range(100):
                if daemond.is_running(str(socket_path)):
                    break
                time.sleep(0.02)
            self.assertTrue(daemond.is_running(str(socket_path)))
            daemon.stop()
            thread.join(5)

    def test_paths_follow_environment(self):
        with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/7", "LINKPLANE_SOCKET": ""}):
            self.assertEqual(daemond.resolve_socket_path(), Path("/run/user/7/linkplane/daemon.sock"))
        with unittest.mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/st", "LINKPLANE_STATE": ""}):
            self.assertEqual(daemond.resolve_state_path(), Path("/tmp/st/linkplane/state.json"))


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
