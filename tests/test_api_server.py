"""The local HTTP API inside linkplaned (docs/local-api-design.md, Slice 1 release gate).

Hermetic: a fake observer, fake providers and service runners, a temporary config,
clients file, history, audit log, and job directory; real HTTP over an ephemeral loopback
port. No phone.
"""

import http.client
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linkplane import clients as clients_module
from linkplane import daemon as daemond
from linkplane.api import execute, security
from linkplane.automations import read_audit
from linkplane.core import errors
from linkplane.core.capability import CATALOGUE, SUPPORTED, UNSUPPORTED, CapabilityReport, complete
from linkplane.core.events import Event
from linkplane.core.state import CONNECTED, DeviceState
from linkplane.core.telemetry import StatusResult
from linkplane.history import read_history
from linkplane.jobs import JobRunner
from linkplane.operations import CancellationToken, OperationResult, ProgressEvent
from linkplane.providers.base import BatteryReading, PingResult

CONFIG = {
    "default_device": "phone",
    "devices": {
        "phone": {"device_id": "HW1", "adb": {"serials": ["S1"]}, "ssh": {"host": "phone.local", "user": "u0", "port": 8022}},
        "tablet": {"device_id": "HW2", "adb": {"serials": ["S2"]}},
    },
}
FULL_SCOPES = ["read", "device.ping", "device.status", "battery.read", "notify.post", "device.find",
               "clipboard.read", "clipboard.write", "clipboard.sync", "files.send", "backup.photos"]


class FakeObserver:
    # When set, `observer.stopped` is held back until the gate opens, so a test can place
    # it deterministically *after* a stream loop has already seen the cancellation.
    stop_gate: threading.Event | None = None

    def __init__(self, sink, *, cancel: CancellationToken, **kwargs):
        self.sink = sink
        self.cancel = cancel
        self.states = {"phone": DeviceState("phone", "adb", CONNECTED, "S1", {"level": 55}, "Home")}

    def run(self):
        self.sink(Event("observer.started", "*", data={"sources": ["fake"], "interval": 1.0}))
        self.cancel.wait(30)
        if FakeObserver.stop_gate is not None:
            FakeObserver.stop_gate.wait(5)
        self.sink(Event("observer.stopped", "*"))


class FakeProvider:
    def __init__(self, name="adb", *, unsupported=(), raise_with=None):
        self.name = name
        self.address = "S1" if name == "adb" else "u0@phone.local:8022"
        self.unsupported = set(unsupported)
        self.raise_with = raise_with

    def ping(self):
        return PingResult(True, self.name, self.address, 1.5)

    def battery(self):
        if self.raise_with is not None:
            raise self.raise_with
        return BatteryReading(55, "charging", "good", ("usb",), self.name)

    def status(self):
        return StatusResult(self.name, {"model": "Fake"}, {"level": 55}, None, None, 10)

    def capabilities(self):
        return complete({name: CapabilityReport(name, UNSUPPORTED if name in self.unsupported else SUPPORTED)
                         for name in CATALOGUE}, self.name)


def ok(payload):
    return OperationResult.success(SimpleNamespace(to_dict=lambda: payload))


class ApiFixture(unittest.TestCase):
    """One daemon with the API on an ephemeral port, per test."""

    provider_kwargs: dict = {}

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.base = base
        self.config_path = str(base / "config.json")
        Path(self.config_path).write_text(json.dumps(CONFIG))
        self.clients_path = str(base / "clients.json")
        _full, self.token = clients_module.create_client("full", client_type="gui", scopes=FULL_SCOPES, path=self.clients_path)
        _reader, self.reader_token = clients_module.create_client("reader", scopes=["read"], path=self.clients_path)
        self.history = str(base / "events.jsonl")
        self.audit = str(base / "audit.jsonl")
        self.jobs = JobRunner(str(base / "jobs"), retry_delays=())
        self.selected = []

        def fake_select(transport, *, serial=None, serial_from_profile=False, ssh_factory=None, adb_factory=None):
            self.selected.append((transport, serial))
            return FakeProvider("ssh" if transport == "ssh" else "adb", **self.provider_kwargs)

        self.select_patch = patch("linkplane.api.execute.select_provider", side_effect=fake_select)
        self.select_patch.start()
        self.runners_patch = patch.dict(execute.RUNNERS, {
            "notify.post": lambda provider, target, request, **kw: ok({"sent": True, "message": request.message, "transport": request.transport}),
            "device.find": lambda provider, target, request, **kw: ok({"found": True, "serial": request.serial}),
            "clipboard.read": lambda provider, target, request, **kw: ok({"text": "secret clipboard", "action": request.action}),
            "clipboard.write": lambda provider, target, request, **kw: ok({"action": request.action, "text": request.text}),
        })
        self.runners_patch.start()
        self.daemon = self.make_daemon(api_port=0)
        self.thread = self.start(self.daemon)
        self.assertIsNotNone(self.daemon.api, self.daemon.api_error)
        self.port = self.daemon.api.port

    def tearDown(self):
        self.daemon.stop()
        self.thread.join(5)
        self.select_patch.stop()
        self.runners_patch.stop()
        self.directory.cleanup()

    def make_daemon(self, **overrides):
        options = dict(
            socket_path=str(self.base / f"{len(self.selected)}{overrides.get('api_port', 0)}.sock"), state_path=str(self.base / "state.json"),
            history_path=self.history, identities={"S1": "phone"}, interval=1.0, observer_factory=FakeObserver,
            audit_path=self.audit, automations_path=str(self.base / "rules.json"), jobs=self.jobs,
            config_path=self.config_path, clients_path=self.clients_path, api_enabled=True, api_bind="127.0.0.1",
            api_keepalive=0.2,
        )
        options.update(overrides)
        return daemond.Daemon(**options)

    @staticmethod
    def start(daemon):
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        for _ in range(200):
            if daemon.api is not None or daemon.api_error is not None:
                break
            time.sleep(0.02)
        for _ in range(200):
            if daemond.is_running(str(daemon.socket_path)):
                break
            time.sleep(0.02)
        return thread

    # -- HTTP helpers -------------------------------------------------------------------

    def connection(self, port=None):
        return http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=5)

    def http(self, method, path, *, token="full", body=None, headers=None, raw=None, port=None):
        request_headers = {}
        if token == "full":
            request_headers["Authorization"] = f"Bearer {self.token}"
        elif token == "reader":
            request_headers["Authorization"] = f"Bearer {self.reader_token}"
        elif token:
            request_headers["Authorization"] = token
        data = None
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        conn = self.connection(port)
        try:
            conn.request(method, path, body=data, headers=request_headers)
            response = conn.getresponse()
            text = response.read().decode()
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                payload = text
            return response.status, payload, dict(response.getheaders())
        finally:
            conn.close()

    def open_stream(self, path="/v1/events/stream", *, token="full", headers=None):
        conn = self.connection()
        request_headers = {"Authorization": f"Bearer {self.token if token == 'full' else self.reader_token}"} if token else {}
        request_headers.update(headers or {})
        conn.request("GET", path, headers=request_headers)
        response = conn.getresponse()
        return conn, response

    @staticmethod
    def read_frame(response, timeout=5.0):
        """One SSE frame as {field: value}; keepalive comments are skipped; None on EOF."""
        deadline = time.monotonic() + timeout
        frame = {}
        while time.monotonic() < deadline:
            line = response.readline()
            if not line:
                return None
            line = line.decode().rstrip("\n")
            if line == "":
                if frame:
                    return frame
                continue
            if line.startswith(":"):
                continue
            name, _sep, value = line.partition(":")
            frame[name] = value.lstrip()
        raise AssertionError("no frame within the timeout")

    emit_lock = threading.Lock()

    def emit(self, type_, device="phone", **data):
        """Inject one canonical event and return its seq (serialized so the seq is exact)."""
        with self.emit_lock:
            self.daemon._sink(Event(type_, device, provider="adb", data=data))
            return self.daemon.last_seq()

    def audit_entries(self, kind=None):
        entries = list(read_audit(self.audit))
        return [entry for entry in entries if kind is None or entry["kind"] == kind]


