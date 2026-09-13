"""Where Linkplane keeps its files, with a fallback to the pre-rename locations.

The project was PhoneBridge until 2026-09-10 (ADR 0010). Anyone who paired a phone before
then has `~/.config/phonebridge/config.json`, state under `~/.local/state/phonebridge/`,
and backups with a `.phonebridge-manifest.json`. New installs use `linkplane`
everywhere; existing files are found where they are, so nothing needs to be moved by
hand. Environment overrides use the new `LINKPLANE_*` names; the old `PHONEBRIDGE_*`
names are honoured as a fallback for one release.
"""

from __future__ import annotations

import os
from pathlib import Path

NAME = "linkplane"
LEGACY_NAME = "phonebridge"


def env(name: str) -> str | None:
    """`LINKPLANE_<name>`, else the legacy `PHONEBRIDGE_<name>`, else None."""
    return os.environ.get(f"LINKPLANE_{name}") or os.environ.get(f"PHONEBRIDGE_{name}") or None


def _prefer_existing(new: Path, legacy: Path) -> Path:
    """The new location, unless only the legacy one exists yet."""
    if not new.exists() and legacy.exists():
        return legacy
    return new


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
    return _prefer_existing(base / NAME, base / LEGACY_NAME)


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"))
    return _prefer_existing(base / NAME, base / LEGACY_NAME)


def runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(os.path.expanduser("~/.local/state"))
    return base / NAME


def config_file(filename: str, override: str | None = None, *, env_name: str | None = None) -> Path:
    if override:
        return Path(os.path.expanduser(override))
    if env_name and env(env_name):
        return Path(os.path.expanduser(env(env_name) or ""))
    directory = config_dir()
    return _prefer_existing(directory / filename, Path(os.path.expanduser("~/.config")) / LEGACY_NAME / filename)


def state_file(filename: str, override: str | None = None, *, env_name: str | None = None) -> Path:
    if override:
        return Path(os.path.expanduser(override))
    if env_name and env(env_name):
        return Path(os.path.expanduser(env(env_name) or ""))
    return state_dir() / filename
