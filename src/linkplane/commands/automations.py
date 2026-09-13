"""`linkplane automations list|log`: the rules file and the daemon's audit log."""

from __future__ import annotations

import json
import time
from typing import Any

from linkplane.automations import load_rules, read_audit, resolve_audit_path
from linkplane.commands import print_envelope


def list_rules(arguments: Any) -> int:
    loaded = load_rules(arguments.file)
    if arguments.json:
        print_envelope(True, {**loaded.to_dict(), "automations": [rule.to_dict() for rule in loaded.active]})
        return 0
    print("Linkplane Automations")
    print(f"File        {loaded.path}")
    if not loaded.active and not loaded.blocked:
        print("No automations defined")
        return 0
    for rule in loaded.active:
        state = "enabled" if rule.enabled else "disabled"
        conditions = f" if {rule.conditions}" if rule.conditions else ""
        device = f" on {rule.device}" if rule.device else ""
        print(f"\n{rule.name}  [{state}]")
        print(f"When        {rule.when}{device}{conditions}")
        print(f"Do          {', '.join(step.action for step in rule.do)}")
        if rule.cooldown_seconds:
            print(f"Cooldown    {rule.cooldown_seconds:g}s")
    for name, blocked in loaded.blocked:
        print(f"\n{name}  [blocked]")
        print(f"Needs       \"allow\": {json.dumps(list(blocked))}")
    return 0


def render_audit(record: dict[str, Any]) -> str:
    clock = str(record.get("ts", ""))[11:19]
    kind = record.get("kind", "")
    details = record.get("details") if isinstance(record.get("details"), dict) else record  # v1 lines
    decision = record.get("decision", "")
    if kind.startswith("rule.") and "outcomes" in details:
        event = details.get("event") or {}
        marks = " ".join(
            ("·" if o.get("skipped") else ("✓" if o.get("ok") else "✗")) + o.get("action", "") for o in details["outcomes"]
        )
        return f"{clock}  {kind:<15} {decision:<9} {record.get('rule', details.get('automation', '')):<16} {event.get('type', ''):<20} {marks}"
    if kind.startswith("job."):
        where = ""
        error = (details.get("error") or {}).get("message", "")
        return f"{clock}  {kind:<15} {decision:<9} {record.get('rule', ''):<16} {record.get('action', ''):<14} {record.get('device', '')}  {error}".rstrip()
    rest = {k: v for k, v in details.items() if k not in {"schema", "ts", "kind", "audit_id", "actor", "source", "decision", "details"}}
    return f"{clock}  {kind:<15} {decision:<9} {json.dumps(rest, sort_keys=True)[:90]}"


def log_audit(arguments: Any) -> int:
    path = resolve_audit_path(arguments.file)
    records = list(read_audit(str(path), limit=arguments.lines))
    if arguments.json:
        for record in records:
            print(json.dumps(record, sort_keys=True))
    else:
        print(f"Linkplane Audit ({path})")
        for record in records:
            print(render_audit(record))
    if not arguments.follow:
        return 0
    offset = path.stat().st_size if path.exists() else 0
    try:
        while True:
            time.sleep(0.5)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size <= offset:
                continue
            with path.open(encoding="utf-8") as handle:
                handle.seek(offset)
                for line in handle:
                    if line.strip():
                        record = json.loads(line)
                        print(json.dumps(record, sort_keys=True) if arguments.json else render_audit(record), flush=True)
                offset = handle.tell()
    except KeyboardInterrupt:
        return 0


def list_jobs(arguments: Any) -> int:
    from linkplane.jobs import read_jobs, resolve_jobs_dir

    records = list(read_jobs(arguments.dir, limit=arguments.lines))
    if arguments.json:
        print_envelope(True, {"jobs": [record.to_dict() for record in records]})
        return 0
    print(f"Linkplane Jobs ({resolve_jobs_dir(arguments.dir)})")
    if not records:
        print("No jobs recorded")
    for record in records:
        progress = record.progress or {}
        where = ""
        if progress.get("current") is not None and progress.get("total") is not None:
            where = f" {progress['current']}/{progress['total']} {progress.get('unit', '')}".rstrip()
        detail = (record.error or {}).get("message") if record.error else (progress.get("message") or "")
        print(f"{record.started[11:19]}  {record.state:<10} {record.action:<15} {record.device:<16} {record.automation:<16}{where}  {detail}")
    return 0


def run(arguments: Any) -> int:
    if arguments.automations_action == "list":
        return list_rules(arguments)
    if arguments.automations_action == "jobs":
        return list_jobs(arguments)
    return log_audit(arguments)