class AuthenticationTests(ApiFixture):
    def test_health_needs_a_token_and_reports_the_client(self):
        status, body, headers = self.http("GET", "/v1/health", token=None)
        self.assertEqual((status, body["error"]["code"]), (401, errors.CLIENT_UNAUTHENTICATED))
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        status, body, headers = self.http("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["api"], "linkplane.api/1")
        self.assertEqual(body["client"]["client_id"], "full")
        self.assertEqual(body["version"], "0.4.0")
        self.assertEqual(headers["Linkplane-Api"], "1")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(set(body), {"status", "api", "protocol", "version", "daemon", "last_seq", "client"})
        self.assertEqual(set(body["daemon"]), {"pid", "started"})  # no paths, no config

    def test_unknown_malformed_and_revoked_tokens_are_401(self):
        for header in ("Bearer nope", "Basic abc", "Bearer", f"Token {self.token}"):
            status, body, _ = self.http("GET", "/v1/devices", token=header)
            self.assertEqual((status, body["error"]["code"]), (401, errors.CLIENT_UNAUTHENTICATED), header)
        clients_module.revoke_client("full", path=self.clients_path)
        status, _, _ = self.http("GET", "/v1/devices")
        self.assertEqual(status, 401)  # revoked: file re-read on mtime change, no reload needed

    def test_query_tokens_are_refused_even_with_a_valid_header(self):
        for name in ("token", "access_token", "bearer"):
            status, body, _ = self.http("GET", f"/v1/health?{name}={self.token}")
            self.assertEqual((status, body["error"]["code"]), (400, errors.REQUEST_INVALID), name)
            self.assertNotIn(self.token, json.dumps(body))

    def test_host_origin_and_preflight_are_refused_before_authentication(self):
        status, body, _ = self.http("GET", "/v1/health", headers={"Host": "evil.example:80"})
        self.assertEqual((status, body["error"]["code"]), (403, errors.CLIENT_UNAUTHENTICATED))
        status, body, _ = self.http("GET", "/v1/health", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 403)
        status, body, headers = self.http("OPTIONS", "/v1/devices", headers={"Origin": "http://localhost:3000",
                                                                                "Access-Control-Request-Method": "GET"})
        self.assertEqual(status, 403)
        self.assertFalse(any(name.lower().startswith("access-control") for name in headers))

    def test_tokens_never_reach_the_log(self):
        with patch.dict(execute.RUNNERS, {"device.find": lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"leak Bearer {self.token}"))}):
            with self.assertLogs("linkplane.api", level="DEBUG") as captured:
                status, body, _ = self.http("POST", "/v1/devices/HW1/actions", body={"action": "device.find"})
                self.http("GET", "/v1/health?bearer=" + self.token)
        self.assertEqual(status, 500)
        text = "\n".join(captured.output)
        self.assertNotIn(self.token, text)
        self.assertIn("[redacted]", text)
        self.assertNotIn(self.token, json.dumps(body))


class ScopeTests(ApiFixture):
    def test_read_scopes_allow_reads_and_deny_actions(self):
        status, body, _ = self.http("GET", "/v1/devices", token="reader")
        self.assertEqual(status, 200)
        status, body, _ = self.http("POST", "/v1/devices/HW1/actions", token="reader", body={"action": "battery.read"})
        self.assertEqual((status, body["error"]["code"], body["error"]["details"]["scope"]), (403, errors.CLIENT_FORBIDDEN, "battery.read"))
        blocked = self.audit_entries("action.blocked")
        self.assertEqual((blocked[-1]["actor"], blocked[-1]["source"], blocked[-1]["action"]), ("client:reader", "api", "battery.read"))
        self.assertEqual(self.selected, [])  # denied before any provider was touched

    def test_route_scope_denial_is_audited_without_leaking_the_resource(self):
        clients_module.create_client("nothing", scopes=["device.ping"], path=self.clients_path)
        token = [line for line in Path(self.clients_path).read_text().splitlines() if "nothing" in line]
        _client, plaintext = clients_module.create_client("pinger", scopes=["device.ping"], path=self.clients_path)
        status, body, _ = self.http("GET", "/v1/devices/no-such-device/state", token=f"Bearer {plaintext}")
        self.assertEqual((status, body["error"]["code"]), (403, errors.CLIENT_FORBIDDEN))  # 403 before 404
        self.assertEqual(self.audit_entries("access.blocked")[-1]["details"]["scope"], "device.state.read")


