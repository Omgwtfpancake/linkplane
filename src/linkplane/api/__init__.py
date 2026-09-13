"""The local HTTP API (docs/local-api-design.md): a transport adapter over the daemon.

Slice 0 holds only what the server will call: the declarative action table
(`linkplane.api.actions`) and the pure security/error helpers (`linkplane.api.security`).
There is no listener yet.
"""

API_PROTOCOL = "linkplane.api/1"
API_MAJOR = 1
