#!/usr/bin/env python3
"""Measure what an automatic backup costs when nothing is new (docs/v0.6-direction.md §3).

Every backup run discovers the phone's files (one `adb shell stat` per file) and re-hashes
every already backed-up local file before deciding there is nothing to do. This tool
answers whether that is too slow for backup-on-connect, before anyone optimises it.

  PYTHONPATH=src tools/measure-backup.py phone DEST [--source /sdcard/DCIM/Camera] [--serial S]
      a dry run against the connected phone: nothing is downloaded or written; prints the
      phone's file count, the existing library's size, and the time spent in each phase

  PYTHONPATH=src tools/measure-backup.py synthetic --files 2000 --size-mb 4
      no phone: builds a throwaway library in a temporary directory and times the local
      re-hash alone, to estimate hashing throughput on this machine's disk
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from linkplane.backup import DEFAULT_SOURCE, BackupRequest, backup_photos, sha256_file


def measure_phone(arguments: argparse.Namespace) -> int:
    result = backup_photos(BackupRequest(destination=arguments.destination, source=arguments.source,
                                         serial=arguments.serial, dry_run=True))
    if result.error is not None:
        print(f"backup plan failed: {result.error.message}", file=sys.stderr)
        return 1
    value = result.value.to_dict()
    report = {key: value[key] for key in (
        "discovered", "skipped", "verified_bytes", "pending", "pending_bytes",
        "discovery_seconds", "unchanged_check_seconds", "duration_seconds",
    )}
    report["preserved"] = len(value["preserved"])
    print(json.dumps(report, indent=2))
    return 0


def measure_synthetic(arguments: argparse.Namespace) -> int:
    chunk = os.urandom(1024 * 1024)
    with tempfile.TemporaryDirectory(prefix="linkplane-measure-", dir=arguments.dir) as directory:
        paths = []
        for index in range(arguments.files):
            path = Path(directory) / f"IMG_{index:06d}.jpg"
            with path.open("wb") as handle:
                for _ in range(arguments.size_mb):
                    handle.write(chunk)
            paths.append(path)
        started = time.monotonic()
        for path in paths:
            sha256_file(path)
        seconds = time.monotonic() - started
    total = arguments.files * arguments.size_mb
    print(json.dumps({
        "files": arguments.files, "total_mb": total, "rehash_seconds": round(seconds, 3),
        "mb_per_second": round(total / seconds, 1) if seconds else None,
        "note": "page cache is warm right after writing; a cold cache after a reboot is slower",
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="mode", required=True)
    phone = modes.add_parser("phone", help="dry-run plan against the connected phone")
    phone.add_argument("destination")
    phone.add_argument("--source", default=DEFAULT_SOURCE)
    phone.add_argument("--serial")
    synthetic = modes.add_parser("synthetic", help="local re-hash of a throwaway library")
    synthetic.add_argument("--files", type=int, default=1000)
    synthetic.add_argument("--size-mb", type=int, default=4)
    synthetic.add_argument("--dir", help="where to build it (default: the system temp dir, which may be RAM-backed)")
    arguments = parser.parse_args()
    return measure_phone(arguments) if arguments.mode == "phone" else measure_synthetic(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
