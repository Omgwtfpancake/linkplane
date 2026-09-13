"""`linkplane watch`: one automation built from flags, run in the foreground."""

from __future__ import annotations

import re
import sys
from typing import Any

from linkplane import actions
from linkplane import daemon as daemond
from linkplane.core import errors
from linkplane.core.automation import Automation, Engine, Firing, Step, parse_automation
from linkplane.core.events import EVENT_TYPES, Event
from linkplane.jobs import JobRunner
from linkplane.observe import Observer
from linkplane.operations import CancellationToken, cancel_on_interrupt

_CONDITION = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op><=|>=|!=|=|<|>| in )\s*(?P<value>.+)$")


def _literal(text: str) -> Any:
    text = text.strip()
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text.strip("\"'")


def parse_condition(text: str) -> tuple[str, Any]:
    """`level<20`, `ssid=Home`, `status!=full`, `powered_by in usb,ac` -> (key, condition)."""
    match = _CONDITION.match(text.strip())
    if match is None:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"cannot parse condition {text!r} (try key=value, key<n, key>n, key!=value, key in a,b)")
    key, op, value = match.group("key"), match.group("op").strip(), match.group("value")
    if op == "=":
        return key, _literal(value)
    if op == "!=":
        return key, {"not": _literal(value)}
    if op == "<":
        return key, {"below": _literal(value)}
    if op == ">":
        return key, {"above": _literal(value)}
    if op == "in":
        return key, {"in": [_literal(part) for part in value.split(",")]}
    raise errors.LinkplaneError(errors.REQUEST_INVALID, f"unsupported operator {op!r} in {text!r}")


def build_rule(arguments: Any) -> Automation:
    steps: list[dict[str, Any]] = []
    for message in arguments.notify or ():
        steps.append({"action": "notify-desktop", "message": message, "urgency": arguments.urgency})
    for message in arguments.notify_phone or ():
        steps.append({"action": "notify-phone", "message": message})
    for command in arguments.run or ():
        steps.append({"action": "run", "command": command})
    for destination in getattr(arguments, "backup", None) or ():
        steps.append({"action": "backup", "destination": destination})
    if not steps:
        steps.append({"action": "notify-desktop", "message": f"{{device}}: {arguments.event}"})
    conditions = dict(parse_condition(text) for text in arguments.conditions or ())
    return parse_automation(
        {
            "name": "watch",
            "when": arguments.event,
            "device": arguments.devices[0] if arguments.devices else None,
            "if": conditions,
            "do": steps,
            "allow": ["run"] if arguments.run else [],  # the flag is the consent
            "cooldown_seconds": arguments.cooldown,
            "on_initial": arguments.on_initial,
            "continue_on_error": True,
        }
    )


def render(firing: Firing) -> str:
    lines = [f"{firing.event.ts[11:19]}  {firing.event.type:<20} {firing.event.device}"]
    for outcome in firing.outcomes:
        mark = "·" if outcome.skipped else ("✓" if outcome.ok else "✗")
        lines.append(f"          {mark} {outcome.action:<15} {outcome.detail}")
    return "\n".join(lines)


def run(arguments: Any, *, identities: dict[str, str] | None = None) -> int:
    if arguments.event not in EVENT_TYPES:
        raise errors.LinkplaneError(
            errors.REQUEST_INVALID, f"unknown event type {arguments.event!r}", (f"one of: {', '.join(EVENT_TYPES)}",)
        )
    rule = build_rule(arguments)
    jobs = JobRunner()
    engine = Engine([rule], lambda step, event, context, automation: actions.run_step(step, event, context, automation, jobs=jobs))
    print(f"Watching {rule.when}" + (f" on {rule.device}" if rule.device else "") + (f" if {rule.conditions}" if rule.conditions else "") + f"; do: {', '.join(step.action for step in rule.do)}", file=sys.stderr, flush=True)

    def handle(event: Event) -> None:
        for firing in engine.handle(event):
            print(render(firing), flush=True)

    with cancel_on_interrupt(CancellationToken()) as cancel:
        if daemond.is_running(getattr(arguments, "socket", None)):
            for event in daemond.subscribe(getattr(arguments, "socket", None), types=(rule.when,), cancel=cancel):
                handle(event)
        else:
            print("no daemon running; observing in this process", file=sys.stderr, flush=True)
            Observer(
                handle, identities=identities, interval=arguments.interval, low_battery=arguments.low_battery,
                cancel=cancel, poll_wifi_enabled=not arguments.no_wifi,
            ).run()
    print("Stopped.", file=sys.stderr)
    return 0
