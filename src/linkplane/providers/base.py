"""The Provider interface: how a capability is implemented for one device endpoint.

A provider wraps exactly one way of talking to one device (an SSH session to Termux, an
ADB serial, later an Android agent) and exposes the public capability vocabulary from
`linkplane.core.capability`. Everything above a provider -- the CLI, future API/GUI --
talks only in terms of `Provider` methods and the typed results below, never in terms of
`ssh`, `adb`, or subprocess output. Errors leave a provider only as `LinkplaneError`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from linkplane.core.capability import CapabilityReport
from linkplane.core.telemetry import StatusResult


@dataclass(frozen=True)
class PingResult:
    reachable: bool
    provider: str
    address: str
    latency_ms: float | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BatteryReading:
    level: int
    status: str
    health: str
    powered_by: tuple[str, ...]
    provider: str
    temperature_c: float | None = None
    voltage_mv: int | None = None

    @classmethod
    def from_status(cls, battery: dict[str, Any], provider: str) -> BatteryReading:
        return cls(
            level=int(battery["level"]),
            status=str(battery.get("status", "unknown")),
            health=str(battery.get("health", "unknown")),
            powered_by=tuple(battery.get("powered_by", ())),
            provider=provider,
            temperature_c=battery.get("temperature_c"),
            voltage_mv=battery.get("voltage_mv"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Provider(ABC):
    """One implementation of the device capabilities for one endpoint."""

    name: ClassVar[str]

    @property
    @abstractmethod
    def address(self) -> str:
        """Where this provider reaches the device (serial, user@host:port, ...)."""

    @abstractmethod
    def ping(self) -> PingResult:
        """Cheapest possible round trip. Never raises for an unreachable device."""

    @abstractmethod
    def status(self) -> StatusResult:
        """Full device telemetry; partial sections are reported via `issues`."""

    @abstractmethod
    def battery(self) -> BatteryReading:
        """Battery telemetry alone."""

    @abstractmethod
    def capabilities(self) -> tuple[CapabilityReport, ...]:
        """One report per catalogue entry, in catalogue order."""

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "address": self.address}
