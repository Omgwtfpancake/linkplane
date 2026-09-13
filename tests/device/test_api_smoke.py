"""Device tier: the local API smoke test (docs/smoke-test.md, "Local API").

Requires the paired phone authorized over ADB. Runs linkplaned with the real observer and
the HTTP API on an ephemeral loopback port, against temporary config, clients, state,
history, audit, and job files. Skips (never fails) without a phone. `make test-device`.
"""

import http.client
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from linkplane import clients as clients_module
from linkplane import daemon as daemond
from linkplane.jobs import JobRunner
from linkplane.providers import ADBProvider
from linkplane.transports import AdbTransport, BridgeError


def wait_until(predicate, timeout=30.0, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class ApiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.serial = AdbTransport(None).select_device()["serial"]
        except BridgeError as error:
            raise unittest.SkipTest(f"no authorized ADB device: {error}")
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        cls.config_path = str(base / "config.json")
        Path(cls.config_path).write_text(json.dumps({
            "default_device": "phone",
            "devices": {"phone": {"device_id": cls.serial, "adb": {"serials": [cls.serial]}}},
        }))
        cls.clients_path = str(base / "clients.json")
        _client, cls.token = clients_module.create_client(
            "smoke", client_type="sdk", path=cls.clients_path,
            scopes=["read", "device.ping", "battery.read", "device.status", "backup.photos",
                    "jobs.cancel", "rules.read", "rules.reload", "audit.read"])
        cls.history = str(base / "events.jsonl")
        cls.daemon = daemond.Daemon(
            socket_path=str(base / "s.sock"), state_path=str(base / "state.json"), history_path=cls.history,
            identities={cls.serial: "phone"}, interval=5.0, audit_path=str(base / "audit.jsonl"),
            automations_path=str(base / "rules.json"), jobs=JobRunner(str(base / "jobs"), retry_delays=()),
            config_path=cls.config_path, clients_path=cls.clients_path,
            api_enabled=True, api_bind="127.0.0.1", api_port=0, api_keepalive=2.0,
        )
        cls.thread = threading.Thread(target=cls.daemon.run, daemon=True)
        cls.thread.start()
        assert wait_until(lambda: cls.daemon.api is not None or cls.daemon.api_error is not None, 10)
        assert cls.daemon.api is not None, cls.daemon.api_error
        cls.port = cls.daemon.api.port
        assert wait_until(lambda: cls.get(f"/v1/devices/{cls.serial}/state")[0] == 200
                          and cls.get(f"/v1/devices/{cls.serial}/state")[1].get("battery"), 40), "device never observed"

    @classmethod
    def tearDownClass(cls):
        if cls.daemon.stop_reason is None:
            cls.daemon.stop("smoke finished")
        cls.thread.join(15)
        cls.directory.cleanup()

    @classmethod
    def request(cls, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=60)
        request_headers = {"Authorization": f"Bearer {cls.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        try:
            conn.request(method, path, body=data, headers=request_headers)
            response = conn.getresponse()
            text = response.read().decode()
            return response.status, (json.loads(text) if text else None), dict(response.getheaders())
        finally:
            conn.close()

    @classmethod
    def get(cls, path):
        return cls.request("GET", path)

    def test_01_api_started_with_the_daemon(self):
        status, body, _ = self.get("/v1/health")
        self.assertEqual((status, body["api"], body["status"]), (200, "linkplane.api/1", "ok"))
        self.assertTrue(self.daemon.api_discovery_path.exists())
        self.assertEqual(daemond.request("status", str(self.daemon.socket_path))["api"]["url"], self.daemon.api.url)

    def test_02_devices_use_the_stable_device_id(self):
        status, body, _ = self.get("/v1/devices")
        self.assertEqual(status, 200)
        phone = next(device for device in body["devices"] if device["device_id"] == self.serial)
        self.assertEqual((phone["name"], phone["observed"], phone["connection"]), ("phone", True, "connected"))
        for alias in ("phone", f"serial:{self.serial}"):
            self.assertEqual(self.get(f"/v1/devices/{alias}")[1]["device_id"], self.serial)
        renamed = json.loads(Path(self.config_path).read_text())
        renamed["devices"]["work-phone"] = renamed["devices"].pop("phone")
        renamed["default_device"] = "work-phone"
        Path(self.config_path).write_text(json.dumps(renamed))
        try:
            status, body, _ = self.get(f"/v1/devices/{self.serial}")
            self.assertEqual((status, body["device_id"], body["name"], body["observed"]), (200, self.serial, "work-phone", True))
        finally:
            renamed["devices"]["phone"] = renamed["devices"].pop("work-phone")
            renamed["default_device"] = "phone"
            Path(self.config_path).write_text(json.dumps(renamed))

    def test_03_state_is_the_observed_record(self):
        status, body, _ = self.get(f"/v1/devices/{self.serial}/state")
        self.assertEqual((status, body["connection"], body["provider"], body["address"]), (200, "connected", "adb", self.serial))
        self.assertIsInstance(body["battery"]["level"], int)

    def test_04_capabilities_match_the_provider_report(self):
        status, body, _ = self.get(f"/v1/devices/{self.serial}/capabilities")
        self.assertEqual(status, 200)
        expected = json.loads(json.dumps([report.to_dict() for report in ADBProvider(AdbTransport(self.serial)).capabilities()]))
        self.assertEqual(body["by_provider"]["adb"], expected)
        effective = {entry["name"]: entry for entry in body["capabilities"]}
        self.assertEqual(effective["battery.read"]["status"], "supported")
        self.assertTrue(effective["battery.read"]["granted"])
        self.assertFalse(effective["clipboard.read"]["granted"])

    def test_05_immediate_actions(self):
        status, body, _ = self.request("POST", f"/v1/devices/{self.serial}/actions", {"action": "device.ping", "correlation_id": "smoke-ping"})
        self.assertEqual((status, body["value"]["reachable"], body["provider"], body["correlation_id"]), (200, True, "adb", "smoke-ping"))
        status, body, _ = self.request("POST", f"/v1/devices/{self.serial}/actions", {"action": "battery.read"})
        self.assertEqual((status, body["action"], body["actor"]), (200, "battery.read", "client:smoke"))
        self.assertIsInstance(body["value"]["level"], int)
        status, body, _ = self.request("POST", f"/v1/devices/{self.serial}/actions", {"action": "notify.post", "parameters": {"message": "x"}})
        self.assertEqual((status, body["error"]["code"]), (403, "LP-CLIENT-002"))  # not granted to this client

    def test_06_job_action_dry_run_backup(self):
        with tempfile.TemporaryDirectory() as destination:
            status, body, headers = self.request("POST", f"/v1/devices/{self.serial}/actions", {
                "action": "backup.photos", "parameters": {"destination": destination, "dry_run": True}})  # default source /sdcard/DCIM
            self.assertEqual(status, 202, body)
            self.assertEqual((body["state"], body["action"], body["actor"], body["rule"]), ("running", "backup.photos", "client:smoke", None))
            job_id = body["id"]
            self.assertEqual(headers["Location"], f"/v1/jobs/{job_id}")
            self.assertTrue(wait_until(lambda: self.get(f"/v1/jobs/{job_id}")[1]["state"] != "running", 120))
            status, record, _ = self.get(f"/v1/jobs/{job_id}")
            self.assertEqual(record["state"], "completed", record)
            self.assertTrue(record["result"]["dry_run"])
            self.assertEqual(list(Path(destination).iterdir()), [])  # dry run wrote nothing
        status, listing, _ = self.get(f"/v1/jobs?device={self.serial}")
        self.assertIn(job_id, [job["id"] for job in listing["jobs"]])

    def test_07_sse_sees_a_real_disconnect_and_reconnect_in_order(self):
        last_seq = self.get("/v1/health")[1]["last_seq"]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("GET", f"/v1/events/stream?after={last_seq}&device={self.serial}",
                     headers={"Authorization": f"Bearer {self.token}"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)

        def frames(count, timeout=60):
            deadline = time.monotonic() + timeout
            found, frame = [], {}
            while len(found) < count and time.monotonic() < deadline:
                line = response.readline()
                if not line:
                    break
                line = line.decode().rstrip("\n")
                if line == "":
                    if frame:
                        found.append(frame)
                        frame = {}
                elif not line.startswith(":"):
                    name, _sep, value = line.partition(":")
                    frame[name] = value.lstrip()
            return found

        control = frames(1)[0]
        self.assertEqual(control["event"], "stream")
        subprocess.run(["adb", "-s", self.serial, "usb"], check=False, capture_output=True, timeout=30)
        received = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            received.extend(frames(1, timeout=30))
            types = [json.loads(frame["data"])["type"] for frame in received]
            if "device.disconnected" in types and types.index("device.disconnected") < len(types) - 1 and "device.connected" in types[types.index("device.disconnected"):]:
                break
        conn.close()
        types = [json.loads(frame["data"])["type"] for frame in received]
        ids = [int(frame["id"]) for frame in received]
        self.assertIn("device.disconnected", types)
        self.assertIn("device.connected", types[types.index("device.disconnected"):])
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(seq > last_seq for seq in ids))
        status, history, _ = self.get(f"/v1/events?after={last_seq}&device={self.serial}")
        self.assertEqual([event["seq"] for event in history["events"]][: len(ids)], ids)
        self.assertTrue(wait_until(lambda: self.get(f"/v1/devices/{self.serial}/state")[1].get("battery"), 60))

    def test_07b_control_plane_metadata_routes(self):
        """Slice 2: rules, reload, audit, and cancel of a terminal job — metadata only, nothing on the phone."""
        status, body, _ = self.get("/v1/rules")
        self.assertEqual((status, body["loaded"], body["rules"]), (200, 0, []))
        status, body, _ = self.request("POST", "/v1/rules/reload")
        self.assertEqual((status, body["loaded"]), (200, 0))
        status, body, _ = self.get(f"/v1/audit?device={self.serial}&action=backup.photos")
        self.assertEqual(status, 200)
        kinds = [entry["kind"] for entry in body["entries"]]
        self.assertIn("job.completed", kinds)
        self.assertTrue(all(entry["device_id"] == self.serial for entry in body["entries"]))
        self.assertTrue(all(entry["action"] == "backup.photos" for entry in body["entries"]))
        completed = next(entry for entry in body["entries"] if entry["kind"] == "job.completed")
        status, body, _ = self.request("POST", f"/v1/jobs/{completed['job_id']}/cancel")
        self.assertEqual((status, body["error"]["code"], body["error"]["details"]["state"]), (409, "LP-STATE-001", "completed"))
        status, body, _ = self.get(f"/v1/jobs/{completed['job_id']}")
        self.assertEqual((body["action"], body["job_action"], body["device_id"]), ("backup.photos", "backup", self.serial))

    def test_08_shutdown_closes_the_api_with_the_daemon(self):
        port = self.port
        self.daemon.stop("smoke finished")
        self.thread.join(15)
        self.assertFalse(self.thread.is_alive())
        with self.assertRaises(ConnectionRefusedError):
            http.client.HTTPConnection("127.0.0.1", port, timeout=5).request("GET", "/v1/health")
        self.assertFalse(self.daemon.api_discovery_path.exists())
        self.assertFalse(self.daemon.socket_path.exists())
        self.assertFalse(Path(self.daemon.socket_path).exists())
