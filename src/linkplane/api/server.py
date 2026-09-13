"""The local HTTP API listener: a transport adapter inside linkplaned (docs/local-api-design.md).

`ApiServer` is a stdlib `ThreadingHTTPServer` that the daemon starts on a thread next to
its control socket. Every request is: Host/Origin check → bearer authentication (header
only) → route → scope → handler. Handlers read the registry, the observed states, the
history file, the job records, and call `ActionRuntime`; they hold no state of their own
and contain no business logic. Errors are `LinkplaneError`s rendered by
`api.security.error_body`; unexpected exceptions become `LP-INTERNAL-001`.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import select
import socket
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from linkplane import __version__
from linkplane.api import API_PROTOCOL, security
from linkplane.api.actions import JOB_ACTIONS, NOT_IMPLEMENTED, spec_for, validate_parameters
from linkplane.api.execute import ActionContext, ActionError, ActionRuntime, capability_reports, effective_capabilities
from linkplane.api.projections import DeviceIdResolver, audit_dict, event_dict, job_dict, rules_dict
from linkplane.automations import read_audit_reverse
from linkplane.clients import Client, authenticate, load_clients, resolve_clients_path
from linkplane.core import errors
from linkplane.core.events import Event, new_id
from linkplane.history import read_history
from linkplane.jobs import TERMINAL, JobRecord, read_jobs
from linkplane.registry import DeviceRecord, Registry
from linkplane.transports import AdbTransport, BridgeError, load_config

log = logging.getLogger("linkplane.api")
log.addFilter(security.RedactingFilter())

EVENTS_DEFAULT_LIMIT, EVENTS_MAX_LIMIT = 100, 1000
JOBS_DEFAULT_LIMIT, JOBS_MAX_LIMIT = 50, 500
AUDIT_DEFAULT_LIMIT, AUDIT_MAX_LIMIT = 100, 1000
STREAM_RETRY_MS = 2000
# How long daemon shutdown waits for open streams to write their final frame and end.
STREAM_DRAIN_TIMEOUT = 2.0
MAX_STREAMS = 64


class ClientStore:
    """`clients.json`, re-read when its mtime changes (so `clients revoke` takes effect on the
    next request without a daemon reload)."""

    def __init__(self, path: str | None = None):
        self.path = path
        self._mtime: int | None = None
        self._clients: tuple[Client, ...] = ()
        self._lock = threading.Lock()

    def clients(self) -> tuple[Client, ...]:
        file = resolve_clients_path(self.path)
        try:
            mtime: int | None = file.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        with self._lock:
            if mtime != self._mtime or (mtime is None and self._clients):
                self._clients = load_clients(self.path) if mtime is not None else ()
                self._mtime = mtime
            return self._clients


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: Mapping[str, str]
    params: dict[str, str] = field(default_factory=dict)
    body: Any = None
    client: Client | None = None
    correlation_id: str = field(default_factory=new_id)

    def first(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None

    def ints(self, name: str, *, minimum: int = 0) -> int | None:
        raw = self.first(name)
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{name} must be an integer") from None
        if value < minimum:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{name} must be >= {minimum}")
        return value

    def limit(self, default: int, maximum: int) -> int:
        value = self.ints("limit", minimum=1)
        if value is None:
            return default
        return min(value, maximum)


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    scope: str | None
    handler: str


ROUTES: tuple[Route, ...] = tuple(
    Route(method, re.compile(pattern), scope, handler) for method, pattern, scope, handler in (
        ("GET", r"/v1/health", None, "health"),
        ("GET", r"/v1/devices", "device.read", "list_devices"),
        ("GET", r"/v1/devices/(?P<device>[^/]+)", "device.read", "get_device"),
        ("GET", r"/v1/devices/(?P<device>[^/]+)/state", "device.state.read", "get_state"),
        ("GET", r"/v1/devices/(?P<device>[^/]+)/capabilities", "device.read", "get_capabilities"),
        ("POST", r"/v1/devices/(?P<device>[^/]+)/actions", None, "post_action"),
        ("GET", r"/v1/jobs", "jobs.read", "list_jobs"),
        ("GET", r"/v1/jobs/(?P<job>[^/]+)", "jobs.read", "get_job"),
        ("POST", r"/v1/jobs/(?P<job>[^/]+)/cancel", "jobs.cancel", "cancel_job"),
        ("GET", r"/v1/events", "events.read", "list_events"),
        ("GET", r"/v1/events/stream", "events.read", "stream_events"),
        ("GET", r"/v1/rules", "rules.read", "list_rules"),
        ("POST", r"/v1/rules/reload", "rules.reload", "reload_rules"),
        ("GET", r"/v1/audit", "audit.read", "list_audit"),
    )
)


def _parse_ts(value: str, name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"{name} must be an ISO-8601 timestamp") from None


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "linkplaned"
    sys_version = ""
    server: "ApiServer"  # type: ignore[assignment]

    # -- plumbing ---------------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("http %s", security.redact(format % args))

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("http error %s", security.redact(format % args))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:
        # A browser preflight is a cross-origin request by definition: there is no CORS grant.
        self._dispatch("OPTIONS")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        request: Request | None = None
        try:
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            query = parse_qs(parts.query, keep_blank_values=True)
            request = Request(method, path, query, self.headers)
            security.check_request_headers(self.headers, port=self.server.port, allowed_origins=self.server.allowed_origins)
            if method == "OPTIONS":
                raise errors.LinkplaneError(errors.CLIENT_UNAUTHENTICATED, "cross-origin requests are not allowed")
            token = security.bearer_token(self.headers, query)
            client = authenticate(token, self.server.clients.clients())
            if client is None:
                raise errors.LinkplaneError(errors.CLIENT_UNAUTHENTICATED, "unknown or revoked token")
            request.client = client
            route, allowed = self._match(method, path, request)
            if route is None:
                if allowed:
                    raise ActionError(errors.REQUEST_INVALID, f"{method} is not allowed on {path}", details={"allow": sorted(allowed)}, status=405)
                raise ActionError(errors.REQUEST_INVALID, f"no such route: {path}", details={"path": path}, status=404)
            if route.scope is not None and not client.allows(route.scope):
                self._audit_blocked(request, route.scope)
                raise ActionError(errors.CLIENT_FORBIDDEN, f"scope {route.scope} is not granted to {client.client_id}", details={"scope": route.scope})
            if method == "POST":
                request.body = self._read_body()
            getattr(self, route.handler)(request)
        except Exception as error:  # noqa: BLE001 - every failure becomes one coded body
            self._send_error(error, request)

    def _match(self, method: str, path: str, request: Request) -> tuple[Route | None, set[str]]:
        allowed: set[str] = set()
        for route in ROUTES:
            match = route.pattern.fullmatch(path)
            if match is None:
                continue
            if route.method == method:
                request.params = {key: unquote(value) for key, value in match.groupdict().items()}
                return route, allowed
            allowed.add(route.method)
        return None, allowed

    def _read_body(self) -> Any:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or 0)
        except ValueError:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "Content-Length must be an integer") from None
        if length > security.MAX_BODY_BYTES:
            raise ActionError(errors.REQUEST_INVALID, "request body too large", details={"max_bytes": security.MAX_BODY_BYTES}, status=413)
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if length and content_type != "application/json":
            raise ActionError(errors.REQUEST_INVALID, "Content-Type must be application/json", status=415)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "request body must be a JSON object")
        return body

    def _send_json(self, status: int, payload: Any, *, code: str | None = None, extra: Mapping[str, str] | None = None) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        for name, value in security.response_headers(status, code).items():
            self.send_header(name, value)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error(self, error: BaseException, request: Request | None) -> None:
        correlation_id = request.correlation_id if request is not None else new_id()
        status, body = security.error_body(error, correlation_id=correlation_id)
        code = body["error"]["code"]
        if status >= 500:
            log.warning("%s %s -> %s %s (%s): %s", self.command, request.path if request else self.path, status, code, correlation_id, str(error))
            log.debug("traceback for %s:\n%s", correlation_id, security.redact("".join(traceback.format_exception(error))))
        else:
            log.info("%s %s -> %s %s (%s)", self.command, request.path if request else self.path, status, code, correlation_id)
        extra = {"Allow": ", ".join(body["error"]["details"].get("allow", []))} if status == 405 else None
        try:
            self._send_json(status, body, code=code, extra=extra)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _audit_blocked(self, request: Request, scope: str, action: str | None = None, device: str | None = None) -> None:
        audit = self.server.daemon.audit
        if audit is None or request.client is None:
            return
        try:
            audit.write("access.blocked" if action is None else "action.blocked", decision="blocked", actor=request.client.actor,
                        source="api", device=device, action=action, correlation_id=request.correlation_id,
                        scope=scope, path=request.path)
        except Exception as error:  # noqa: BLE001
            log.warning("audit write failed: %s", error)

    # -- helpers ----------------------------------------------------------------------

    def _registry(self) -> Registry:
        try:
            return Registry.load(self.server.config_path, states=self.server.daemon.observed_states())
        except BridgeError as error:
            raise errors.LinkplaneError(errors.CONFIG_INVALID, str(error)) from error

    def _device(self, request: Request, registry: Registry | None = None) -> tuple[Registry, DeviceRecord]:
        registry = registry or self._registry()
        device = registry.resolve(request.params["device"])
        if device is None:
            raise ActionError(errors.DEVICE_NOT_FOUND, f"no such device: {request.params['device']}",
                              ("list devices with GET /v1/devices",), details={"device": request.params["device"]})
        return registry, device

    def _resolver(self, registry: Registry | None = None) -> DeviceIdResolver:
        try:
            return DeviceIdResolver(registry or self._registry())
        except errors.LinkplaneError:
            return DeviceIdResolver(None)  # a broken config must not hide records; device_id is then null

    def _audit_api(self, request: Request, kind: str, decision: str, **details: Any) -> None:
        audit = self.server.daemon.audit
        if audit is None or request.client is None:
            return
        try:
            audit.write(kind, decision=decision, actor=request.client.actor, source="api",
                        correlation_id=request.correlation_id, **details)
        except Exception as error:  # noqa: BLE001
            log.warning("audit write failed: %s", error)

    def _device_names(self, registry: Registry, references: list[str]) -> set[str]:
        """Filter values may be canonical ids or aliases; records carry registry names."""
        names: set[str] = set()
        for reference in references:
            device = registry.resolve(reference)
            names.add(device.name if device is not None else reference)
        return names

    # -- routes -----------------------------------------------------------------------

    def health(self, request: Request) -> None:
        daemon = self.server.daemon
        client = request.client
        assert client is not None
        self._send_json(200, {
            "status": "ok",
            "api": API_PROTOCOL,
            "protocol": daemon.PROTOCOL,
            "version": __version__,
            "daemon": {"pid": daemon.pid, "started": daemon.started},
            "last_seq": daemon.last_seq(),
            "client": {"client_id": client.client_id, "client_type": client.client_type, "scopes": list(client.scopes)},
        })

    def list_devices(self, request: Request) -> None:
        registry = self._registry()
        self._send_json(200, {"devices": [device.to_dict() for device in registry.devices()]})

    def get_device(self, request: Request) -> None:
        _registry, device = self._device(request)
        self._send_json(200, device.to_dict())

    def get_state(self, request: Request) -> None:
        _registry, device = self._device(request)
        if device.state is None:
            raise ActionError(errors.RESOURCE_NOT_FOUND, f"{device.device_id} is known but not observed by the daemon",
                              ("SSH-only devices are not observed; connect it over ADB",), details={"device_id": device.device_id})
        self._send_json(200, device.state.to_dict())

    def get_capabilities(self, request: Request) -> None:
        registry, device = self._device(request)
        wanted = request.first("provider")
        if wanted is not None and wanted not in ("adb", "ssh"):
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "provider must be adb or ssh")
        target = registry.target(device)
        by_provider = capability_reports(target, adb_factory=self.server.adb_factory, only=wanted)
        client = request.client
        assert client is not None
        effective = [
            {**report.to_dict(), "provider": provider, "granted": client.allows(report.name)}
            for provider, report in effective_capabilities(by_provider)
        ]
        self._send_json(200, {
            "device_id": device.device_id,
            "queried": datetime.now().astimezone().isoformat(timespec="seconds"),
            "capabilities": effective,
            "by_provider": {name: [report.to_dict() for report in reports] for name, reports in by_provider.items()},
        })

    def post_action(self, request: Request) -> None:
        client = request.client
        assert client is not None
        body = request.body or {}
        action = body.get("action")
        if not isinstance(action, str) or not action:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "action is required")
        request.correlation_id = security.correlation_id_from(body.get("correlation_id"))
        for key in body:
            if key not in ("action", "parameters", "correlation_id"):
                raise ActionError(errors.REQUEST_INVALID, f"unknown field {key!r}", details={"field": key})
        if action in NOT_IMPLEMENTED:
            raise ActionError(errors.CAPABILITY_UNSUPPORTED, f"{action} is not available over the local API",
                              details={"action": action}, not_implemented=True)
        spec = spec_for(action)
        if not client.allows(spec.scope):
            self._audit_blocked(request, spec.scope, action=action)
            raise ActionError(errors.CLIENT_FORBIDDEN, f"scope {spec.scope} is not granted to {client.client_id}",
                              details={"scope": spec.scope})
        registry, device = self._device(request)
        parameters = body.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "parameters must be an object")
        validate_parameters(spec, parameters)
        context = ActionContext(device, registry.target(device), client.actor, request.correlation_id)
        outcome = self.server.runtime.execute(spec, parameters, context)
        if isinstance(outcome, JobRecord):
            self._send_json(202, job_dict(outcome, self._resolver(registry)), extra={"Location": f"/v1/jobs/{outcome.id}"})
        else:
            self._send_json(200, outcome)

    def list_jobs(self, request: Request) -> None:
        registry = self._registry()
        records = list(read_jobs(self.server.daemon.jobs_dir))
        records.sort(key=lambda record: record.started, reverse=True)
        devices = self._device_names(registry, request.query.get("device", []))
        # `action` accepts the public name (`backup.photos`) or the job name (`backup`).
        actions = {JOB_ACTIONS.get(name, name) for name in request.query.get("action", [])}
        states = set(request.query.get("state", []))
        rules = set(request.query.get("rule", []))
        correlation = request.first("correlation_id")
        selected = [
            record for record in records
            if (not devices or record.device in devices)
            and (not actions or record.action in actions)
            and (not states or record.state in states)
            and (not rules or record.automation in rules)
            and (correlation is None or record.correlation_id == correlation)
        ]
        resolve = self._resolver(registry)
        self._send_json(200, {"jobs": [job_dict(record, resolve) for record in selected[: request.limit(JOBS_DEFAULT_LIMIT, JOBS_MAX_LIMIT)]]})

    def _job(self, job_id: str) -> JobRecord:
        for record in read_jobs(self.server.daemon.jobs_dir):
            if record.id == job_id:
                return record
        raise ActionError(errors.RESOURCE_NOT_FOUND, f"no such job: {job_id}", details={"job_id": job_id})

    def get_job(self, request: Request) -> None:
        self._send_json(200, job_dict(self._job(request.params["job"]), self._resolver()))

    def cancel_job(self, request: Request) -> None:
        """Cooperative cancellation through the existing runner (docs/state-machines.md).

        202: accepted, the job will reach `cancelled` at its next boundary (body: the record
        as it stands). 200: already `cancelled` (idempotent). 409 LP-STATE-001: terminal
        (`completed`/`failed`/`skipped`), or recorded as running but not running in this
        daemon (a stale record from a crashed run). 404 LP-RESOURCE-001: unknown id.
        """
        client = request.client
        assert client is not None
        if request.body:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "cancel takes no body")
        job_id = request.params["job"]
        record = self._job(job_id)
        resolve = self._resolver()
        common = {"device": record.device, "action": record.action, "job_id": record.id}
        if record.state == "cancelled":
            self._audit_api(request, "job.cancel", "skipped", reason="already cancelled", **common)
            self._send_json(200, job_dict(record, resolve))
            return
        if record.state in TERMINAL:
            self._audit_api(request, "job.cancel", "blocked", reason=f"job is {record.state}", **common)
            raise ActionError(errors.STATE_CONFLICT, f"job {job_id} is already {record.state}",
                              details={"job_id": job_id, "state": record.state})
        accepted = self.server.daemon.jobs.cancel(job_id, f"cancelled by {client.actor}")
        if not accepted:
            self._audit_api(request, "job.cancel", "blocked", reason="not running in this daemon", **common)
            raise ActionError(errors.STATE_CONFLICT, f"job {job_id} is recorded as {record.state} but is not running in this daemon",
                              ("the record may be stale (a previous daemon run); it cannot be cancelled",),
                              details={"job_id": job_id, "state": record.state, "stale": True})
        self._audit_api(request, "job.cancel", "started", **common)
        self._send_json(202, job_dict(record, resolve))

    # -- rules ----------------------------------------------------------------------------

    def list_rules(self, request: Request) -> None:
        daemon = self.server.daemon
        self._send_json(200, rules_dict(daemon.rules, fired=daemon.fired, resolve=self._resolver()))

    def reload_rules(self, request: Request) -> None:
        """Reload the daemon's configured rules source. No body, no path, no rules."""
        if request.body:
            raise ActionError(errors.REQUEST_INVALID, "reload takes no body: it reloads the daemon's configured rules file",
                              details={"fields": sorted(request.body)})
        daemon = self.server.daemon
        try:
            loaded = daemon.reload_rules()
        except errors.LinkplaneError as error:
            self._audit_api(request, "rules.reload", "error", code=error.code, error=str(error))
            raise
        self._audit_api(request, "rules.reload", "loaded", loaded=len(loaded.active), blocked=len(loaded.blocked))
        summary = rules_dict(loaded, fired=daemon.fired)
        summary.pop("rules")
        self._send_json(200, summary)

    # -- audit ----------------------------------------------------------------------------

    def list_audit(self, request: Request) -> None:
        """Newest first, read backwards, bounded by `limit`; never the whole file."""
        registry = self._registry()
        devices = self._device_names(registry, request.query.get("device", []))
        actors = set(request.query.get("actor", []))
        actions = {JOB_ACTIONS.get(name, name) for name in request.query.get("action", [])}
        decisions = set(request.query.get("decision", []))
        kinds = set(request.query.get("kind", []))
        job_ids = set(request.query.get("job_id", []))
        rules = set(request.query.get("rule", []))
        correlation = request.first("correlation_id")
        since_raw = request.first("since")
        since = _parse_ts(since_raw, "since") if since_raw else None
        limit = request.limit(AUDIT_DEFAULT_LIMIT, AUDIT_MAX_LIMIT)
        resolve = self._resolver(registry)
        entries: list[dict[str, Any]] = []
        for entry in read_audit_reverse(self.server.daemon.audit_file):
            if devices and entry.get("device") not in devices:
                continue
            if actors and entry.get("actor") not in actors:
                continue
            if actions and entry.get("action") not in actions:
                continue
            if decisions and entry.get("decision") not in decisions:
                continue
            if kinds and entry.get("kind") not in kinds:
                continue
            if job_ids and entry.get("job_id") not in job_ids:
                continue
            if rules and entry.get("rule") not in rules:
                continue
            if correlation is not None and entry.get("correlation_id") != correlation:
                continue
            if since is not None:
                try:
                    if datetime.fromisoformat(str(entry.get("ts"))) < since:
                        break  # older than `since`: everything after this is older still
                except ValueError:
                    continue
            entries.append(audit_dict(entry, resolve))
            if len(entries) >= limit:
                break
        self._send_json(200, {"entries": entries})

    # -- events -------------------------------------------------------------------------

    def _event_filter(self, request: Request, registry: Registry) -> Callable[[Event], bool]:
        types = set(request.query.get("type", []))
        devices = self._device_names(registry, request.query.get("device", []))
        correlation = request.first("correlation_id")
        since_raw = request.first("since")
        since = _parse_ts(since_raw, "since") if since_raw else None

        def wanted(event: Event) -> bool:
            if types and event.type not in types:
                return False
            if devices and event.device not in devices and event.device != "*":
                return False
            if correlation is not None and event.correlation_id != correlation:
                return False
            if since is not None:
                try:
                    if datetime.fromisoformat(event.ts) < since:
                        return False
                except ValueError:
                    return False
            return True

        return wanted

    def _history(self) -> Iterator[Event]:
        daemon = self.server.daemon
        if not daemon.history_enabled:
            return iter(())
        return read_history(daemon.history_path)

    def list_events(self, request: Request) -> None:
        daemon = self.server.daemon
        after = request.ints("after")
        before = request.ints("before")
        order = request.first("order") or "asc"
        if order not in ("asc", "desc"):
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "order must be asc or desc")
        if (after is not None or before is not None) and not daemon.history_enabled:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "history is disabled on this daemon; after/before cannot be used")
        registry = self._registry()
        wanted = self._event_filter(request, registry)
        resolve = self._resolver(registry)
        limit = request.limit(EVENTS_DEFAULT_LIMIT, EVENTS_MAX_LIMIT)
        events = [
            event for event in self._history()
            if event.seq is not None
            and (after is None or event.seq > after)
            and (before is None or event.seq < before)
            and wanted(event)
        ]
        if order == "desc":
            events.reverse()
        self._send_json(200, {"events": [event_dict(event, resolve) for event in events[:limit]], "last_seq": daemon.last_seq()})

    def _cursor(self, request: Request) -> int | None:
        """`after` wins over `Last-Event-ID`; both mean "events with seq > N"."""
        after = request.ints("after")
        if after is not None:
            return after
        header = self.headers.get("Last-Event-ID")
        if header is None or header == "":
            return None
        try:
            value = int(header)
        except ValueError:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "Last-Event-ID must be an integer sequence number") from None
        if value < 0:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "Last-Event-ID must be >= 0")
        return value

    def _write_frame(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def _client_gone(self) -> bool:
        readable, _w, _x = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def stream_events(self, request: Request) -> None:
        daemon = self.server.daemon
        after = self._cursor(request)
        if after is not None and not daemon.history_enabled:
            raise errors.LinkplaneError(errors.REQUEST_INVALID, "history is disabled on this daemon; resume is not possible")
        registry = self._registry()
        wanted = self._event_filter(request, registry)
        resolve = self._resolver(registry)
        if not self.server.claim_stream():
            raise ActionError(errors.STATE_CONFLICT, "too many open event streams", details={"max_streams": MAX_STREAMS})
        subscriber = daemon.new_subscriber(types=set(request.query.get("type", [])),
                                           devices=self._device_names(registry, request.query.get("device", [])))
        daemon.add_subscriber(subscriber)
        hooks = self.server.stream_hooks
        try:
            if "after_subscribe" in hooks:
                hooks["after_subscribe"]()
            self.send_response(200)
            for name, value in security.response_headers(200).items():
                if name != "Content-Type":
                    self.send_header(name, value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            last_seq = daemon.last_seq()
            gap = after is not None and last_seq is not None and after > last_seq
            control = {"protocol": API_PROTOCOL, "last_seq": last_seq, "resumed_after": after, "gap": gap}
            self._write_frame(f"retry: {STREAM_RETRY_MS}\nevent: stream\ndata: {json.dumps(control, sort_keys=True)}\n\n")
            # The dedup cursor: everything <= last_sent was already delivered. A gap means
            # nothing could be replayed, so live delivery must not be filtered by it.
            last_sent: int | None = None if gap else after
            if after is not None and not gap:
                for event in self._history():
                    if event.seq is None or event.seq <= after or not wanted(event):
                        continue
                    self._write_frame(_event_frame(event, resolve))
                    last_sent = event.seq
            if "after_replay" in hooks:
                hooks["after_replay"]()
            keepalive = self.server.keepalive
            last_write = time.monotonic()
            # Contract (docs/local-api-design.md, streaming): daemon stop closes every
            # stream *after* `observer.stopped`. The daemon guarantees that ordering by
            # offering the `None` sentinel to each subscriber once the observer has
            # unwound, so the only things that end this loop are that sentinel and a
            # client that went away. Checking `cancel.cancelled` here instead used to end
            # the stream while the final event was still being produced (a race seen as a
            # flaky shutdown test, 2026-09-12). `closing` is a backstop for a subscriber
            # whose sentinel was dropped by a full queue: once the listener is closing
            # and the queue has been drained, there is nothing left to wait for.
            while True:
                if self._client_gone():
                    break
                try:
                    event = subscriber.queue.get(timeout=min(0.5, keepalive))
                except queue.Empty:
                    if "on_idle" in hooks:
                        hooks["on_idle"]()
                    if self.server.closing:
                        break
                    if time.monotonic() - last_write >= keepalive:
                        self._write_frame(": keepalive\n\n")
                        last_write = time.monotonic()
                    continue
                if event is None:
                    break
                if event.seq is not None and last_sent is not None and event.seq <= last_sent:
                    continue  # already delivered by the replay: exactly once per connection
                if not wanted(event):
                    continue
                self._write_frame(_event_frame(event, resolve))
                if event.seq is not None:
                    last_sent = event.seq
                last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            daemon.remove_subscriber(subscriber)
            self.server.release_stream()


def _event_frame(event: Event, resolve: DeviceIdResolver | None = None) -> str:
    identity = f"id: {event.seq}\n" if event.seq is not None else ""
    return f"{identity}event: event\ndata: {json.dumps(event_dict(event, resolve), sort_keys=True)}\n\n"


class ApiServer(ThreadingHTTPServer):
    """One listener; refuses non-loopback binds; owns the client store and the runtime."""

    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False

    def __init__(
        self,
        bind: str,
        port: int,
        daemon: Any,
        *,
        clients_path: str | None = None,
        config_path: str | None = None,
        allowed_origins: tuple[str, ...] = (),
        keepalive: float = 15.0,
        adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
        runtime: ActionRuntime | None = None,
    ):
        host = security.check_bind_address(bind)
        if host == "localhost":
            host = "127.0.0.1"
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.daemon = daemon
        self.config_path = config_path
        self.allowed_origins = tuple(allowed_origins)
        self.keepalive = keepalive
        self.adb_factory = adb_factory
        self.clients = ClientStore(clients_path)
        self.runtime = runtime or ActionRuntime(daemon.jobs, audit=daemon.audit, adb_factory=adb_factory)
        self.stream_hooks: dict[str, Callable[[], None]] = {}
        self.closing = False
        self._streams = 0
        self._streams_lock = threading.Lock()
        self._streams_drained = threading.Condition(self._streams_lock)
        try:
            super().__init__((host, port), ApiHandler)
        except OSError as error:
            raise errors.LinkplaneError(
                errors.API_BIND_FAILED, f"cannot listen on {host}:{port}: {error.strerror or error}",
                ("choose another port with --api-port, or disable the API with --no-api",),
            ) from error

    @property
    def host(self) -> str:
        return str(self.server_address[0])

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def streams(self) -> int:
        with self._streams_lock:
            return self._streams

    def claim_stream(self) -> bool:
        with self._streams_lock:
            if self._streams >= MAX_STREAMS:
                return False
            self._streams += 1
            return True

    def release_stream(self) -> None:
        with self._streams_lock:
            self._streams = max(0, self._streams - 1)
            self._streams_drained.notify_all()

    def wait_streams_drained(self, timeout: float) -> bool:
        """Block until every open stream has ended, or `timeout` elapses (True if drained).

        Called by `close()` after the daemon has offered its end-of-stream sentinel to
        every subscriber, so in the normal shutdown the wait ends as soon as each stream
        thread has written `observer.stopped` and released its slot -- no polling."""
        with self._streams_lock:
            return self._streams_drained.wait_for(lambda: self._streams == 0, timeout=timeout)

    def describe(self) -> dict[str, Any]:
        return {"url": self.url, "protocol": API_PROTOCOL, "streams": self.streams}

    def close(self) -> None:
        """Stop accepting, end streams, release the port; safe to call twice.

        Streams are given a bounded chance to drain first (the daemon has already offered
        their sentinel), so the final `observer.stopped` frame reaches every connected
        client before the listener goes away."""
        self.wait_streams_drained(STREAM_DRAIN_TIMEOUT)
        self.closing = True
        self.shutdown()
        self.server_close()
        self.runtime.close()


def write_discovery(path: Path, server: ApiServer, pid: int) -> None:
    payload = {"url": server.url, "pid": pid, "protocol": API_PROTOCOL}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
