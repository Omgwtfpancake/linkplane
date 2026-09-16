"""`docs/openapi.json`, generated from the route table, the action table, the pinned
dataclasses, and the status map — never hand-edited. `python -m linkplane.api.openapi`
rewrites the file; `tests/test_openapi.py` fails when the file and this module disagree.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from linkplane.api import API_PROTOCOL, actions, security, server
from linkplane.core import errors
from linkplane.core.capability import CATALOGUE, STATUSES
from linkplane.core.events import EVENT_TYPES, Event
from linkplane.core.state import DeviceState
from linkplane.jobs import STATES, JobRecord

DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "openapi.json"


def _fields(cls: type, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        ann = str(f.type)
        t: dict[str, Any] = {"type": "string"}
        if "int" in ann and "None" in ann:
            t = {"type": ["integer", "null"]}
        elif ann.startswith("int"):
            t = {"type": "integer"}
        elif "float" in ann:
            t = {"type": "number"}
        elif "bool" in ann:
            t = {"type": "boolean"}
        elif "dict" in ann and "None" in ann:
            t = {"type": ["object", "null"]}
        elif "dict" in ann:
            t = {"type": "object"}
        elif "tuple" in ann:
            t = {"type": "array", "items": {"type": "string"}}
        elif "None" in ann:
            t = {"type": ["string", "null"]}
        props[f.name] = t
    props.update(overrides or {})
    return props


def _err(description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def _ok(ref: str, description: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"description": description, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}}}
    if headers:
        response["headers"] = headers
    return response


def _query(name: str, schema: dict[str, Any], description: str | None = None, repeatable: bool = False) -> dict[str, Any]:
    parameter: dict[str, Any] = {"name": name, "in": "query", "schema": schema}
    if repeatable:
        parameter.update({"schema": {"type": "array", "items": schema}, "style": "form", "explode": True})
    if description:
        parameter["description"] = description
    return parameter


def build_spec() -> dict[str, Any]:
    lp_codes = sorted(v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str) and v.startswith("LP-"))
    auth = {
        "401": _err("no, malformed, unknown, or revoked bearer token (LP-CLIENT-001)"),
        "403": _err("scope not granted (LP-CLIENT-002), or an unacceptable Host/Origin header (LP-CLIENT-001)"),
    }
    device_param = {"name": "device_id", "in": "path", "required": True, "schema": {"type": "string"},
                    "description": "canonical device_id; a profile name or serial:<x> alias is also accepted"}
    job_param = {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}
    event_filters = [
        _query("type", {"type": "string", "enum": list(EVENT_TYPES)}, repeatable=True),
        _query("device", {"type": "string"}, "canonical id or alias", repeatable=True),
        _query("correlation_id", {"type": "string"}),
        _query("since", {"type": "string", "format": "date-time"}, "filter (not a cursor): ts >= since"),
    ]
    device_note = "`device` is the registry name pinned records carry; `device_id` is the canonical id resolved at read time, null when the name no longer resolves (renamed/removed profile, `*`)."
    paths: dict[str, Any] = {
        "/v1/health": {"get": {"summary": "API and daemon status", "responses": {"200": _ok("Health", "status and version information only"), **auth}}},
        "/v1/devices": {"get": {"summary": "registry: profiles and observed devices", "x-scope": "device.read",
                                "responses": {"200": _ok("DeviceList", "devices"), **auth}}},
        "/v1/devices/{device_id}": {"get": {"summary": "one device (identity, providers, observed state)", "x-scope": "device.read", "parameters": [device_param],
                                            "responses": {"200": _ok("Device", "device"), "404": _err("no such device (LP-CONFIG-003)"), **auth}}},
        "/v1/devices/{device_id}/state": {"get": {"summary": "observed state", "x-scope": "device.state.read", "parameters": [device_param],
                                                  "responses": {"200": _ok("DeviceState", "DeviceState as observed by the daemon"),
                                                                "404": _err("no such device (LP-CONFIG-003) or not observed (LP-RESOURCE-001)"), **auth}}},
        "/v1/devices/{device_id}/capabilities": {"get": {"summary": "what Linkplane can do with this device now", "x-scope": "device.read",
                                                         "parameters": [device_param, _query("provider", {"type": "string", "enum": ["adb", "ssh"]})],
                                                         "responses": {"200": _ok("Capabilities", "per-provider reports and the effective list"), "404": _err("no such device"), **auth}}},
        "/v1/devices/{device_id}/actions": {"post": {"summary": "run an action", "x-scope": "the action name", "parameters": [device_param],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ActionRequest"}}}},
            "responses": {"200": _ok("Result", "immediate action completed"),
                          "202": _ok("Job", "job started", {"Location": {"schema": {"type": "string"}, "description": "/v1/jobs/{job_id}"}}),
                          "400": _err("invalid request (LP-REQUEST-001)"), "404": _err("no such device (LP-CONFIG-003)"),
                          "409": _err("already running (LP-STATE-001, details.job_id), device cannot do it (LP-CAPABILITY-001), unavailable now (LP-CAPABILITY-002)"),
                          "501": _err("catalogue capability not implemented over the API (LP-CAPABILITY-001)"),
                          "502": _err("provider failure (LP-PROVIDER-*, LP-AUTH-001)"),
                          "503": _err("device unreachable/not connected/not authorized/host USB permission, dependency missing (LP-CONNECT-*, LP-AUTH-002, LP-AUTH-003, LP-DEPENDENCY-001)"),
                          "504": _err("timeout (LP-TIMEOUT-001)"), "500": _err("internal (LP-INTERNAL-001), configuration (LP-CONFIG-001/002/004)"), **auth}}},
        "/v1/jobs": {"get": {"summary": "job records, newest first", "x-scope": "jobs.read",
                             "parameters": [_query("device", {"type": "string"}, "canonical id or alias"),
                                            _query("action", {"type": "string"}, "public capability name (backup.photos) or job action (backup)"),
                                            _query("state", {"type": "string", "enum": list(STATES)}), _query("rule", {"type": "string"}),
                                            _query("correlation_id", {"type": "string"}),
                                            _query("limit", {"type": "integer", "default": server.JOBS_DEFAULT_LIMIT, "maximum": server.JOBS_MAX_LIMIT})],
                             "responses": {"200": _ok("JobList", "jobs"), **auth}}},
        "/v1/jobs/{job_id}": {"get": {"summary": "one job", "x-scope": "jobs.read", "parameters": [job_param],
                                      "responses": {"200": _ok("Job", "job"), "404": _err("no such job (LP-RESOURCE-001)"), **auth}}},
        "/v1/jobs/{job_id}/cancel": {"post": {"summary": "cancel a running job (cooperative)", "x-scope": "jobs.cancel", "parameters": [job_param],
            "description": "Uses the daemon's JobRunner. No body. The job reaches `cancelled` at its next boundary; observe it on GET /v1/jobs/{job_id}.",
            "responses": {"202": _ok("Job", "cancellation accepted; the record as it stands (running or retrying)"),
                          "200": _ok("Job", "already cancelled (idempotent)"),
                          "409": _err("already terminal (completed/failed/skipped) or recorded as running but not running in this daemon (LP-STATE-001, details.state, details.stale)"),
                          "404": _err("no such job (LP-RESOURCE-001)"), "400": _err("a body was sent (LP-REQUEST-001)"), **auth}}},
        "/v1/events": {"get": {"summary": "canonical event history by seq", "x-scope": "events.read",
                               "parameters": [_query("after", {"type": "integer"}, "seq > after"), _query("before", {"type": "integer"}),
                                              _query("order", {"type": "string", "enum": ["asc", "desc"], "default": "asc"}),
                                              _query("limit", {"type": "integer", "default": server.EVENTS_DEFAULT_LIMIT, "maximum": server.EVENTS_MAX_LIMIT})] + event_filters,
                               "responses": {"200": _ok("EventList", "events and last_seq"), "400": _err("bad cursor/filter, or history disabled"), **auth}}},
        "/v1/events/stream": {"get": {"summary": "Server-Sent Events: canonical Events only", "x-scope": "events.read",
                                      "description": "Frames: `event: stream` once (control: protocol, last_seq, resumed_after, gap), then `event: event` with `id:` = seq. Resume with `after=<seq>` or `Last-Event-ID: <seq>` (after wins); both mean seq > N. Replay from history then live, no duplicates within a connection, strictly increasing seq. Browser clients use fetch() streaming with the Authorization header.",
                                      "parameters": [_query("after", {"type": "integer"}), {"name": "Last-Event-ID", "in": "header", "schema": {"type": "integer"}}] + event_filters[:2],
                                      "responses": {"200": {"description": "text/event-stream", "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                                                    "400": _err("malformed cursor or history disabled"), "409": _err("too many open streams (LP-STATE-001)"), **auth}}},
        "/v1/rules": {"get": {"summary": "the daemon's loaded rules (inspection only)", "x-scope": "rules.read (sensitive; not in the read default)",
                              "description": "The rules as the daemon loaded them (active and blocked). `run` steps never expose their command text. No filesystem path is exposed.",
                              "responses": {"200": _ok("Rules", "rules"), **auth}}},
        "/v1/rules/reload": {"post": {"summary": "reload the daemon's configured rules file", "x-scope": "rules.reload",
                                      "description": "No body. Reloads the file the daemon was started with; a broken file leaves the previous rules in force (500 LP-CONFIG-002).",
                                      "responses": {"200": _ok("RulesSummary", "reloaded"), "400": _err("a body was sent (LP-REQUEST-001)"), "500": _err("rules file invalid (LP-CONFIG-002)"), **auth}}},
        "/v1/audit": {"get": {"summary": "audit entries, newest first, redacted for HTTP", "x-scope": "audit.read (sensitive; explicit grant only)",
                              "description": "Read backwards from the end of the log, bounded by `limit`; sensitive keys (clipboard text, commands, stdout, tokens, credentials) are redacted in the projection, never in the file. " + device_note,
                              "parameters": [_query(name, {"type": "string"}, repeatable=True) for name in ("device", "actor", "action", "decision", "kind", "job_id", "rule")]
                                            + [_query("correlation_id", {"type": "string"}), _query("since", {"type": "string", "format": "date-time"}),
                                               _query("limit", {"type": "integer", "default": server.AUDIT_DEFAULT_LIMIT, "maximum": server.AUDIT_MAX_LIMIT})],
                              "responses": {"200": _ok("AuditList", "entries"), "400": _err("bad filter"), **auth}}},
    }
    step_schema = {"type": "object", "required": ["action"], "properties": {"action": {"type": "string"}, "capability": {"type": ["string", "null"]},
                   "privileged": {"type": "boolean"}, "command": {"const": security.REDACTED, "description": "run command text is never exposed"},
                   "if": {"type": "object", "description": "step-level condition over the event data and earlier results"}},
                   "additionalProperties": True}
    rule_schema = {"type": "object", "required": ["name", "state", "enabled", "when", "device", "device_id", "if", "cooldown_seconds", "on_initial", "continue_on_error", "allow", "blocked_actions", "do"],
                   "properties": {"name": {"type": "string"}, "state": {"type": "string", "enum": ["active", "blocked"]}, "enabled": {"type": "boolean"},
                                  "when": {"type": "string", "enum": list(EVENT_TYPES)}, "device": {"type": ["string", "null"]}, "device_id": {"type": ["string", "null"]},
                                  "if": {"type": "object"}, "cooldown_seconds": {"type": "number"}, "on_initial": {"type": "boolean"}, "continue_on_error": {"type": "boolean"},
                                  "allow": {"type": "array", "items": {"type": "string"}}, "blocked_actions": {"type": "array", "items": {"type": "string"}},
                                  "do": {"type": "array", "items": step_schema},
                                  "preset": {"type": ["string", "null"], "description": "the built-in preset that wrote this rule, if any"}}}
    rules_summary = {"type": "object", "required": ["loaded", "active", "blocked", "fired"],
                     "properties": {"loaded": {"type": "integer"}, "active": {"type": "array", "items": {"type": "string"}},
                                    "blocked": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}, "fired": {"type": "integer"}}}
    schemas: dict[str, Any] = {
        "Error": {"type": "object", "required": ["error"],
                  "properties": {"error": {"type": "object", "required": ["type", "message", "code", "hints", "correlation_id", "details"],
                                           "properties": {"type": {"type": "string"}, "message": {"type": "string"}, "code": {"type": "string", "enum": lp_codes},
                                                          "hints": {"type": "array", "items": {"type": "string"}}, "correlation_id": {"type": "string"}, "details": {"type": "object"}}}}},
        "Health": {"type": "object", "required": ["status", "api", "protocol", "version", "daemon", "last_seq", "client"],
                   "properties": {"status": {"const": "ok"}, "api": {"const": API_PROTOCOL}, "protocol": {"type": "string"}, "version": {"type": "string"},
                                  "daemon": {"type": "object", "properties": {"pid": {"type": "integer"}, "started": {"type": "string"}}},
                                  "last_seq": {"type": ["integer", "null"]},
                                  "client": {"type": "object", "properties": {"client_id": {"type": "string"}, "client_type": {"type": "string"}, "scopes": {"type": "array", "items": {"type": "string"}}}}}},
        "DeviceState": {"type": "object", "required": [f.name for f in dataclasses.fields(DeviceState)], "properties": _fields(DeviceState, {"battery": {"type": ["object", "null"]}})},
        "ProviderEndpoint": {"type": "object", "properties": {"provider": {"type": "string", "enum": ["adb", "ssh"]}, "address": {"type": "string"}, "observed": {"type": "boolean"}}},
        "Device": {"type": "object", "required": ["device_id", "name", "observed", "default", "connection", "provider", "address", "last_seen", "providers", "state"],
                   "properties": {"device_id": {"type": "string", "description": "canonical, stable across renames"},
                                  "name": {"type": "string", "description": "registry name (alias); the value pinned records carry in `device`"},
                                  "observed": {"type": "boolean"}, "default": {"type": "boolean"}, "connection": {"type": ["string", "null"]}, "provider": {"type": ["string", "null"]},
                                  "address": {"type": ["string", "null"]}, "last_seen": {"type": ["string", "null"]},
                                  "providers": {"type": "array", "items": {"$ref": "#/components/schemas/ProviderEndpoint"}},
                                  "state": {"oneOf": [{"$ref": "#/components/schemas/DeviceState"}, {"type": "null"}]}}},
        "DeviceList": {"type": "object", "properties": {"devices": {"type": "array", "items": {"$ref": "#/components/schemas/Device"}}}},
        "CapabilityReport": {"type": "object", "required": ["name", "status", "detail", "requirements", "metadata"],
                             "properties": {"name": {"type": "string", "enum": list(CATALOGUE)}, "status": {"type": "string", "enum": list(STATUSES)}, "detail": {"type": ["string", "null"]},
                                            "requirements": {"type": "array", "items": {"type": "string"}}, "metadata": {"type": "object"}}},
        "Capabilities": {"type": "object", "properties": {"device_id": {"type": "string"}, "queried": {"type": "string"},
                          "capabilities": {"type": "array", "items": {"allOf": [{"$ref": "#/components/schemas/CapabilityReport"},
                                                                                 {"type": "object", "properties": {"provider": {"type": "string"},
                                                                                                                   "granted": {"type": "boolean", "description": "authorization metadata about the calling client, not device state"}}}]}},
                          "by_provider": {"type": "object", "additionalProperties": {"type": "array", "items": {"$ref": "#/components/schemas/CapabilityReport"}}}}},
        "ActionRequest": {"type": "object", "required": ["action"], "additionalProperties": False,
                          "properties": {"action": {"type": "string", "enum": sorted(actions.ACTIONS), "description": "capability name; execution mode is declared per action (x-actions)"},
                                         "parameters": {"type": "object", "description": "the service Request fields the action accepts (see x-actions)"},
                                         "correlation_id": {"type": "string", "maxLength": security.MAX_CORRELATION_ID}}},
        "Result": {"type": "object", "required": ["action", "device_id", "device", "provider", "actor", "correlation_id", "started", "finished", "value", "warnings"],
                   "properties": {"action": {"type": "string"}, "device_id": {"type": "string"}, "device": {"type": "string"}, "provider": {"type": "string"}, "actor": {"type": "string"},
                                  "correlation_id": {"type": "string"}, "started": {"type": "string"}, "finished": {"type": "string"},
                                  "value": {"type": "object", "description": "the service result's to_dict()"}, "warnings": {"type": "array", "items": {"type": "string"}}}},
        "Job": {"type": "object", "required": [f.name for f in dataclasses.fields(JobRecord)] + ["rule", "job_action", "device_id"],
                "description": "JobRecord as persisted, plus the public vocabulary: `action` is the public capability name (backup.photos), `job_action` the record's own rules/jobs name (backup); `rule` aliases `automation` (null for API-started jobs). " + device_note,
                "properties": _fields(JobRecord, {"action": {"type": "string", "enum": sorted(actions.PUBLIC_ACTIONS.values())},
                                                  "job_action": {"type": "string", "enum": sorted(actions.PUBLIC_ACTIONS)},
                                                  "state": {"type": "string", "enum": list(STATES)}, "rule": {"type": ["string", "null"]}, "actor": {"type": ["string", "null"]},
                                                  "device_id": {"type": ["string", "null"]}, "result": {"type": ["object", "null"]}, "error": {"type": ["object", "null"]}})},
        "JobList": {"type": "object", "properties": {"jobs": {"type": "array", "items": {"$ref": "#/components/schemas/Job"}}}},
        "Event": {"type": "object", "required": [f.name for f in dataclasses.fields(Event)] + ["schema", "device_id"],
                  "description": "Event.to_dict() as persisted (schema linkplane.event/1) plus `device_id`. " + device_note,
                  "properties": _fields(Event, {"schema": {"const": "linkplane.event/1"}, "type": {"type": "string", "enum": list(EVENT_TYPES)},
                                                "seq": {"type": ["integer", "null"]}, "data": {"type": "object"}, "device_id": {"type": ["string", "null"]}})},
        "EventList": {"type": "object", "properties": {"events": {"type": "array", "items": {"$ref": "#/components/schemas/Event"}}, "last_seq": {"type": ["integer", "null"]}}},
        "Rule": rule_schema,
        "RulesSummary": rules_summary,
        "Rules": {"allOf": [rules_summary, {"type": "object", "required": ["rules"], "properties": {"rules": {"type": "array", "items": {"$ref": "#/components/schemas/Rule"}}}}]},
        "AuditEntry": {"type": "object", "required": ["schema", "audit_id", "ts", "kind", "actor", "source", "decision", "details", "device_id"],
                       "description": "linkplane.audit/2 line, redacted for HTTP; `action` is the public name when the entry's action is a job action (then `job_action` holds the original). " + device_note,
                       "properties": {"schema": {"type": "string"}, "audit_id": {"type": "string"}, "ts": {"type": "string"}, "kind": {"type": "string"}, "actor": {"type": "string"},
                                      "source": {"type": "string"}, "decision": {"type": "string"}, "device": {"type": "string"}, "device_id": {"type": ["string", "null"]},
                                      "rule": {"type": "string"}, "job_id": {"type": "string"}, "action": {"type": "string"}, "job_action": {"type": "string"},
                                      "correlation_id": {"type": "string"}, "details": {"type": "object"}}},
        "AuditList": {"type": "object", "properties": {"entries": {"type": "array", "items": {"$ref": "#/components/schemas/AuditEntry"}}}},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Linkplane local API", "version": "1", "x-protocol": API_PROTOCOL,
                 "description": "A transport adapter over linkplaned (docs/local-api-design.md, ADR 0012). Loopback only. Every request needs `Authorization: Bearer <token>` (linkplane clients create); tokens are never accepted in the URL. Public contracts describe Linkplane's domain, not its internal vocabulary."},
        "servers": [{"url": f"http://127.0.0.1:{security.DEFAULT_PORT}"}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "description": "token from `linkplane clients create`; header only"}}, "schemas": schemas},
        "x-actions": {name: spec.to_dict() for name, spec in actions.ACTIONS.items()},
        "x-public-actions": dict(sorted(actions.PUBLIC_ACTIONS.items())),
        "x-status-for-code": dict(sorted(security.STATUS_FOR_CODE.items())),
        "x-sensitive-keys": sorted(security.SENSITIVE_KEYS),
    }


def routes_in(spec: dict[str, Any]) -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, operations in spec["paths"].items() for method in operations}


def routes_served() -> set[tuple[str, str]]:
    return {(route.method, route.pattern.pattern.replace("(?P<device>[^/]+)", "{device_id}").replace("(?P<job>[^/]+)", "{job_id}"))
            for route in server.ROUTES}


def render() -> str:
    return json.dumps(build_spec(), indent=2) + "\n"


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DOC_PATH
    target.write_text(render(), encoding="utf-8")
    print(f"wrote {target}")