class DeviceTests(ApiFixture):
    def test_devices_use_the_canonical_id_and_resolve_aliases(self):
        status, body, _ = self.http("GET", "/v1/devices")
        self.assertEqual(status, 200)
        by_id = {device["device_id"]: device for device in body["devices"]}
        self.assertEqual(set(by_id), {"HW1", "HW2"})
        phone = by_id["HW1"]
        self.assertEqual((phone["name"], phone["observed"], phone["connection"], phone["default"]), ("phone", True, "connected", True))
        self.assertNotIn("identity_file", json.dumps(body))
        for reference in ("HW1", "phone", "serial:S1"):
            status, body, _ = self.http("GET", f"/v1/devices/{reference}")
            self.assertEqual((status, body["device_id"]), (200, "HW1"), reference)
        status, body, _ = self.http("GET", "/v1/devices/nope")
        self.assertEqual((status, body["error"]["code"]), (404, errors.DEVICE_NOT_FOUND))

    def test_profile_rename_keeps_the_resource_identity(self):
        renamed = json.loads(json.dumps(CONFIG))
        renamed["devices"]["work-phone"] = renamed["devices"].pop("phone")
        renamed["default_device"] = "work-phone"
        Path(self.config_path).write_text(json.dumps(renamed))
        status, body, _ = self.http("GET", "/v1/devices/HW1")
        self.assertEqual((status, body["device_id"], body["name"]), (200, "HW1", "work-phone"))
        status, _, _ = self.http("GET", "/v1/devices/phone")
        self.assertEqual(status, 404)

    def test_state_is_the_observed_record_or_404_when_unobserved(self):
        status, body, _ = self.http("GET", "/v1/devices/HW1/state")
        self.assertEqual((status, body["connection"], body["battery"]["level"], body["device"]), (200, "connected", 55, "phone"))
        status, body, _ = self.http("GET", "/v1/devices/HW2/state")
        self.assertEqual((status, body["error"]["code"]), (404, errors.RESOURCE_NOT_FOUND))

    def test_capabilities_are_per_provider_with_an_effective_view_and_grants(self):
        reports = {
            "adb": complete({"battery.read": CapabilityReport("battery.read", SUPPORTED, "ADB shell")}, "adb"),
            "ssh": complete({"battery.read": CapabilityReport("battery.read", SUPPORTED, "Termux"),
                             "clipboard.read": CapabilityReport("clipboard.read", SUPPORTED, "Termux:API")}, "ssh"),
        }
        with patch("linkplane.api.server.capability_reports", return_value=reports) as reporter:
            status, body, _ = self.http("GET", "/v1/devices/phone/capabilities", token="reader")
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], "HW1")
        effective = {entry["name"]: entry for entry in body["capabilities"]}
        self.assertEqual((effective["battery.read"]["provider"], effective["battery.read"]["status"]), ("adb", "supported"))
        self.assertEqual((effective["clipboard.read"]["provider"], effective["clipboard.read"]["status"]), ("ssh", "supported"))
        self.assertEqual(effective["screen.control"]["status"], "unsupported")
        self.assertTrue(effective["battery.read"]["granted"] is False)  # reader has no battery.read
        self.assertEqual(set(body["by_provider"]), {"adb", "ssh"})
        self.assertEqual(reporter.call_args.kwargs["only"], None)
        status, body, _ = self.http("GET", "/v1/devices/phone/capabilities?provider=usb")
        self.assertEqual(status, 400)


