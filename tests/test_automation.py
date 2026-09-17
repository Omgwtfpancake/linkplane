"""Rule schema, matching, placeholders, cooldown, and the engine -- no I/O."""

import unittest

from linkplane.core import errors
from linkplane.core.automation import (
    Automation,
    Engine,
    Step,
    StepOutcome,
    condition_holds,
    fill,
    matches,
    parse_automation,
    parse_automations,
)
from linkplane.core.events import Event


def rule(**overrides):
    record = {"name": "r", "when": "battery.low", "do": [{"action": "notify-desktop", "message": "hi"}]}
    record.update(overrides)
    return parse_automation(record)


class ParseTests(unittest.TestCase):
    def test_full_record_round_trips(self):
        parsed = parse_automation({
            "name": "home", "when": "wifi.connected", "device": "phone", "if": {"ssid": "Home"},
            "do": [{"action": "run", "command": "~/x.sh", "timeout": 5}], "allow": ["run"],
            "cooldown_seconds": 60, "on_initial": True, "enabled": False,
        })
        self.assertEqual(parsed.do, (Step("run", {"command": "~/x.sh", "timeout": 5}),))
        self.assertEqual(parsed.to_dict()["if"], {"ssid": "Home"})
        self.assertEqual(parsed.to_dict()["do"], [{"action": "run", "command": "~/x.sh", "timeout": 5}])
        self.assertEqual(parsed.blocked_actions, ())
        self.assertFalse(parsed.enabled)

    def test_privileged_action_without_allow_is_blocked_not_rejected(self):
        parsed = rule(do=[{"action": "run", "command": "x"}])
        self.assertEqual(parsed.blocked_actions, ("run",))

    def test_invalid_records_are_coded_config_errors(self):
        for bad, fragment in [
            ({"when": "battery.low", "do": [{"action": "run"}]}, "without a name"),
            ({"name": "a", "when": "phone.connected", "do": [{"action": "run"}]}, "unknown event type"),
            ({"name": "a", "when": "battery.low", "do": []}, "non-empty"),
            ({"name": "a", "when": "battery.low", "do": [{"action": "dance"}]}, "unknown action"),
            ({"name": "a", "when": "battery.low", "do": [{"action": "run"}], "if": {"level": {"lt": 3}}}, "must be a value or one of"),
            ({"name": "a", "when": "battery.low", "do": [{"action": "run"}], "allow": ["dance"]}, "known actions"),
            ({"name": "a", "when": "battery.low", "do": [{"action": "run"}], "cooldown_seconds": "soon"}, "number"),
        ]:
            with self.subTest(bad=bad), self.assertRaises(errors.LinkplaneError) as raised:
                parse_automation(bad)
            self.assertEqual(raised.exception.code, errors.CONFIG_INVALID)
            self.assertIn(fragment, str(raised.exception))

    def test_file_level_validation(self):
        good = {"schema_version": 1, "automations": [rule().to_dict()]}
        self.assertEqual(len(parse_automations(good)), 1)
        with self.assertRaises(errors.LinkplaneError):
            parse_automations({"schema_version": 2, "automations": []})
        with self.assertRaises(errors.LinkplaneError) as raised:
            parse_automations({"automations": [rule().to_dict(), rule().to_dict()]})
        self.assertIn("duplicate", str(raised.exception))


