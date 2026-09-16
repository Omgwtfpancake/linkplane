"""Built-in presets: named, reviewed rules a user switches on instead of writing JSON (v0.6).

A preset is not a second automation system. Enabling one writes an ordinary rule into
`automations.json`, marked with `"preset"`, which the daemon loads, matches, runs, and
audits exactly like a hand-written rule (docs/v0.6-direction.md §6). Disabling sets that
rule's `enabled` to false and keeps it, so its settings stay visible and re-enabling
restores them; nothing a rule ever wrote (backed-up photos) is touched.

The one preset today is automatic photo backup: when a paired phone connects, including a
phone already connected when the daemon starts, run the verified, additive, one-way backup
of its camera folder, then notify the desktop only if something new arrived.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from linkplane.automations import resolve_automations_path
from linkplane.backup import DEFAULT_DESTINATION, DEFAULT_SOURCE, LEGACY_MANIFEST_NAME, MANIFEST_NAME
from linkplane.core import errors
from linkplane.core.automation import AUTOMATIONS_SCHEMA_VERSION, parse_automations
from linkplane.jobs import JobRecord, resolve_jobs_dir
from linkplane.transports import BridgeError, load_config

PHOTO_BACKUP = "photo-backup"


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    summary: tuple[str, ...]


PRESETS = {
    PHOTO_BACKUP: Preset(
        PHOTO_BACKUP,
        "Automatic photo backup",
        (
            f"When this phone connects, copy new photos and videos from {DEFAULT_SOURCE} to a folder on this computer.",
            "One-way and additive: nothing on the phone is changed or deleted, and deleting photos",
            "from the phone never deletes their backups. Every copy is checksum-verified.",
            "A desktop notification appears only when new files were copied.",
        ),
    ),
}

# Absolute locations a backup must never be written into.
SYSTEM_ROOTS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/run", "/sbin", "/sys", "/usr", "/var")


def rule_name(preset: str, device: str) -> str:
    return f"{preset}:{device}"


def photo_backup_rule(device: str, destination: str, *, enabled: bool = True) -> dict[str, Any]:
    """The exact rule the photo-backup preset writes for one device profile."""
    return {
        "name": rule_name(PHOTO_BACKUP, device),
        "preset": PHOTO_BACKUP,
        "enabled": enabled,
        "when": "device.connected",
        "device": device,
        # A phone already connected when the daemon starts (login, restart) is reported as
        # an initial observation; the backup is idempotent, so it runs then too.
        "on_initial": True,
        "do": [
            {"action": "backup", "source": DEFAULT_SOURCE, "destination": destination},
            {
                "action": "notify-desktop",
                "if": {"downloaded": {"above": 0}},
                "title": "Linkplane photo backup",
                "message": "{downloaded} new photo(s) or video(s) backed up to {destination}",
            },
        ],
    }


def rule_destination(rule: dict[str, Any]) -> str | None:
    for step in rule.get("do") or ():
        if isinstance(step, dict) and step.get("action") == "backup":
            return step.get("destination")
    return None


# -- destination safety ------------------------------------------------------------------


def validate_destination(raw: str, *, device_id: str | None = None, home: str | None = None) -> Path:
    """An absolute, user-writable folder that is neither a system location nor the home
    directory itself, and not already holding another phone's or folder's backup."""
    text = (raw or "").strip()
    if not text:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, "choose a folder for the backup")
    expanded = Path(os.path.expanduser(text))
    if not expanded.is_absolute():
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"the backup folder must be an absolute path or start with ~: {text}")
    path = expanded.resolve()
    home_path = Path(home or os.path.expanduser("~")).resolve()
    if path == Path("/") or path == home_path:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{path} cannot be the backup folder itself; choose a folder inside it",
                                    (f"the backup folder is a folder you own, such as {DEFAULT_DESTINATION}",))
    if any(path == Path(root) or Path(root) in path.parents for root in SYSTEM_ROOTS):
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{path} is a system location; choose a folder you own",
                                    (f"the backup folder is a folder you own, such as {DEFAULT_DESTINATION}",))
    if path.exists() and not path.is_dir():
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{path} exists and is not a folder")
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"this user cannot write to {probe}")
    for manifest_name in (MANIFEST_NAME, LEGACY_MANIFEST_NAME):
        manifest_path = path / manifest_name
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{manifest_path} is unreadable; choose another folder") from None
        other_device = device_id is not None and manifest.get("device_serial") not in (None, device_id)
        if other_device or manifest.get("source") not in (None, DEFAULT_SOURCE):
            raise errors.LinkplaneError(errors.REQUEST_INVALID,
                                        f"{path} already holds a backup of another phone or phone folder; choose another folder")
        break
    return path


# -- the rules file ----------------------------------------------------------------------


def _read_rules_file(path: str | None) -> tuple[Path, dict[str, Any]]:
    file = resolve_automations_path(path)
    if not file.exists():
        return file, {"schema_version": AUTOMATIONS_SCHEMA_VERSION, "automations": []}
    try:
        config = load_config(str(file))
    except BridgeError as error:
        raise errors.LinkplaneError(errors.CONFIG_INVALID, str(error),
                                    (f"fix or move {file}; Linkplane will not overwrite a file it cannot read",)) from error
    parse_automations(config)  # never rewrite a file that does not validate as it is
    return file, config


def _write_rules_file(file: Path, config: dict[str, Any]) -> None:
    parse_automations(config)
    temporary: Path | None = None
    try:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{file.name}.", dir=file.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(config, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(file)
    except OSError as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise errors.LinkplaneError(errors.CONFIG_UNWRITABLE, f"unable to save {file}: {error.strerror or error}",
                                    errors.CONFIG_UNWRITABLE_HINTS) from error


def find_rule(config: dict[str, Any], preset: str, device: str) -> dict[str, Any] | None:
    name = rule_name(preset, device)
    return next((rule for rule in config.get("automations", []) if isinstance(rule, dict) and rule.get("name") == name), None)


def preset_rules(path: str | None = None, preset: str = PHOTO_BACKUP) -> list[dict[str, Any]]:
    """Every rule the preset generated, in file order (a missing file means none)."""
    _file, config = _read_rules_file(path)
    return [rule for rule in config.get("automations", []) if isinstance(rule, dict) and rule.get("preset") == preset]


@dataclass(frozen=True)
class PresetChange:
    preset: str
    device: str
    change: str  # created | enabled | updated | disabled | unchanged
    rule: dict[str, Any]
    path: str
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return self.change != "unchanged"

    def to_dict(self) -> dict[str, Any]:
        return {"preset": self.preset, "device": self.device, "change": self.change, "rule": self.rule,
                "path": self.path, "dry_run": self.dry_run}


def enable_photo_backup(device: str, destination: str | None = None, *, device_id: str | None = None,
                        path: str | None = None, dry_run: bool = False, home: str | None = None) -> PresetChange:
    """Create or re-enable this device's photo-backup rule. `destination` None keeps an
    existing rule's folder, or uses the default for a new one."""
    file, config = _read_rules_file(path)
    rules = config.setdefault("automations", [])
    name = rule_name(PHOTO_BACKUP, device)
    existing = find_rule(config, PHOTO_BACKUP, device)
    if existing is not None and existing.get("preset") != PHOTO_BACKUP:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{file} already has a hand-written rule named {name!r}",
                                    ("rename or remove that rule first",))
    current = rule_destination(existing) if existing is not None else None
    chosen = validate_destination(destination or current or DEFAULT_DESTINATION, device_id=device_id, home=home)
    for rule in rules:
        if (isinstance(rule, dict) and rule.get("preset") == PHOTO_BACKUP and rule.get("name") != name
                and rule_destination(rule) and Path(os.path.expanduser(rule_destination(rule))).resolve() == chosen):
            raise errors.LinkplaneError(errors.REQUEST_INVALID,
                                        f"{chosen} is already the photo backup folder of {rule.get('device')!r}; each phone needs its own folder")
    wanted = photo_backup_rule(device, str(chosen))
    if existing is None:
        change = "created"
        rules.append(wanted)
    elif existing == wanted:
        change = "unchanged"
    else:
        change = "enabled" if not existing.get("enabled", True) and rule_destination(existing) == str(chosen) else "updated"
        rules[rules.index(existing)] = wanted
    if change != "unchanged" and not dry_run:
        _write_rules_file(file, config)
    return PresetChange(PHOTO_BACKUP, device, change, wanted, str(file), dry_run)


