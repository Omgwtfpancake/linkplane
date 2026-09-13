# 0008 — linkplaned: one process, a Unix control socket, and a registry snapshot file

**Status:** Accepted (2026-09-10, Core v0.2 slice 2)

## Context

Slice 1 (ADR 0007) made observation and events real but only for the lifetime of one
`linkplane events` command. The control plane needs a process that keeps observing
between commands, lets several clients subscribe at once, and leaves the last known state
somewhere a non-subscriber can read.

## Decision

- `daemon.py::Daemon` hosts exactly one `Observer` (the same class the foreground command
  uses), a `HistoryWriter`, and a `socketserver.ThreadingUnixStreamServer` on
  `$XDG_RUNTIME_DIR/linkplane/daemon.sock` (mode 0600; `LINKPLANE_SOCKET` overrides).
- The protocol is newline-delimited JSON, one request per connection: `status`, `state`,
  `stop`, and `subscribe {types, devices}` which acknowledges and then streams
  `Event.to_dict()` lines until either side closes. Errors are `{ok: false, code, message}`
  with PB codes; the client raises them as `LinkplaneError`. `LP-DAEMON-001/002/003`
  cover not running / already running / protocol.
- The registry snapshot `$XDG_STATE_HOME/linkplane/state.json` (`LINKPLANE_STATE`) is
  rewritten atomically on every event and on shutdown (`daemon: null`), so anything can
  read last known state without talking to the socket.
- Fan-out is a bounded queue per subscriber; a slow subscriber drops events with a warning
  rather than stalling the observer. A departed subscriber is detected by peeking for EOF.
- Liveness is "the socket answers `status`"; a stale socket file is removed on start, a
  live one refuses the second daemon.
- `events --follow` subscribes when a daemon answers and otherwise observes in-process,
  saying so on stderr.

## Alternatives considered

- **TCP on localhost / HTTP.** Rejected for now: a Unix socket gives filesystem permissions
  for free and there is no remote client yet; the API layer (later) can add an HTTP front
  over the same daemon.
- **D-Bus.** Rejected: a dependency and a desktop-session assumption the core should not
  carry; a D-Bus adapter can wrap the socket later.
- **Client-side history.** Rejected: the daemon owns the history file so `seq` stays
  monotonic across subscribers; `events --follow` writes nothing.

## Slice 3 addendum

`service.py` writes a `systemd --user` unit (`ExecStart=<linkplane> daemon run`,
`Restart=on-failure`, `WantedBy=default.target`) and runs `daemon-reload` / `enable --now`
(injectable runner; `--dry-run` shows everything without touching the system). A real
`systemctl --user stop` sends SIGTERM, which `daemon run` now routes to the same
cooperative stop as Ctrl+C — found on this host: the first stop left the socket behind
and the snapshot claiming a live pid. `devices --observed` reads the snapshot.

## Consequences

The daemon is the natural host for automation rules (a subscriber with actions), the GUI,
and an API. Slice 3 adds `daemon install` (systemd user unit). `linkplane devices`
answering from `state.json` when a daemon runs is the next additive step.
