"""`linkplane watch` through the CLI with a fake observer and fake actions."""

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from linkplane.cli import main
from linkplane.commands.watch import parse_condition
from linkplane.core.automation import StepOutcome
from linkplane.core.events import Event


class Scripted:
    def __init__(self, sink, **kwargs):
        self.sink = sink

    def run(self):
        self.sink(Event("observer.started", "*", data={"sources": [], "interval": 1.0}))
        self.sink(Event("battery.low", "phone", provider="adb", data={"level": 30, "threshold": 40, "initial": True}))
        self.sink(Event("battery.low", "phone", provider="adb", data={"level": 12, "threshold": 20}))
        self.sink(Event("battery.low", "tablet", provider="adb", data={"level": 3, "threshold": 20}))
        self.sink(Event("observer.stopped", "*"))


def cli(argv, steps):
    def fake_step(step, event, context, rule, **kwargs):
        steps.append((step.action, dict(context), rule.allow))
        return StepOutcome(step.action, True, f"fake {step.action}")

    out, err = StringIO(), StringIO()
    with patch("linkplane.commands.watch.Observer", Scripted), patch(
        "linkplane.commands.watch.daemond.is_running", return_value=False
    ), patch("linkplane.commands.watch.actions.run_step", fake_step), patch(
        "linkplane.cli.profile_identities", return_value={}
    ), redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class ConditionParsingTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_condition("level<20"), ("level", {"below": 20}))
        self.assertEqual(parse_condition("level > 80"), ("level", {"above": 80}))
        self.assertEqual(parse_condition("ssid=Home Net"), ("ssid", "Home Net"))
        self.assertEqual(parse_condition("status!=full"), ("status", {"not": "full"}))
        self.assertEqual(parse_condition("powered_by in usb,ac"), ("powered_by", {"in": ["usb", "ac"]}))
        self.assertEqual(parse_condition("ok=true"), ("ok", True))

    def test_bad_condition_is_a_request_error(self):
        from linkplane.core import errors

        with self.assertRaises(errors.LinkplaneError) as raised:
            parse_condition("???")
        self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)


class WatchCommandTests(unittest.TestCase):
    def test_fires_only_on_matching_non_initial_events(self):
        steps = []
        code, out, err = cli(["watch", "battery.low", "--if", "level<15", "--notify", "Low: {level}%"], steps)
        self.assertEqual(code, 0)
        self.assertEqual([s[0] for s in steps], ["notify-desktop", "notify-desktop"])  # phone 12 and tablet 3; initial 30 skipped
        self.assertIn("✓ notify-desktop", out)
        self.assertIn("Watching battery.low", err)
        self.assertIn("no daemon running", err)

    def test_device_filter_and_run_consent(self):
        steps = []
        code, _out, _ = cli(["watch", "battery.low", "--device", "tablet", "--run", "~/x.sh"], steps)
        self.assertEqual(code, 0)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][2], ("run",))  # --run implies allow
        self.assertEqual(steps[0][1]["device"], "tablet")

    def test_backup_flag_adds_a_job_step(self):
        steps = []
        cli(["watch", "battery.low", "--backup", "~/Pictures/Auto"], steps)
        self.assertEqual([s[0] for s in steps], ["backup", "backup"])

    def test_on_initial_includes_opening_observation(self):
        steps = []
        cli(["watch", "battery.low", "--on-initial", "--notify", "x"], steps)
        self.assertEqual(len(steps), 3)

    def test_unknown_event_type_is_a_coded_error(self):
        out, err = StringIO(), StringIO()
        with patch("linkplane.cli.profile_identities", return_value={}), redirect_stdout(out), redirect_stderr(err):
            code = main(["watch", "phone.connected"])
        self.assertEqual(code, 1)
        self.assertIn("LP-REQUEST-001", err.getvalue())
        self.assertIn("device.connected", err.getvalue())


if __name__ == "__main__":
    unittest.main()
