"""`linkplane uninstall`: remove Linkplane-managed service integration, and with `--purge`
the Linkplane-owned directories -- never the software, never user data.

Three things are deliberately different commands (`docs/install-onboarding-design.md` §21):

    linkplane uninstall          service unit, runtime socket and discovery file
    linkplane uninstall --purge  + ~/.config/linkplane and ~/.local/state/linkplane
    pipx uninstall linkplane     the software itself (owned by whatever installed it)

Purge targets are the fixed roots `paths.config_dir()`, `paths.state_dir()` and
`paths.runtime_dir()` -- the same functions every other module resolves through -- and
nothing read from a configuration value. A backup destination, a transfer path, or any
directory a user chose can therefore never become a target, and a root that is itself a
symlink, `$HOME`, `/`, or not named after Linkplane is refused. Symlinks *inside* a root
are unlinked, never followed.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from linkplane import daemon as daemond
from linkplane import service
from linkplane.core import errors
from linkplane.models import Check
from linkplane.operations import CancellationToken, check_cancelled
from linkplane.paths import LEGACY_NAME, NAME, config_dir, runtime_dir, state_dir
from linkplane.transports import BridgeError, run_command

OWNED_ROOT_NAMES = frozenset({NAME, LEGACY_NAME})
RUNTIME_FILES = ("daemon.sock", "api.json")
STOP_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class UninstallOptions:
    purge: bool = False
    force: bool = False
    dry_run: bool = False
    interactive: bool = True
    unit_dir: str | None = None
    socket_path: str | None = None
    stop_wait: float = STOP_WAIT_SECONDS
    poll_interval: float = 0.25


@dataclass(frozen=True)
class UninstallSeams:
    finder: Callable[[str], str | None] | None = None
    daemon_status: Callable[[str | None], dict[str, Any] | None] | None = None
    unit_main_pid: Callable[[], int | None] | None = None
    service_uninstall: Callable[..., service.ServiceResult] | None = None
    confirm: Callable[[str], bool] | None = None
    clock: Callable[[], float] | None = None
    sleep: Callable[[float], None] | None = None


@dataclass(frozen=True)
class PurgeTarget:
    path: str
    exists: bool
    files: int
    bytes: int
    refused: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists, "files": self.files, "bytes": self.bytes, "refused": self.refused}


@dataclass(frozen=True)
class UninstallResult:
    ok: bool
    steps: tuple[Check, ...]
    daemon_was_running: bool
    daemon_stopped: bool | None
    unmanaged_daemon: dict[str, Any] | None
    unit_path: str
    unit_removed: bool | None
    runtime_removed: tuple[str, ...]
    configuration_retained: bool
    purge_requested: bool
    purge_performed: bool
    purge_targets: tuple[PurgeTarget, ...]
    removed: tuple[str, ...]
    retained: tuple[str, ...]
    warnings: tuple[str, ...]
    software_hint: str
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "steps": [step.to_dict() for step in self.steps],
            "daemon": {"was_running": self.daemon_was_running, "stopped": self.daemon_stopped, "unmanaged": self.unmanaged_daemon},
            "unit": {"path": self.unit_path, "removed": self.unit_removed},
            "runtime_removed": list(self.runtime_removed),
            "configuration_retained": self.configuration_retained,
            "purge": {"requested": self.purge_requested, "performed": self.purge_performed,
                      "targets": [target.to_dict() for target in self.purge_targets]},
            "removed": list(self.removed),
            "retained": list(self.retained),
            "warnings": list(self.warnings),
            "software_hint": self.software_hint,
        }


def _default_daemon_status(socket_path: str | None) -> dict[str, Any] | None:
    try:
        return daemond.request("status", socket_path)
    except (errors.LinkplaneError, OSError):
        return None


def _default_unit_main_pid() -> int | None:
    """The unit's MainPID per systemd, 0/None when inactive or systemd is absent."""
    if shutil.which("systemctl") is None:
        return None
    try:
        output = run_command(["systemctl", "--user", "show", "-p", "MainPID", "--value", service.UNIT_NAME], timeout=10)
    except BridgeError:
        return None
    try:
        pid = int(output.strip() or "0")
    except ValueError:
        return None
    return pid or None


def software_hint(finder: Callable[[str], str | None]) -> str:
    """How to remove the software itself; this command never does that."""
    executable = finder("linkplane") or ""
    if "/pipx/" in executable or "/.local/bin/linkplane" in executable and "pipx" in os.path.realpath(executable):
        return "The Linkplane software is still installed; remove it with: pipx uninstall linkplane"
    return "The Linkplane software is still installed; remove it with the tool that installed it (for pipx: pipx uninstall linkplane)"


