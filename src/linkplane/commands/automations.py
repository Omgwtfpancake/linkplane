"""`linkplane automations list|log|jobs|presets|enable|disable`: the rules file, the daemon's
audit log and job records, and the built-in presets that write ordinary rules."""

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
        preset = f"  (preset {rule.preset})" if rule.preset else ""
        initial = ", including when already connected" if rule.on_initial else ""
        print(f"\n{rule.name}  [{state}]{preset}")
        print(f"When        {rule.when}{device}{conditions}{initial}")
        print(f"Do          {', '.join(_describe_step(step) for step in rule.do)}")
        if rule.cooldown_seconds:
            print(f"Cooldown    {rule.cooldown_seconds:g}s")
    for name, blocked in loaded.blocked:
        print(f"\n{name}  [blocked]")
        print(f"Needs       \"allow\": {json.dumps(list(blocked))}")
    return 0


def _describe_step(step: Any) -> str:
    if not step.conditions:
        return step.action
    return f"{step.action} (if {json.dumps(step.conditions, sort_keys=True)})"


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


def _profile(arguments: Any):
    """The device profile a preset command targets: `--device`, else the default."""
    from linkplane.core import errors
    from linkplane.profiles import selected_profile
    from linkplane.transports import BridgeError, load_config

    try:
        profile = selected_profile(load_config(arguments.config), arguments.device)
    except BridgeError as error:
        raise errors.LinkplaneError(errors.DEVICE_NOT_FOUND, str(error), ("see: linkplane profiles list",)) from error
    if profile is None:
        raise errors.LinkplaneError(errors.CONFIG_MISSING, "no device profile to use",
                                    ("run `linkplane setup` first, or pass --device NAME",))
    return profile


def _reload_daemon(socket: str | None) -> str:
    from linkplane import daemon as daemond
    from linkplane.core import errors

    try:
        daemond.request("reload", socket)
    except errors.LinkplaneError as error:
        if error.code == errors.DAEMON_NOT_RUNNING:
            return "The daemon is not running; the change applies when it starts."
        return f"The daemon did not reload ({error}); run: linkplane daemon reload"
    except OSError as error:
        return f"The daemon did not reload ({error}); run: linkplane daemon reload"
    return "The running daemon reloaded its rules."


def list_presets(arguments: Any) -> int:
    from linkplane import presets
    from linkplane.profiles import profiles_from_config
    from linkplane.transports import BridgeError, load_config

    try:
        devices = list(profiles_from_config(load_config(arguments.config)))
    except BridgeError:
        devices = []
    rows = presets.summaries(arguments.file, jobs_dir=arguments.jobs_dir, devices=devices)
    preset = presets.PRESETS[presets.PHOTO_BACKUP]
    if arguments.json:
        print_envelope(True, {"presets": [{"name": preset.name, "title": preset.title, "summary": " ".join(preset.summary),
                                           "devices": rows}]})
        return 0
    print("Linkplane Presets")
    print(f"\n{preset.name}  {preset.title}")
    for line in preset.summary:
        print(f"  {line}")
    if not rows:
        print("\n  No device profiles yet; run `linkplane setup` first.")
    for row in rows:
        print(f"\n  Device      {row['device']}")
        print(f"  State       {row['state']}")
        if row["destination"]:
            print(f"  Folder      {row['destination']}")
            print(f"  Last run    {row['last_run_summary']}")
            print(f"  Rule        {row['rule']} (shown by `linkplane automations list`)")
    print(f"\nTurn on:  linkplane automations enable {preset.name} [--device NAME] [--destination DIR]")
    print(f"Turn off: linkplane automations disable {preset.name} [--device NAME]")
    return 0


def change_preset(arguments: Any) -> int:
    from linkplane import presets
    from linkplane.core import errors

    if arguments.preset not in presets.PRESETS:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"unknown preset {arguments.preset!r}",
                                    (f"available: {', '.join(presets.PRESETS)}",))
    profile = _profile(arguments)
    if arguments.automations_action == "enable":
        change = presets.enable_photo_backup(profile.name, arguments.destination, device_id=profile.device_id,
                                             path=arguments.file, dry_run=arguments.dry_run)
    else:
        change = presets.disable_photo_backup(profile.name, path=arguments.file, dry_run=arguments.dry_run)
    reload_note = None
    if change.changed and not arguments.dry_run:
        reload_note = _reload_daemon(arguments.socket)
    if arguments.json:
        print_envelope(True, {**change.to_dict(), "daemon": reload_note})
        return 0
    destination = presets.rule_destination(change.rule)
    verb = {"created": "is now on", "enabled": "is now on", "updated": "is now on", "disabled": "is now off",
            "unchanged": "was already " + ("on" if change.rule.get("enabled", True) else "off")}[change.change]
    prefix = "Would change: " if arguments.dry_run and change.changed else ""
    print(f"{prefix}Automatic photo backup {verb} for {profile.name}.")
    print(f"Folder      {destination}")
    print(f"Rule        {change.rule['name']} in {change.path}")
    if change.rule.get("enabled", True):
        print("New photos and videos are copied each time this phone connects; nothing on the phone is changed,")
        print("and deleting photos from the phone never deletes these backups.")
        if change.changed and not arguments.dry_run:
            print("It runs the next time the phone connects (or when the daemon starts with it connected).")
            print(f"To back up right now instead: linkplane backup {destination} --device {profile.name}")
    else:
        print("Photos already backed up stay where they are.")
    if reload_note:
        print(reload_note)
    return 0


def run(arguments: Any) -> int:
    if arguments.automations_action == "list":
        return list_rules(arguments)
    if arguments.automations_action == "jobs":
        return list_jobs(arguments)
    if arguments.automations_action == "presets":
        return list_presets(arguments)
    if arguments.automations_action in ("enable", "disable"):
        return change_preset(arguments)
    return log_audit(arguments)
