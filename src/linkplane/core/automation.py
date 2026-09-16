"""Automation rules: schema, matching, placeholders, cooldown (docs/automation-design.md §3, §10).

A rule is pure data; `Engine` decides whether an event fires it and hands the steps to an
action runner. Nothing here performs I/O, so the whole decision surface is unit-tested
without a phone or a daemon.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from linkplane.core import errors
from linkplane.core.events import EVENT_TYPES, Event

AUTOMATIONS_SCHEMA_VERSION = 1
OPERATORS = ("below", "above", "in", "not")
# Actions a rule may name. Those needing explicit consent are listed in PRIVILEGED.
ACTIONS = ("notify-desktop", "notify-phone", "run", "backup", "send", "clipboard-sync")
PRIVILEGED = ("run",)


@dataclass(frozen=True)
class Step:
    action: str
    options: dict[str, Any] = field(default_factory=dict)
    # Optional per-step `if`: the same flat condition map as a rule's `if`, tested against
    # the firing context when the step is reached (the event's data plus every earlier
    # step's outcome data), so `{"downloaded": {"above": 0}}` gates a notification on a
    # backup's result. Unmet → the step is skipped, which is not a failure.
    conditions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        record = {"action": self.action, **self.options}
        if self.conditions:
            record["if"] = dict(self.conditions)
        return record


@dataclass(frozen=True)
class Automation:
    name: str
    when: str
    do: tuple[Step, ...]
    device: str | None = None
    conditions: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    allow: tuple[str, ...] = ()
    cooldown_seconds: float = 0.0
    on_initial: bool = False
    continue_on_error: bool = False
    # The built-in preset this rule was generated from (`linkplane.presets`), or None for a
    # hand-written rule. Informational: the engine runs preset rules like any other.
    preset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["if"] = record.pop("conditions")
        record["do"] = [step.to_dict() for step in self.do]
        if record["preset"] is None:
            del record["preset"]  # hand-written rules keep their original shape
        return record

    @property
    def blocked_actions(self) -> tuple[str, ...]:
        """Privileged actions this rule uses without listing them in `allow`."""
        return tuple(
            step.action for step in self.do if step.action in PRIVILEGED and step.action not in self.allow
        )


def _invalid(name: str, message: str) -> errors.LinkplaneError:
    return errors.LinkplaneError(errors.CONFIG_INVALID, f"automation {name!r}: {message}")


def _parse_conditions(name: str, conditions: Any, where: str) -> dict[str, Any]:
    if not isinstance(conditions, Mapping):
        raise _invalid(name, f"{where} must be an object")
    for key, value in conditions.items():
        if isinstance(value, Mapping):
            if len(value) != 1 or next(iter(value)) not in OPERATORS:
                raise _invalid(name, f"condition {key!r} must be a value or one of {OPERATORS}")
    return dict(conditions)


def parse_automation(record: Mapping[str, Any]) -> Automation:
    name = str(record.get("name") or "").strip()
    if not name:
        raise errors.LinkplaneError(errors.CONFIG_INVALID, "automation without a name")
    when = record.get("when")
    if when not in EVENT_TYPES:
        raise _invalid(name, f"unknown event type {when!r}")
    raw_steps = record.get("do")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _invalid(name, "'do' must be a non-empty list of actions")
    steps: list[Step] = []
    for raw in raw_steps:
        if not isinstance(raw, Mapping) or "action" not in raw:
            raise _invalid(name, "each 'do' entry needs an 'action'")
        action = str(raw["action"])
        if action not in ACTIONS:
            raise _invalid(name, f"unknown action {action!r}")
        step_conditions = _parse_conditions(name, raw.get("if") or {}, f"'if' on the {action!r} step")
        steps.append(Step(action, {k: v for k, v in raw.items() if k not in ("action", "if")}, step_conditions))
    conditions = _parse_conditions(name, record.get("if") or {}, "'if'")
    allow = record.get("allow") or ()
    if not isinstance(allow, (list, tuple)) or any(a not in ACTIONS for a in allow):
        raise _invalid(name, "'allow' must list known actions")
    try:
        cooldown = float(record.get("cooldown_seconds", 0) or 0)
    except (TypeError, ValueError):
        raise _invalid(name, "'cooldown_seconds' must be a number") from None
    preset = record.get("preset")
    if preset is not None and not isinstance(preset, str):
        raise _invalid(name, "'preset' must be a string")
    return Automation(
        name=name,
        when=str(when),
        do=tuple(steps),
        device=record.get("device"),
        conditions=dict(conditions),
        enabled=bool(record.get("enabled", True)),
        allow=tuple(allow),
        cooldown_seconds=cooldown,
        on_initial=bool(record.get("on_initial", False)),
        continue_on_error=bool(record.get("continue_on_error", False)),
        preset=preset,
    )


def parse_automations(config: Mapping[str, Any]) -> list[Automation]:
    version = config.get("schema_version", AUTOMATIONS_SCHEMA_VERSION)
    if version != AUTOMATIONS_SCHEMA_VERSION:
        raise errors.LinkplaneError(
            errors.CONFIG_INVALID, f"unsupported automations schema_version {version!r}"
        )
    raw = config.get("automations", [])
    if not isinstance(raw, list):
        raise errors.LinkplaneError(errors.CONFIG_INVALID, "'automations' must be a list")
    rules = [parse_automation(item) for item in raw]
    names = [rule.name for rule in rules]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"duplicate automation names: {', '.join(duplicates)}")
    return rules


def condition_holds(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        operator, operand = next(iter(expected.items()))
        if operator == "in":
            return actual in (operand if isinstance(operand, (list, tuple)) else [operand])
        if operator == "not":
            return actual != operand
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        return actual < operand if operator == "below" else actual > operand
    return actual == expected


def matches(rule: Automation, event: Event) -> bool:
    if not rule.enabled or event.type != rule.when:
        return False
    if rule.device is not None and event.device != rule.device:
        return False
    if event.initial and not rule.on_initial:
        return False
    return all(condition_holds(event.data.get(key), expected) for key, expected in rule.conditions.items())


class _Lenient(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fill(template: str, context: Mapping[str, Any]) -> str:
    """`{name}` placeholders from the context; unknown names are left as written."""
    try:
        return template.format_map(_Lenient(context))
    except (ValueError, IndexError):
        return template


def event_context(event: Event) -> dict[str, Any]:
    return {
        **event.data,
        "type": event.type, "device": event.device, "provider": event.provider or "", "ts": event.ts,
        "event_id": event.event_id, "correlation_id": event.correlation_id,
    }


@dataclass(frozen=True)
class StepOutcome:
    action: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Firing:
    automation: str
    event: Event
    outcomes: tuple[StepOutcome, ...]

    @property
    def ok(self) -> bool:
        return all(outcome.ok for outcome in self.outcomes)

    @property
    def correlation_id(self) -> str | None:
        return self.event.correlation_id

    @property
    def skipped(self) -> bool:
        return all(outcome.skipped for outcome in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "automation": self.automation,
            "rule": self.automation,  # preferred term (docs/refinement-pass-plan.md §7)
            "event": self.event.to_dict(),
            "correlation_id": self.correlation_id,
            "ok": self.ok,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


# Preferred public term for a rule definition; `Automation` is the original class name.
Rule = Automation

ActionRunner = Callable[[Step, Event, Mapping[str, Any], Automation], StepOutcome]


class Engine:
    """Match events against rules, honour cooldowns, run steps in order."""

    def __init__(self, rules: list[Automation], run_step: ActionRunner, *, clock: Callable[[], float] = time.monotonic):
        self.rules = list(rules)
        self.run_step = run_step
        self.clock = clock
        self._last_fired: dict[str, float] = {}

    def replace_rules(self, rules: list[Automation]) -> None:
        self.rules = list(rules)

    def select(self, event: Event) -> list[tuple[Automation, Firing | None]]:
        """Rules the event fires; a cooldown hit yields a ready-made skipped Firing."""
        selected: list[tuple[Automation, Firing | None]] = []
        for rule in self.rules:
            if not matches(rule, event):
                continue
            now = self.clock()
            last = self._last_fired.get(rule.name)
            if last is not None and rule.cooldown_seconds and now - last < rule.cooldown_seconds:
                selected.append((rule, Firing(rule.name, event, (StepOutcome("*", True, f"cooldown: {rule.cooldown_seconds:g}s not elapsed", skipped=True),))))
                continue
            self._last_fired[rule.name] = now
            selected.append((rule, None))
        return selected

    def fire(self, rule: Automation, event: Event) -> Firing:
        """Run one rule's steps in order; the context accumulates each outcome's data."""
        context = event_context(event)
        outcomes: list[StepOutcome] = []
        for step in rule.do:
            unmet = [key for key, expected in step.conditions.items() if not condition_holds(context.get(key), expected)]
            if unmet:
                outcomes.append(StepOutcome(step.action, True, f"condition not met: {', '.join(unmet)}", skipped=True))
                continue
            outcome = self.run_step(step, event, context, rule)
            outcomes.append(outcome)
            context.update(outcome.data)
            if not outcome.ok and not rule.continue_on_error:
                break
        return Firing(rule.name, event, tuple(outcomes))

    def handle(self, event: Event) -> list[Firing]:
        """select + fire, sequentially (the foreground `watch` path)."""
        return [skipped if skipped is not None else self.fire(rule, event) for rule, skipped in self.select(event)]