def owned_roots() -> tuple[Path, ...]:
    """The only directories purge may ever touch, resolved exactly as the product does."""
    roots: list[Path] = []
    for root in (config_dir(), state_dir(), runtime_dir()):
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def refuse_reason(root: Path) -> str | None:
    """Why a root must not be purged, or None when it is a bounded Linkplane-owned directory."""
    home = Path(os.path.expanduser("~"))
    if root.name not in OWNED_ROOT_NAMES:
        return f"not a Linkplane-owned directory name ({root.name!r})"
    if root == Path("/") or root == home or root in home.parents or len(root.parts) < 3:
        return "refusing to purge a directory that is not below the user's home"
    if root.is_symlink():
        return "the directory is a symbolic link; purge never follows links"
    return None


def measure(root: Path) -> tuple[int, int]:
    """Count regular files and bytes below `root` without following any symlink."""
    files = 0
    size = 0
    if not root.exists() or root.is_symlink():
        return files, size
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            files += 1
            size += stat.st_size
    return files, size


def remove_tree(root: Path) -> None:
    """Delete `root` without ever following a symlink out of it."""
    if root.is_symlink():
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{root} is a symbolic link; refusing to purge it")
    shutil.rmtree(root)  # rmtree unlinks symlinks it meets and never descends into them


