"""Pure security and error helpers for the local API (design §14, §15, §18.1, §19).

Nothing here opens a socket; everything is testable without a server. The listener
(slice 1) composes these: refuse a non-loopback bind, validate Host and Origin, take the
bearer token from the header only, map an `LP-` code to an HTTP status, render an error
body that never leaks, and redact tokens from log records.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any, Mapping

from linkplane.core import errors
from linkplane.core.events import new_id
from linkplane.operations import OperationCancelled
from linkplane.transports import BridgeError

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
DEFAULT_PORT = 8741
MAX_BODY_BYTES = 64 * 1024
MAX_CORRELATION_ID = 128

# design §15. Codes absent here fall back to 500 (the truthful "unexpected").
STATUS_FOR_CODE: dict[str, int] = {
    errors.REQUEST_INVALID: 400,
    errors.CLIENT_UNAUTHENTICATED: 401,
    errors.CLIENT_FORBIDDEN: 403,
    errors.DEVICE_NOT_FOUND: 404,
    errors.RESOURCE_NOT_FOUND: 404,
    errors.CONNECT_AMBIGUOUS: 409,
    errors.CAPABILITY_UNSUPPORTED: 409,   # this device cannot; 501 only when Linkplane itself does not implement it
    errors.CAPABILITY_UNAVAILABLE: 409,
    errors.STATE_CONFLICT: 409,
    errors.CONFIG_MISSING: 500,
    errors.CONFIG_INVALID: 500,
    errors.CONFIG_UNWRITABLE: 500,
    errors.DAEMON_PROTOCOL: 500,
    errors.INTERNAL: 500,
    errors.AUTH_FAILED: 502,
    errors.PROVIDER_FAILED: 502,
    errors.PROVIDER_BAD_OUTPUT: 502,
    errors.PROVIDER_PARTIAL: 502,
    errors.CONNECT_UNREACHABLE: 503,
    errors.CONNECT_NO_DEVICE: 503,
    errors.AUTH_UNAUTHORIZED_DEVICE: 503,
    errors.AUTH_USB_PERMISSION: 503,
    errors.DEPENDENCY_MISSING: 503,
    errors.CANCELLED: 503,
    errors.DAEMON_NOT_RUNNING: 503,
    errors.DAEMON_ALREADY_RUNNING: 503,
    errors.API_BIND_FAILED: 503,   # never a response body in practice (start-up only); mapped so no code is accidental
    errors.TIMEOUT: 504,
}
RETRY_AFTER_CODES = frozenset({errors.CONNECT_UNREACHABLE, errors.CONNECT_NO_DEVICE, errors.AUTH_UNAUTHORIZED_DEVICE})
INTERNAL_MESSAGE = "internal error; see the daemon log"


class ApiError(errors.LinkplaneError):
    """A `LinkplaneError` with transport context: structured `details` for the body, an
    optional HTTP `status` override for the few places the design fixes one (403 for a bad
    Host/Origin, 404/405 for routing, 413/415 for bodies, 501 for not-implemented), and
    nothing else. Same hierarchy, same codes."""

    def __init__(self, code: str, message: str, hints: tuple[str, ...] = (), *,
                 details: Mapping[str, Any] | None = None, status: int | None = None,
                 not_implemented: bool = False):
        super().__init__(code, message, hints)
        self.details = dict(details or {})
        self.status = status
        self.not_implemented = not_implemented


def status_for(code: str, *, not_implemented: bool = False) -> int:
    """HTTP status for an LP code. `not_implemented` is the one 501 case: a catalogue
    capability the API does not implement (`api.actions.spec_for`)."""
    if not_implemented and code == errors.CAPABILITY_UNSUPPORTED:
        return 501
    return STATUS_FOR_CODE.get(code, 500)


# -- binding ---------------------------------------------------------------------------


def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1, and the name `localhost`; False for everything else,
    including 0.0.0.0, ::, LAN addresses, and any other hostname."""
    candidate = host.strip().strip("[]").lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def check_bind_address(host: str) -> str:
    """The address to bind, or LP-REQUEST-001: remote binding does not exist (design §19)."""
    if not is_loopback(host):
        raise errors.LinkplaneError(
            errors.REQUEST_INVALID,
            f"cannot bind the local API to {host!r}: remote binding is not supported in this version; the API is local-only",
            ("use 127.0.0.1 (the default)",),
        )
    return host.strip()


# -- request validation ----------------------------------------------------------------


