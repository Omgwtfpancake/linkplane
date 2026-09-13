"""Canonical device events: one immutable record per observed state change.

An `Event` is what the observer emits, what the history file stores, what a subscriber
receives, and what a future automation rule matches on. Types are dotted like capability
names (`battery.low`) and listed in `EVENT_TYPES`; new types may be appended, existing
ones are never renamed (docs/core-v0.2-design.md).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

EVENT_SCHEMA = "linkplane.event/1"

EVENT_TYPES = (
    "observer.started",
    "observer.stopped",
    "device.connected",
    "device.disconnected",
    "device.authorized",
    "device.unauthorized",
    "battery.changed",
    "battery.low",
    "battery.ok",
    "charging.started",
    "charging.stopped",
    "wifi.connected",
    "wifi.disconnected",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    """Opaque, unguessable, carries no information (docs/refinement-pass-brief.md §4)."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Event:
    type: str
    device: str
    ts: str = field(default_factory=now_iso)
    provider: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None
    # Identity and causality (additive, refinement pass). `event_id` names this record;
    # `correlation_id` names the causal chain it belongs to and defaults to the event's own
    # id, so a device event is the root of everything a rule does because of it.
    event_id: str = field(default_factory=new_id)
    correlation_id: str | None = None
    source: str = "observer"

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {self.type}")
        if self.correlation_id is None:
            object.__setattr__(self, "correlation_id", self.event_id)

    @property
    def initial(self) -> bool:
        return bool(self.data.get("initial"))

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["schema"] = EVENT_SCHEMA
        return record

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> Event:
        return cls(
            type=str(record["type"]),
            device=str(record["device"]),
            ts=str(record.get("ts") or now_iso()),
            provider=record.get("provider"),
            data=dict(record.get("data") or {}),
            seq=record.get("seq"),
            event_id=str(record.get("event_id") or new_id()),
            correlation_id=record.get("correlation_id"),
            source=str(record.get("source") or "observer"),
        )

    def with_seq(self, seq: int) -> Event:
        return Event(self.type, self.device, self.ts, self.provider, dict(self.data), seq,
                     self.event_id, self.correlation_id, self.source)
