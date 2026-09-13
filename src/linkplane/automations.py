"""Rules file and audit log for daemon-hosted automations (docs/automation-design.md §6, §10).

Rules live in `~/.config/linkplane/automations.json` (`LINKPLANE_AUTOMATIONS` overrides)
and are parsed by `core.automation`. The audit log is JSON Lines under
`$XDG_STATE_HOME/linkplane/audit.jsonl` (`LINKPLANE_AUDIT` overrides), one line per
load, block, firing, or skip, written only by the daemon.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from linkplane.core import errors
from linkplane.core.automation import Automation, Firing, parse_automations
from linkplane.core.events import new_id, now_iso
from linkplane.jobs import JobRecord
from linkplane.transports import BridgeError, load_config

AUDIT_SCHEMA = "linkplane.audit/2"  # 2: one structured shape (refinement pass); readers accept 1

# `decision` vocabulary: what the entry concluded.
DECISIONS = ("started", "stopped", "loaded", "blocked", "error", "fired", "skipped", "failed",
             "retrying", "completed", "cancelled")


def resolve_automations_path(path: str | None = None) -> Path:
    from linkplane.paths import config_file

    return config_file("automations.json", path, env_name="AUTOMATIONS")


def resolve_audit_path(path: str | None = None) -> Path:
    from linkplane.paths import state_file

    return state_file("audit.jsonl", path, env_name="AUDIT")


@dataclass(frozen=True)
class LoadedRules:
    path: str
    active: tuple[Automation, ...]
    blocked: tuple[tuple[str, tuple[str, ...]], ...]  # (name, blocked actions)
    # The blocked rules themselves (additive; the API projects them for inspection).
    blocked_rules: tuple[Automation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "loaded": len(self.active),
            "active": [rule.name for rule in self.active],
            "blocked": {name: list(actions) for name, actions in self.blocked},
        }


def load_rules(path: str | None = None) -> LoadedRules:
    """Parse the rules file; a missing file means no rules. Blocked rules are set aside."""
    file = resolve_automations_path(path)
    if not file.exists():
        return LoadedRules(str(file), (), ())
    try:
        config = load_config(str(file))
    except BridgeError as error:
        raise errors.LinkplaneError(errors.CONFIG_INVALID, str(error)) from error
    rules = parse_automations(config)
    active = tuple(rule for rule in rules if not rule.blocked_actions)
    blocked_rules = tuple(rule for rule in rules if rule.blocked_actions)
    blocked = tuple((rule.name, rule.blocked_actions) for rule in blocked_rules)
    return LoadedRules(str(file), active, blocked, blocked_rules)


class AuditWriter:
    """Append-only, one structured shape per line (docs/refinement-pass-brief.md §6):

        schema, audit_id, ts, kind, actor, source, decision,
        device?, rule?, job_id?, action?, correlation_id?, details{}

    Optional keys are omitted when unknown rather than written as null.
    """

    def __init__(self, path: str | None = None, *, source: str = "daemon"):
        self.path = resolve_audit_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.source = source
        self._handle = self.path.open("a", encoding="utf-8")

    def write(
        self,
        kind: str,
        *,
        decision: str,
        actor: str = "daemon",
        device: str | None = None,
        rule: str | None = None,
        job_id: str | None = None,
        action: str | None = None,
        correlation_id: str | None = None,
        source: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ValueError(f"unknown audit decision: {decision}")
        record: dict[str, Any] = {
            "schema": AUDIT_SCHEMA, "audit_id": new_id(), "ts": now_iso(), "kind": kind,
            "actor": actor, "source": source or self.source, "decision": decision,
        }
        for key, value in (("device", device), ("rule", rule), ("job_id", job_id),
                           ("action", action), ("correlation_id", correlation_id)):
            if value is not None:
                record[key] = value
        record["details"] = details
        self._handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._handle.flush()
        return record

    def firing(self, firing: Firing) -> dict[str, Any]:
        if firing.skipped:
            kind, decision = "rule.skipped", "skipped"
        elif firing.ok:
            kind, decision = "rule.fired", "fired"
        else:
            kind, decision = "rule.fired", "failed"
        return self.write(
            kind, decision=decision, actor=f"rule:{firing.automation}", device=firing.event.device,
            rule=firing.automation, correlation_id=firing.correlation_id,
            event=firing.event.to_dict(), ok=firing.ok,
            outcomes=[outcome.to_dict() for outcome in firing.outcomes],
        )

    def job(self, record: "JobRecord") -> dict[str, Any]:
        """One entry per job state change: job.started / job.retrying / job.<terminal>."""
        details: dict[str, Any] = {"attempt": record.attempt}
        if record.state == "retrying":
            details["progress"] = record.progress
        if record.result is not None:
            details["result"] = record.result
        if record.error is not None:
            details["error"] = record.error
        state = "started" if record.state == "running" and record.attempt == 1 and record.finished is None else record.state
        if state == "running":
            state = "started"  # a resumed attempt after a retry wait
        actor = record.actor or f"rule:{record.automation}"
        return self.write(
            f"job.{state}", decision=state, actor=actor, device=record.device,
            rule=record.automation or None, job_id=record.id, action=record.action,
            correlation_id=record.correlation_id,
            source="api" if actor.startswith("client:") else None, **details,
        )

    def close(self) -> None:
        self._handle.close()


def read_audit_reverse(path: str | None = None, *, chunk_size: int = 65536) -> Iterator[dict[str, Any]]:
    """Audit entries newest first, reading the file backwards in chunks so a bounded
    request never loads the whole history (docs/local-api-design.md §21, §22)."""
    file = resolve_audit_path(path)
    if not file.exists():
        return
    with file.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            size = min(chunk_size, position)
            position -= size
            handle.seek(position)
            block = handle.read(size) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]  # may be a partial line; completed by the next (earlier) chunk
            for raw in reversed(lines[1:]):
                record = _parse_audit_line(raw)
                if record is not None:
                    yield record
        record = _parse_audit_line(remainder)
        if record is not None:
            yield record


def _parse_audit_line(raw: bytes) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    try:
        record = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def read_audit(path: str | None = None, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    file = resolve_audit_path(path)
    if not file.exists():
        return
    lines = file.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
