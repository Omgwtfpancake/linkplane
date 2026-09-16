"""Public projections of pinned records (design §6.1, §10, §11, §22; Slice 2).

A projection adds what an API client needs — the public action name, the canonical
`device_id`, redaction — without changing the pinned record it wraps. The pinned keys
stay exactly as written to disk; every added key is documented in `docs/openapi.json`.
"""

from __future__ import annotations

from typing import Any, Mapping

from linkplane.api.actions import public_action
from linkplane.api.security import REDACTED, redact_record
from linkplane.automations import LoadedRules
from linkplane.core.automation import PRIVILEGED, Automation
from linkplane.core.events import Event
from linkplane.jobs import JobRecord
from linkplane.registry import Registry


class DeviceIdResolver:
    """Registry name -> canonical `device_id`, memoised for one request or stream.

    Pinned records carry the registry *name* in their `device` field. When the name still
    resolves, the projection adds `device_id`; when it does not (a renamed or removed
    profile in an old record, the observer's `*`), `device_id` is null — never guessed.
    """

    def __init__(self, registry: Registry | None):
        self.registry = registry
        self._cache: dict[str, str | None] = {}

    def __call__(self, name: str | None) -> str | None:
        if not name or name == "*" or self.registry is None:
            return None
        if name not in self._cache:
            device = self.registry.resolve(name)
            self._cache[name] = device.device_id if device is not None else None
        return self._cache[name]


def job_dict(record: JobRecord, resolve: DeviceIdResolver | None = None) -> dict[str, Any]:
    """`JobRecord.to_dict()` with the public vocabulary on top.

    `action` is the **public** capability name (`backup.photos`); the record's own
    `action` (`backup`, the rules/jobs vocabulary) is exposed as `job_action`. `rule` is
    the preferred alias of `automation` (null for API-started jobs). `device_id` is the
    canonical id resolved from the record's `device` name, or null.
    """
    raw = record.to_dict()
    return {
        **raw,
        "action": public_action(record.action) or record.action,
        "job_action": record.action,
        "rule": record.automation or None,
        "device_id": resolve(record.device) if resolve is not None else None,
    }


def event_dict(event: Event, resolve: DeviceIdResolver | None = None) -> dict[str, Any]:
    """`Event.to_dict()` plus `device_id` (null for `*` and unresolvable names)."""
    payload = event.to_dict()
    payload["device_id"] = resolve(event.device) if resolve is not None else None
    return payload


def audit_dict(entry: Mapping[str, Any], resolve: DeviceIdResolver | None = None) -> dict[str, Any]:
    """An audit line as written, redacted for HTTP, plus `device_id` and the public `action`
    when the entry's `action` is a job-action name. The file itself is never changed."""
    projected = redact_record(dict(entry))
    action = entry.get("action")
    if isinstance(action, str) and public_action(action):
        projected["action"] = public_action(action)
        projected["job_action"] = action
    projected["device_id"] = resolve(entry.get("device")) if resolve is not None else None
    return projected


def _step_dict(step_action: str, options: Mapping[str, Any], conditions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A rule step for inspection. `run` command text is intentionally omitted: it may
    embed credentials or paths the operator never meant to publish; only its presence,
    its timeout, and the public name of what it does are shown."""
    if step_action in PRIVILEGED:
        return {
            "action": step_action,
            "privileged": True,
            "command": REDACTED,
            "timeout": options.get("timeout", 300),
        }
    projected = redact_record(dict(options))
    if conditions:
        projected["if"] = redact_record(dict(conditions))  # a step-level condition (v0.6)
    return {"action": step_action, "capability": public_action(step_action), **projected}


def rule_dict(rule: Automation, *, state: str, blocked_actions: tuple[str, ...] = (),
              resolve: DeviceIdResolver | None = None) -> dict[str, Any]:
    return {
        "name": rule.name,
        "state": state,
        "enabled": rule.enabled,
        "when": rule.when,
        "device": rule.device,
        "device_id": resolve(rule.device) if resolve is not None and rule.device else None,
        "if": redact_record(dict(rule.conditions)),
        "cooldown_seconds": rule.cooldown_seconds,
        "on_initial": rule.on_initial,
        "continue_on_error": rule.continue_on_error,
        "allow": list(rule.allow),
        "blocked_actions": list(blocked_actions),
        "do": [_step_dict(step.action, step.options, step.conditions) for step in rule.do],
        "preset": rule.preset,
    }


def rules_dict(loaded: LoadedRules, *, fired: int, resolve: DeviceIdResolver | None = None) -> dict[str, Any]:
    """`LoadedRules.to_dict()` without the filesystem path, plus the projected rules."""
    blocked = dict(loaded.blocked)
    rules = [rule_dict(rule, state="active", resolve=resolve) for rule in loaded.active]
    rules += [rule_dict(rule, state="blocked", blocked_actions=blocked.get(rule.name, ()), resolve=resolve)
              for rule in loaded.blocked_rules]
    return {
        "loaded": len(loaded.active),
        "active": [rule.name for rule in loaded.active],
        "blocked": {name: list(actions) for name, actions in loaded.blocked},
        "fired": fired,
        "rules": rules,
    }
