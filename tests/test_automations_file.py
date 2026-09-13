import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkplane.automations import AuditWriter, load_rules, read_audit, resolve_audit_path, resolve_automations_path
from linkplane.core import errors
from linkplane.core.automation import Firing, StepOutcome
from linkplane.core.events import Event


RULES = {
    "schema_version": 1,
    "automations": [
        {"name": "low", "when": "battery.low", "do": [{"action": "notify-desktop", "message": "low"}]},
        {"name": "script", "when": "device.connected", "do": [{"action": "run", "command": "x"}]},
        {"name": "ok-script", "when": "device.connected", "do": [{"action": "run", "command": "x"}], "allow": ["run"]},
    ],
}


class LoadRulesTests(unittest.TestCase):
    def test_missing_file_means_no_rules(self):
        loaded = load_rules("/nonexistent/automations.json")
        self.assertEqual((loaded.active, loaded.blocked), ((), ()))

    def test_blocked_rules_are_set_aside_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.json"
            path.write_text(json.dumps(RULES))
            loaded = load_rules(str(path))
        self.assertEqual([r.name for r in loaded.active], ["low", "ok-script"])
        self.assertEqual(loaded.blocked, (("script", ("run",)),))
        self.assertEqual(loaded.to_dict()["blocked"], {"script": ["run"]})

    def test_invalid_json_is_a_coded_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.json"
            path.write_text("{oops")
            with self.assertRaises(errors.LinkplaneError) as raised:
                load_rules(str(path))
        self.assertEqual(raised.exception.code, errors.CONFIG_INVALID)

    def test_paths_follow_environment(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/cfg", "LINKPLANE_AUTOMATIONS": ""}):
            self.assertEqual(resolve_automations_path(), Path("/tmp/cfg/linkplane/automations.json"))
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/st", "LINKPLANE_AUDIT": ""}):
            self.assertEqual(resolve_audit_path(), Path("/tmp/st/linkplane/audit.jsonl"))


class AuditTests(unittest.TestCase):
    def test_writes_one_structured_shape_and_reads_it_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "audit.jsonl")
            writer = AuditWriter(path)
            started = writer.write("daemon.started", decision="started", pid=1)
            fired = Firing("low", Event("battery.low", "phone", data={"level": 3}), (StepOutcome("notify-desktop", True, "ok"),))
            skipped = Firing("low", Event("battery.low", "phone"), (StepOutcome("*", True, "cooldown", skipped=True),))
            failed = Firing("low", Event("battery.low", "phone"), (StepOutcome("run", False, "exit 1"),))
            self.assertEqual((writer.firing(fired)["kind"], writer.firing(fired)["decision"]), ("rule.fired", "fired"))
            self.assertEqual((writer.firing(skipped)["kind"], writer.firing(skipped)["decision"]), ("rule.skipped", "skipped"))
            self.assertEqual(writer.firing(failed)["decision"], "failed")
            with self.assertRaises(ValueError):
                writer.write("x", decision="maybe")
            writer.close()
            records = list(read_audit(path))
            self.assertEqual([r["kind"] for r in read_audit(path, limit=1)], ["rule.fired"])
        self.assertEqual(started["details"], {"pid": 1})
        self.assertEqual(started["actor"], "daemon")
        self.assertEqual(len(started["audit_id"]), 32)
        fired_record = records[1]
        self.assertEqual(fired_record["kind"], "rule.fired")
        self.assertEqual(fired_record["actor"], "rule:low")
        self.assertEqual(fired_record["device"], "phone")
        self.assertEqual(fired_record["rule"], "low")
        self.assertEqual(fired_record["correlation_id"], fired.correlation_id)
        self.assertEqual(fired_record["details"]["event"]["data"]["level"], 3)
        self.assertNotIn("job_id", fired_record)  # omitted, not null

    def test_job_transitions_become_audit_entries(self):
        from linkplane.jobs import JobRecord

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "audit.jsonl")
            writer = AuditWriter(path)
            base = JobRecord("j1", "bk", "backup", "phone", "running", "t", correlation_id="c1")
            self.assertEqual(writer.job(base)["kind"], "job.started")
            retry = writer.job(JobRecord("j1", "bk", "backup", "phone", "retrying", "t", attempt=1, progress={"phase": "retrying"}, correlation_id="c1"))
            done = writer.job(JobRecord("j1", "bk", "backup", "phone", "completed", "t", "t2", attempt=2, result={"downloaded": 1}, correlation_id="c1"))
            writer.close()
        self.assertEqual(done["actor"], "rule:bk")
        self.assertEqual(done["source"], "daemon")
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(str(Path(directory) / "audit.jsonl"))
            api_job = writer.job(JobRecord("j2", "", "backup", "phone", "running", "t", correlation_id="c2", actor="client:gui"))
            explicit = writer.write("action.requested", decision="started", actor="client:gui", source="api", action="battery.read")
            writer.close()
        self.assertEqual((api_job["actor"], api_job["source"], api_job["kind"]), ("client:gui", "api", "job.started"))
        self.assertNotIn("rule", api_job)  # empty rule is omitted, not written as ""
        self.assertEqual((explicit["source"], explicit["actor"], explicit["decision"]), ("api", "client:gui", "started"))
        self.assertEqual((retry["kind"], retry["decision"]), ("job.retrying", "retrying"))
        self.assertEqual((done["kind"], done["decision"], done["job_id"], done["action"], done["correlation_id"]), ("job.completed", "completed", "j1", "backup", "c1"))
        self.assertEqual(done["details"]["result"], {"downloaded": 1})


if __name__ == "__main__":
    unittest.main()