class _Run:
    def __init__(self, options: UninstallOptions, seams: UninstallSeams, cancel: CancellationToken | None) -> None:
        self.options = options
        self.cancel = cancel
        self.finder = seams.finder or shutil.which
        self.daemon_status = seams.daemon_status or _default_daemon_status
        self.unit_main_pid = seams.unit_main_pid or _default_unit_main_pid
        self.service_uninstall = seams.service_uninstall or service.uninstall
        self.confirm = seams.confirm or (lambda question: False)
        self.clock = seams.clock or time.monotonic
        self.sleep = seams.sleep or time.sleep
        self.steps: list[Check] = []
        self.warnings: list[str] = []
        self.removed: list[str] = []
        self.retained: list[str] = []
        self.ok = True

    def step(self, name: str, status: str, summary: str, fix: str | None = None, code: str | None = None) -> None:
        self.steps.append(Check(name, status, summary, fix, code))
        if status == "warning" and fix is None:
            self.warnings.append(summary)
        if status == "error":
            self.ok = False

    # -- daemon + unit -------------------------------------------------------------

    def daemon_and_unit(self) -> tuple[bool, bool | None, dict[str, Any] | None, str, bool | None]:
        unit_path = service.resolve_unit_dir(self.options.unit_dir) / service.UNIT_NAME
        info = self.daemon_status(self.options.socket_path)
        was_running = info is not None
        managed_pid = self.unit_main_pid() if unit_path.exists() else None
        unmanaged: dict[str, Any] | None = None
        if info is not None:
            pid = info.get("pid")
            if managed_pid is None or pid != managed_pid:
                unmanaged = {"pid": pid, "socket": info.get("socket"), "version": info.get("version")}
                self.step("Daemon", "warning", f"a Linkplane daemon is running outside the service (pid {pid}); it was left alone",
                          "Stop it yourself with `linkplane daemon stop` (or Ctrl+C in its terminal), then run `linkplane uninstall` again")
        stopped: bool | None = None
        unit_removed: bool | None = None
        if unit_path.exists():
            if self.options.dry_run:
                self.step("Service unit", "ok", f"would stop, disable and remove {unit_path}")
                unit_removed = False
                stopped = False
            else:
                try:
                    self.service_uninstall(unit_dir=self.options.unit_dir)
                except errors.LinkplaneError as error:
                    self.step("Service unit", "error", str(error), "; ".join(error.hints) or "See `systemctl --user status linkplaned`", error.code)
                    return was_running, None, unmanaged, str(unit_path), False
                unit_removed = True
                self.step("Service unit", "ok", f"stopped, disabled and removed {unit_path}")
                if was_running and unmanaged is None:
                    stopped = self._wait_stopped()
                    if stopped:
                        self.step("Daemon", "ok", "stopped")
                    else:
                        self.step("Daemon", "warning", "the daemon is still answering on its socket after the service was disabled",
                                  "Check `systemctl --user status linkplaned`; run `linkplane uninstall` again once it has stopped")
        else:
            unit_removed = False
            self.step("Service unit", "ok", f"not installed ({unit_path})")
            if was_running and unmanaged is None:
                unmanaged = {"pid": info.get("pid") if info else None, "socket": info.get("socket") if info else None}
        if not was_running:
            self.step("Daemon", "ok", "not running")
            stopped = None
        return was_running, stopped, unmanaged, str(unit_path), unit_removed

    def _wait_stopped(self) -> bool:
        deadline = self.clock() + self.options.stop_wait
        while True:
            if self.daemon_status(self.options.socket_path) is None:
                return True
            if self.clock() >= deadline:
                return False
            check_cancelled(self.cancel)
            self.sleep(self.options.poll_interval)

    # -- runtime -------------------------------------------------------------------

    def runtime(self, daemon_alive: bool) -> tuple[str, ...]:
        directory = runtime_dir()
        present = [directory / name for name in RUNTIME_FILES if (directory / name).exists() or (directory / name).is_symlink()]
        if not present:
            # A stopped daemon removes its own socket and discovery file; the empty
            # directory it leaves is Linkplane-owned and trivially recreated.
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()) and not self.options.dry_run and not daemon_alive:
                try:
                    directory.rmdir()
                    self.removed.append(str(directory))
                    self.step("Runtime files", "ok", f"removed the empty {directory}")
                    return (str(directory),)
                except OSError:
                    pass
            self.step("Runtime files", "ok", f"nothing to remove in {directory}")
            return ()
        if daemon_alive:
            self.step("Runtime files", "warning", f"left {', '.join(p.name for p in present)} in place: a daemon is still using them",
                      "They are removed when that daemon stops")
            return ()
        if self.options.dry_run:
            self.step("Runtime files", "ok", "would remove " + ", ".join(str(p) for p in present))
            return ()
        removed: list[str] = []
        for path in present:
            try:
                path.unlink()
                removed.append(str(path))
            except OSError as error:
                self.step("Runtime files", "warning", f"could not remove {path}: {error.strerror or error}", "Remove it by hand")
        try:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass
        if removed:
            self.step("Runtime files", "ok", "removed " + ", ".join(Path(p).name for p in removed))
        self.removed.extend(removed)
        return tuple(removed)

    # -- purge ---------------------------------------------------------------------

    def purge(self, daemon_alive: bool) -> tuple[bool, tuple[PurgeTarget, ...]]:
        roots = owned_roots()
        targets: list[PurgeTarget] = []
        for root in roots:
            reason = refuse_reason(root)
            files, size = measure(root) if reason is None else (0, 0)
            targets.append(PurgeTarget(str(root), root.exists() or root.is_symlink(), files, size, reason))
        if not self.options.purge:
            for target in targets:
                if target.exists and Path(target.path) != runtime_dir():
                    self.retained.append(target.path)
            self.step("Configuration and state", "ok", "kept: " + (", ".join(self.retained) if self.retained else "nothing present"),
                      "Remove them too with `linkplane uninstall --purge`" if self.retained else None)
            return False, tuple(targets)
        deletable = [target for target in targets if target.exists and target.refused is None]
        for target in targets:
            if target.refused:
                self.step("Purge", "warning", f"{target.path}: {target.refused}", "Left untouched")
        if not deletable:
            self.step("Purge", "ok", "nothing to remove")
            return False, tuple(targets)
        if daemon_alive:
            self.step("Purge", "error", "a Linkplane daemon is still running; refusing to remove its state underneath it",
                      "Stop it with `linkplane daemon stop`, then run `linkplane uninstall --purge` again", errors.STATE_CONFLICT)
            return False, tuple(targets)
        listing = ", ".join(f"{t.path} ({t.files} files)" for t in deletable)
        if self.options.dry_run:
            self.step("Purge", "ok", f"would remove {listing}")
            return False, tuple(targets)
        if not self.options.force:
            if not self.options.interactive:
                self.step("Purge", "error", f"would remove {listing}", "Pass --force to remove these without a prompt", errors.REQUEST_INVALID)
                return False, tuple(targets)
            if not self.confirm(f"Delete {listing}? This cannot be undone."):
                self.step("Purge", "warning", "declined; nothing removed")
                return False, tuple(targets)
        for target in deletable:
            check_cancelled(self.cancel)
            root = Path(target.path)
            try:
                remove_tree(root)
            except (OSError, errors.LinkplaneError) as error:
                self.step("Purge", "error", f"could not remove {root}: {getattr(error, 'strerror', None) or error}",
                          "Fix the permissions and run `linkplane uninstall --purge` again", errors.CONFIG_UNWRITABLE)
                continue
            self.removed.append(str(root))
            self.step("Purge", "ok", f"removed {root}")
        return any(t.path in self.removed for t in deletable), tuple(targets)

    # -- sequencing ----------------------------------------------------------------

    def run(self) -> UninstallResult:
        was_running, stopped, unmanaged, unit_path, unit_removed = self.daemon_and_unit()
        daemon_alive = self.daemon_status(self.options.socket_path) is not None
        runtime_removed = self.runtime(daemon_alive)
        purged, targets = self.purge(daemon_alive)
        hint = software_hint(self.finder)
        return UninstallResult(
            ok=self.ok,
            steps=tuple(self.steps),
            daemon_was_running=was_running,
            daemon_stopped=stopped,
            unmanaged_daemon=unmanaged,
            unit_path=unit_path,
            unit_removed=unit_removed,
            runtime_removed=runtime_removed,
            configuration_retained=not purged,
            purge_requested=self.options.purge,
            purge_performed=purged,
            purge_targets=targets,
            removed=tuple(self.removed),
            retained=tuple(self.retained),
            warnings=tuple(self.warnings),
            software_hint=hint,
            dry_run=self.options.dry_run,
        )


def run_uninstall(options: UninstallOptions, seams: UninstallSeams | None = None, *, cancel: CancellationToken | None = None) -> UninstallResult:
    return _Run(options, seams or UninstallSeams(), cancel).run()