class MatchTests(unittest.TestCase):
    def test_conditions(self):
        self.assertTrue(condition_holds("Home", "Home"))
        self.assertTrue(condition_holds(19, {"below": 20}))
        self.assertFalse(condition_holds(20, {"below": 20}))
        self.assertTrue(condition_holds(21, {"above": 20}))
        self.assertFalse(condition_holds("nan", {"above": 20}))
        self.assertFalse(condition_holds(True, {"below": 20}))  # bools are not levels
        self.assertTrue(condition_holds("usb", {"in": ["usb", "ac"]}))
        self.assertTrue(condition_holds("full", {"not": "charging"}))

    def test_matching_rules(self):
        low = Event("battery.low", "phone", data={"level": 12, "threshold": 20})
        self.assertTrue(matches(rule(), low))
        self.assertFalse(matches(rule(when="battery.ok"), low))
        self.assertFalse(matches(rule(device="tablet"), low))
        self.assertTrue(matches(rule(device="phone"), low))
        self.assertFalse(matches(rule(enabled=False), low))
        self.assertTrue(matches(rule(**{"if": {"level": {"below": 15}}}), low))
        self.assertFalse(matches(rule(**{"if": {"level": {"below": 10}}}), low))
        self.assertFalse(matches(rule(**{"if": {"missing": 1}}), low))

    def test_initial_observations_do_not_fire_unless_asked(self):
        opening = Event("device.connected", "phone", data={"address": "S1", "initial": True})
        self.assertFalse(matches(rule(when="device.connected"), opening))
        self.assertTrue(matches(rule(when="device.connected", on_initial=True), opening))

    def test_fill_placeholders_leniently(self):
        self.assertEqual(fill("Battery {level}% on {device}", {"level": 9, "device": "phone"}), "Battery 9% on phone")
        self.assertEqual(fill("{unknown} stays", {}), "{unknown} stays")
        self.assertEqual(fill("literal { brace", {}), "literal { brace")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.now = [100.0]

        def runner(step, event, context, automation):
            self.calls.append((automation.name, step.action, dict(context)))
            ok = step.options.get("fail") is not True
            return StepOutcome(step.action, ok, "ran", {"exit_code": 0 if ok else 1})

        self.runner = runner

    def engine(self, *rules):
        return Engine(list(rules), self.runner, clock=lambda: self.now[0])

    def test_steps_run_in_order_and_context_accumulates(self):
        two = rule(do=[{"action": "notify-desktop", "message": "a"}, {"action": "notify-phone", "message": "b {exit_code}"}])
        firings = self.engine(two).handle(Event("battery.low", "phone", data={"level": 5}))
        self.assertEqual(len(firings), 1)
        self.assertTrue(firings[0].ok)
        self.assertEqual([call[1] for call in self.calls], ["notify-desktop", "notify-phone"])
        self.assertEqual(self.calls[1][2]["exit_code"], 0)  # previous outcome's data is visible
        self.assertEqual(self.calls[0][2]["level"], 5)
        self.assertEqual(self.calls[0][2]["device"], "phone")

    def test_failure_stops_unless_continue_on_error(self):
        steps = [{"action": "notify-desktop", "fail": True}, {"action": "notify-phone"}]
        self.engine(rule(do=steps)).handle(Event("battery.low", "phone"))
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.engine(rule(do=steps, continue_on_error=True)).handle(Event("battery.low", "phone"))
        self.assertEqual(len(self.calls), 2)

    def test_cooldown_skips_repeat_triggers(self):
        engine = self.engine(rule(cooldown_seconds=60))
        event = Event("battery.low", "phone")
        first = engine.handle(event)
        self.now[0] += 10
        second = engine.handle(event)
        self.now[0] += 60
        third = engine.handle(event)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(second[0].outcomes[0].skipped)
        self.assertFalse(first[0].outcomes[0].skipped)
        self.assertFalse(third[0].outcomes[0].skipped)

    def test_step_condition_over_an_earlier_result_skips_without_failing(self):
        def backup_runner(step, event, context, automation):
            self.calls.append((automation.name, step.action, dict(context)))
            return StepOutcome(step.action, True, "ran", {"downloaded": event.data["copied"]})

        steps = [{"action": "backup"}, {"action": "notify-desktop", "message": "{downloaded} new", "if": {"downloaded": {"above": 0}}}]
        engine = Engine([rule(when="device.connected", do=steps)], backup_runner)
        nothing_new = engine.handle(Event("device.connected", "phone", data={"copied": 0}))[0]
        self.assertEqual([call[1] for call in self.calls], ["backup"])
        self.assertTrue(nothing_new.ok, "an unmet step condition is not a failure")
        self.assertFalse(nothing_new.skipped, "the backup ran, so the firing is not a skip")
        self.assertTrue(nothing_new.outcomes[1].skipped)
        self.assertEqual(nothing_new.outcomes[1].detail, "condition not met: downloaded")
        self.calls.clear()
        something_new = engine.handle(Event("device.connected", "phone", data={"copied": 3}))[0]
        self.assertEqual([call[1] for call in self.calls], ["backup", "notify-desktop"])
        self.assertEqual(self.calls[1][2]["downloaded"], 3)
        self.assertFalse(something_new.outcomes[1].skipped)

    def test_step_condition_is_not_passed_to_the_action_and_round_trips(self):
        parsed = rule(do=[{"action": "notify-desktop", "message": "m", "if": {"downloaded": {"above": 0}}}])
        self.assertEqual(parsed.do[0].options, {"message": "m"})
        self.assertEqual(parsed.do[0].conditions, {"downloaded": {"above": 0}})
        self.assertEqual(parsed.to_dict()["do"], [{"action": "notify-desktop", "message": "m", "if": {"downloaded": {"above": 0}}}])
        self.assertEqual(parse_automation(parsed.to_dict()), parsed)
        with self.assertRaises(errors.LinkplaneError) as raised:
            rule(do=[{"action": "notify-desktop", "if": {"downloaded": {"gt": 0}}}])
        self.assertIn("must be a value or one of", str(raised.exception))
        with self.assertRaises(errors.LinkplaneError):
            rule(do=[{"action": "notify-desktop", "if": "downloaded"}])

    def test_preset_marker_round_trips_and_hand_written_rules_keep_their_shape(self):
        self.assertNotIn("preset", rule().to_dict())
        marked = rule(preset="photo-backup")
        self.assertEqual(marked.preset, "photo-backup")
        self.assertEqual(parse_automation(marked.to_dict()), marked)
        with self.assertRaises(errors.LinkplaneError):
            rule(preset=7)

    def test_on_error_steps_run_once_after_a_failure_with_the_failure_in_context(self):
        failing = rule(do=[{"action": "notify-desktop", "fail": True}, {"action": "notify-phone"}],
                       on_error=[{"action": "notify-phone", "message": "failed: {failed_action}", "fail": True}])
        firing = self.engine(failing).handle(Event("battery.low", "phone"))[0]
        self.assertEqual([call[1] for call in self.calls], ["notify-desktop", "notify-phone"])
        self.assertEqual(self.calls[1][2]["failed_action"], "notify-desktop")
        self.assertFalse(firing.ok)
        self.assertEqual([o.ok for o in firing.outcomes], [False, False], "the original failure stays first; a failing notice never re-enters on_error")
        self.calls.clear()
        self.engine(rule(on_error=[{"action": "notify-phone"}])).handle(Event("battery.low", "phone"))
        self.assertEqual([call[1] for call in self.calls], ["notify-desktop"], "no failure, no on_error")

    def test_non_matching_event_yields_no_firing(self):
        self.assertEqual(self.engine(rule()).handle(Event("battery.ok", "phone")), [])

    def test_firing_to_dict(self):
        firing = self.engine(rule()).handle(Event("battery.low", "phone", data={"level": 3}))[0]
        record = firing.to_dict()
        self.assertEqual(record["automation"], "r")
        self.assertEqual(record["event"]["type"], "battery.low")
        self.assertEqual(record["outcomes"][0]["action"], "notify-desktop")


if __name__ == "__main__":
    unittest.main()