class ActionTests(ApiFixture):
    def test_immediate_action_returns_a_result_with_provenance(self):
        status, body, headers = self.http("POST", "/v1/devices/HW1/actions",
                                          body={"action": "notify.post", "parameters": {"message": "hi", "title": "CI"}, "correlation_id": "ci-291"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["action"], body["device_id"], body["device"], body["provider"], body["actor"], body["correlation_id"]),
                         ("notify.post", "HW1", "phone", "adb", "client:full", "ci-291"))
        self.assertEqual(body["value"], {"sent": True, "message": "hi", "transport": "adb"})
        self.assertEqual(body["warnings"], [])
        self.assertEqual(self.selected, [("auto", "S1")])  # provider chosen by Linkplane, not the client
        kinds = [entry["kind"] for entry in self.audit_entries() if entry.get("correlation_id") == "ci-291"]
        self.assertEqual(kinds, ["action.requested", "action.completed"])
        requested = self.audit_entries("action.requested")[-1]
        self.assertEqual((requested["actor"], requested["source"], requested["device"], requested["details"]["parameters"]),
                         ("client:full", "api", "phone", {"message": "hi", "title": "CI"}))

    def test_provider_methods_are_immediate_actions_too(self):
        for action in ("device.ping", "battery.read", "device.status"):
            status, body, _ = self.http("POST", "/v1/devices/phone/actions", body={"action": action})
            self.assertEqual((status, body["action"]), (200, action), body)
        self.assertEqual(body["value"]["transport"], "adb")

    def test_ssh_only_actions_pick_the_ssh_provider_and_hide_clipboard_text_from_audit(self):
        status, body, _ = self.http("POST", "/v1/devices/HW1/actions", body={"action": "clipboard.write", "parameters": {"text": "secret clipboard"}})
        self.assertEqual((status, body["provider"]), (200, "ssh"))
        self.assertEqual(self.selected[-1][0], "ssh")
        audit_text = Path(self.audit).read_text()
        self.assertNotIn("secret clipboard", audit_text)
        self.assertIn('"text_length": 16', audit_text)
        status, body, _ = self.http("POST", "/v1/devices/HW2/actions", body={"action": "clipboard.read"})
        self.assertEqual((status, body["error"]["code"]), (409, errors.CAPABILITY_UNAVAILABLE))  # tablet has no SSH

    def test_request_validation(self):
        cases = [
            ({"action": "files.backup"}, 400, errors.REQUEST_INVALID),
            ({"action": "screen.control"}, 501, errors.CAPABILITY_UNSUPPORTED),
            ({"action": "notify.post"}, 400, errors.REQUEST_INVALID),
            ({"action": "notify.post", "parameters": {"message": 5}}, 400, errors.REQUEST_INVALID),
            ({"action": "notify.post", "parameters": {"message": "x", "serial": "S9"}}, 400, errors.REQUEST_INVALID),
            ({"action": "notify.post", "parameters": {"message": "x"}, "provider": "adb"}, 400, errors.REQUEST_INVALID),
            ({"action": "notify.post", "parameters": {"message": "x"}, "correlation_id": "x" * 200}, 400, errors.REQUEST_INVALID),
            ({"action": "notify.post", "parameters": "nope"}, 400, errors.REQUEST_INVALID),
        ]
        for body, expected_status, code in cases:
            status, payload, _ = self.http("POST", "/v1/devices/HW1/actions", body=body)
            self.assertEqual((status, payload["error"]["code"]), (expected_status, code), body)
        status, payload, _ = self.http("POST", "/v1/devices/nope/actions", body={"action": "battery.read"})
        self.assertEqual((status, payload["error"]["code"]), (404, errors.DEVICE_NOT_FOUND))
        status, payload, _ = self.http("POST", "/v1/devices/HW1/actions", raw=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, payload, _ = self.http("POST", "/v1/devices/HW1/actions", raw=b"x=1", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, payload, _ = self.http("POST", "/v1/devices/HW1/actions", raw=b"{}", headers={"Content-Type": "application/json", "Content-Length": str(security.MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)

    def test_job_action_returns_202_then_409_for_a_duplicate_with_the_running_id(self):
        gate = threading.Event()

        def backup(provider, target, request, *, progress, cancel):
            progress(ProgressEvent("backup", "item_completed", "IMG_1.jpg", current=1, total=2, unit="files"))
            gate.wait(5)
            return ok({"downloaded": 2, "serial": request.serial})

        with patch.dict(execute.RUNNERS, {"backup.photos": backup}):
            status, body, headers = self.http("POST", "/v1/devices/HW1/actions",
                                              body={"action": "backup.photos", "parameters": {"source": "DCIM"}, "correlation_id": "bk-1"})
            self.assertEqual(status, 202, body)
            job_id = body["id"]
            self.assertEqual(headers["Location"], f"/v1/jobs/{job_id}")
            self.assertEqual((body["state"], body["action"], body["job_action"], body["device"], body["device_id"], body["actor"], body["rule"], body["automation"], body["correlation_id"]),
                             ("running", "backup.photos", "backup", "phone", "HW1", "client:full", None, "", "bk-1"))
            status, dup, _ = self.http("POST", "/v1/devices/phone/actions", body={"action": "backup.photos"})
            self.assertEqual((status, dup["error"]["code"], dup["error"]["details"]["job_id"]), (409, errors.STATE_CONFLICT, job_id))
            status, record, _ = self.http("GET", f"/v1/jobs/{job_id}")
            self.assertEqual((status, record["state"]), (200, "running"))
            gate.set()
            for _ in range(100):
                status, record, _ = self.http("GET", f"/v1/jobs/{job_id}")
                if record["state"] == "completed":
                    break
                time.sleep(0.02)
        self.assertEqual((record["state"], record["result"]["downloaded"], record["attempt"]), ("completed", 2, 1))
        status, listing, _ = self.http("GET", "/v1/jobs?device=HW1&state=completed")
        self.assertEqual([job["id"] for job in listing["jobs"]], [job_id])
        for name in ("backup.photos", "backup"):  # public name is authoritative; the job name still works
            status, listing, _ = self.http("GET", f"/v1/jobs?action={name}&state=completed")
            self.assertEqual([job["action"] for job in listing["jobs"]], ["backup.photos"], name)
        status, listing, _ = self.http("GET", "/v1/jobs?state=skipped")
        self.assertEqual(len(listing["jobs"]), 1)  # the duplicate is a skipped record, from the runner
        started = self.audit_entries("job.started")[-1]
        self.assertEqual((started["actor"], started["source"], started["job_id"]), ("client:full", "api", job_id))
        self.assertEqual(self.audit_entries("action.requested")[-1]["job_id"], job_id)
        status, body, _ = self.http("GET", "/v1/jobs/none")
        self.assertEqual((status, body["error"]["code"]), (404, errors.RESOURCE_NOT_FOUND))


class ErrorMappingTests(ApiFixture):
    def test_lp_codes_map_to_the_design_statuses(self):
        with patch("linkplane.api.execute.select_provider", side_effect=errors.LinkplaneError(errors.CONNECT_NO_DEVICE, "no ADB device connected", errors.ADB_HINTS)):
            status, body, headers = self.http("POST", "/v1/devices/HW1/actions", body={"action": "battery.read"})
        self.assertEqual((status, body["error"]["code"], headers["Retry-After"]), (503, errors.CONNECT_NO_DEVICE, "5"))
        self.assertEqual(body["error"]["hints"], list(errors.ADB_HINTS))
        with patch("linkplane.api.execute.select_provider", return_value=FakeProvider(raise_with=errors.LinkplaneError(errors.TIMEOUT, "timed out"))):
            status, body, _ = self.http("POST", "/v1/devices/HW1/actions", body={"action": "battery.read"})
        self.assertEqual((status, body["error"]["code"]), (504, errors.TIMEOUT))
        with patch("linkplane.api.execute.select_provider", return_value=FakeProvider(unsupported=("device.find",))):
            status, body, _ = self.http("POST", "/v1/devices/HW1/actions", body={"action": "device.find"})
        self.assertEqual((status, body["error"]["code"]), (409, errors.CAPABILITY_UNSUPPORTED))  # the device cannot: not 501
        self.assertEqual(body["error"]["details"]["capability"]["status"], "unsupported")
        self.assertEqual(self.audit_entries("action.blocked")[-1]["details"]["code"], errors.CAPABILITY_UNSUPPORTED)
        with patch.dict(execute.RUNNERS, {"notify.post": lambda *a, **k: OperationResult.failure("transport_unavailable", "adb: device offline")}):
            status, body, _ = self.http("POST", "/v1/devices/HW1/actions", body={"action": "notify.post", "parameters": {"message": "x"}})
        self.assertEqual((status, body["error"]["code"]), (503, errors.CONNECT_UNREACHABLE))
        self.assertEqual(self.audit_entries("action.failed")[-1]["details"]["code"], errors.CONNECT_UNREACHABLE)

    def test_unexpected_exceptions_are_internal_and_leak_nothing(self):
        with patch.dict(execute.RUNNERS, {"notify.post": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("/home/user/.ssh/id_ed25519 exploded"))}):
            status, body, _ = self.http("POST", "/v1/devices/HW1/actions", body={"action": "notify.post", "parameters": {"message": "x"}})
        self.assertEqual((status, body["error"]["code"], body["error"]["message"]), (500, errors.INTERNAL, security.INTERNAL_MESSAGE))
        self.assertNotIn("id_ed25519", json.dumps(body))
        self.assertNotIn("Traceback", json.dumps(body))

    def test_routes_and_methods(self):
        status, body, _ = self.http("GET", "/v1/nothing")
        self.assertEqual((status, body["error"]["code"]), (404, errors.REQUEST_INVALID))
        status, body, headers = self.http("DELETE", "/v1/jobs/abc")
        self.assertEqual((status, headers["Allow"]), (405, "GET"))
        status, body, _ = self.http("POST", "/v1/jobs/abc/cancel")
        self.assertEqual(status, 403)  # scope first: the "full" client has no jobs.cancel
        status, body, _ = self.http("GET", "/v1/audit")
        self.assertEqual(status, 403)
        status, body, _ = self.http("GET", "/v1/rules")
        self.assertEqual(status, 403)


class EventHistoryTests(ApiFixture):
    def test_history_is_ordered_filtered_and_paged_by_seq(self):
        first = self.emit("device.connected", address="S1")
        self.emit("battery.changed", level=50)
        self.emit("battery.changed", "other", level=10)
        last = self.emit("battery.low", level=5, threshold=20)
        status, body, _ = self.http("GET", "/v1/events")
        self.assertEqual(status, 200)
        seqs = [event["seq"] for event in body["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(body["last_seq"], last)
        self.assertEqual(body["events"][-1]["schema"], "linkplane.event/1")
        self.assertEqual(set(body["events"][-1]), {"schema", "type", "device", "ts", "provider", "data", "seq", "event_id", "correlation_id", "source", "device_id"})
        self.assertEqual(body["events"][-1]["device_id"], "HW1")
        self.assertIsNone(body["events"][0]["device_id"])  # observer.started, device "*"
        status, body, _ = self.http("GET", f"/v1/events?after={first}&limit=2")
        self.assertEqual([e["seq"] for e in body["events"]], [first + 1, first + 2])
        status, body, _ = self.http("GET", "/v1/events?device=HW1&type=battery.changed")
        self.assertEqual([(e["device"], e["data"]["level"]) for e in body["events"]], [("phone", 50)])
        status, body, _ = self.http("GET", "/v1/events?order=desc&limit=1")
        self.assertEqual(body["events"][0]["seq"], last)
        status, body, _ = self.http("GET", f"/v1/events?before={first + 1}")
        self.assertEqual(body["events"][-1]["seq"], first)
        for bad in ("after=abc", "after=-1", "limit=0", "order=sideways", "since=yesterday"):
            status, body, _ = self.http("GET", f"/v1/events?{bad}")
            self.assertEqual((status, body["error"]["code"]), (400, errors.REQUEST_INVALID), bad)
        status, body, _ = self.http("GET", "/v1/events?since=2000-01-01T00:00:00%2B00:00")
        self.assertEqual(len(body["events"]), len(seqs))


class StreamTests(ApiFixture):
    def test_stream_requires_authentication(self):
        conn, response = self.open_stream(token=None)
        self.assertEqual(response.status, 401)
        conn.close()
        status, body, _ = self.http("GET", f"/v1/events/stream?token={self.token}")
        self.assertEqual(status, 400)

    def test_live_events_arrive_in_seq_order_with_ids(self):
        conn, response = self.open_stream()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        control = self.read_frame(response)
        self.assertEqual(control["event"], "stream")
        self.assertEqual(json.loads(control["data"])["gap"], False)
        seqs = [self.emit("battery.changed", level=level) for level in (40, 30, 20)]
        frames = [self.read_frame(response) for _ in seqs]
        self.assertEqual([int(frame["id"]) for frame in frames], seqs)
        self.assertEqual([json.loads(frame["data"])["data"]["level"] for frame in frames], [40, 30, 20])
        self.assertTrue(all(frame["event"] == "event" for frame in frames))
        conn.close()

    def test_resume_replays_after_the_cursor_then_continues_live(self):
        seqs = [self.emit("battery.changed", level=level) for level in (90, 80, 70)]
        conn, response = self.open_stream(headers={"Last-Event-ID": str(seqs[0])})
        control = json.loads(self.read_frame(response)["data"])
        self.assertEqual((control["resumed_after"], control["last_seq"], control["gap"]), (seqs[0], seqs[2], False))
        replayed = [int(self.read_frame(response)["id"]) for _ in range(2)]
        self.assertEqual(replayed, seqs[1:])
        live = self.emit("battery.changed", level=60)
        self.assertEqual(int(self.read_frame(response)["id"]), live)
        conn.close()
        # `after` wins over the header
        conn, response = self.open_stream(f"/v1/events/stream?after={seqs[2]}", headers={"Last-Event-ID": "0"})
        self.read_frame(response)
        self.assertEqual(int(self.read_frame(response)["id"]), live)
        conn.close()

    def test_malformed_cursors_and_gaps(self):
        for headers, path in (({"Last-Event-ID": "abc"}, "/v1/events/stream"), ({"Last-Event-ID": "-3"}, "/v1/events/stream"),
                              ({}, "/v1/events/stream?after=x")):
            conn, response = self.open_stream(path, headers=headers)
            self.assertEqual(response.status, 400, (headers, path))
            self.assertEqual(json.loads(response.read())["error"]["code"], errors.REQUEST_INVALID)
            conn.close()
        last = self.emit("battery.changed", level=50)
        conn, response = self.open_stream(f"/v1/events/stream?after={last + 100}")
        control = json.loads(self.read_frame(response)["data"])
        self.assertEqual((control["gap"], control["last_seq"]), (True, last))
        live = self.emit("battery.changed", level=49)
        self.assertEqual(int(self.read_frame(response)["id"]), live)  # live only, nothing replayed
        conn.close()

    def test_replay_to_live_boundary_delivers_exactly_once_in_order(self):
        base = self.emit("battery.changed", level=99)
        during = {}
        # after_subscribe: the event lands in history AND the live queue -> must be sent once (from replay).
        # after_replay: the event lands in the queue only -> must be sent once (from live).
        self.daemon.api.stream_hooks["after_subscribe"] = lambda: during.setdefault("early", self.emit("battery.changed", level=98))
        self.daemon.api.stream_hooks["after_replay"] = lambda: during.setdefault("late", self.emit("battery.changed", level=97))
        conn, response = self.open_stream(f"/v1/events/stream?after={base}")
        self.read_frame(response)
        later = self.emit("battery.changed", level=96)
        received = [self.read_frame(response) for _ in range(3)]
        ids = [int(frame["id"]) for frame in received]
        # Exactly once, strictly increasing, nothing lost: the early event (in history and in
        # the live queue) arrives once, the late one (queue only) arrives once, and the
        # test's own live event arrives once; their relative seqs depend on thread timing.
        self.assertEqual(sorted(ids), sorted({during["early"], during["late"], later}))
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(sorted(json.loads(f["data"])["data"]["level"] for f in received), [96, 97, 98])
        conn.close()

    def test_no_history_daemon_streams_live_only_and_refuses_resume(self):
        other = self.make_daemon(api_port=0, write_history=False, socket_path=str(self.base / "nh.sock"))
        thread = self.start(other)
        try:
            status, body, _ = self.http("GET", "/v1/events?after=1", port=other.api.port)
            self.assertEqual(status, 400)
            status, body, _ = self.http("GET", "/v1/events", port=other.api.port)
            self.assertEqual((status, body["last_seq"]), (200, None))
        finally:
            other.stop()
            thread.join(5)

    def test_client_disconnect_releases_the_subscriber(self):
        conn, response = self.open_stream()
        self.read_frame(response)
        self.assertEqual((self.daemon.describe()["subscribers"], self.daemon.api.streams), (1, 1))
        response.close()
        conn.close()
        for _ in range(100):
            if self.daemon.describe()["subscribers"] == 0 and self.daemon.api.streams == 0:
                break
            time.sleep(0.02)
        self.assertEqual((self.daemon.describe()["subscribers"], self.daemon.api.streams), (0, 0))


class LifecycleTests(ApiFixture):
    def test_shutdown_closes_streams_and_the_listener(self):
        conn, response = self.open_stream()
        self.read_frame(response)
        self.assertTrue(self.daemon.api_discovery_path.exists())
        self.assertEqual(json.loads(self.daemon.api_discovery_path.read_text())["url"], self.daemon.api.url)
        self.daemon.stop("test")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        frames = []
        while (frame := self.read_frame(response)) is not None:  # observer.stopped, then EOF
            frames.append(frame)
        self.assertEqual(json.loads(frames[-1]["data"])["type"], "observer.stopped")
        conn.close()
        with self.assertRaises(ConnectionRefusedError):
            self.connection().request("GET", "/v1/health")
        self.assertFalse(self.daemon.api_discovery_path.exists())
        self.assertFalse(self.daemon.socket_path.exists())
        kinds = [entry["kind"] for entry in self.audit_entries()]
        self.assertEqual(kinds[-2:], ["api.stopped", "daemon.stopped"])
        self.assertIn("api.started", kinds)

    def test_stream_delivers_observer_stopped_even_when_cancelled_before_it_is_produced(self):
        """The race behind the flaky shutdown test, made deterministic.

        `daemon.stop()` cancels the token at once; the observer produces `observer.stopped`
        only when it unwinds; the daemon then offers the `None` sentinel. A stream loop
        that ended on `cancel.cancelled` (as it did before 2026-09-12) could wake on an
        empty queue in between and close the stream without the final event, which the
        contract forbids (docs/local-api-design.md: stop closes every stream *after*
        `observer.stopped`). Here the final event is held until the loop has provably
        idled after the cancellation.
        """
        idled_after_cancel = threading.Event()
        FakeObserver.stop_gate = threading.Event()
        self.addCleanup(setattr, FakeObserver, "stop_gate", None)

        def on_idle():
            if self.daemon.cancel.cancelled and not idled_after_cancel.is_set():
                idled_after_cancel.set()
                FakeObserver.stop_gate.set()

        self.daemon.api.stream_hooks["on_idle"] = on_idle
        conn, response = self.open_stream()
        self.read_frame(response)  # the control frame
        self.daemon.stop("test")
        self.assertTrue(idled_after_cancel.wait(5), "the stream loop never idled after the cancellation")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        frames = []
        while (frame := self.read_frame(response)) is not None:
            frames.append(frame)
        conn.close()
        self.assertEqual([json.loads(frame["data"])["type"] for frame in frames], ["observer.stopped"])
        self.assertEqual(self.daemon.describe()["subscribers"], 0)  # the stream released itself

    def test_request_failures_do_not_touch_daemon_state(self):
        before = list(read_history(self.history))
        with patch.dict(execute.RUNNERS, {"notify.post": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))}):
            for _ in range(3):
                status, _, _ = self.http("POST", "/v1/devices/HW1/actions", body={"action": "notify.post", "parameters": {"message": "x"}})
                self.assertEqual(status, 500)
        with patch("linkplane.api.server.Registry.load", side_effect=RuntimeError("registry exploded")):
            status, body, _ = self.http("GET", "/v1/devices")
            self.assertEqual((status, body["error"]["code"]), (500, errors.INTERNAL))
        self.assertTrue(daemond.is_running(str(self.daemon.socket_path)))
        self.assertEqual(self.daemon.observed_states()["phone"].battery, {"level": 55})
        self.assertEqual([e.seq for e in read_history(self.history)], [e.seq for e in before])
        status, body, _ = self.http("GET", "/v1/devices/HW1/state")
        self.assertEqual((status, body["battery"]["level"]), (200, 55))
        self.assertTrue(self.thread.is_alive())

    def test_non_loopback_bind_is_refused_and_the_daemon_keeps_running(self):
        for bind in ("0.0.0.0", "::", "198.51.100.5"):
            other = self.make_daemon(api_port=0, api_bind=bind, socket_path=str(self.base / f"b{len(bind)}.sock"))
            thread = self.start(other)
            try:
                self.assertIsNone(other.api)
                self.assertEqual(other.api_error.code, errors.REQUEST_INVALID, bind)
                self.assertIn("remote binding is not supported", str(other.api_error))
                self.assertTrue(daemond.is_running(str(other.socket_path)))
                self.assertIsNone(daemond.request("status", str(other.socket_path))["api"])
            finally:
                other.stop()
                thread.join(5)
        errors_audited = [entry for entry in self.audit_entries("api.error")]
        self.assertEqual({entry["details"]["code"] for entry in errors_audited}, {errors.REQUEST_INVALID})

    def test_port_collision_is_deterministic_and_non_fatal(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            other = self.make_daemon(api_port=port, socket_path=str(self.base / "c.sock"))
            thread = self.start(other)
            try:
                self.assertIsNone(other.api)
                self.assertEqual(other.api_error.code, errors.API_BIND_FAILED)
                self.assertTrue(daemond.is_running(str(other.socket_path)))
            finally:
                other.stop()
                thread.join(5)
        finally:
            blocker.close()
        self.assertEqual(self.audit_entries("api.error")[-1]["details"]["code"], errors.API_BIND_FAILED)


class SliceTwoTests(ApiFixture):
    """Cancel, rules, reload, audit: authorization, semantics, privacy."""

    def setUp(self):
        super().setUp()
        _admin, self.admin_token = clients_module.create_client(
            "admin", scopes=["read", "jobs.cancel", "rules.read", "rules.reload", "audit.read", "backup.photos"], path=self.clients_path)
        _reloader, self.reload_only_token = clients_module.create_client("reloader", scopes=["rules.reload"], path=self.clients_path)
        _viewer, self.rules_viewer_token = clients_module.create_client("viewer", scopes=["rules.read"], path=self.clients_path)

    def admin(self, method, path, **kwargs):
        return self.http(method, path, token=f"Bearer {self.admin_token}", **kwargs)

    def start_backup(self, gate):
        def backup(provider, target, request, *, progress, cancel):
            gate.wait(10)
            if cancel.cancelled:
                return OperationResult.failure("cancelled", cancel.reason or "cancelled")
            return ok({"downloaded": 0})
        patcher = patch.dict(execute.RUNNERS, {"backup.photos": backup})
        patcher.start()
        self.addCleanup(patcher.stop)
        status, body, _ = self.admin("POST", "/v1/devices/HW1/actions", body={"action": "backup.photos"})
        self.assertEqual(status, 202, body)
        return body["id"]

    def wait_state(self, job_id, wanted):
        for _ in range(200):
            status, record, _ = self.admin("GET", f"/v1/jobs/{job_id}")
            if record["state"] in wanted:
                return record
            time.sleep(0.02)
        return record

    # -- authorization ------------------------------------------------------------------

    def test_jobs_read_does_not_grant_cancel_and_reads_do_not_grant_rules_or_audit(self):
        status, body, _ = self.http("POST", "/v1/jobs/whatever/cancel")  # "full" has jobs.read only
        self.assertEqual((status, body["error"]["code"], body["error"]["details"]["scope"]), (403, errors.CLIENT_FORBIDDEN, "jobs.cancel"))
        for path in ("/v1/rules", "/v1/audit"):
            status, body, _ = self.http("GET", path, token="reader")
            self.assertEqual((status, body["error"]["code"]), (403, errors.CLIENT_FORBIDDEN), path)
        self.assertNotIn("audit.read", clients_module.READ_SCOPES)
        self.assertNotIn("rules.read", clients_module.READ_SCOPES)

    def test_rules_read_does_not_imply_reload_and_vice_versa(self):
        status, body, _ = self.http("POST", "/v1/rules/reload", token=f"Bearer {self.rules_viewer_token}")
        self.assertEqual((status, body["error"]["details"]["scope"]), (403, "rules.reload"))
        status, body, _ = self.http("GET", "/v1/rules", token=f"Bearer {self.reload_only_token}")
        self.assertEqual((status, body["error"]["details"]["scope"]), (403, "rules.read"))

    def test_unauthorized_clients_cannot_infer_existence(self):
        for method, path in (("POST", "/v1/jobs/nope/cancel"), ("GET", "/v1/rules"), ("GET", "/v1/audit"), ("GET", "/v1/jobs/nope")):
            status, body, _ = self.http(method, path, token=None)
            self.assertEqual(status, 401, path)
            status, body, _ = self.http(method, path, token="reader")
            self.assertEqual(status, 403 if path != "/v1/jobs/nope" else 404, path)
        self.assertEqual(self.http("POST", "/v1/jobs/nope/cancel", token="reader")[0], 403)  # 403 before 404

    # -- cancel -----------------------------------------------------------------------------

    def test_cancel_running_job_is_accepted_then_cancelled_and_idempotent(self):
        gate = threading.Event()
        job_id = self.start_backup(gate)
        status, body, _ = self.admin("POST", f"/v1/jobs/{job_id}/cancel")
        self.assertEqual((status, body["state"], body["action"]), (202, "running", "backup.photos"))
        gate.set()
        record = self.wait_state(job_id, {"cancelled"})
        self.assertEqual((record["state"], record["error"]["message"]), ("cancelled", "cancelled by client:admin"))
        status, body, _ = self.admin("POST", f"/v1/jobs/{job_id}/cancel")
        self.assertEqual((status, body["state"]), (200, "cancelled"))  # idempotent
        kinds = [(e["kind"], e["decision"], e["actor"]) for e in self.audit_entries() if e.get("job_id") == job_id]
        self.assertIn(("job.cancel", "started", "client:admin"), kinds)
        self.assertIn(("job.cancelled", "cancelled", "client:admin"), kinds)
        self.assertIn(("job.cancel", "skipped", "client:admin"), kinds)
        self.assertTrue(all(e["source"] == "api" for e in self.audit_entries("job.cancel")))

    def test_cancel_terminal_stale_unknown_and_bodies(self):
        gate = threading.Event()
        job_id = self.start_backup(gate)
        gate.set()
        self.wait_state(job_id, {"completed"})
        status, body, _ = self.admin("POST", f"/v1/jobs/{job_id}/cancel")
        self.assertEqual((status, body["error"]["code"], body["error"]["details"]["state"]), (409, errors.STATE_CONFLICT, "completed"))
        self.assertEqual(self.audit_entries("job.cancel")[-1]["decision"], "blocked")
        stale = JobRunner(str(self.base / "jobs"), write_records=True)
        stale._save(type(stale)._save.__globals__["JobRecord"]("stale-1", "r", "backup", "phone", "running", "t"))
        status, body, _ = self.admin("POST", "/v1/jobs/stale-1/cancel")
        self.assertEqual((status, body["error"]["code"], body["error"]["details"]["stale"]), (409, errors.STATE_CONFLICT, True))
        status, body, _ = self.admin("POST", "/v1/jobs/none/cancel")
        self.assertEqual((status, body["error"]["code"]), (404, errors.RESOURCE_NOT_FOUND))
        status, body, _ = self.admin("POST", f"/v1/jobs/{job_id}/cancel", body={"force": True})
        self.assertEqual((status, body["error"]["code"]), (400, errors.REQUEST_INVALID))

    # -- rules ------------------------------------------------------------------------------

    def write_rules(self, rules):
        Path(self.base / "rules.json").write_text(json.dumps({"automations": rules}))

    def test_rules_projection_and_reload(self):
        status, body, _ = self.admin("GET", "/v1/rules")
        self.assertEqual((status, body["loaded"], body["rules"]), (200, 0, []))
        self.assertNotIn("path", body)
        self.write_rules([
            {"name": "low", "when": "battery.low", "device": "phone", "if": {"level": {"below": 10}},
             "do": [{"action": "notify-desktop", "message": "Low: {level}%"}, {"action": "run", "command": "curl -u admin:hunter2 https://x"}], "allow": ["run"]},
            {"name": "blocked", "when": "device.connected", "do": [{"action": "run", "command": "rm -rf /secret"}]},
        ])
        status, body, _ = self.admin("POST", "/v1/rules/reload")
        self.assertEqual((status, body["loaded"], body["active"], body["blocked"]), (200, 1, ["low"], {"blocked": ["run"]}))
        self.assertNotIn("rules", body)
        self.assertNotIn("path", body)
        status, body, _ = self.admin("GET", "/v1/rules")
        text = json.dumps(body)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("/secret", text)
        by_name = {rule["name"]: rule for rule in body["rules"]}
        self.assertEqual((by_name["low"]["state"], by_name["low"]["device_id"], by_name["low"]["do"][1]["command"]), ("active", "HW1", security.REDACTED))
        self.assertEqual((by_name["blocked"]["state"], by_name["blocked"]["blocked_actions"]), ("blocked", ["run"]))
        reload_entries = self.audit_entries("rules.reload")
        self.assertEqual((reload_entries[-1]["actor"], reload_entries[-1]["decision"], reload_entries[-1]["source"]), ("client:admin", "loaded", "api"))

    def test_reload_rejects_paths_and_keeps_previous_rules_on_a_broken_file(self):
        self.write_rules([{"name": "keep", "when": "battery.low", "do": [{"action": "notify-desktop", "message": "x"}]}])
        self.assertEqual(self.admin("POST", "/v1/rules/reload")[1]["active"], ["keep"])
        for body in ({"path": "/etc/passwd"}, {"file": "x"}, {"automations": []}, {"url": "http://x"}):
            status, payload, _ = self.admin("POST", "/v1/rules/reload", body=body)
            self.assertEqual((status, payload["error"]["code"]), (400, errors.REQUEST_INVALID), body)
        Path(self.base / "rules.json").write_text("{broken")
        status, payload, _ = self.admin("POST", "/v1/rules/reload")
        self.assertEqual((status, payload["error"]["code"]), (500, errors.CONFIG_INVALID))
        self.assertEqual(self.admin("GET", "/v1/rules")[1]["active"], ["keep"])  # previous rules stay in force
        self.assertEqual(self.audit_entries("rules.reload")[-1]["decision"], "error")

    # -- audit ------------------------------------------------------------------------------

    def test_audit_is_newest_first_filtered_bounded_and_redacted(self):
        self.http("POST", "/v1/devices/HW1/actions", body={"action": "clipboard.write", "parameters": {"text": "top secret"}, "correlation_id": "cw"})
        self.http("POST", "/v1/devices/HW1/actions", body={"action": "notify.post", "parameters": {"message": "hello"}, "correlation_id": "np"})
        self.daemon.audit.write("rule.fired", decision="fired", actor="rule:r", device="phone", rule="r",
                                outcomes=[{"action": "run", "ok": True, "data": {"stdout": "Bearer LEAK", "exit_code": 0}}])
        status, body, _ = self.admin("GET", "/v1/audit")
        self.assertEqual(status, 200)
        entries = body["entries"]
        self.assertEqual([e["kind"] for e in entries[:1]], ["rule.fired"])  # newest first
        timestamps = [e["ts"] for e in entries]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        text = json.dumps(body)
        self.assertNotIn("top secret", text)
        self.assertNotIn("LEAK", text)
        self.assertIn('"text_length": 10', text)
        self.assertEqual(entries[0]["details"]["outcomes"][0]["data"]["stdout"], security.REDACTED)
        self.assertEqual(entries[0]["device_id"], "HW1")
        status, body, _ = self.admin("GET", "/v1/audit?correlation_id=np")
        self.assertEqual([e["kind"] for e in body["entries"]], ["action.completed", "action.requested"])
        self.assertTrue(all(e["actor"] == "client:full" and e["source"] == "api" for e in body["entries"]))
        status, body, _ = self.admin("GET", "/v1/audit?actor=client:full&kind=action.requested&action=notify.post")
        self.assertEqual(len(body["entries"]), 1)
        status, body, _ = self.admin("GET", "/v1/audit?device=HW1&decision=fired")
        self.assertEqual([e["kind"] for e in body["entries"]], ["rule.fired"])
        status, body, _ = self.admin("GET", "/v1/audit?limit=2")
        self.assertEqual(len(body["entries"]), 2)
        status, body, _ = self.admin("GET", "/v1/audit?limit=5000")
        self.assertLessEqual(len(body["entries"]), 1000)
        for bad in ("limit=0", "limit=x", "since=never"):
            status, body, _ = self.admin("GET", f"/v1/audit?{bad}")
            self.assertEqual(status, 400, bad)
        self.assertIn("text_length", Path(self.audit).read_text())
        self.assertNotIn("top secret", Path(self.audit).read_text())  # never written in the first place

    def test_audit_limits_are_pinned(self):
        from linkplane.api import server as api_server
        self.assertEqual((api_server.AUDIT_DEFAULT_LIMIT, api_server.AUDIT_MAX_LIMIT), (100, 1000))
        for _ in range(120):
            self.daemon.audit.write("daemon.started", decision="started")
        status, body, _ = self.admin("GET", "/v1/audit")
        self.assertEqual(len(body["entries"]), 100)
