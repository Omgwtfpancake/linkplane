"""`linkplane clients create|list|revoke`: local API client provisioning (design §16)."""

from __future__ import annotations

from typing import Any

from linkplane import clients as clients_module
from linkplane.clients import CLIENT_TYPES, READ_SHORTHAND, sensitive
from linkplane.commands import print_envelope

__all__ = ["CLIENT_TYPES", "run"]


def create(arguments: Any) -> int:
    scopes = arguments.scopes or [READ_SHORTHAND]
    client, token = clients_module.create_client(
        arguments.client_id, name=arguments.name, client_type=arguments.client_type,
        scopes=scopes, path=arguments.file,
    )
    token_file = None
    if arguments.token_file:
        token_file = clients_module.write_token_file(client.client_id, token, path=arguments.file)
    if arguments.json:
        print_envelope(True, {**client.to_dict(), "token": token,
                              "token_file": str(token_file) if token_file else None})
        return 0
    print("Linkplane Client")
    print(f"ID          {client.client_id}")
    print(f"Name        {client.client_name}")
    print(f"Type        {client.client_type}")
    print(f"Scopes      {', '.join(client.scopes)}")
    flagged = sensitive(client.scopes)
    if flagged:
        print(f"Sensitive   {', '.join(flagged)}  (granted explicitly; revoke to withdraw)")
    print(f"Token       {token}")
    print("            shown once; only its hash is stored")
    if token_file is not None:
        print(f"Token file  {token_file}")
    return 0


def list_clients(arguments: Any) -> int:
    clients = clients_module.load_clients(arguments.file)
    if arguments.json:
        print_envelope(True, {"clients": [client.to_dict() for client in clients]})
        return 0
    print(f"Linkplane Clients ({clients_module.resolve_clients_path(arguments.file)})")
    if not clients:
        print("No clients")
    for client in clients:
        print(f"{client.client_id:<16} {client.client_type:<11} {', '.join(client.scopes)}")
    return 0


def revoke(arguments: Any) -> int:
    client = clients_module.revoke_client(arguments.client_id, path=arguments.file)
    if arguments.json:
        print_envelope(True, {"revoked": client.to_dict()})
    else:
        print(f"Revoked     {client.client_id}")
    return 0


def run(arguments: Any) -> int:
    if arguments.clients_action == "create":
        return create(arguments)
    if arguments.clients_action == "list":
        return list_clients(arguments)
    return revoke(arguments)
