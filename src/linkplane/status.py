from __future__ import annotations

from typing import Callable

from linkplane.core import errors
from linkplane.core.telemetry import StatusRequest, StatusResult, TelemetryIssue
from linkplane.core.errors import LinkplaneError
from linkplane.operations import OperationResult
from linkplane.providers.adb import ADBProvider
from linkplane.providers.base import Provider
from linkplane.providers.ssh import SSHProvider
from linkplane.transports import AdbTransport, BridgeError, SshTransport

__all__ = ["StatusRequest", "StatusResult", "TelemetryIssue", "read_status"]


AdbFactory = Callable[[str | None], AdbTransport]
SshFactory = Callable[[], SshTransport]

# Every failure below keeps the frozen `transport_unavailable` / `invalid_request`
# categories (docs/api-contracts.md); the precise PB code and hints ride along in the
# additive `error_code` / `hints` fields.
def _unavailable(error: LinkplaneError) -> OperationResult[StatusResult]:
    return OperationResult.failure(
        "transport_unavailable", str(error), error_code=error.code, hints=error.hints
    )


def _status_via(provider: Provider) -> OperationResult[StatusResult]:
    return OperationResult.success_with(
        provider.status(),
        operation="device.status",
        resource_id=provider.address,
        provider=provider.name,
    )


def read_status(
    request: StatusRequest,
    *,
    ssh_factory: SshFactory | None = None,
    adb_factory: AdbFactory | None = None,
) -> OperationResult[StatusResult]:
    """Read device telemetry through the provider layer.

    Transport semantics are unchanged from before providers existed: `adb` and `ssh` use
    exactly that provider; `auto` tries ADB first and falls back to SSH unless an explicit
    `serial` (not one inherited from a profile) pins the request to ADB. Partial results
    (a failed telemetry section) are a success with `issues`, never a failure -- that is
    the providers' `status()` contract.
    """
    if request.transport not in {"auto", "adb", "ssh"}:
        return OperationResult.failure(
            "invalid_request", f"unsupported status transport: {request.transport}"
        )
    if request.transport == "ssh" and request.serial:
        return OperationResult.failure(
            "invalid_request", "--serial cannot be used with the SSH transport"
        )

    make_adb = adb_factory or AdbTransport

    def adb_provider() -> Provider:
        return ADBProvider(make_adb(request.serial))

    def ssh_provider() -> Provider:
        if ssh_factory is None:
            raise LinkplaneError(
                errors.CONFIG_MISSING,
                "SSH transport is not configured",
                ("run `linkplane pair ssh` or set ssh.host/ssh.user in the configuration",),
            )
        try:
            return SSHProvider(ssh_factory())
        except BridgeError as error:
            raise errors.classify(error) from error

    if request.transport == "adb":
        try:
            return _status_via(adb_provider())
        except LinkplaneError as error:
            return _unavailable(error)

    if request.transport == "ssh":
        try:
            return _status_via(ssh_provider())
        except LinkplaneError as error:
            return _unavailable(error)

    try:
        return _status_via(adb_provider())
    except LinkplaneError as adb_error:
        if request.serial and not request.serial_from_profile:
            return _unavailable(adb_error)
        try:
            return _status_via(ssh_provider())
        except LinkplaneError as ssh_error:
            return OperationResult.failure(
                "transport_unavailable",
                f"ADB unavailable ({adb_error}); SSH unavailable ({ssh_error})",
                error_code=errors.CONNECT_UNREACHABLE,
                hints=errors.ADB_HINTS + errors.SSH_HINTS,
            )
