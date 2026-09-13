"""The public capability catalogue and status vocabulary.

A capability names *what* a device can do (``battery.read``), never *how* it is done.
Providers report one `CapabilityReport` per catalogue entry, so `linkplane capabilities`
always lists the same names for every device and simply varies the status.

Status vocabulary (docs/core-v0.1-brief.md, "Capability Model"):

- ``supported``          the provider implements it and it is usable now
- ``unsupported``        this provider can never do it (a different provider might)
- ``unavailable``        the provider could do it, but something is missing right now
                         (a remote script, a host dependency, a disconnected link)
- ``permission-denied``  the phone/OS refused (e.g. ADB not authorized, missing Termux:API grant)
- ``provider-error``     the probe itself failed, so the status is unknown

`requirements` is reserved for the Android capability classes (A normal API, B user
permission, C foreground, D ADB/developer mode, E device-owner, F root, X unreliable) so
later providers can carry them without changing the shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNAVAILABLE = "unavailable"
PERMISSION_DENIED = "permission-denied"
PROVIDER_ERROR = "provider-error"

STATUSES = (SUPPORTED, UNSUPPORTED, UNAVAILABLE, PERMISSION_DENIED, PROVIDER_ERROR)

# The v0.1 core set every provider must report on.
CORE_CAPABILITIES = (
    "device.ping",
    "device.status",
    "battery.read",
    "storage.read",
)

# Capabilities the existing commands already implement; listed so `capabilities` shows the
# real shape of the product, with honest "unsupported" entries per provider.
EXTENDED_CAPABILITIES = (
    "files.send",
    "backup.photos",
    "notify.post",
    "clipboard.read",
    "clipboard.write",
    "screen.control",
    "camera.capture",
    # Local API additions (docs/local-api-design.md §8.1): the existing `find` command and
    # the existing `clipboard sync` / `clipboard-sync` rule action, named as capabilities.
    "device.find",
    "clipboard.sync",
)

CATALOGUE = CORE_CAPABILITIES + EXTENDED_CAPABILITIES

# The `linkplane` subcommand that exercises each capability, for the human rendering.
COMMANDS = {
    "device.ping": "ping",
    "device.status": "status",
    "battery.read": "battery",
    "storage.read": "status",
    "files.send": "send",
    "backup.photos": "backup",
    "notify.post": "notify",
    "clipboard.read": "clipboard get",
    "clipboard.write": "clipboard set",
    "screen.control": "screen",
    "camera.capture": "camera capture",
    "device.find": "find",
    "clipboard.sync": "clipboard sync",
}


@dataclass(frozen=True)
class CapabilityReport:
    name: str
    status: str
    detail: str | None = None
    requirements: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown capability status: {self.status}")

    @property
    def supported(self) -> bool:
        return self.status == SUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def complete(reports: dict[str, CapabilityReport], provider: str) -> tuple[CapabilityReport, ...]:
    """Fill every catalogue entry a provider did not report as `unsupported`, in order."""
    return tuple(
        reports.get(name, CapabilityReport(name, UNSUPPORTED, f"not implemented by the {provider} provider"))
        for name in CATALOGUE
    )
