"""Device tier: the release smoke test (docs/smoke-test.md), one repeatable procedure.

Requires the paired phone authorized over ADB. Everything runs against a temporary socket,
state file, history, rules file, audit log, and job directory, so nothing on the machine is
touched. Skips (never fails) when no authorized ADB device is present. Run with
`make test-device` or `make smoke`.
"""

import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from linkplane import daemon as daemond
from linkplane.automations import read_audit
from linkplane.cli import main
from linkplane.core.capability import CATALOGUE
from linkplane.jobs import JobRunner, read_jobs
from linkplane.operations import CancellationToken
from linkplane.transports import AdbTransport, BridgeError


def cli(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def wait_until(predicate, timeout=20.0, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class ReleaseSmokeTests(unittest.TestCase):
    """Ordered like the procedure in docs/smoke-test.md; each test stands alone."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.serial = AdbTransport(None).select_device()["serial"]
        except BridgeError as error:
            raise unittest.SkipTest(f"no authorized ADB device: {error}")

    # -- 1. core commands ----------------------------------------------------------------

    def test_01_core_commands_answer_in_json(self):
        for argv in (["devices", "--json"], ["status", "--transport", "adb", "--json"],
                     ["battery", "--transport", "adb", "--json"], ["ping", "--transport", "adb", "--json"],
                     ["capabilities", "--transport", "adb", "--json"], ["doctor", "--transport", "adb", "--json"]):
            with self.subTest(command=argv[0]):
                code, out, _ = cli(argv)
                envelope = json.loads(out)
                self.assertEqual(code, 0, envelope)
                self.assertEqual(envelope["schema_version"], 1)
                self.assertTrue(envelope["ok"])
        _, out, _ = cli(["capabilities", "--transport", "adb", "--json"])
        names = [r["name"] for r in json.loads(out)["data"]["capabilities"]]
        self.assertEqual(names, list(CATALOGUE))

    # -- 2..5. daemon, observed state, subscription, reconnect, rules, jobs, audit --------

    def test_02_daemon_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            socket = str(base / "d.sock")
            rules = base / "rules.json"
            payload = base / "smoke.txt"
            payload.write_text("linkplane smoke test\n")
            rules.write_text(json.dumps({"schema_version": 1, "automations": [
                {"name": "on-connect-send", "when": "device.connected",
                 "do": [{"action": "send", "paths": [str(payload)], "destination": "/sdcard/Download"},
                        {"action": "notify-desktop", "message": "smoke: sent {total_bytes} bytes"}]},
                {"name": "needs-consent", "when": "battery.low", "do": [{"action": "run", "command": "true"}]},
            ]}))
            jobs = JobRunner(str(base / "jobs"), retry_delays=())
            daemon = daemond.Daemon(
                socket_path=socket, state_path=str(base / "state.json"), history_path=str(base / "events.jsonl"),
                automations_path=str(rules), audit_path=str(base / "audit.jsonl"), jobs=jobs,
                interval=5.0, poll_wifi=True,
            )
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            try:
                self.assertTrue(wait_until(lambda: daemond.is_running(socket), 10), "daemon did not start")

                # daemon status + observed state
                status = daemond.request("status", socket)
                self.assertEqual(status["protocol"], daemond.PROTOCOL)
                self.assertTrue(wait_until(lambda: any(
                    s.get("connection") == "connected" for s in daemond.request("state", socket)["devices"].values()), 15))
                observed = daemond.request("state", socket)["devices"]
                device = next(name for name, s in observed.items() if s["address"] == self.serial)
                self.assertIsNotNone(observed[device]["battery"])
                self.assertEqual(status["automations"]["loaded"], 1)
                self.assertEqual(status["automations"]["blocked"], {"needs-consent": ["run"]})

                # event subscription + a real disconnect/reconnect (adbd restart on the phone)
                received = []
                token = CancellationToken()

                def consume():
                    for event in daemond.subscribe(socket, types=("device.disconnected", "device.connected"), cancel=token):
                        received.append(event)
                        if event.type == "device.connected" and not event.initial:
                            token.cancel()

                consumer = threading.Thread(target=consume, daemon=True)
                consumer.start()
                time.sleep(0.5)
                subprocess.run(["adb", "usb"], capture_output=True, timeout=20)
                consumer.join(30)
                types = [e.type for e in received]
                self.assertIn("device.disconnected", types)
                self.assertIn("device.connected", types)
                root = next(e for e in received if e.type == "device.connected" and not e.initial)
                self.assertEqual(root.correlation_id, root.event_id)

                # the reconnect fired the send job; wait for it to complete
                self.assertTrue(wait_until(lambda: any(
                    r.state == "completed" and r.action == "send" for r in read_jobs(str(base / "jobs"))), 60), "send job did not complete")
                job = next(r for r in read_jobs(str(base / "jobs")) if r.action == "send")
                self.assertEqual(job.correlation_id, root.correlation_id)

                # consent block -> reload picks it up
                data = json.loads(rules.read_text())
                data["automations"][1]["allow"] = ["run"]
                rules.write_text(json.dumps(data))
                reloaded = daemond.request("reload", socket)
                self.assertEqual((reloaded["loaded"], reloaded["blocked"]), (2, {}))

                # audit trail: loaded, blocked, rule fired, job started/completed, all correlated
                kinds = [r["kind"] for r in read_audit(str(base / "audit.jsonl"))]
                for kind in ("daemon.started", "rules.loaded", "rule.blocked", "rule.fired", "job.started", "job.completed"):
                    self.assertIn(kind, kinds, kinds)
                chain = [r for r in read_audit(str(base / "audit.jsonl")) if r.get("correlation_id") == root.correlation_id]
                self.assertEqual({r["kind"] for r in chain} >= {"rule.fired", "job.started", "job.completed"}, True)

                # stop over the socket
                daemond.request("stop", socket)
                thread.join(15)
                self.assertFalse(thread.is_alive())
                self.assertFalse(Path(socket).exists())
                self.assertIsNone(json.loads((base / "state.json").read_text())["daemon"])
                self.assertEqual([r["kind"] for r in read_audit(str(base / "audit.jsonl"))][-1], "daemon.stopped")
            finally:
                daemon.stop("smoke teardown")
                thread.join(10)
                subprocess.run(["adb", "-s", self.serial, "shell", "rm", "-f", "/sdcard/Download/smoke.txt"], capture_output=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
