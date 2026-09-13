"""`linkplane.clients`: provisioning, storage, scopes, and authentication (design §16–§17)."""

import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from linkplane import clients
from linkplane.cli import main
from linkplane.core import errors


class ScopeTests(unittest.TestCase):
    def test_read_shorthand_excludes_audit_and_rules(self):
        self.assertEqual(clients.expand_scopes(["read"]), clients.READ_SCOPES)
        self.assertNotIn("audit.read", clients.READ_SCOPES)
        self.assertNotIn("rules.read", clients.READ_SCOPES)

    def test_scopes_are_capability_names_plus_resources(self):
        for name in ("battery.read", "files.send", "clipboard.sync", "device.find", "audit.read", "jobs.cancel"):
            self.assertIn(name, clients.SCOPES)
        self.assertEqual(len(clients.SCOPES), len(set(clients.SCOPES)))

    def test_wildcards_admin_unknown_and_reserved_are_refused(self):
        for bad in ("*", "admin", "all", "battery", "device.write"):
            with self.assertRaises(errors.LinkplaneError) as raised:
                clients.expand_scopes([bad])
            self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)
        with self.assertRaises(errors.LinkplaneError) as raised:
            clients.expand_scopes(["shell.execute"])
        self.assertIn("reserved", str(raised.exception))

    def test_expansion_keeps_order_and_drops_duplicates(self):
        self.assertEqual(clients.expand_scopes(["battery.read", "read", "battery.read", "events.read,jobs.read"]),
                         ("battery.read",) + clients.READ_SCOPES)

    def test_sensitive_scopes_are_named(self):
        self.assertEqual(clients.sensitive(("device.read", "clipboard.read", "files.send")), ("clipboard.read", "files.send"))


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "cfg" / "clients.json")

    def tearDown(self):
        self.directory.cleanup()

    def test_create_stores_only_the_hash_with_private_permissions(self):
        client, token = clients.create_client("waybar", name="Waybar", client_type="tool", scopes=["read", "battery.read"], path=self.path)
        self.assertEqual(client.client_id, "waybar")
        self.assertEqual(client.scopes, clients.READ_SCOPES + ("battery.read",))
        self.assertGreaterEqual(len(token), 40)
        raw = Path(self.path).read_text()
        self.assertNotIn(token, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["schema"], clients.CLIENTS_SCHEMA)
        self.assertEqual(payload["clients"][0]["token_sha256"], clients.hash_token(token))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(Path(self.path).parent).st_mode), 0o700)
        self.assertFalse(Path(self.path).with_suffix(".json.tmp").exists())
        self.assertNotIn("token_sha256", client.to_dict())

    def test_tokens_are_unique_and_high_entropy(self):
        tokens = {clients.generate_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(t) >= 40 for t in tokens))

    def test_authenticate_matches_only_the_right_token(self):
        one, token_one = clients.create_client("one", path=self.path)
        two, token_two = clients.create_client("two", path=self.path)
        loaded = clients.load_clients(self.path)
        self.assertEqual(clients.authenticate(token_one, loaded).client_id, "one")
        self.assertEqual(clients.authenticate(token_two, loaded).client_id, "two")
        self.assertIsNone(clients.authenticate(token_one[:-1] + "x", loaded))
        self.assertIsNone(clients.authenticate("", loaded))
        self.assertIsNone(clients.authenticate(None, loaded))
        self.assertEqual(one.actor, "client:one")
        self.assertTrue(one.allows("device.read"))
        self.assertFalse(one.allows("audit.read"))

    def test_invalid_ids_types_and_duplicates_are_refused(self):
        for bad in ("", "Waybar", "-x", "a" * 33, "with space", "x:y"):
            with self.assertRaises(errors.LinkplaneError) as raised:
                clients.create_client(bad, path=self.path)
            self.assertEqual(raised.exception.code, errors.REQUEST_INVALID, bad)
        with self.assertRaises(errors.LinkplaneError):
            clients.create_client("ok", client_type="browser", path=self.path)
        clients.create_client("ok", path=self.path)
        with self.assertRaises(errors.LinkplaneError) as raised:
            clients.create_client("ok", path=self.path)
        self.assertEqual(raised.exception.code, errors.STATE_CONFLICT)

    def test_revoke_removes_the_client_and_its_token_file(self):
        _client, token = clients.create_client("gui", client_type="gui", path=self.path)
        token_file = clients.write_token_file("gui", token, path=self.path)
        self.assertEqual(token_file.read_text().strip(), token)
        self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)
        revoked = clients.revoke_client("gui", path=self.path)
        self.assertEqual(revoked.client_id, "gui")
        self.assertEqual(clients.load_clients(self.path), ())
        self.assertFalse(token_file.exists())
        with self.assertRaises(errors.LinkplaneError) as raised:
            clients.revoke_client("gui", path=self.path)
        self.assertEqual(raised.exception.code, errors.RESOURCE_NOT_FOUND)

    def test_missing_file_means_no_clients_and_a_broken_file_is_a_config_error(self):
        self.assertEqual(clients.load_clients(self.path), ())
        Path(self.path).parent.mkdir(parents=True)
        Path(self.path).write_text("{nope")
        with self.assertRaises(errors.LinkplaneError) as raised:
            clients.load_clients(self.path)
        self.assertEqual(raised.exception.code, errors.CONFIG_INVALID)
        Path(self.path).write_text(json.dumps({"schema": clients.CLIENTS_SCHEMA, "clients": [
            {"client_id": "x", "token_sha256": "0" * 64, "scopes": ["device.read", "shell.execute"]}]}))
        loaded = clients.load_clients(self.path)
        self.assertEqual(loaded[0].scopes, ("device.read",))  # reserved scopes never load


class ClientsCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "clients.json")

    def tearDown(self):
        self.directory.cleanup()

    def run_cli(self, *argv):
        out = StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_create_prints_the_token_once_and_list_never_does(self):
        code, out = self.run_cli("clients", "create", "agent", "--type", "agent", "--scope", "read",
                                 "--scope", "clipboard.read", "--file", self.path)
        self.assertEqual(code, 0)
        token = next(line.split()[1] for line in out.splitlines() if line.startswith("Token "))
        self.assertIn("Sensitive   clipboard.read", out)
        code, listing = self.run_cli("clients", "list", "--file", self.path)
        self.assertEqual(code, 0)
        self.assertIn("agent", listing)
        self.assertNotIn(token, listing)
        code, as_json = self.run_cli("clients", "list", "--json", "--file", self.path)
        self.assertNotIn("token", as_json)
        self.assertEqual(json.loads(as_json)["data"]["clients"][0]["client_type"], "agent")

    def test_create_json_and_revoke(self):
        code, out = self.run_cli("clients", "create", "gui", "--json", "--token-file", "--file", self.path)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["scopes"], list(clients.READ_SCOPES))
        self.assertTrue(Path(payload["data"]["token_file"]).exists())
        self.assertEqual(clients.authenticate(payload["data"]["token"], clients.load_clients(self.path)).client_id, "gui")
        code, out = self.run_cli("clients", "revoke", "gui", "--json", "--file", self.path)
        self.assertEqual(json.loads(out)["data"]["revoked"]["client_id"], "gui")
        self.assertEqual(clients.load_clients(self.path), ())

    def test_reserved_scope_is_a_clean_error(self):
        err = StringIO()
        from contextlib import redirect_stderr
        with redirect_stdout(StringIO()), redirect_stderr(err):
            code = main(["clients", "create", "x", "--scope", "shell.execute", "--file", self.path])
        self.assertEqual(code, 1)
        self.assertIn("LP-REQUEST-001", err.getvalue())
