from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from linkplane.operations import (
    CancellationToken,
    OperationCancelled,
    OperationResult,
    ProgressCallback,
    ProgressEvent,
    cancel_on_interrupt,
    check_cancelled,
    is_cancelled,
    report_progress,
)
from linkplane.core import errors
from linkplane.transfer import format_bytes
from linkplane.transports import AdbTransport, BridgeError


DEFAULT_SOURCE = "/sdcard/DCIM/Camera"
DEFAULT_DESTINATION = "~/Pictures/Linkplane"
MANIFEST_NAME = ".linkplane-manifest.json"
LEGACY_MANIFEST_NAME = ".phonebridge-manifest.json"  # written before the rename (ADR 0010)
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RemoteFile:
    path: str
    relative_path: str
    size: int
    modified: int


@dataclass(frozen=True)
class BackupRequest:
    destination: str = DEFAULT_DESTINATION
    source: str = DEFAULT_SOURCE
    serial: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class BackupResult:
    transport: str
    device: str
    serial: str
    source: str
    destination: str
    discovered: int
    pending: int
    pending_bytes: int
    downloaded: int
    skipped: int
    pending_files: tuple[str, ...]
    dry_run: bool
    # Additive (v0.6). `preserved`: files already in the destination that no manifest
    # records and whose content does not match the phone's, left untouched rather than
    # overwritten (on a dry run: every such file, unchecked). `adopted`: such files that
    # were byte-identical to the phone's and are now recorded instead of downloaded again.
    preserved: tuple[str, ...] = ()
    adopted: int = 0
    downloaded_bytes: int = 0
    # Measurement for the unchanged-file strategy (docs/v0.6-direction.md §3): bytes and
    # seconds spent re-verifying already backed-up files, and the phases around it.
    verified_bytes: int = 0
    discovery_seconds: float = 0.0
    unchanged_check_seconds: float = 0.0
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_source(source: str) -> PurePosixPath:
    path = PurePosixPath(source)
    if not source.startswith("/") or any(part == ".." for part in path.parts):
        raise BridgeError("backup source must be an absolute Android path")
    if any(character in source for character in ("\0", "\n", "\r")):
        raise BridgeError("backup source contains an unsupported control character")
    return path


def adb_shell(transport: AdbTransport, command: str, *, timeout: int = 30) -> str:
    if not transport.serial:
        raise BridgeError("unable to determine the ADB device serial")
    return transport.run(
        ["adb", "-s", transport.serial, "shell", command],
        timeout=timeout,
    )


def discover_remote_files(transport: AdbTransport, source: str) -> list[RemoteFile]:
    source_path = validate_source(source)
    output = adb_shell(transport, f"find {shlex.quote(source)} -type f -print0")
    remote_files: list[RemoteFile] = []
    for raw_path in output.split("\0"):
        if not raw_path:
            continue
        path = PurePosixPath(raw_path)
        try:
            relative = path.relative_to(source_path)
        except ValueError as error:
            raise BridgeError(
                f"ADB returned a file outside the backup source: {raw_path}"
            ) from error
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise BridgeError(f"ADB returned an invalid backup path: {raw_path}")
        stat_output = adb_shell(
            transport,
            f"stat -c '%s\t%Y' {shlex.quote(raw_path)}",
        ).strip()
        fields = stat_output.split("\t")
        if len(fields) != 2:
            raise BridgeError(f"unable to read file metadata for {raw_path}")
        try:
            size, modified = (int(field) for field in fields)
        except ValueError as error:
            raise BridgeError(f"invalid file metadata for {raw_path}") from error
        remote_files.append(RemoteFile(raw_path, relative.as_posix(), size, modified))
    return sorted(remote_files, key=lambda item: item.relative_path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise BridgeError(f"unable to checksum {path}: {error}") from error
    return digest.hexdigest()


def remote_sha256(transport: AdbTransport, path: str) -> str:
    output = adb_shell(transport, f"sha256sum {shlex.quote(path)}", timeout=300)
    checksum = output.split(maxsplit=1)[0] if output.split() else ""
    if len(checksum) != 64 or any(character not in "0123456789abcdefABCDEF" for character in checksum):
        raise BridgeError(f"phone returned an invalid SHA-256 for {path}")
    return checksum.lower()


def new_manifest(serial: str, source: str) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "device_serial": serial,
        "source": source,
        "files": {},
    }


