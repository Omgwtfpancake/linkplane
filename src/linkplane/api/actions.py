"""The declarative action table for `POST /v1/devices/{id}/actions` (design §8, §9).

Every API action is a capability name from the catalogue with an explicit execution
mode. Membership of `JOB_ACTIONS` is the contract — it was derived once from today's
service behaviour (the services that take `cancel=` and report progress) and is never
inferred from a function signature again. `parameters` is the allow-list that maps onto
the service's `Request` dataclass; seam fields (serial, transport, factories, install…)
are set by the server and refused from clients.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Mapping

from linkplane.backup import BackupRequest
from linkplane.camera import CaptureRequest
from linkplane.clipboard import ClipboardRequest
from linkplane.core import errors
from linkplane.core.capability import CATALOGUE
from linkplane.core.telemetry import StatusRequest
from linkplane.find import FindPhoneRequest
from linkplane.notification import NotificationRequest
from linkplane.transfer import SendRequest

IMMEDIATE = "immediate"
JOB = "job"
EXECUTIONS = (IMMEDIATE, JOB)

# Request fields the server sets itself; never accepted from a client (design §8.1).
SEAM_FIELDS = frozenset({
    "serial", "transport", "serial_from_profile", "config_path", "state_path", "install",
    "foreground", "extra_arguments",
})


@dataclass(frozen=True)
class ActionSpec:
    name: str
    execution: str
    providers: tuple[str, ...]
    request_type: type | None = None
    parameters: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    fixed: dict[str, Any] = field(default_factory=dict)
    job_action: str | None = None
    slice: int = 1

    def __post_init__(self) -> None:
        if self.name not in CATALOGUE:
            raise ValueError(f"not a catalogue capability: {self.name}")
        if self.execution not in EXECUTIONS:
            raise ValueError(f"unknown execution mode: {self.execution}")
        if (self.execution == JOB) != (self.job_action is not None):
            raise ValueError(f"{self.name}: job actions need a job_action and immediate ones must not have one")
        if self.request_type is not None:
            names = {f.name for f in dataclasses.fields(self.request_type)}
            unknown = set(self.parameters) - names
            if unknown:
                raise ValueError(f"{self.name}: parameters not on {self.request_type.__name__}: {sorted(unknown)}")
            leaked = set(self.parameters) & SEAM_FIELDS
            if leaked:
                raise ValueError(f"{self.name}: seam fields exposed as parameters: {sorted(leaked)}")
        elif self.parameters:
            raise ValueError(f"{self.name}: parameters without a request type")

    @property
    def scope(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.name, "execution": self.execution, "scope": self.scope,
            "providers": list(self.providers), "parameters": list(self.parameters),
            "required": list(self.required), "job_action": self.job_action,
        }


_SPECS = (
    ActionSpec("device.ping", IMMEDIATE, ("adb", "ssh")),
    ActionSpec("device.status", IMMEDIATE, ("adb", "ssh"), StatusRequest),
    ActionSpec("battery.read", IMMEDIATE, ("adb", "ssh")),
    ActionSpec("notify.post", IMMEDIATE, ("adb", "ssh"), NotificationRequest,
               ("message", "title", "notification_id", "dry_run"), ("message",)),
    ActionSpec("device.find", IMMEDIATE, ("adb",), FindPhoneRequest,
               ("duration", "ring", "torch", "vibrate", "message", "dry_run")),
    ActionSpec("clipboard.read", IMMEDIATE, ("ssh",), ClipboardRequest, fixed={"action": "get"}),
    ActionSpec("clipboard.write", IMMEDIATE, ("ssh",), ClipboardRequest, ("text",), ("text",),
               fixed={"action": "set"}),
    ActionSpec("files.send", JOB, ("adb",), SendRequest, ("paths", "destination", "dry_run"), ("paths",),
               job_action="send"),
    ActionSpec("backup.photos", JOB, ("adb",), BackupRequest, ("source", "destination", "dry_run"),
               job_action="backup"),
    ActionSpec("clipboard.sync", JOB, ("ssh",), ClipboardRequest, ("interval", "prefer"),
               fixed={"action": "sync", "foreground": False}, job_action="clipboard-sync"),
    ActionSpec("camera.capture", IMMEDIATE, ("ssh",), CaptureRequest,
               ("output", "camera_id", "force", "dry_run"), slice=2),
)

ACTIONS: dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}
# The declarative job set: capability name -> the job action name rules already use.
JOB_ACTIONS: dict[str, str] = {spec.name: spec.job_action for spec in _SPECS if spec.job_action}
# Inverse: the rules/jobs vocabulary -> the public capability name (design: public
# contracts describe the domain, not the internal vocabulary).
PUBLIC_ACTIONS: dict[str, str] = {job: name for name, job in JOB_ACTIONS.items()}


def public_action(job_action: str) -> str | None:
    """`backup` -> `backup.photos`; None for a job action with no public capability."""
    return PUBLIC_ACTIONS.get(job_action)


# Catalogue capabilities with no API execution entry (501 over HTTP, design §15).
NOT_IMPLEMENTED: tuple[str, ...] = tuple(name for name in CATALOGUE if name not in ACTIONS)


def spec_for(action: str) -> ActionSpec:
    """400 for a name outside the catalogue; 501 (LP-CAPABILITY-001) for a catalogue
    capability the API does not implement; the spec otherwise."""
    if action not in CATALOGUE:
        raise errors.LinkplaneError(
            errors.REQUEST_INVALID, f"unknown action {action!r}",
            (f"actions: {', '.join(sorted(ACTIONS))}",),
        )
    spec = ACTIONS.get(action)
    if spec is None:
        raise errors.LinkplaneError(
            errors.CAPABILITY_UNSUPPORTED, f"{action} is not available over the local API",
        )
    return spec


def execution_of(action: str) -> str:
    return JOB if action in JOB_ACTIONS else IMMEDIATE


def _accepts(annotation: Any, value: Any) -> bool:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return any(_accepts(member, value) for member in typing.get_args(annotation))
    if annotation is type(None):
        return value is None
    if annotation is bool:
        return isinstance(value, bool)
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is str:
        return isinstance(value, str)
    if origin is tuple:
        args = typing.get_args(annotation)
        item = args[0] if args else Any
        return isinstance(value, (list, tuple)) and all(_accepts(item, element) for element in value)
    return True


def validate_parameters(spec: ActionSpec, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Check names, presence, and types against the Request dataclass; return the clean map.

    Raises LP-REQUEST-001 naming the offending parameter. Lists become tuples where the
    field is a tuple.
    """
    given = dict(parameters or {})
    if parameters is not None and not isinstance(parameters, Mapping):
        raise errors.LinkplaneError(errors.REQUEST_INVALID, "parameters must be an object")
    for name in given:
        if name in SEAM_FIELDS:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, f"parameter {name!r} is set by Linkplane, not by clients")
        if name not in spec.parameters:
            raise errors.LinkplaneError(
                errors.REQUEST_INVALID, f"unknown parameter {name!r} for {spec.name}",
                (f"accepted: {', '.join(spec.parameters) or 'none'}",),
            )
    for name in spec.required:
        if name not in given:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{spec.name} requires parameter {name!r}")
    if spec.request_type is None:
        return {}
    hints = typing.get_type_hints(spec.request_type)
    clean: dict[str, Any] = {}
    for name, value in given.items():
        annotation = hints.get(name, Any)
        if not _accepts(annotation, value):
            raise errors.LinkplaneError(
                errors.REQUEST_INVALID, f"parameter {name!r} has the wrong type for {spec.name}",
            )
        if typing.get_origin(annotation) is tuple and isinstance(value, list):
            value = tuple(value)
        clean[name] = value
    return clean


def build_request(spec: ActionSpec, parameters: Mapping[str, Any] | None, **seams: Any) -> Any:
    """The service Request: validated client parameters + fixed values + server seams."""
    clean = validate_parameters(spec, parameters)
    if spec.request_type is None:
        return None
    allowed = {f.name for f in dataclasses.fields(spec.request_type)}
    for name in seams:
        if name not in allowed:
            raise TypeError(f"{spec.request_type.__name__} has no field {name!r}")
    return spec.request_type(**{**clean, **spec.fixed, **seams})
