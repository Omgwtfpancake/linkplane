"""linkplaned: the persistent control-plane process (Core v0.2, slice 2).

Hosts one `Observer`, fans its events out to the history file, a registry snapshot
(`state.json`), and any number of socket subscribers, and answers a tiny newline-delimited
JSON protocol on a Unix socket:

    -> {"op": "status"}                         <- {"ok": true, "pid": ..., "started": ..., ...}
    -> {"op": "state"}                          <- {"ok": true, "devices": {name: DeviceState}}
    -> {"op": "subscribe", "types": [...], "devices": [...]}
                                                <- {"ok": true, "subscribed": true}
                                                <- one Event object per line, until either side closes
    -> {"op": "stop"}                           <- {"ok": true, "stopping": true}

Errors are `{"ok": false, "code": "LP-...", "message": "..."}`. Everything stays in one
process on threads; there is no network listener and no privilege boundary crossed.
"""

from __future__ import annotations

import json
import logging
import os
import concurrent.futures
import queue
import select
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from linkplane import __version__
from linkplane import actions as actions_module
from linkplane.api import server as api_server
from linkplane.automations import AuditWriter, LoadedRules, load_rules
from linkplane.core import errors
from linkplane.core.automation import Engine, Firing
from linkplane.core.events import Event, now_iso
from linkplane.core.state import DeviceState
from linkplane.history import HistoryWriter
from linkplane.jobs import JobRunner
from linkplane.observe import Observer
from linkplane.operations import CancellationToken

log = logging.getLogger("linkplane.daemon")

STATE_SCHEMA = "linkplane.state/1"
PROTOCOL = "linkplane.daemon/1"


def resolve_socket_path(path: str | None = None) -> Path:
    from linkplane.paths import env, runtime_dir

    if path:
        return Path(os.path.expanduser(path))
    if env("SOCKET"):
        return Path(os.path.expanduser(env("SOCKET") or ""))
    return runtime_dir() / "daemon.sock"


def resolve_state_path(path: str | None = None) -> Path:
    from linkplane.paths import state_file

    return state_file("state.json", path, env_name="STATE")


# -- client side ----------------------------------------------------------------------


def _send_line(sock: socket.socket, payload: dict[str, Any]) -> None:
    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def _read_lines(sock: socket.socket) -> Iterator[dict[str, Any]]:
    buffer = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise errors.LinkplaneError(errors.DAEMON_PROTOCOL, str(error)) from error


