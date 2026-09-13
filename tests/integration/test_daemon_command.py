"""`linkplane daemon status|stop` and `events --follow` through the CLI against a daemon
running in-process on a temp socket, with a fake observer."""

import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from linkplane import daemon as daemond
from linkplane.cli import main
from linkplane.core.events import Event
from linkplane.core.state import CONNECTED, DeviceState
from linkplane.operations import CancellationToken


class ScriptedObserver:
    def __init__(self, sink, *, cancel: CancellationToken, **kwargs):
        self.sink, self.cancel = sink, cancel
        self.states = {"phone": DeviceState("phone", "adb", CONNECTED, "S1", {"level": 88}, None)}

    def run(self):
        self.sink(Event("observer.started", "*", data={"sources": ["fake"], "interval": 1.0}))
        for _ in range(10):
            if self.cancel.wait(0.1):
                break
            self.sink(Event("battery.changed", "phone", provider="adb", data={"level": 88}))
        self.sink(Event("observer.stopped", "*"))


def cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class DaemonCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.socket = str(base / "d.sock")
        self.daemon = daemond.Daemon(socket_path=self.socket, state_path=str(base / "state.json"),
                                     history_path=str(base / "h.jsonl"), observer_factory=ScriptedObserver,
                                     audit_path=str(base / "a.jsonl"), automations_path=str(base / "r.json"))
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

    def test_status_text_and_json(self):
        code, out, _ = cli(["daemon", "status", "--socket", self.socket])
        self.assertEqual(code, 0)
        self.assertIn("Linkplane Daemon", out)
        self.assertIn("Device      phone: connected via adb; battery 88%; wifi -", out)
        code, out, _ = cli(["daemon", "status", "--json", "--socket", self.socket])
        envelope = json.loads(out)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["devices"]["phone"]["connection"], "connected")

    def test_events_follow_streams_from_the_daemon_and_stops(self):
        # Stop the daemon shortly after the follower connects so the stream ends.
        threading.Timer(0.5, lambda: self.daemon.stop("test")).start()
        code, out, err = cli(["events", "--follow", "--json", "--socket", self.socket, "--type", "battery.changed"])
        records = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(code, 0)
        self.assertTrue(records)
        self.assertTrue(all(r["type"] == "battery.changed" for r in records))
        self.assertTrue(all(r["seq"] is not None for r in records))
        self.assertNotIn("no daemon running", err)

    def test_stop_command(self):
        code, out, _ = cli(["daemon", "stop", "--socket", self.socket])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "linkplaned is stopping")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())


class WithoutDaemonTests(unittest.TestCase):
    def test_status_without_daemon_is_a_coded_error(self):
        with tempfile.TemporaryDirectory() as directory:
            code, out, err = cli(["daemon", "status", "--json", "--socket", str(Path(directory) / "x.sock")])
        envelope = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(envelope["error"]["code"], "LP-DAEMON-001")

    def test_events_follow_falls_back_in_process(self):
        from unittest.mock import patch

        class Once:
            def __init__(self, sink, **kwargs):
                self.sink = sink

            def run(self):
                self.sink(Event("observer.started", "*", data={"sources": [], "interval": 1.0}))
                self.sink(Event("observer.stopped", "*"))

        with tempfile.TemporaryDirectory() as directory, patch("linkplane.commands.events.Observer", Once):
            code, out, err = cli(["events", "--follow", "--json", "--no-history",
                                  "--socket", str(Path(directory) / "x.sock")])
        self.assertEqual(code, 0)
        self.assertIn("no daemon running; observing in this process instead", err)
        self.assertEqual([json.loads(l)["type"] for l in out.splitlines()], ["observer.started", "observer.stopped"])


