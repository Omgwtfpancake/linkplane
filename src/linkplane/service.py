"""Install linkplaned as a `systemd --user` service (Core v0.2, slice 3).

The only Linux-desktop-specific piece of the control plane. Everything here is a unit file
plus three `systemctl --user` calls, all injectable so tests never touch systemd. The unit
runs the same `linkplane daemon run` the user can run by hand.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from linkplane.core import errors
from linkplane.transports import BridgeError, run_command

UNIT_NAME = "linkplaned.service"


def resolve_unit_dir(path: str | None = None) -> Path:
    if path:
        return Path(os.path.expanduser(path))
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def daemon_command(extra_arguments: tuple[str, ...] = ()) -> list[str]:
    """The ExecStart argv: the installed `linkplane` if any, else this interpreter."""
    executable = shutil.which("linkplane")
    if executable:
        command = [executable]
    else:
        command = [sys.executable, "-m", "linkplane"]
    return [*command, "daemon", "run", *extra_arguments]


def unit_text(command: list[str]) -> str:
    return (
        "[Unit]\n"
        "Description=Linkplane device control plane (linkplaned)\n"
        "Documentation=file://%s\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=%s\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ) % (Path(__file__).resolve().parent.parent.parent / "README.md", shlex.join(command))


@dataclass(frozen=True)
class ServiceResult:
    action: str
    unit_path: str
    unit_text: str
    commands: tuple[tuple[str, ...], ...]
    dry_run: bool

    def to_dict(self) -> dict:
        return {**asdict(self), "commands": [list(c) for c in self.commands]}


def _systemctl(runner: Callable[..., str], *arguments: str) -> None:
    if shutil.which("systemctl") is None:
        raise errors.LinkplaneError(
            errors.DEPENDENCY_MISSING,
            "systemctl is not installed; install the daemon with your init system by hand",
            ("run `linkplane daemon run` under any supervisor that restarts on failure",),
        )
    try:
        runner(["systemctl", "--user", *arguments], timeout=30)
    except BridgeError as error:
        raise errors.LinkplaneError(errors.PROVIDER_FAILED, str(error)) from error


def install(
    *,
    unit_dir: str | None = None,
    extra_arguments: tuple[str, ...] = (),
    dry_run: bool = False,
    runner: Callable[..., str] | None = None,
) -> ServiceResult:
    runner = runner or run_command  # resolved at call time: `linkplane setup` composes this
    directory = resolve_unit_dir(unit_dir)
    unit_path = directory / UNIT_NAME
    text = unit_text(daemon_command(extra_arguments))
    commands = (
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", UNIT_NAME),
    )
    result = ServiceResult("install", str(unit_path), text, commands, dry_run)
    if dry_run:
        return result
    directory.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(text, encoding="utf-8")
    for command in commands:
        _systemctl(runner, *command[2:])
    return result


def uninstall(
    *,
    unit_dir: str | None = None,
    dry_run: bool = False,
    runner: Callable[..., str] | None = None,
) -> ServiceResult:
    runner = runner or run_command
    directory = resolve_unit_dir(unit_dir)
    unit_path = directory / UNIT_NAME
    commands = (
        ("systemctl", "--user", "disable", "--now", UNIT_NAME),
        ("systemctl", "--user", "daemon-reload"),
    )
    result = ServiceResult("uninstall", str(unit_path), "", commands, dry_run)
    if dry_run:
        return result
    if not unit_path.exists():
        raise errors.LinkplaneError(
            errors.DEVICE_NOT_FOUND, f"no unit installed at {unit_path}", ("nothing to uninstall",)
        )
    _systemctl(runner, "disable", "--now", UNIT_NAME)
    unit_path.unlink()
    _systemctl(runner, "daemon-reload")
    return result