def disable_photo_backup(device: str, *, path: str | None = None, dry_run: bool = False) -> PresetChange:
    """Turn this device's photo backup off; the rule (and every backed-up file) stays."""
    file, config = _read_rules_file(path)
    existing = find_rule(config, PHOTO_BACKUP, device)
    if existing is None or existing.get("preset") != PHOTO_BACKUP:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"automatic photo backup was never enabled for {device!r}",
                                    (f"enable it with: linkplane automations enable {PHOTO_BACKUP} --device {device}",))
    if not existing.get("enabled", True):
        return PresetChange(PHOTO_BACKUP, device, "unchanged", existing, str(file), dry_run)
    rules = config["automations"]
    disabled = {**existing, "enabled": False}
    rules[rules.index(existing)] = disabled
    if not dry_run:
        _write_rules_file(file, config)
    return PresetChange(PHOTO_BACKUP, device, "disabled", disabled, str(file), dry_run)


# -- last run, derived from job records (no state of its own) ----------------------------


FINISHED = ("completed", "failed", "cancelled")


def last_run(rule: str, action: str = "backup", *, jobs_dir: str | None = None, scan: int = 500) -> JobRecord | None:
    """The newest finished job this rule started, read from the job records the daemon
    already keeps; the newest `scan` records are considered, newest first."""
    directory = resolve_jobs_dir(jobs_dir)
    if not directory.exists():
        return None
    for file in sorted(directory.glob("*.json"), reverse=True)[:scan]:
        try:
            record = JobRecord(**json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        if record.automation == rule and record.action == action and record.state in FINISHED:
            return record
    return None


def describe_run(record: JobRecord | None) -> str:
    """One human line: when, and what happened."""
    if record is None:
        return "never ran"
    when = (record.finished or record.started or "")[:16].replace("T", " ")
    if record.state == "completed":
        result = record.result or {}
        copied = result.get("downloaded", 0)
        from linkplane.transfer import format_bytes

        size = f" ({format_bytes(result.get('downloaded_bytes') or 0)})" if copied else ""
        preserved = len(result.get("preserved") or ())
        kept = f"; {preserved} existing file(s) left untouched" if preserved else ""
        return f"{when}, {copied} new file(s) copied{size}{kept}"
    message = (record.error or {}).get("message", record.state)
    return f"{when}, {record.state}: {message}"


def summaries(path: str | None = None, *, jobs_dir: str | None = None, devices: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Per device: the preset's state for every device that has a rule or is named."""
    rules = {rule.get("device"): rule for rule in preset_rules(path)}
    names = list(rules) + [device for device in devices if device not in rules]
    output = []
    for device in names:
        rule = rules.get(device)
        record = last_run(rule["name"], jobs_dir=jobs_dir) if rule is not None else None
        output.append({
            "preset": PHOTO_BACKUP,
            "device": device,
            "state": "not set up" if rule is None else ("enabled" if rule.get("enabled", True) else "disabled"),
            "rule": rule["name"] if rule is not None else None,
            "destination": rule_destination(rule) if rule is not None else None,
            "last_run": record.to_dict() if record is not None else None,
            "last_run_summary": describe_run(record) if rule is not None else None,
        })
    return output
