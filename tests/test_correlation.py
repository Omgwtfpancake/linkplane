"""One correlation id survives event -> rule firing -> job (across a retry) -> audit."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from linkplane.automations import AuditWriter, read_audit
from linkplane.core.automation import Engine, Step, StepOutcome, parse_automation
from linkplane.core.events import Event
from linkplane.jobs import JobRunner
from linkplane.operations import OperationResult


class EventIdentityTests(unittest.TestCase):
    def test_every_event_has_an_id_and_is_its_own_root(self):
        event = Event("battery.low", "phone")
        self.assertEqual(len(event.event_id), 32)
        self.assertEqual(event.correlation_id, event.event_id)
        self.assertEqual(event.source, "observer")
        self.assertNotEqual(Event("battery.low", "phone").event_id, event.event_id)

    def test_child_events_can_join_a_chain(self):
        root = Event("device.connected", "phone")
        child = Event("battery.changed", "phone", correlation_id=root.correlation_id, source="daemon")
        self.assertEqual(child.correlation_id, root.event_id)
        self.assertNotEqual(child.event_id, root.event_id)

    def test_ids_round_trip_and_survive_sequencing(self):
        event = Event("wifi.connected", "phone", data={"ssid": "x"})
        again = Event.from_dict(event.to_dict())
        self.assertEqual((again.event_id, again.correlation_id, again.source), (event.event_id, event.correlation_id, "observer"))
        stamped = event.with_seq(7)
        self.assertEqual((stamped.seq, stamped.event_id, stamped.correlation_id), (7, event.event_id, event.correlation_id))
        record = event.to_dict()
        self.assertIn("event_id", record)
        self.assertIn("correlation_id", record)

    def test_pre_refinement_history_lines_still_load(self):
        old = {"schema": "linkplane.event/1", "type": "battery.low", "device": "phone", "ts": "2026-09-10T00:00:00", "provider": "adb", "data": {}, "seq": 3}
        event = Event.from_dict(old)
        self.assertEqual(event.seq, 3)
        self.assertEqual(event.correlation_id, event.event_id)  # assigned on load, never None


class PropagationTests(unittest.TestCase):
    def test_chain_reaches_job_record_audit_and_context_across_a_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = JobRunner(str(Path(directory) / "jobs"), retry_delays=(0,), sleep=lambda s: None)
            attempts = []
            seen_context = {}

            def step_runner(step, event, context, rule):
                seen_context.update(context)
                if step.action == "backup":
                    def call(cancel, progress):
                        attempts.append(1)
                        if len(attempts) == 1:
                            return OperationResult.failure("transport_unavailable", "flaky")
                        return OperationResult.success(SimpleNamespace(to_dict=lambda: {"downloaded": 2}))

                    record = jobs.run(automation=rule.name, action="backup", device=event.device,
                                      correlation_id=event.correlation_id, call=call)
                    return StepOutcome("backup", record.ok, record.state, {"job_id": record.id, "job_correlation": record.correlation_id})
                return StepOutcome(step.action, True, "ok")

            rule = parse_automation({"name": "bk", "when": "device.connected",
                                     "do": [{"action": "backup"}, {"action": "notify-desktop", "message": "{downloaded}"}]})
            engine = Engine([rule], step_runner)
            event = Event("device.connected", "phone", provider="adb", data={"address": "S1"})
            firing = engine.handle(event)[0]

            audit_path = str(Path(directory) / "audit.jsonl")
            writer = AuditWriter(audit_path)
            writer.firing(firing)
            writer.close()
            record = list(read_audit(audit_path))[-1]

        self.assertEqual(len(attempts), 2)  # one retry
        self.assertEqual(firing.correlation_id, event.event_id)
        self.assertEqual(firing.outcomes[0].data["job_correlation"], event.event_id)
        self.assertEqual(seen_context["correlation_id"], event.event_id)
        self.assertEqual(seen_context["event_id"], event.event_id)
        self.assertEqual(record["correlation_id"], event.event_id)
        self.assertEqual(record["details"]["event"]["event_id"], event.event_id)
        self.assertEqual(record["rule"], "bk")

    def test_duplicate_job_record_carries_the_requesting_chain(self):
        import threading, time

        with tempfile.TemporaryDirectory() as directory:
            jobs = JobRunner(str(Path(directory) / "jobs"), retry_delays=())
            gate = threading.Event()
            thread = threading.Thread(target=lambda: jobs.run(automation="a", action="backup", device="phone", correlation_id="root-1",
                                                              call=lambda c, p: (gate.wait(5), OperationResult.success(SimpleNamespace(to_dict=lambda: {})))[1]))
            thread.start(); time.sleep(0.05)
            skipped = jobs.run(automation="b", action="backup", device="phone", correlation_id="root-2", call=lambda c, p: None)
            gate.set(); thread.join(5)
        self.assertEqual((skipped.state, skipped.correlation_id), ("skipped", "root-2"))


if __name__ == "__main__":
    unittest.main()
