"""Append-only local event history (JSON Lines) with a monotonic sequence number.

One file, `$XDG_STATE_HOME/linkplane/events.jsonl` by default (`LINKPLANE_HISTORY`
overrides). Each line is `Event.to_dict()`; the `seq` continues from the last valid line
on open, so a restarted observer never reuses a number. A corrupt trailing line (a crash
mid-write) is skipped on read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from linkplane.core.events import Event


def resolve_history_path(path: str | None = None) -> Path:
    from linkplane.paths import state_file

    return state_file("events.jsonl", path, env_name="HISTORY")


def read_history(path: str | None = None) -> Iterator[Event]:
    file = resolve_history_path(path)
    if not file.exists():
        return
    with file.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield Event.from_dict(json.loads(line))
            except (ValueError, KeyError, TypeError):
                continue


def last_seq(path: str | None = None) -> int:
    seq = 0
    for event in read_history(path):
        if event.seq is not None and event.seq > seq:
            seq = event.seq
    return seq


class HistoryWriter:
    def __init__(self, path: str | None = None):
        self.path = resolve_history_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = last_seq(str(self.path))
        self._handle = self.path.open("a+", encoding="utf-8")
        # A crash mid-write leaves a partial last line; start a fresh line so the next
        # record is not glued onto the fragment (which would lose both on read).
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() > 0:
            self._handle.seek(self._handle.tell() - 1)
            if self._handle.read(1) != "\n":
                self._handle.write("\n")
        self._handle.seek(0, os.SEEK_END)

    @property
    def seq(self) -> int:
        return self._seq

    def append(self, event: Event) -> Event:
        self._seq += 1
        stamped = event.with_seq(self._seq)
        self._handle.write(json.dumps(stamped.to_dict(), sort_keys=True) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return stamped

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> HistoryWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