def load_manifest(path: Path, serial: str, source: str) -> dict[str, Any]:
    if not path.exists():
        legacy = path.with_name(LEGACY_MANIFEST_NAME)
        if legacy.exists():
            path = legacy  # an existing backup keeps its verified history
        else:
            return new_manifest(serial, source)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError(f"unable to read backup manifest at {path}: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise BridgeError(f"backup manifest at {path} is invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise BridgeError(f"backup manifest at {path} uses an unsupported schema")
    if manifest.get("device_serial") != serial or manifest.get("source") != source:
        raise BridgeError(
            "backup destination belongs to a different device or source; choose another destination"
        )
    return manifest


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BridgeError(f"unable to save backup manifest at {path}: {error}") from error


def is_unchanged(local_path: Path, remote: RemoteFile, record: Any) -> bool:
    if not isinstance(record, dict) or not local_path.is_file():
        return False
    if record.get("size") != remote.size or record.get("modified") != remote.modified:
        return False
    checksum = record.get("sha256")
    return isinstance(checksum, str) and sha256_file(local_path) == checksum


def free_bytes(path: Path) -> int:
    """Free space on the filesystem that holds `path`, or would once it is created."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def required_bytes(pending: list[tuple[RemoteFile, Path]]) -> int:
    """What a run needs free: every pending file, plus the largest one that replaces an
    existing local copy, because the verified temporary sits beside it until the swap."""
    replacing = [remote.size for remote, local in pending if local.exists()]
    return sum(remote.size for remote, _local in pending) + max(replacing, default=0)


def ensure_inside(destination: Path, local_path: Path) -> None:
    """Refuse to write through a symbolic link anywhere below the destination root, so a
    link planted in the backup folder can never redirect a download elsewhere."""
    current = destination
    for part in local_path.relative_to(destination).parts:
        current = current / part
        if current.is_symlink():
            raise BridgeError(f"refusing to write through a symbolic link in the backup destination: {current}")


def pull_verified(
    transport: AdbTransport,
    remote: RemoteFile,
    local_path: Path,
) -> str:
    if not transport.serial:
        raise BridgeError("unable to determine the ADB device serial")
    expected_checksum = remote_sha256(transport, remote.path)
    temporary = local_path.with_name(f".{local_path.name}.linkplane-part")
    published = False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        transport.run(
            ["adb", "-s", transport.serial, "pull", "-a", remote.path, str(temporary)],
            timeout=300,
        )
        actual_checksum = sha256_file(temporary)
        if actual_checksum != expected_checksum:
            raise BridgeError(f"checksum verification failed for {remote.path}")
        temporary.replace(local_path)
        published = True
    except OSError as error:
        raise BridgeError(f"unable to store backup file at {local_path}: {error}") from error
    finally:
        if not published:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return expected_checksum


def backup_selected(
    transport: AdbTransport,
    device: dict[str, str],
    source: str,
    destination: Path,
    *,
    dry_run: bool,
) -> int:
    _backup_selected(
        transport,
        device,
        source,
        destination,
        dry_run=dry_run,
        progress=render_backup_progress,
    )
    return 0


def _backup_selected(
    transport: AdbTransport,
    device: dict[str, str],
    source: str,
    destination: Path,
    *,
    dry_run: bool,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> BackupResult:
    serial = transport.serial
    if serial is None:
        raise BridgeError("unable to determine the ADB device serial")
    started_clock = time.monotonic()
    source = str(validate_source(source))
    report_progress(
        progress,
        ProgressEvent(
            "backup",
            "discovering",
            "Discovering phone files",
            details={"source": source, "destination": str(destination), "serial": serial},
        ),
    )
    check_cancelled(cancel)
    remote_files = discover_remote_files(transport, source)
    discovery_seconds = time.monotonic() - started_clock
    check_cancelled(cancel)
    manifest_path = destination / MANIFEST_NAME
    manifest = load_manifest(manifest_path, serial, source)
    records = manifest["files"]
    pending: list[tuple[RemoteFile, Path]] = []
    untracked: list[tuple[RemoteFile, Path]] = []
    skipped = 0
    verified_bytes = 0
    check_clock = time.monotonic()
    for remote in remote_files:
        local_path = destination.joinpath(*PurePosixPath(remote.relative_path).parts)
        record = records.get(remote.relative_path)
        if is_unchanged(local_path, remote, record):
            skipped += 1
            verified_bytes += remote.size
        elif record is None and (local_path.exists() or local_path.is_symlink()):
            # Not written by Linkplane: never overwrite it (checked, and maybe adopted, below).
            untracked.append((remote, local_path))
        else:
            pending.append((remote, local_path))
    unchanged_check_seconds = time.monotonic() - check_clock

    model = device.get("model", "Android device").replace("_", " ")
    pending_size = sum(remote.size for remote, _local in pending)
    result = BackupResult(
        transport="adb",
        device=model,
        serial=serial,
        source=source,
        destination=str(destination),
        discovered=len(remote_files),
        pending=len(pending),
        pending_bytes=pending_size,
        downloaded=0,
        skipped=skipped,
        pending_files=tuple(remote.relative_path for remote, _local in pending),
        dry_run=dry_run,
        preserved=tuple(remote.relative_path for remote, _local in untracked),
        verified_bytes=verified_bytes,
        discovery_seconds=round(discovery_seconds, 3),
        unchanged_check_seconds=round(unchanged_check_seconds, 3),
    )
    report_progress(
        progress,
        ProgressEvent(
            "backup",
            "started",
            "Backup plan ready",
            current=0,
            total=len(pending),
            unit="files",
            details=result.to_dict(),
        ),
    )

    if dry_run:
        for remote, _local in pending:
            report_progress(
                progress,
                ProgressEvent(
                    "backup",
                    "item_pending",
                    f"Would pull {remote.relative_path}",
                    total=len(pending),
                    unit="files",
                    item=remote.relative_path,
                ),
            )
        report_progress(
            progress,
            ProgressEvent(
                "backup",
                "completed",
                "Dry run completed",
                current=0,
                total=len(pending),
                unit="files",
                details=result.to_dict(),
            ),
        )
        return replace(result, duration_seconds=round(time.monotonic() - started_clock, 3))

    required = required_bytes(pending)
    if required:
        try:
            available = free_bytes(destination)
        except OSError as error:
            raise BridgeError(f"unable to check free space for {destination}: {error}") from error
        if required > available:
            raise errors.LinkplaneError(
                errors.STORAGE_INSUFFICIENT,
                f"not enough free space in {destination}: this backup needs {format_bytes(required)}, "
                f"{format_bytes(available)} is available; nothing was downloaded",
                ("free up space on this computer, or back up to another folder",),
            )
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BridgeError(f"unable to create backup destination {destination}: {error}") from error
    for index, (remote, local_path) in enumerate(pending, start=1):
        if is_cancelled(cancel):
            # Every completed file is already recorded in the manifest, so a cancelled
            # backup resumes exactly where it stopped on the next run.
            partial = replace(result, downloaded=index - 1)
            report_progress(
                progress,
                ProgressEvent(
                    "backup",
                    "cancelled",
                    f"Backup cancelled after {index - 1} of {len(pending)} file(s)",
                    current=index - 1,
                    total=len(pending),
                    unit="files",
                    details=partial.to_dict(),
                ),
            )
            check_cancelled(cancel)
        report_progress(
            progress,
            ProgressEvent(
                "backup",
                "item_started",
                f"Pulling {remote.relative_path}",
                current=index - 1,
                total=len(pending),
                unit="files",
                item=remote.relative_path,
                details={"size": remote.size},
            ),
        )
        ensure_inside(destination, local_path)
        checksum = pull_verified(transport, remote, local_path)
        records[remote.relative_path] = {
            "size": remote.size,
            "modified": remote.modified,
            "sha256": checksum,
            "backed_up_at": datetime.now(timezone.utc).isoformat(),
        }
        save_manifest(manifest_path, manifest)
        report_progress(
            progress,
            ProgressEvent(
                "backup",
                "item_completed",
                f"Backed up {remote.relative_path}",
                current=index,
                total=len(pending),
                unit="files",
                item=remote.relative_path,
                details={"size": remote.size, "sha256": checksum},
            ),
        )
    adopted = 0
    preserved: list[str] = []
    for remote, local_path in untracked:
        check_cancelled(cancel)
        if local_path.is_symlink() or not local_path.is_file():
            preserved.append(remote.relative_path)
            continue
        checksum = remote_sha256(transport, remote.path)
        if sha256_file(local_path) != checksum:
            preserved.append(remote.relative_path)
            continue
        records[remote.relative_path] = {
            "size": remote.size,
            "modified": remote.modified,
            "sha256": checksum,
            "backed_up_at": datetime.now(timezone.utc).isoformat(),
        }
        save_manifest(manifest_path, manifest)
        adopted += 1
    if not manifest_path.exists():
        save_manifest(manifest_path, manifest)
    result = replace(
        result,
        downloaded=len(pending),
        downloaded_bytes=result.pending_bytes,
        adopted=adopted,
        preserved=tuple(preserved),
        duration_seconds=round(time.monotonic() - started_clock, 3),
    )
    report_progress(
        progress,
        ProgressEvent(
            "backup",
            "completed",
            "Backup completed",
            current=len(pending),
            total=len(pending),
            unit="files",
            details=result.to_dict(),
        ),
    )
    return result


def backup_photos(
    request: BackupRequest,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> OperationResult[BackupResult]:
    try:
        source = str(validate_source(request.source))
    except BridgeError as error:
        return OperationResult.failure("invalid_request", str(error))

    destination = Path(request.destination).expanduser().resolve()
    transport = AdbTransport(request.serial)
    try:
        device = transport.select_device()
    except BridgeError as error:
        return OperationResult.failure("transport_unavailable", str(error))
    try:
        return OperationResult.success(
            _backup_selected(
                transport,
                device,
                source,
                destination,
                dry_run=request.dry_run,
                progress=progress,
                cancel=cancel,
            )
        )
    except OperationCancelled as error:
        return OperationResult.failure("cancelled", str(error))
    except errors.LinkplaneError as error:
        return OperationResult.failure("operation_failed", str(error), error_code=error.code, hints=error.hints)
    except BridgeError as error:
        return OperationResult.failure("operation_failed", str(error))


def render_backup_progress(event: ProgressEvent) -> None:
    if event.phase == "started":
        result = event.details
        print("Linkplane Backup")
        print(f"Device      {result['device']} ({result['serial']})")
        print(f"Source      {result['source']}")
        print(f"Destination {result['destination']}")
        print(f"Found       {result['discovered']} files")
        print(
            f"Pending     {result['pending']} files "
            f"({format_bytes(result['pending_bytes'])})"
        )
        print(f"Verified    {result['skipped']} unchanged")
        if result.get("preserved"):
            print(f"Existing    {len(result['preserved'])} file(s) Linkplane did not create; never overwritten")
    elif event.phase == "item_pending":
        print(f"Would pull  {event.item}")
    elif event.phase == "item_started":
        print(f"Pulling     {event.item}", flush=True)
    elif event.phase == "cancelled":
        result = event.details
        print(
            f"Cancelled   {result['downloaded']} of {result['pending']} downloaded; "
            "rerun to resume",
            flush=True,
        )
    elif event.phase == "completed" and event.message != "Dry run completed":
        result = event.details
        print(
            f"Completed   {result['downloaded']} downloaded, "
            f"{result['skipped']} unchanged"
        )
        if result.get("adopted"):
            print(f"Recorded    {result['adopted']} existing identical file(s)")
        if result.get("preserved"):
            print(f"Kept        {len(result['preserved'])} existing different file(s) untouched")


def backup(arguments: Any) -> int:
    with cancel_on_interrupt(CancellationToken()) as cancel:
        result = backup_photos(
            BackupRequest(
                destination=arguments.destination,
                source=arguments.source,
                serial=arguments.serial,
                dry_run=arguments.dry_run,
            ),
            progress=render_backup_progress,
            cancel=cancel,
        )
    if result.error is not None:
        if result.error.code == "cancelled":
            raise OperationCancelled(result.error.message)
        if result.error.error_code is not None:
            raise errors.LinkplaneError(result.error.error_code, result.error.message, result.error.hints)
        raise BridgeError(result.error.message)
    return 0
