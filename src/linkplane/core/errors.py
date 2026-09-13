"""Structured Linkplane errors with stable, machine-matchable codes.

`LinkplaneError` extends the existing `BridgeError` so every `except BridgeError` in the
codebase keeps working unchanged; what it adds is a stable `code` (``LP-<CATEGORY>-<NNN>``),
optional actionable `hints` for the human CLI rendering, and `classify()` for wrapping a
plain `BridgeError` raised by the transports into the closest category without touching
the transport code.
"""

from __future__ import annotations

import re

from linkplane.transports import BridgeError

# Stable codes. The category names the kind of problem; the number distinguishes specific
# situations within it. Codes are never renumbered or reused once shipped.
CONFIG_MISSING = "LP-CONFIG-001"
CONFIG_INVALID = "LP-CONFIG-002"
DEVICE_NOT_FOUND = "LP-CONFIG-003"
# The configuration file or its directory could not be created, locked, or written.
CONFIG_UNWRITABLE = "LP-CONFIG-004"
CONNECT_UNREACHABLE = "LP-CONNECT-001"
CONNECT_NO_DEVICE = "LP-CONNECT-002"
CONNECT_AMBIGUOUS = "LP-CONNECT-003"
AUTH_FAILED = "LP-AUTH-001"
AUTH_UNAUTHORIZED_DEVICE = "LP-AUTH-002"
# The Linux host may not open the phone's USB device (udev rules / group membership).
# Distinct from LP-AUTH-002, which is the phone declining this computer.
AUTH_USB_PERMISSION = "LP-AUTH-003"
TIMEOUT = "LP-TIMEOUT-001"
PROVIDER_FAILED = "LP-PROVIDER-001"
PROVIDER_BAD_OUTPUT = "LP-PROVIDER-002"
PROVIDER_PARTIAL = "LP-PROVIDER-003"
CAPABILITY_UNSUPPORTED = "LP-CAPABILITY-001"
CAPABILITY_UNAVAILABLE = "LP-CAPABILITY-002"
DEPENDENCY_MISSING = "LP-DEPENDENCY-001"
REQUEST_INVALID = "LP-REQUEST-001"
CANCELLED = "LP-CANCELLED-001"
STATE_CONFLICT = "LP-STATE-001"
DAEMON_NOT_RUNNING = "LP-DAEMON-001"
DAEMON_ALREADY_RUNNING = "LP-DAEMON-002"
DAEMON_PROTOCOL = "LP-DAEMON-003"
# Local API (docs/local-api-design.md §15). CLIENT is the *calling client* (token, scopes);
# AUTH stays the device-side meaning it always had.
CLIENT_UNAUTHENTICATED = "LP-CLIENT-001"
CLIENT_FORBIDDEN = "LP-CLIENT-002"
RESOURCE_NOT_FOUND = "LP-RESOURCE-001"
INTERNAL = "LP-INTERNAL-001"
API_BIND_FAILED = "LP-API-001"

# Human titles for the friendly CLI rendering, keyed by code.
TITLES = {
    CONFIG_MISSING: "Linkplane is not configured.",
    CONFIG_INVALID: "The Linkplane configuration is invalid.",
    DEVICE_NOT_FOUND: "No such device profile.",
    CONFIG_UNWRITABLE: "Linkplane could not write its configuration.",
    CONNECT_UNREACHABLE: "Phone unreachable.",
    CONNECT_NO_DEVICE: "No phone connected.",
    CONNECT_AMBIGUOUS: "More than one phone is connected.",
    AUTH_FAILED: "Authentication failed.",
    AUTH_UNAUTHORIZED_DEVICE: "The phone has not authorized this computer.",
    AUTH_USB_PERMISSION: "This computer is not allowed to access the phone over USB.",
    TIMEOUT: "The phone did not respond in time.",
    PROVIDER_FAILED: "The phone command failed.",
    PROVIDER_BAD_OUTPUT: "The phone returned data Linkplane could not read.",
    PROVIDER_PARTIAL: "Some phone data could not be retrieved.",
    CAPABILITY_UNSUPPORTED: "This device cannot do that.",
    CAPABILITY_UNAVAILABLE: "That capability is not available right now.",
    DEPENDENCY_MISSING: "A required program is not installed.",
    REQUEST_INVALID: "The request is invalid.",
    CANCELLED: "Cancelled.",
    STATE_CONFLICT: "Already running.",
    DAEMON_NOT_RUNNING: "The Linkplane daemon is not running.",
    DAEMON_ALREADY_RUNNING: "The Linkplane daemon is already running.",
    DAEMON_PROTOCOL: "The Linkplane daemon sent something this client could not read.",
    CLIENT_UNAUTHENTICATED: "This client is not authenticated.",
    CLIENT_FORBIDDEN: "This client is not allowed to do that.",
    RESOURCE_NOT_FOUND: "No such resource.",
    INTERNAL: "Linkplane hit an internal error.",
    API_BIND_FAILED: "The local API could not start.",
}

# The pre-existing short `OperationError.code` categories (frozen in docs/api-contracts.md)
# mapped onto the closest stable code, so results built before this module existed still
# surface a PB code in the CLI without changing what they return.
OPERATION_CODE_MAP = {
    "invalid_request": REQUEST_INVALID,
    "transport_unavailable": CONNECT_UNREACHABLE,
    "dependency_missing": DEPENDENCY_MISSING,
    "not_found": DEVICE_NOT_FOUND,
    "already_running": STATE_CONFLICT,
    "operation_failed": PROVIDER_FAILED,
    "cancelled": CANCELLED,
}


