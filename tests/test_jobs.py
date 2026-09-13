"""JobRunner: records, retry policy, cancellation, one-per-(device, action)."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from linkplane.jobs import JobRunner, read_jobs
from linkplane.operations import CancellationToken, OperationResult, ProgressEvent


def ok(value=None):
    return OperationResult.success(SimpleNamespace(to_dict=lambda: value or {"done": True}))


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.sleeps = []
        self.jobs = JobRunner(self.directory.name, retry_delays=(1, 2), sleep=self.sleeps.append)

    def tearDown(self):
        self.directory.cleanup()

    def test_actor_and_on_started_hand_back_the_record_before_completion(self):
        seen = []
        gate = threading.Event()

        def call(cancel, progress):
            gate.wait(5)
            if cancel.cancelled:  # cooperative, as every real service behaves
                return OperationResult.failure("cancelled", cancel.reason or "cancelled")
            return ok()

        thread = threading.Thread(target=lambda: seen.append(self.jobs.run(
            automation="", action="backup", device="phone", call=call, actor="client:gui", correlation_id="c1",
            on_started=lambda record: seen.append(("started", record)))))
        thread.start()
        for _ in range(100):
            if seen:
                break
            time.sleep(0.01)
        label, started = seen[0]
        self.assertEqual((label, started.state, started.actor, started.automation), ("started", "running", "client:gui", ""))
        self.assertIn("client-gui-backup", started.id)
        self.assertTrue(self.jobs.cancel(started.id, "cancelled by client:gui"))
        gate.set()
        thread.join(5)
        final = seen[-1]
        self.assertEqual((final.id, final.state, final.actor), (started.id, "cancelled", "client:gui"))
        self.assertEqual(final.error["message"], "cancelled by client:gui")
        self.assertFalse(self.jobs.cancel(started.id, "again"))  # terminal: nothing to cancel
        self.assertFalse(self.jobs.cancel("no-such-job", "x"))
        self.assertEqual(read_jobs(self.directory.name).__next__().actor, "client:gui")

    def test_duplicate_also_reports_through_on_started(self):
        gate = threading.Event()
        thread = threading.Thread(target=lambda: self.jobs.run(automation="r", action="backup", device="phone", call=lambda c, p: (gate.wait(5), ok())[1]))
        thread.start()
        for _ in range(100):
            if self.jobs.running():
                break
            time.sleep(0.01)
        seen = []
        skipped = self.jobs.run(automation="", action="backup", device="phone", call=lambda c, p: ok(), actor="client:x", on_started=seen.append)
        gate.set()
        thread.join(5)
        self.assertEqual((skipped.state, seen[0].id, seen[0].actor), ("skipped", skipped.id, "client:x"))

    def test_success_records_progress_and_result(self):
        def call(cancel, progress):
            progress(ProgressEvent("backup", "item_completed", "one", current=1, total=2, unit="files"))
            return ok({"downloaded": 2})

        record = self.jobs.run(automation="r", action="backup", device="phone", call=call)
        self.assertEqual(record.state, "completed")
        self.assertEqual(record.result, {"downloaded": 2})
        self.assertEqual(record.progress["current"], 1)
        self.assertIsNotNone(record.finished)
        saved = json.loads((Path(self.directory.name) / f"{record.id}.json").read_text())
        self.assertEqual(saved["state"], "completed")
        self.assertEqual([r.id for r in read_jobs(self.directory.name)], [record.id])
        self.assertEqual(self.jobs.running(), [])

    def test_transport_failures_retry_with_backoff_then_fail(self):
        attempts = []

        def call(cancel, progress):
            attempts.append(1)
            return OperationResult.failure("transport_unavailable", "no phone")

        record = self.jobs.run(automation="r", action="backup", device="phone", call=call)
        self.assertEqual(record.state, "failed")
        self.assertEqual(len(attempts), 3)  # first try + two retries
        self.assertEqual(self.sleeps, [1, 2])
        self.assertEqual(record.attempt, 3)
        self.assertEqual(record.error["code"], "transport_unavailable")

    def test_other_failures_do_not_retry(self):
        attempts = []

        def call(cancel, progress):
            attempts.append(1)
            return OperationResult.failure("invalid_request", "bad")

        record = self.jobs.run(automation="r", action="send", device="phone", call=call)
        self.assertEqual((record.state, len(attempts), self.sleeps), ("failed", 1, []))

    def test_transitions_are_reported_in_order_including_retrying(self):
        seen = []
        jobs = JobRunner(self.directory.name, retry_delays=(1,), sleep=lambda s: None, on_transition=lambda r: seen.append((r.state, r.attempt)))
        results = iter([OperationResult.failure("transport_unavailable", "x"), ok()])
        record = jobs.run(automation="r", action="backup", device="phone", call=lambda c, p: next(results))
        self.assertEqual(seen, [("running", 1), ("retrying", 1), ("running", 2), ("completed", 2)])
        self.assertTrue(record.terminal)

    def test_hook_errors_never_break_a_job(self):
        def boom(record):
            raise RuntimeError("audit down")

        jobs = JobRunner(self.directory.name, retry_delays=(), on_transition=boom)
        self.assertEqual(jobs.run(automation="r", action="send", device="phone", call=lambda c, p: ok()).state, "completed")

    def test_retry_succeeds_on_second_attempt(self):
        results = iter([OperationResult.failure("transport_unavailable", "x"), ok()])
        record = self.jobs.run(automation="r", action="backup", device="phone", call=lambda c, p: next(results))
        self.assertEqual((record.state, record.attempt), ("completed", 2))

    def test_cancel_device_cancels_the_running_job(self):
        started = threading.Event()

        def call(cancel, progress):
            started.set()
            cancel.wait(5)
            return OperationResult.failure("cancelled", cancel.reason or "cancelled")

        holder = {}
        thread = threading.Thread(target=lambda: holder.setdefault("record", self.jobs.run(automation="r", action="backup", device="phone", call=call)))
        thread.start()
        started.wait(2)
        self.assertEqual(self.jobs.running(), [("phone", "backup")])
        self.assertEqual(self.jobs.cancel_device("phone", "phone disconnected"), 1)
        thread.join(5)
        record = holder["record"]
        self.assertEqual(record.state, "cancelled")
        self.assertEqual(record.error["message"], "phone disconnected")
        self.assertEqual(self.jobs.cancel_device("phone", "again"), 0)

    def test_second_job_for_same_device_and_action_is_skipped(self):
        gate = threading.Event()
        thread = threading.Thread(target=lambda: self.jobs.run(automation="r", action="backup", device="phone", call=lambda c, p: (gate.wait(5), ok())[1]))
        thread.start()
        time.sleep(0.05)
        skipped = self.jobs.run(automation="r2", action="backup", device="phone", call=lambda c, p: ok())
        gate.set()
        thread.join(5)
        self.assertEqual(skipped.state, "skipped")
        self.assertEqual(skipped.error["code"], "already_running")
        other = self.jobs.run(automation="r3", action="send", device="phone", call=lambda c, p: ok())
        self.assertEqual(other.state, "completed")

    def test_exception_in_call_leaves_a_failed_record(self):
        def call(cancel, progress):
            raise RuntimeError("boom")

        record = self.jobs.run(automation="r", action="backup", device="phone", call=call)
        self.assertEqual((record.state, record.error["code"]), ("failed", "exception"))
        self.assertEqual(self.jobs.running(), [])

    def test_cancel_all_and_wait_idle(self):
        def call(cancel, progress):
            cancel.wait(5)
            return OperationResult.failure("cancelled", "stop")

        threads = [threading.Thread(target=lambda a=a: self.jobs.run(automation="r", action=a, device="phone", call=call)) for a in ("backup", "send")]
        for t in threads:
            t.start()
        time.sleep(0.05)
        self.assertEqual(self.jobs.cancel_all("daemon stopping"), 2)
        self.assertTrue(self.jobs.wait_idle(5))
        for t in threads:
            t.join(5)


if __name__ == "__main__":
    unittest.main()
