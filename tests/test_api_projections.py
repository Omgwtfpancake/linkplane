"""`linkplane.api.projections` and the reverse audit reader."""

import json
import tempfile
import unittest
from pathlib import Path

from linkplane.api import projections
from linkplane.api.security import REDACTED, redact_record
from linkplane.automations import AuditWriter, load_rules, read_audit, read_audit_reverse
from linkplane.core.events import Event
from linkplane.core.state import CONNECTED, DeviceState
from linkplane.jobs import JobRecord
from linkplane.registry import Registry

CONFIG = {"devices": {"phone": {"device_id": "HW1", "adb": {"serials": ["S1"]}}}}


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.resolve = projections.DeviceIdResolver(Registry(CONFIG, {"phone": DeviceState("phone", "adb", CONNECTED, "S1")}))

    def test_job_projection_uses_the_public_action_and_keeps_the_record(self):
        record = JobRecord("j1", "", "backup", "phone", "running", "t", correlation_id="c", actor="client:x")
        projected = projections.job_dict(record, self.resolve)
        self.assertEqual((projected["action"], projected["job_action"], projected["rule"], projected["device"], projected["device_id"]),
                         ("backup.photos", "backup", None, "phone", "HW1"))
        for key, value in record.to_dict().items():
            if key != "action":
                self.assertEqual(projected[key], value)
        rule_job = projections.job_dict(JobRecord("j2", "nightly", "send", "old-name", "completed", "t"), self.resolve)
        self.assertEqual((rule_job["action"], rule_job["rule"], rule_job["device_id"]), ("files.send", "nightly", None))  # unresolvable: null, never guessed

    def test_event_projection_adds_device_id_and_nothing_else_changes(self):
        event = Event("battery.low", "phone", seq=3)
        projected = projections.event_dict(event, self.resolve)
        self.assertEqual(projected["device_id"], "HW1")
        self.assertEqual({k: v for k, v in projected.items() if k != "device_id"}, event.to_dict())
        self.assertIsNone(projections.event_dict(Event("observer.started", "*"), self.resolve)["device_id"])

    def test_audit_projection_redacts_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "audit.jsonl")
            writer = AuditWriter(path)
            writer.write("rule.fired", decision="fired", actor="rule:r", device="phone", rule="r", action="backup",
                         outcomes=[{"action": "run", "ok": True, "data": {"stdout": "PASSWORD=hunter2", "exit_code": 0}}],
                         event={"data": {"ssid": "HomeNet"}}, note="Bearer abc.def")
            writer.close()
            raw = list(read_audit(path))[0]
            projected = projections.audit_dict(raw, self.resolve)
            self.assertEqual(projected["details"]["outcomes"][0]["data"]["stdout"], REDACTED)
            self.assertEqual(projected["details"]["outcomes"][0]["data"]["exit_code"], 0)
            self.assertEqual(projected["details"]["event"]["data"]["ssid"], "HomeNet")
            self.assertEqual(projected["details"]["note"], f"Bearer {REDACTED}")
            self.assertEqual((projected["action"], projected["job_action"], projected["device_id"]), ("backup.photos", "backup", "HW1"))
            self.assertIn("hunter2", Path(path).read_text())  # the log itself is untouched
            self.assertEqual(raw["details"]["outcomes"][0]["data"]["stdout"], "PASSWORD=hunter2")  # and the source dict is not mutated

    def test_redact_record_covers_nested_sensitive_keys(self):
        self.assertEqual(redact_record({"text": "clip", "nested": [{"Command": "x", "ok": 1}], "s": "token=abc"}),
                         {"text": REDACTED, "nested": [{"Command": REDACTED, "ok": 1}], "s": f"token={REDACTED}"})

    def test_rule_projection_omits_run_commands_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(json.dumps({"automations": [
                {"name": "bk", "when": "device.connected", "device": "phone", "if": {"level": {"below": 20}}, "cooldown_seconds": 5,
                 "do": [{"action": "backup", "destination": "~/Pictures"}, {"action": "run", "command": "curl -H 'Authorization: Bearer S' https://x", "timeout": 5}],
                 "allow": ["run"]},
                {"name": "blocked", "when": "battery.low", "do": [{"action": "run", "command": "secret"}]},
            ]}))
            loaded = load_rules(str(path))
        projected = projections.rules_dict(loaded, fired=2, resolve=self.resolve)
        self.assertNotIn("path", projected)
        self.assertEqual((projected["loaded"], projected["active"], projected["blocked"], projected["fired"]), (1, ["bk"], {"blocked": ["run"]}, 2))
        by_name = {rule["name"]: rule for rule in projected["rules"]}
        bk = by_name["bk"]
        self.assertEqual((bk["state"], bk["device"], bk["device_id"], bk["if"], bk["cooldown_seconds"], bk["allow"]), ("active", "phone", "HW1", {"level": {"below": 20}}, 5, ["run"]))
        self.assertEqual(bk["do"][0], {"action": "backup", "capability": "backup.photos", "destination": "~/Pictures"})
        self.assertEqual(bk["do"][1], {"action": "run", "privileged": True, "command": REDACTED, "timeout": 5})
        self.assertEqual((by_name["blocked"]["state"], by_name["blocked"]["blocked_actions"]), ("blocked", ["run"]))
        self.assertNotIn("secret", json.dumps(projected))
        self.assertNotIn("Bearer S", json.dumps(projected))


class ReverseAuditReaderTests(unittest.TestCase):
    def test_reads_newest_first_across_chunk_boundaries_and_skips_junk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "audit.jsonl")
            writer = AuditWriter(path)
            for index in range(300):
                writer.write("daemon.started", decision="started", index=index, pad="x" * 200)
            writer.close()
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{not json\n\n")
            forward = [entry["details"]["index"] for entry in read_audit(path)]
            backward = [entry["details"]["index"] for entry in read_audit_reverse(path, chunk_size=777)]
        self.assertEqual(backward, list(reversed(forward)))
        self.assertEqual(len(backward), 300)
        self.assertEqual(list(read_audit_reverse("/nonexistent/audit.jsonl")), [])