class LinkplaneError(BridgeError):
    """A `BridgeError` with a stable code and optional hints."""

    def __init__(self, code: str, message: str, hints: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.hints = tuple(hints)

    @property
    def title(self) -> str:
        return TITLES.get(self.code, "Linkplane error.")

    def to_dict(self) -> dict[str, object]:
        return {
            "type": type(self).__name__,
            "message": str(self),
            "code": self.code,
            "hints": list(self.hints),
        }


_TIMEOUT = re.compile(r"timed out", re.IGNORECASE)
_AUTH = re.compile(
    r"permission denied|publickey|authentication|host key verification", re.IGNORECASE
)
_UNAUTHORIZED = re.compile(r"unauthorized|no authorized", re.IGNORECASE)
# adb's `no permissions` state: the host denied ADB access to the USB device.
_NO_PERMISSIONS = re.compile(r"no permissions", re.IGNORECASE)
_OFFLINE = re.compile(r"\bis offline\b", re.IGNORECASE)
_CONFIG_UNWRITABLE = re.compile(
    r"unable to (?:save|lock|write|create) (?:the )?configuration", re.IGNORECASE
)
_AMBIGUOUS = re.compile(r"multiple ADB devices", re.IGNORECASE)
_NO_DEVICE = re.compile(r"no ADB device|was not found|not connected", re.IGNORECASE)
_UNREACHABLE = re.compile(
    r"connection refused|no route to host|network is unreachable|could not resolve|"
    r"connection reset|connection closed|unreachable|ssh: connect|kex_exchange",
    re.IGNORECASE,
)
_DEPENDENCY = re.compile(r"is not installed", re.IGNORECASE)
_BAD_OUTPUT = re.compile(r"invalid .*json|must contain|unexpected .*response|unable to read", re.IGNORECASE)
_CONFIG = re.compile(r"configuration|config", re.IGNORECASE)

SSH_HINTS = (
    "the phone is on the same network",
    "Termux and its SSH server (sshd) are running on the phone",
    "the configured host, port, user, and key are correct",
)
ADB_HINTS = (
    "the phone is plugged in or wireless debugging is connected",
    "USB debugging is enabled and this computer is authorized",
)
# LP-AUTH-003: fixed on the computer, never by tapping Allow on the phone.
USB_PERMISSION_HINTS = (
    "Linux denied ADB access to the USB device; the phone itself is not asking anything",
    "install your distribution's ADB udev rules (Arch: android-udev; Debian, Ubuntu and "
    "Fedora ship them with the adb package), then unplug and replug the phone",
    "if the rules are installed, make sure your user may access the device (a udev rule "
    "for its vendor id, or membership of the plugdev/adbusers group), then log in again",
)
OFFLINE_HINTS = (
    "unplug the cable and plug it back in, or toggle USB debugging off and on",
    "if the phone was connected wirelessly, reconnect it",
)
CONFIG_UNWRITABLE_HINTS = (
    "check that the directory exists and is writable by your user",
    "LINKPLANE_CONFIG or --config selects a different configuration file",
)


def classify(error: BridgeError, *, provider: str | None = None) -> LinkplaneError:
    """Wrap a plain `BridgeError` in the closest-matching `LinkplaneError`.

    A `LinkplaneError` passes through unchanged. The heuristics match the messages the
    transports actually raise (see `transports.py`); anything unrecognized becomes
    `LP-PROVIDER-001`, which is always a truthful category for "a command on the phone
    side failed", never a guess at connectivity.
    """
    if isinstance(error, LinkplaneError):
        return error
    message = str(error)
    hints: tuple[str, ...] = ()
    if provider == "ssh":
        hints = SSH_HINTS
    elif provider == "adb":
        hints = ADB_HINTS
    if _CONFIG_UNWRITABLE.search(message):
        # Checked first: the OS error it quotes ("Permission denied", "Read-only file
        # system") would otherwise match the SSH authentication pattern below.
        return LinkplaneError(CONFIG_UNWRITABLE, message, CONFIG_UNWRITABLE_HINTS)
    if _UNREACHABLE.search(message):
        # Checked before the timeout pattern: "connect to host ...: Connection timed out"
        # is the phone being unreachable, not a slow command.
        return LinkplaneError(CONNECT_UNREACHABLE, message, hints)
    if _TIMEOUT.search(message):
        return LinkplaneError(TIMEOUT, message, hints)
    if _NO_PERMISSIONS.search(message):
        return LinkplaneError(AUTH_USB_PERMISSION, message, USB_PERMISSION_HINTS)
    if _OFFLINE.search(message):
        return LinkplaneError(CONNECT_UNREACHABLE, message, OFFLINE_HINTS)
    if _UNAUTHORIZED.search(message):
        return LinkplaneError(AUTH_UNAUTHORIZED_DEVICE, message, ADB_HINTS[1:])
    if _AUTH.search(message):
        return LinkplaneError(AUTH_FAILED, message, hints)
    if _AMBIGUOUS.search(message):
        return LinkplaneError(CONNECT_AMBIGUOUS, message, ("pass --serial or --device",))
    if _DEPENDENCY.search(message):
        return LinkplaneError(DEPENDENCY_MISSING, message, ("run `linkplane doctor`",))
    if _NO_DEVICE.search(message):
        return LinkplaneError(CONNECT_NO_DEVICE, message, hints)
    if _BAD_OUTPUT.search(message):
        return LinkplaneError(PROVIDER_BAD_OUTPUT, message)
    if provider is None and _CONFIG.search(message):
        return LinkplaneError(CONFIG_INVALID, message, ("run `linkplane doctor`",))
    return LinkplaneError(PROVIDER_FAILED, message, hints)