def _split_host(value: str) -> tuple[str, int | None]:
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        rest = value[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return value[: end + 1], int(rest[1:])
        return value[: end + 1], None
    host, sep, port = value.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return value, None


def valid_host_header(value: str | None, port: int) -> bool:
    """`Host` must name loopback and the bound port (DNS-rebinding defence, design §19)."""
    if not value:
        return False
    host, given_port = _split_host(value)
    if host.lower() not in LOOPBACK_NAMES:
        return False
    if given_port is None:
        return port == 80
    return given_port == port


def origin_allowed(origin: str | None, allowed: tuple[str, ...] = ()) -> bool:
    """No Origin = not a browser page = fine. An Origin must be on the allow-list exactly."""
    if origin is None or origin == "":
        return True
    return origin.strip().rstrip("/") in {entry.rstrip("/") for entry in allowed}


def check_request_headers(headers: Mapping[str, str], *, port: int, allowed_origins: tuple[str, ...] = ()) -> None:
    """403 LP-CLIENT-001 before authentication when Host or Origin is not acceptable."""
    if not valid_host_header(headers.get("Host"), port):
        raise ApiError(errors.CLIENT_UNAUTHENTICATED, "unacceptable Host header", status=403)
    if not origin_allowed(headers.get("Origin"), allowed_origins):
        raise ApiError(errors.CLIENT_UNAUTHENTICATED, "origin not allowed", status=403)


_BEARER = re.compile(r"^\s*Bearer\s+(?P<token>\S+)\s*$", re.IGNORECASE)


def bearer_token(headers: Mapping[str, str], query: Mapping[str, Any] | None = None) -> str:
    """The token from `Authorization: Bearer …` — the only accepted place.

    A `token` (or `access_token`) query parameter is refused outright with LP-REQUEST-001
    (design §12.3), before anything else, so it is never honoured even if also valid.
    A missing or malformed header is LP-CLIENT-001.
    """
    if query:
        for name in ("token", "access_token", "bearer"):
            if name in query:
                raise errors.LinkplaneError(
                    errors.REQUEST_INVALID, "tokens are not accepted in the URL",
                    ("send `Authorization: Bearer <token>`",),
                )
    header = headers.get("Authorization")
    if not header:
        raise errors.LinkplaneError(errors.CLIENT_UNAUTHENTICATED, "missing Authorization header",
                                    ("send `Authorization: Bearer <token>`",))
    match = _BEARER.match(header)
    if match is None:
        raise errors.LinkplaneError(errors.CLIENT_UNAUTHENTICATED, "malformed Authorization header",
                                    ("send `Authorization: Bearer <token>`",))
    return match.group("token")


def correlation_id_from(value: Any) -> str:
    """The caller's opaque id when acceptable (string, ≤ 128 printable chars), else a new one."""
    if value is None:
        return new_id()
    if not isinstance(value, str) or not value or len(value) > MAX_CORRELATION_ID or not value.isprintable():
        raise errors.LinkplaneError(errors.REQUEST_INVALID, "correlation_id must be a printable string of at most 128 characters")
    return value


# -- error bodies ----------------------------------------------------------------------


def to_linkplane_error(error: BaseException) -> errors.LinkplaneError:
    """Every failure becomes a coded error; anything unexpected is LP-INTERNAL-001 with a
    fixed message (the exception text goes to the log, never to the client)."""
    if isinstance(error, errors.LinkplaneError):
        return error
    if isinstance(error, OperationCancelled):
        return errors.LinkplaneError(errors.CANCELLED, str(error) or "cancelled")
    if isinstance(error, BridgeError):
        return errors.classify(error)
    return errors.LinkplaneError(errors.INTERNAL, INTERNAL_MESSAGE)


def error_body(
    error: BaseException,
    *,
    correlation_id: str,
    details: Mapping[str, Any] | None = None,
    not_implemented: bool = False,
) -> tuple[int, dict[str, Any]]:
    """(status, body) for one failure: the existing error object under `error`, plus the
    correlation id and optional structured details (design §14)."""
    coded = to_linkplane_error(error)
    body = coded.to_dict()
    body["correlation_id"] = correlation_id
    body["details"] = dict(details if details is not None else getattr(coded, "details", None) or {})
    override = getattr(coded, "status", None)
    status = override if isinstance(override, int) else status_for(
        coded.code, not_implemented=not_implemented or bool(getattr(coded, "not_implemented", False)))
    return status, {"error": body}


def response_headers(status: int, code: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json; charset=utf-8", "Linkplane-Api": "1", "Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="linkplane"'
    if code in RETRY_AFTER_CODES:
        headers["Retry-After"] = "5"
    return headers


# -- logging ----------------------------------------------------------------------------

_TOKEN_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b((?:token|access_token|bearer)=)[^\s&]+"),
)
REDACTED = "[redacted]"


def redact(text: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    return text


# Keys whose values never leave the daemon in an HTTP projection, wherever they appear
# in a nested record (design §22, Slice 2 audit privacy). Matched case-insensitively.
SENSITIVE_KEYS = frozenset({
    "text", "command", "stdout", "stderr", "token", "tokens", "token_sha256", "authorization",
    "password", "passphrase", "secret", "secrets", "identity_file", "private_key", "env",
    "environment", "cookie", "credentials",
})


def redact_record(value: Any) -> Any:
    """A deep copy with sensitive keys replaced by REDACTED and token patterns removed
    from every string. The original is never mutated (the audit log stays as written)."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SENSITIVE_KEYS else redact_record(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_record(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


class RedactingFilter(logging.Filter):
    """Attach to the `linkplane.api` logger: bearer tokens never reach a log line, even at
    DEBUG, even inside an exception message."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if isinstance(record.args, dict):
            record.args = {key: _redact_arg(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        return True


def _redact_arg(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))
