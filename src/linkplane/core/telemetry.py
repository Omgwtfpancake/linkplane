"""Device telemetry models: the request/result shapes behind `device.status`.

Kept free of any provider or transport import so both the status service and the
providers can depend on them without a cycle. `linkplane.status` re-exports them, which
is the module path the frozen contract (tests/test_contracts.py) pins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class StatusRequest:
    transport: str = "auto"
    serial: str | None = None
    serial_from_profile: bool = False


@dataclass(frozen=True)
class TelemetryIssue:
    component: str
    error: str


@dataclass(frozen=True)
class StatusResult:
    transport: str
    device: dict[str, Any]
    battery: dict[str, Any] | None
    memory: dict[str, Any] | None
    storage: dict[str, Any] | None
    uptime_seconds: int | None
    issues: tuple[TelemetryIssue, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StatusResult:
        return cls(
            transport=str(value["transport"]),
            device=dict(value["device"]),
            battery=dict(value["battery"]) if value.get("battery") is not None else None,
            memory=dict(value["memory"]) if value.get("memory") is not None else None,
            storage=dict(value["storage"]) if value.get("storage") is not None else None,
            uptime_seconds=value.get("uptime_seconds"),
            issues=tuple(
                TelemetryIssue(str(issue["component"]), str(issue["error"]))
                for issue in value.get("issues", ())
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
