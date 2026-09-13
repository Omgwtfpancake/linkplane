# Release Smoke Test

One repeatable live-device procedure, separate from the normal suite (`make test` never
touches a phone; CI must never depend on one). It is `tests/device/test_release_smoke.py`,
run by `make smoke` (or `make test-device`, which also runs the other device-tier tests).

## Prerequisites

- The paired phone authorized over USB ADB (`adb devices` shows `device`, not `unauthorized`).
  The test skips itself, with the reason, when no authorized device is present.
- `notify-send` on the host (a desktop notification is one of the rule actions).
- Nothing else: the test uses a temporary socket, state file, history, rules file, audit
  log, and job directory, and removes the one file it pushes to `/sdcard/Download/`.
- The phone's SSH daemon is **not** required; the SSH clipboard round trip is a separate
  device test that skips when `sshd` is down.

## What it exercises, in order

1. **Core commands** — `devices`, `status`, `battery`, `ping`, `capabilities`, `doctor`,
   all `--json`, all `ok: true`, capability catalogue complete.
2. **Daemon start** — in-process on a temporary socket; `status` answers with the protocol.
3. **Observed state** — the phone appears `connected` with battery telemetry.
4. **Rules loaded** — one active rule (send a file on connect), one **blocked** rule
   (`run` without `allow`).
5. **Event subscription and reconnect** — a subscriber receives `device.disconnected` then a
   non-initial `device.connected` after `adb usb` restarts the phone's ADB daemon; the
   connect event is the root of its own correlation chain.
6. **Tracked job** — the reconnect fires the rule; the `send` job reaches `completed` and
   carries the event's correlation id.
7. **Consent and reload** — `allow: ["run"]` is added to the file and `daemon reload`
   reports two active, none blocked.
8. **Audit** — `daemon.started`, `rules.loaded`, `rule.blocked`, `rule.fired`,
   `job.started`, `job.completed` are present, and the chain for the reconnect's
   correlation id contains the firing and both job entries.
9. **Daemon stop** — over the socket; socket removed, snapshot marked `daemon: null`,
   last audit line `daemon.stopped`.

## Expected output

```text
$ make smoke
test_01_core_commands_answer_in_json ... ok
test_02_daemon_end_to_end ... ok
----------------------------------------------------------------------
Ran 2 tests in ~5s
OK
```

Without a phone:

```text
test_01_core_commands_answer_in_json ... skipped 'no authorized ADB device: no ADB device connected'
```

## When it fails

- A failure in step 1 is a provider or connectivity problem: run `linkplane doctor`.
- A failure in step 5 usually means the phone did not come back within 30 s after
  `adb usb`; check `adb devices`. Never use `adb reconnect` as a substitute — on some
  hosts it detaches the USB device until the ADB server is restarted.
- A failure in step 6 with the job `failed` means `adb push` failed; the job record under
  the temporary directory holds the error (re-run with `--debug` on `daemon run` to keep
  a copy).

## Local API (Slice 1)

`tests/device/test_api_smoke.py` (`make test-device`, or point discovery at it) runs the
daemon with the real observer and the API on an ephemeral loopback port against temporary
files, then checks: health and `api.json`; `GET /v1/devices` returns the phone under its
canonical `device_id` (stable across a profile rename); `/state` is the observed record;
`/capabilities` equals `ADBProvider.capabilities()`; `device.ping` and `battery.read` are
200 Results and an ungranted action is 403; a `backup.photos` dry run is a 202 Job that
completes and writes nothing; `adb usb` produces `device.disconnected` then
`device.connected` on `/v1/events/stream` in `seq` order, matching `GET /v1/events?after=`;
stopping the daemon closes the listener and removes `api.json` and the socket.
Slice 2 adds one metadata step: `GET /v1/rules`, `POST /v1/rules/reload`, `GET /v1/audit`
filtered by the phone and `backup.photos` (every entry carries the canonical `device_id`
and the public action name), and a cancel of the completed dry-run job answering 409.
