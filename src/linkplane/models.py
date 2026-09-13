from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Capability:
    name: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class Endpoint:
    transport: str
    address: str
    state: str
    device_id: str
    capabilities: tuple[Capability, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    manufacturer: str | None
    model: str | None
    android: str | None
    capabilities: tuple[Capability, ...]
    endpoints: tuple[Endpoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    summary: str
    fix: str | None = None
    # Stable LP-<CATEGORY>-<NNN> code when the check failed for a classifiable reason
    # (linkplane.core.errors). Additive; None for informational and passing checks.
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryIssue:
    backend: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    devices: tuple[Device, ...]
    issues: tuple[DiscoveryIssue, ...] = ()
