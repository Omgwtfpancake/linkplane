from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Generic, Iterator, TypeVar


T = TypeVar("T")

# Version of the typed application-service contract: the `Request`/`Result` dataclasses,
# `OperationResult`/`OperationError`/`ProgressEvent`, the set of `OperationError.code`
# values, and the `(operation, phase)` progress pairs catalogued in docs/api-contracts.md.
# Frozen as of version 1: fields may be *added* (with defaults) and new codes/phases may
# appear without a bump; renaming or removing a field, changing a field's type, removing a
# code, or changing an existing code's meaning is a breaking change and must bump this.
# tests/test_contracts.py pins the frozen surface so an accidental break fails loudly.
CONTRACT_VERSION = 2  # 2 = the Linkplane rename (ADR 0010); every shape unchanged from 1

# Version of the top-level `{"schema_version": ..., "ok": ..., "data"/"error": ...}` JSON
# envelope every `--json` command output shares. Bump this only when that shared envelope
# shape itself changes in a way that breaks consumers — not when an individual command's
# `data` payload gains fields. Defined here (not in cli.py) so application-service modules
# that print their own JSON envelope can import it without a circular import on cli.py.
JSON_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class OperationError:
    code: str
    message: str
    # Stable ``LP-<CATEGORY>-<NNN>`` identifier (linkplane.core.errors); `code` stays the
    # short category consumers have matched on since contract version 1.
    error_code: str | None = None
    hints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProgressEvent:
    operation: str
    phase: str
    message: str
    current: int | None = None
    total: int | None = None
    unit: str | None = None
    item: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ProgressCallback = Callable[[ProgressEvent], None]


def report_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


class OperationCancelled(Exception):
    """Raised inside a service when its `CancellationToken` has been cancelled.

    Service entry points catch this and return `OperationResult.failure("cancelled", ...)`;
    it only escapes to a caller that invokes an internal helper directly.
    """


class CancellationToken:
    """Cooperative, thread-safe cancellation signal for a long-running operation.

    A caller creates one, passes it as the `cancel=` keyword of a service function, and
    calls `cancel()` from any thread (or a signal handler) to ask the operation to stop.
    Operations check the token at their natural boundaries (between files, hosts, or
    polling intervals) and finish the current step first -- an `adb push` already in
    flight is not killed. Nothing is ever partially written because of a cancel: each
    service documents what state a cancelled run leaves behind (docs/api-contracts.md).
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str = "operation cancelled") -> None:
        if not self._event.is_set():
            self._reason = reason
        self._event.set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to `timeout` seconds, returning early (True) if cancelled."""
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled(self._reason or "operation cancelled")


def check_cancelled(token: CancellationToken | None) -> None:
    """Internal helper: raise `OperationCancelled` if a token was passed and is set."""
    if token is not None:
        token.raise_if_cancelled()


def is_cancelled(token: CancellationToken | None) -> bool:
    return token is not None and token.cancelled


@contextmanager
def cancel_on_interrupt(token: CancellationToken) -> Iterator[CancellationToken]:
    """Route the first SIGINT (Ctrl+C) to `token.cancel()` instead of KeyboardInterrupt.

    CLI helper, not a contract surface. The first interrupt asks the running operation to
    stop cleanly at its next boundary; a second interrupt restores Python's default
    handler so it raises `KeyboardInterrupt` as usual. Only the main thread may install
    signal handlers, so this is a no-op (the token is still returned) elsewhere.
    """
    if threading.current_thread() is not threading.main_thread():
        yield token
        return
    previous = signal.getsignal(signal.SIGINT)

    def handler(_signum: int, _frame: Any) -> None:
        token.cancel("interrupted (press Ctrl+C again to force quit)")
        signal.signal(signal.SIGINT, previous)

    signal.signal(signal.SIGINT, handler)
    try:
        yield token
    finally:
        signal.signal(signal.SIGINT, previous)


@dataclass(frozen=True)
class OperationResult(Generic[T]):
    value: T | None = None
    error: OperationError | None = None
    # Optional provenance (docs/core-v0.1-brief.md "Result Model"): which capability ran,
    # against which logical device, through which provider. Additive; unset by services
    # that predate them.
    operation: str | None = None
    resource_id: str | None = None
    provider: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.value is None) == (self.error is None):
            raise ValueError("an operation result must contain exactly one value or error")

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def success(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        """Wire shape of a result with its provenance (docs/local-api-design.md §9.1).

        `value` is rendered through its own `to_dict()` when it has one; results are
        otherwise unchanged. Additive: nothing rendered a whole result before.
        """
        value: Any = self.value
        if value is not None and hasattr(value, "to_dict"):
            value = value.to_dict()
        return {
            "value": value,
            "error": self.error.to_dict() if self.error is not None else None,
            "operation": self.operation,
            "resource_id": self.resource_id,
            "provider": self.provider,
            "warnings": list(self.warnings),
        }

    @classmethod
    def success_with(cls, value: T, **provenance: Any) -> OperationResult[T]:
        return cls(value=value, **provenance)

    @classmethod
    def success(cls, value: T) -> OperationResult[T]:  # type: ignore[no-redef]
        return cls(value=value)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        error_code: str | None = None,
        hints: tuple[str, ...] = (),
    ) -> OperationResult[T]:
        return cls(error=OperationError(code, message, error_code, hints))
