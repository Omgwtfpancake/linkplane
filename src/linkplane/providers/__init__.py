"""Providers implement the public capability vocabulary for one device endpoint.

`select_provider` is the single place the CLI decides *how* to reach a device, mirroring
the long-standing `status --transport auto|adb|ssh` semantics: ADB first, SSH as the
fallback, an explicit choice honoured exactly.
"""

from __future__ import annotations

from typing import Callable

from linkplane.core import errors
from linkplane.providers.adb import ADBProvider
from linkplane.providers.base import BatteryReading, PingResult, Provider
from linkplane.providers.ssh import SSHProvider
from linkplane.transports import AdbTransport, BridgeError, SshTransport

__all__ = [
    "ADBProvider",
    "BatteryReading",
    "PingResult",
    "Provider",
    "SSHProvider",
    "select_provider",
]

TRANSPORTS = ("auto", "adb", "ssh")


def select_provider(
    transport: str = "auto",
    *,
    serial: str | None = None,
    serial_from_profile: bool = False,
    ssh_factory: Callable[[], SshTransport] | None = None,
    adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
) -> Provider:
    """Pick the provider for one command, raising a structured error when none can be."""
    if transport not in TRANSPORTS:
        raise errors.LinkplaneError(
            errors.REQUEST_INVALID, f"unsupported transport: {transport}"
        )
    if transport == "ssh" and serial:
        raise errors.LinkplaneError(
            errors.REQUEST_INVALID, "--serial cannot be used with the SSH transport"
        )

    def ssh_provider() -> Provider:
        if ssh_factory is None:
            raise errors.LinkplaneError(
                errors.CONFIG_MISSING,
                "SSH transport is not configured",
                ("run `linkplane pair ssh` or set ssh.host/ssh.user in the configuration",),
            )
        try:
            ssh = ssh_factory()
        except BridgeError as error:
            raise errors.classify(error) from error
        if not ssh.host or not ssh.user:
            raise errors.LinkplaneError(
                errors.CONFIG_MISSING,
                "SSH requires a host and user in the Linkplane configuration",
                ("run `linkplane pair ssh` to save one",),
            )
        return SSHProvider(ssh)

    if transport == "ssh":
        return ssh_provider()

    adb = adb_factory(serial)
    try:
        adb.select_device()
        return ADBProvider(adb)
    except BridgeError as adb_error:
        if transport == "adb" or (serial and not serial_from_profile):
            raise errors.classify(adb_error, provider="adb") from adb_error
        try:
            return ssh_provider()
        except errors.LinkplaneError as ssh_error:
            raise errors.LinkplaneError(
                errors.CONNECT_UNREACHABLE,
                f"ADB unavailable ({adb_error}); SSH unavailable ({ssh_error})",
                errors.ADB_HINTS + errors.SSH_HINTS,
            ) from ssh_error