class RunCommandSignalTests(unittest.TestCase):
    def test_sigterm_stops_the_daemon_cleanly(self):
        # Regression: under systemd, `disable --now` sends SIGTERM; before this handler the
        # daemon died without removing its socket or marking state.json stopped.
        import signal

        seen = {}

        class FakeDaemon:
            def __init__(self, **kwargs):
                from linkplane.operations import CancellationToken

                self.cancel = CancellationToken()
                self.socket_path = "fake.sock"
                self.stop_reason = None

            def stop(self, reason):
                self.stop_reason = reason
                self.cancel.cancel(reason)

            def run(self):
                threading.Timer(0.05, lambda: signal.raise_signal(signal.SIGTERM)).start()
                # Poll rather than block: a signal raised from another thread is only
                # noticed by the main thread between bytecodes, and a blocking wait would
                # sit through its whole timeout first (an artifact of raising it from a
                # thread; a real external SIGTERM interrupts the main thread's wait).
                for _ in range(100):
                    if self.cancel.cancelled:
                        break
                    time.sleep(0.05)
                seen["reason"] = self.stop_reason

        from unittest.mock import patch

        with patch("linkplane.commands.daemon.daemond.Daemon", FakeDaemon):
            code, _out, err = cli(["daemon", "run", "--no-history"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["reason"], "SIGTERM")
        self.assertIn("linkplaned stopped: SIGTERM", err)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class ObservedDevicesTests(unittest.TestCase):
    def test_reads_snapshot_without_probing(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({
                "schema": "linkplane.state/1", "updated": "2026-09-10T19:00:00-05:00",
                "daemon": {"pid": 1, "started": "x", "socket": "s"},
                "devices": {"phone": {"device": "phone", "provider": "adb", "connection": "connected",
                                       "address": "S1", "battery": {"level": 42}, "wifi_ssid": "Home",
                                       "last_seen": "2026-09-10T19:00:00-05:00", "updated": "x"}},
            }))
            code, out, _ = cli(["devices", "--observed", "--state", str(state)])
            self.assertEqual(code, 0)
            self.assertIn("Daemon       running, pid 1", out)
            self.assertIn("Connection   connected via adb (S1)", out)
            self.assertIn("Battery      42%", out)
            code, out, _ = cli(["devices", "--observed", "--json", "--state", str(state)])
            envelope = json.loads(out)
            self.assertEqual(envelope["data"]["devices"]["phone"]["wifi_ssid"], "Home")

    def test_missing_snapshot_is_a_coded_error(self):
        with tempfile.TemporaryDirectory() as directory:
            code, out, _ = cli(["devices", "--observed", "--json", "--state", str(Path(directory) / "none.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "LP-DAEMON-001")


class AutomationsCommandTests(unittest.TestCase):
    def test_list_shows_active_and_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "a.json"
            rules.write_text(json.dumps({"automations": [
                {"name": "low", "when": "battery.low", "if": {"level": {"below": 10}}, "do": [{"action": "notify-desktop"}], "cooldown_seconds": 60},
                {"name": "s", "when": "device.connected", "do": [{"action": "run", "command": "x"}]},
            ]}))
            code, out, _ = cli(["automations", "list", "--file", str(rules)])
            self.assertEqual(code, 0)
            self.assertIn("low  [enabled]", out)
            self.assertIn("When        battery.low if {'level': {'below': 10}}", out)
            self.assertIn("Cooldown    60s", out)
            self.assertIn("s  [blocked]", out)
            self.assertIn('"allow": ["run"]', out)
            code, out, _ = cli(["automations", "list", "--json", "--file", str(rules)])
            envelope = json.loads(out)
            self.assertEqual(envelope["data"]["active"], ["low"])
            self.assertEqual(envelope["data"]["automations"][0]["when"], "battery.low")

    def test_log_reads_recent_audit_lines(self):
        from linkplane.automations import AuditWriter
        from linkplane.core.automation import Firing, StepOutcome

        with tempfile.TemporaryDirectory() as directory:
            audit = str(Path(directory) / "audit.jsonl")
            writer = AuditWriter(audit)
            writer.write("daemon.started", decision="started", pid=9)
            writer.firing(Firing("low", Event("battery.low", "phone", data={"level": 3}), (StepOutcome("notify-desktop", True, "ok"),)))
            writer.close()
            code, out, _ = cli(["automations", "log", "--file", audit, "-n", "1"])
            self.assertEqual(code, 0)
            self.assertIn("rule.fired      fired     low              battery.low          ✓notify-desktop", out)
            code, out, _ = cli(["automations", "log", "--json", "--file", audit])
            self.assertEqual([json.loads(l)["kind"] for l in out.splitlines()], ["daemon.started", "rule.fired"])

    def test_reload_command_reports_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "r.json").write_text(json.dumps({"automations": [{"name": "x", "when": "battery.low", "do": [{"action": "notify-desktop"}]}]}))
            daemon = daemond.Daemon(socket_path=str(base / "d.sock"), state_path=str(base / "s.json"), write_history=False,
                                    observer_factory=ScriptedObserver, automations_path=str(base / "r.json"), audit_path=str(base / "a.jsonl"))
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            for _ in range(100):
                if daemond.is_running(str(base / "d.sock")):
                    break
                time.sleep(0.02)
            try:
                code, out, _ = cli(["daemon", "reload", "--socket", str(base / "d.sock")])
                self.assertEqual(code, 0)
                self.assertIn("Reloaded    1 automation(s)", out)
                code, out, _ = cli(["daemon", "status", "--socket", str(base / "d.sock")])
                self.assertIn("Automations 1 active, 0 blocked, 0 fired", out)
            finally:
                daemon.stop()
                thread.join(5)


class InstallCommandTests(unittest.TestCase):
    def test_install_dry_run_shows_unit_and_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            code, out, _ = cli(["daemon", "install", "--dry-run", "--interval", "15", "--unit-dir", directory])
            self.assertEqual(os.listdir(directory), [])
        self.assertEqual(code, 0)
        self.assertIn("Would install", out)
        self.assertIn("daemon run --interval 15", out)
        self.assertIn("systemctl --user enable --now linkplaned.service", out)

    def test_install_json_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            code, out, _ = cli(["daemon", "install", "--dry-run", "--json", "--unit-dir", directory])
        envelope = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(envelope["data"]["action"], "install")
        self.assertTrue(envelope["data"]["dry_run"])
        self.assertEqual(envelope["data"]["commands"][0], ["systemctl", "--user", "daemon-reload"])


import os  # noqa: E402

if __name__ == "__main__":
    unittest.main()

