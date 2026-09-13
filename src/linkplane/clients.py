"""Local API clients: who is asking, and what they may do (docs/local-api-design.md §16–§17).

`~/.config/linkplane/clients.json` (`LINKPLANE_CLIENTS` overrides) holds one record per
client: id, name, type, scopes, and the SHA-256 of a machine-generated bearer token. The
plaintext token is shown once at creation and never stored, logged, or audited. Scopes
are capability names plus a few resource names; there is no wildcard and no admin.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from linkplane.core import errors
from linkplane.core.capability import CATALOGUE
from linkplane.core.events import now_iso

CLIENTS_SCHEMA = "linkplane.clients/1"
CLIENT_TYPES = ("cli", "gui", "sdk", "mcp", "automation", "agent", "tool")

READ_SCOPES = ("device.read", "device.state.read", "events.read", "jobs.read")
CONTROL_SCOPES = ("audit.read", "rules.read", "rules.reload", "jobs.cancel")
ACTION_SCOPES = CATALOGUE
# Grantable scopes: everything a client may be given.
SCOPES = READ_SCOPES + CONTROL_SCOPES + ACTION_SCOPES
# Named but never grantable (docs/local-api-design.md §17): no endpoint exists for it.
RESERVED_SCOPES = ("shell.execute",)
# Named on creation so the human sees what they are consenting to (§18).
SENSITIVE_SCOPES = (
    "audit.read", "rules.read", "clipboard.read", "clipboard.sync", "files.send",
    "backup.photos", "camera.capture", "screen.control",
)
# CLI shorthand, not a scope: expands to READ_SCOPES (excludes audit.read and rules.read).
READ_SHORTHAND = "read"

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TOKEN_BYTES = 32  # 256 bits from secrets.token_urlsafe


def resolve_clients_path(path: str | None = None) -> Path:
    from linkplane.paths import config_file

    return config_file("clients.json", path, env_name="CLIENTS")


@dataclass(frozen=True)
class Client:
    client_id: str
    client_name: str
    client_type: str
    scopes: tuple[str, ...]
    token_sha256: str
    created: str

    @property
    def actor(self) -> str:
        return f"client:{self.client_id}"

    def allows(self, scope: str) -> bool:
        return scope in self.scopes

    def to_dict(self) -> dict[str, Any]:
        """Public shape: never the digest."""
        return {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "client_type": self.client_type,
            "scopes": list(self.scopes),
            "created": self.created,
        }

    def _record(self) -> dict[str, Any]:
        return {**self.to_dict(), "token_sha256": self.token_sha256}


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expand_scopes(requested: Iterable[str]) -> tuple[str, ...]:
    """Validate and expand a scope list: `read` -> READ_SCOPES; unknown, reserved, or
    wildcard names are refused. Order preserved, duplicates dropped."""
    expanded: list[str] = []
    for raw in requested:
        for name in str(raw).replace(",", " ").split():
            if name == READ_SHORTHAND:
                candidates: tuple[str, ...] = READ_SCOPES
            elif name in RESERVED_SCOPES:
                raise errors.LinkplaneError(
                    errors.REQUEST_INVALID, f"scope {name!r} is reserved and cannot be granted",
                )
            elif name in {"*", "admin", "all"} or name not in SCOPES:
                raise errors.LinkplaneError(
                    errors.REQUEST_INVALID, f"unknown scope {name!r}",
                    (f"valid scopes: {', '.join(SCOPES)}", "'read' expands to the read-only set"),
                )
            else:
                candidates = (name,)
            for candidate in candidates:
                if candidate not in expanded:
                    expanded.append(candidate)
    return tuple(expanded)


def sensitive(scopes: Iterable[str]) -> tuple[str, ...]:
    return tuple(scope for scope in scopes if scope in SENSITIVE_SCOPES)


def _parse(record: Any) -> Client:
    if not isinstance(record, dict):
        raise errors.LinkplaneError(errors.CONFIG_INVALID, "each client must be an object")
    try:
        client_id = str(record["client_id"])
        digest = str(record["token_sha256"])
    except KeyError as error:
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"client record missing {error}") from error
    if not ID_PATTERN.match(client_id):
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"invalid client id {client_id!r}")
    scopes = record.get("scopes") or []
    if not isinstance(scopes, list):
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"client {client_id}: scopes must be a list")
    return Client(
        client_id,
        str(record.get("client_name") or client_id),
        str(record.get("client_type") or "tool"),
        tuple(str(scope) for scope in scopes if str(scope) not in RESERVED_SCOPES),
        digest,
        str(record.get("created") or ""),
    )


def load_clients(path: str | None = None) -> tuple[Client, ...]:
    """A missing file means no clients; a malformed one is LP-CONFIG-002."""
    file = resolve_clients_path(path)
    if not file.exists():
        return ()
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"clients file {file}: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != CLIENTS_SCHEMA:
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"clients file {file}: unsupported schema")
    raw = payload.get("clients")
    if not isinstance(raw, list):
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"clients file {file}: 'clients' must be a list")
    clients = tuple(_parse(record) for record in raw)
    ids = [client.client_id for client in clients]
    if len(set(ids)) != len(ids):
        raise errors.LinkplaneError(errors.CONFIG_INVALID, f"clients file {file}: duplicate client ids")
    return clients


def _save(clients: Iterable[Client], file: Path) -> None:
    """Atomic, private: temp file + rename, mode 0600, parent 0700 when newly created."""
    if not file.parent.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(file.parent, 0o700)
        except OSError:
            pass
    payload = {"schema": CLIENTS_SCHEMA, "clients": [client._record() for client in clients]}
    tmp = file.with_suffix(".json.tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, file)


def create_client(
    client_id: str,
    *,
    name: str | None = None,
    client_type: str = "tool",
    scopes: Iterable[str] = (READ_SHORTHAND,),
    path: str | None = None,
    token: str | None = None,
) -> tuple[Client, str]:
    """Create a client; returns it with the plaintext token, which is shown once."""
    if not ID_PATTERN.match(client_id or ""):
        raise errors.LinkplaneError(
            errors.REQUEST_INVALID, f"invalid client id {client_id!r}",
            ("use 1–32 lowercase letters, digits, or dashes, starting with a letter or digit",),
        )
    if client_type not in CLIENT_TYPES:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, f"unknown client type {client_type!r}",
                                    (f"one of: {', '.join(CLIENT_TYPES)}",))
    granted = expand_scopes(scopes)
    if not granted:
        raise errors.LinkplaneError(errors.REQUEST_INVALID, "a client needs at least one scope")
    file = resolve_clients_path(path)
    existing = load_clients(str(file))
    if any(client.client_id == client_id for client in existing):
        raise errors.LinkplaneError(errors.STATE_CONFLICT, f"client {client_id!r} already exists",
                                    (f"revoke it first: linkplane clients revoke {client_id}",))
    plaintext = token or generate_token()
    client = Client(client_id, name or client_id, client_type, granted, hash_token(plaintext), now_iso())
    _save((*existing, client), file)
    return client, plaintext


def revoke_client(client_id: str, *, path: str | None = None) -> Client:
    file = resolve_clients_path(path)
    existing = load_clients(str(file))
    match = next((client for client in existing if client.client_id == client_id), None)
    if match is None:
        raise errors.LinkplaneError(errors.RESOURCE_NOT_FOUND, f"no client {client_id!r}")
    _save((client for client in existing if client.client_id != client_id), file)
    token_file = token_file_path(client_id, path)
    if token_file.exists():
        token_file.unlink()
    return match


def token_file_path(client_id: str, path: str | None = None) -> Path:
    return resolve_clients_path(path).parent / "clients" / f"{client_id}.token"


def write_token_file(client_id: str, token: str, *, path: str | None = None) -> Path:
    """`clients/<id>.token`, mode 0600 in a 0700 directory, for a same-user process."""
    file = token_file_path(client_id, path)
    file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(file.parent, 0o700)
    descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(file, 0o600)
    return file


def authenticate(token: str | None, clients: Iterable[Client]) -> Client | None:
    """The client whose digest matches, compared in constant time over every record."""
    if not token:
        return None
    digest = hash_token(token)
    found: Client | None = None
    for client in clients:
        if hmac.compare_digest(digest, client.token_sha256):
            found = client
    return found