def connect(path: str | None = None, *, timeout: float = 3.0) -> socket.socket:
    """Open a client socket, raising LP-DAEMON-001 when no daemon answers."""
    socket_path = resolve_socket_path(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
    except OSError as error:
        sock.close()
        raise errors.LinkplaneError(
            errors.DAEMON_NOT_RUNNING,
            f"no daemon at {socket_path} ({error.strerror or error})",
            ("start it with `linkplane daemon run`",),
        ) from error
    return sock


def request(op: str, path: str | None = None, **fields: Any) -> dict[str, Any]:
    """One request, one reply."""
    with connect(path) as sock:
        _send_line(sock, {"op": op, **fields})
        for reply in _read_lines(sock):
            if not reply.get("ok", False):
                raise errors.LinkplaneError(
                    str(reply.get("code") or errors.DAEMON_PROTOCOL), str(reply.get("message", "daemon error"))
                )
            return reply
    raise errors.LinkplaneError(errors.DAEMON_PROTOCOL, "daemon closed the connection without replying")


def is_running(path: str | None = None) -> bool:
    try:
        request("status", path)
        return True
    except errors.LinkplaneError:
        return False


def subscribe(
    path: str | None = None,
    *,
    types: tuple[str, ...] = (),
    devices: tuple[str, ...] = (),
    cancel: CancellationToken | None = None,
) -> Iterator[Event]:
    """Stream events from the daemon until cancelled or the daemon stops."""
    sock = connect(path)
    sock.settimeout(0.5)
    try:
        _send_line(sock, {"op": "subscribe", "types": list(types), "devices": list(devices)})
        buffer = b""
        acknowledged = False
        while cancel is None or not cancel.cancelled:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                record = json.loads(line)
                if not acknowledged:
                    acknowledged = True
                    if not record.get("ok", False):
                        raise errors.LinkplaneError(
                            str(record.get("code") or errors.DAEMON_PROTOCOL), str(record.get("message"))
                        )
                    continue
                yield Event.from_dict(record)
    finally:
        sock.close()


# -- server side ----------------------------------------------------------------------


class Subscriber:
    def __init__(self, types: set[str], devices: set[str]):
        self.types = types
        self.devices = devices
        self.queue: queue.Queue[Event | None] = queue.Queue(maxsize=1000)

    def wants(self, event: Event) -> bool:
        if self.types and event.type not in self.types:
            return False
        if self.devices and event.device not in self.devices and event.device != "*":
            return False
        return True

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            log.warning("subscriber too slow; dropping %s", event.type)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: C901 - one small dispatch table
        daemon: Daemon = self.server.daemon  # type: ignore[attr-defined]
        line = self.rfile.readline()
        if not line:
            return
        try:
            message = json.loads(line)
            op = str(message.get("op", ""))
        except (json.JSONDecodeError, AttributeError):
            self._reply({"ok": False, "code": errors.REQUEST_INVALID, "message": "malformed request"})
            return
        if op == "status":
            self._reply({"ok": True, **daemon.describe()})
        elif op == "state":
            self._reply({"ok": True, "devices": daemon.snapshot_devices()})
        elif op == "stop":
            self._reply({"ok": True, "stopping": True})
            daemon.stop("stop requested over the control socket")
        elif op == "reload":
            try:
                loaded = daemon.reload_rules()
            except errors.LinkplaneError as error:
                self._reply({"ok": False, "code": error.code, "message": str(error)})
            else:
                self._reply({"ok": True, **loaded.to_dict()})
        elif op == "subscribe":
            subscriber = Subscriber(
                set(message.get("types") or ()), set(message.get("devices") or ())
            )
            daemon.add_subscriber(subscriber)
            try:
                self._reply({"ok": True, "subscribed": True, "protocol": PROTOCOL})
                # Ends on the daemon's end-of-stream sentinel (offered after the observer
                # has produced `observer.stopped`) or when the client leaves -- never on
                # the cancellation flag alone, which would race the final event.
                while True:
                    # A subscriber that went away only fails on the next write; peek for
                    # EOF so the subscriber count and queue are released promptly.
                    readable, _w, _x = select.select([self.request], [], [], 0)
                    if readable and not self.request.recv(1, socket.MSG_PEEK):
                        break
                    try:
                        event = subscriber.queue.get(timeout=0.5)
                    except queue.Empty:
                        if daemon.subscribers_closed:
                            break  # sentinel was dropped by a full queue; nothing left
                        continue
                    if event is None:
                        break
                    self._reply(event.to_dict())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                daemon.remove_subscriber(subscriber)
        else:
            self._reply({"ok": False, "code": errors.REQUEST_INVALID, "message": f"unknown op: {op}"})

    def _reply(self, payload: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        self.wfile.flush()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, daemon: Daemon):
        self.daemon = daemon
        super().__init__(path, _Handler)


class Daemon:
    """One observer, one socket, one snapshot file, many subscribers."""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        state_path: str | None = None,
        history_path: str | None = None,
        identities: dict[str, str] | None = None,
        interval: float = 30.0,
        low_battery: int = 20,
        poll_wifi: bool = True,
        observer_factory: Callable[..., Observer] | None = None,
        write_history: bool = True,
        automations_path: str | None = None,
        audit_path: str | None = None,
        step_runner: Callable[..., Any] | None = None,
        jobs: JobRunner | None = None,
        max_firings: int = 4,
        config_path: str | None = None,
        clients_path: str | None = None,
        api_enabled: bool = False,
        api_bind: str = "127.0.0.1",
        api_port: int = api_server.security.DEFAULT_PORT,
        api_origins: tuple[str, ...] = (),
        api_keepalive: float = 15.0,
        adb_factory: Any = None,
    ):
        self.socket_path = resolve_socket_path(socket_path)
        self.state_path = resolve_state_path(state_path)
        self.history_path = history_path
        self.identities = identities or {}
        self.interval = interval
        self.low_battery = low_battery
        self.poll_wifi = poll_wifi
        self.observer_factory = observer_factory or Observer
        self.write_history = write_history
        self.cancel = CancellationToken()
        self.started = now_iso()
        self.pid = os.getpid()
        self.events_seen = 0
        self._subscribers: list[Subscriber] = []
        self._subscribers_lock = threading.Lock()
        # True once shutdown has offered the end-of-stream sentinel; a subscriber that
        # arrives after that gets its sentinel immediately so no stream can outlive stop.
        self._subscribers_closed = False
        self._observer: Observer | None = None
        self._history: HistoryWriter | None = None
        self._server: _Server | None = None
        self.stop_reason: str | None = None
        # Automations: rules from the file run on a worker thread so a slow action
        # (a script, a phone notification) never delays the observer or subscribers.
        self.automations_path = automations_path
        self.audit_path = audit_path
        self.jobs = jobs or JobRunner()
        base_runner = step_runner or actions_module.run_step
        self.step_runner = base_runner if step_runner is not None else (
            lambda step, event, context, rule: base_runner(step, event, context, rule, jobs=self.jobs)
        )
        self.rules: LoadedRules = LoadedRules("", (), ())
        self.engine = Engine([], self.step_runner)
        # Firings run on a small pool so one long job never delays another rule.
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_firings, thread_name_prefix="linkplaned-firing")
        self.fired = 0
        self._audit: AuditWriter | None = None
        self._rule_queue: queue.Queue[Event | None] = queue.Queue(maxsize=1000)
        self._rule_thread: threading.Thread | None = None
        # Local API (docs/local-api-design.md): a second listener over the same objects.
        # Off by default for embedders and tests; `daemon run` turns it on.
        self.config_path = config_path
        self.clients_path = clients_path
        self.api_enabled = api_enabled
        self.api_bind = api_bind
        self.api_port = api_port
        self.api_origins = tuple(api_origins)
        self.api_keepalive = api_keepalive
        self.adb_factory = adb_factory
        self._api: api_server.ApiServer | None = None
        self._api_thread: threading.Thread | None = None
        self.api_error: errors.LinkplaneError | None = None

    # -- introspection --------------------------------------------------------------

    PROTOCOL = PROTOCOL

    @property
    def audit(self) -> AuditWriter | None:
        return self._audit

    @property
    def api(self) -> api_server.ApiServer | None:
        return self._api

    @property
    def api_address(self) -> tuple[str, int] | None:
        return (self._api.host, self._api.port) if self._api is not None else None

    @property
    def history_enabled(self) -> bool:
        return self._history is not None

    @property
    def jobs_dir(self) -> str:
        return str(self.jobs.records_dir)

    @property
    def audit_file(self) -> str | None:
        return str(self._audit.path) if self._audit is not None else self.audit_path

    def last_seq(self) -> int | None:
        return self._history.seq if self._history is not None else None

    def observed_states(self) -> dict[str, DeviceState]:
        if self._observer is None:
            return {}
        return dict(self._observer.states)

    @staticmethod
    def new_subscriber(*, types: set[str], devices: set[str]) -> Subscriber:
        return Subscriber(types, devices)

    def describe(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            # The package version of the running process (the HTTP `/v1/health` reports
            # the same), so `linkplane setup` can tell an installed CLI apart from a
            # daemon still running older code and restart it. Additive.
            "version": __version__,
            "pid": self.pid,
            "started": self.started,
            "socket": str(self.socket_path),
            "state_file": str(self.state_path),
            "interval": self.interval,
            "events_seen": self.events_seen,
            "subscribers": len(self._subscribers),
            "devices": self.snapshot_devices(),
            "automations": {**self.rules.to_dict(), "fired": self.fired},
            "jobs": [{"device": device, "action": action} for device, action in self.jobs.running()],
            # Local API (docs/local-api-design.md §13): the newest history sequence, so a
            # client can read state and then stream from here. `api` is the listener's
            # address once one exists (slice 1); None until then / when it failed to bind.
            "last_seq": self.last_seq(),
            "api": self._api.describe() if self._api is not None else None,
        }

    def snapshot_devices(self) -> dict[str, dict[str, Any]]:
        if self._observer is None:
            return {}
        return {name: state.to_dict() for name, state in sorted(self._observer.states.items())}

    # -- subscribers ----------------------------------------------------------------

    def add_subscriber(self, subscriber: Subscriber) -> None:
        with self._subscribers_lock:
            self._subscribers.append(subscriber)
            if self._subscribers_closed:
                subscriber.offer(None)  # type: ignore[arg-type]

    @property
    def subscribers_closed(self) -> bool:
        """Shutdown has offered every subscriber its end-of-stream sentinel."""
        with self._subscribers_lock:
            return self._subscribers_closed

    def remove_subscriber(self, subscriber: Subscriber) -> None:
        with self._subscribers_lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    # -- automations ----------------------------------------------------------------

    def reload_rules(self) -> LoadedRules:
        loaded = load_rules(self.automations_path)
        self.rules = loaded
        self.engine.replace_rules(list(loaded.active))
        if self._audit is not None:
            self._audit.write("rules.loaded", decision="loaded", **loaded.to_dict())
            for name, blocked in loaded.blocked:
                self._audit.write("rule.blocked", decision="blocked", rule=name, actions=list(blocked),
                                  reason='privileged actions need "allow" in the rule')
        log.info("automations: %d active, %d blocked from %s", len(loaded.active), len(loaded.blocked), loaded.path)
        return loaded

    def _record_firing(self, firing: Firing) -> None:
        if not all(outcome.skipped for outcome in firing.outcomes):
            self.fired += 1
        if self._audit is not None:
            self._audit.firing(firing)
        log.info("automation %s on %s: %s", firing.automation, firing.event.type, "ok" if firing.ok else "failed")

    def _fire(self, rule: Any, event: Event) -> None:
        try:
            self._record_firing(self.engine.fire(rule, event))
        except Exception as error:  # noqa: BLE001 - one bad rule must not kill the pool
            log.warning("automation %s error on %s: %s", rule.name, event.type, error)
            if self._audit is not None:
                self._audit.write("rule.error", decision="error", actor=f"rule:{rule.name}", rule=rule.name,
                                  device=event.device, correlation_id=event.correlation_id,
                                  event=event.to_dict(), error=str(error))

    def _run_rules(self) -> None:
        while True:
            event = self._rule_queue.get()
            if event is None:
                return
            if event.type == "device.disconnected":
                cancelled = self.jobs.cancel_device(event.device, f"{event.device} disconnected")
                if cancelled and self._audit is not None:
                    self._audit.write("jobs.cancelled", decision="cancelled", device=event.device,
                                      correlation_id=event.correlation_id, count=cancelled, reason="device.disconnected")
            for rule, skipped in self.engine.select(event):
                if skipped is not None:
                    self._record_firing(skipped)
                else:
                    self._pool.submit(self._fire, rule, event)

    # -- fan-out --------------------------------------------------------------------

    def _sink(self, event: Event) -> None:
        stamped = self._history.append(event) if self._history is not None else event
        self.events_seen += 1
        self._write_snapshot()
        if self.rules.active or stamped.type == "device.disconnected":
            try:
                self._rule_queue.put_nowait(stamped)
            except queue.Full:
                log.warning("automation queue full; dropping %s", stamped.type)
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            if subscriber.wants(stamped):
                subscriber.offer(stamped)

    def _write_snapshot(self, *, running: bool = True) -> None:
        payload = {
            "schema": STATE_SCHEMA,
            "updated": now_iso(),
            "daemon": (
                {"pid": self.pid, "started": self.started, "socket": str(self.socket_path)}
                if running
                else None
            ),
            "devices": self.snapshot_devices(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    # -- lifecycle ------------------------------------------------------------------

    def _claim_socket(self) -> None:
        if self.socket_path.exists():
            if is_running(str(self.socket_path)):
                raise errors.LinkplaneError(
                    errors.DAEMON_ALREADY_RUNNING,
                    f"another daemon answers at {self.socket_path}",
                    ("stop it with `linkplane daemon stop`",),
                )
            log.info("removing stale socket %s", self.socket_path)
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

    def stop(self, reason: str = "stopped") -> None:
        self.stop_reason = reason
        self.cancel.cancel(reason)

    # -- local API ------------------------------------------------------------------

    @property
    def api_discovery_path(self) -> Path:
        return self.socket_path.parent / "api.json"

    def _start_api(self) -> None:
        """Start the HTTP listener; a failure is logged and audited, never fatal."""
        if not self.api_enabled:
            return
        try:
            server = api_server.ApiServer(
                self.api_bind, self.api_port, self, clients_path=self.clients_path, config_path=self.config_path,
                allowed_origins=self.api_origins, keepalive=self.api_keepalive,
                **({"adb_factory": self.adb_factory} if self.adb_factory is not None else {}),
            )
        except errors.LinkplaneError as error:
            self.api_error = error
            log.warning("local API not started: %s", error)
            if self._audit is not None:
                self._audit.write("api.error", decision="error", source="api", code=error.code, error=str(error),
                                  bind=f"{self.api_bind}:{self.api_port}")
            return
        self._api = server
        self._api_thread = threading.Thread(target=server.serve_forever, name="linkplaned-api", daemon=True)
        self._api_thread.start()
        try:
            api_server.write_discovery(self.api_discovery_path, server, self.pid)
        except OSError as error:
            log.warning("could not write %s: %s", self.api_discovery_path, error)
        if self._audit is not None:
            self._audit.write("api.started", decision="started", source="api", url=server.url)
        log.info("local API listening on %s", server.url)

    def _stop_api(self) -> None:
        server, self._api = self._api, None
        if server is None:
            return
        try:
            server.close()
        except Exception as error:  # noqa: BLE001 - shutdown must finish
            log.warning("local API close: %s", error)
        if self._api_thread is not None:
            self._api_thread.join(timeout=3)
        try:
            if self.api_discovery_path.exists():
                self.api_discovery_path.unlink()
        except OSError:
            pass
        if self._audit is not None:
            self._audit.write("api.stopped", decision="stopped", source="api")

    def run(self) -> None:
        self._claim_socket()
        if self.write_history:
            self._history = HistoryWriter(self.history_path)
        self._observer = self.observer_factory(
            self._sink,
            identities=self.identities,
            interval=self.interval,
            low_battery=self.low_battery,
            cancel=self.cancel,
            poll_wifi_enabled=self.poll_wifi,
        )
        self._audit = AuditWriter(self.audit_path)
        self._audit.write("daemon.started", decision="started", pid=self.pid, socket=str(self.socket_path))
        if self.jobs.on_transition is None:
            self.jobs.on_transition = lambda record: self._audit is not None and self._audit.job(record)
        try:
            self.reload_rules()
        except errors.LinkplaneError as error:
            # A broken rules file must not stop the daemon from observing.
            log.warning("automations not loaded: %s", error)
            self._audit.write("rules.error", decision="error", error=str(error), code=error.code)
        self._rule_thread = threading.Thread(target=self._run_rules, name="linkplaned-rules", daemon=True)
        self._rule_thread.start()
        self._server = _Server(str(self.socket_path), self)
        os.chmod(self.socket_path, 0o600)
        server_thread = threading.Thread(target=self._server.serve_forever, name="linkplaned-socket", daemon=True)
        server_thread.start()
        log.info("linkplaned listening on %s (pid %s)", self.socket_path, self.pid)
        self._start_api()
        self._write_snapshot()
        try:
            self._observer.run()
        finally:
            self.cancel.cancel(self.stop_reason or "observer exited")
            # Every event the observer produced (including `observer.stopped`) is already
            # queued ahead of this sentinel, which is what lets each stream end *after*
            # delivering it (docs/local-api-design.md, streaming).
            with self._subscribers_lock:
                self._subscribers_closed = True
                for subscriber in self._subscribers:
                    subscriber.offer(None)  # type: ignore[arg-type]
            self._stop_api()
            self._server.shutdown()
            self._server.server_close()
            server_thread.join(timeout=3)
            if self.socket_path.exists():
                self.socket_path.unlink()
            self._write_snapshot(running=False)
            self._rule_queue.put(None)
            if self._rule_thread is not None:
                self._rule_thread.join(timeout=10)
            cancelled = self.jobs.cancel_all(self.stop_reason or "daemon stopping")
            if cancelled:
                log.info("cancelling %d running job(s)", cancelled)
                self.jobs.wait_idle(10)
            self._pool.shutdown(wait=True, cancel_futures=True)
            if self._audit is not None:
                self._audit.write("daemon.stopped", decision="stopped", reason=self.stop_reason or "observer exited", fired=self.fired)
                self._audit.close()
            if self._history is not None:
                self._history.close()
            log.info("linkplaned stopped: %s", self.stop_reason or "observer exited")
