# Architecture

Linkplane presents one product while delegating specialized work to proven Android and
Linux tools. Backend names are implementation details, not the user-facing model.

## Layers

```text
GUI                 CLI                 API / MCP
 |                   |                     |
 +--------------- Application services ---+
                         |
                 Device registry
              identity + capabilities
                         |
        +----------------+----------------+
        |                |                |
       ADB          Termux / SSH      LocalSend
        |
      scrcpy
```

Frontends translate user intent and render structured results. Application services own
operations such as status, screen control, transfer, notifications, backup, and
automation. Transport adapters discover endpoints and expose capabilities. External
process execution remains inside adapters; dry-run results may report planned command
metadata, but frontends do not construct those commands.

## Device identity

A logical device may have several endpoints, but Linkplane merges endpoints only when
they have the same proven identifier or the user explicitly pairs them into one named
profile. Profiles retain USB and wireless ADB serial aliases plus a Termux SSH endpoint.
Termux cannot read the hardware serial on current Android versions, so an unprofiled SSH
endpoint requires an explicit `ssh.device_id` association in configuration.

An explicit selector is strict. If a user requests a serial that is offline, unauthorized,
or absent, Linkplane reports that error instead of silently operating on another phone.

Wireless endpoint addresses drift under DHCP. The default path does not discover a device
from nothing; it refreshes a profile only when at least one of its aliases is already
reachable (commonly USB), asks that live link for the device's current Wi-Fi address, and
updates a drifted SSH host or verifies a corrected wireless ADB alias with `adb connect`
before saving it. This works without mDNS/OpenScreen support, which some `adb` builds
omit.

When no alias is reachable at all, an opt-in bounded sweep (`profiles refresh --scan`)
can still recover a profile, but only under a narrow, verifiable condition: the profile
must have both a recorded wireless alias, to anchor the /24 and reuse its exact port
(never guessed), and a hardware serial learned once over USB, the only thing a candidate
match is trusted against. `adb connect` succeeding proves this host's key was authorized
by *some* device before, not which one; a profile with no USB-learned serial has no
trustworthy way to verify a scan result and is skipped rather than trusting an
unverified connection. Every non-matching candidate is disconnected immediately so a scan
leaves neither a stray authorized session nor a pending authorization prompt on another
device's screen.

## Capabilities

Endpoint reachability and capability readiness are separate concerns. Every capability is
reported as `ready`, `needs_dependency`, `unverified`, or `unavailable`, with supporting
detail. For example, an authorized ADB endpoint may support screen transport while the
host still needs scrcpy.

Current capability names are `status`, `screen`, `send`, `backup`, `notify`, `clipboard`,
and `camera`. Future frontends should consume the JSON contracts from `devices` and
`doctor` rather than infer support from executable names.

## Application services

Application operations use immutable request, result, error, and progress-event models.
Device discovery, profiles and pairing, status, diagnostics, screen control, camera
capture/preview, clipboard, notifications, file sending, and verified photo backup now
expose typed service functions and request/result models. Each returns an
`OperationResult`; operations may also emit structured events through an optional
callback. These services do not print, so a GUI, API, or MCP adapter can invoke them
directly. The CLI renders returned data and events and converts failed results into its
existing command-line error behavior. Clipboard text and wireless pairing codes are kept
out of progress events and process arguments.

Host dependencies use structured plans that report all required executables, missing
commands, package-manager selection, distro-specific package names, privilege requirements,
and the proposed installation command. Named plans cover ADB, SSH, scrcpy, LocalSend CLI,
and Wayland/X11 clipboard tools. Discovery, diagnostics, pairing, transfer, screen, camera,
and clipboard workflows consume these plans instead of implementing their own host checks.
Clipboard selection follows the active display session, and scrcpy support is probed for
the options required by Linkplane rather than inferred from executable presence alone.
Unsupported automatic installations are explicit rather than guessed.

Progress events identify the operation and phase, include machine-usable counters and item
paths when relevant, and may carry operation-specific details. Callbacks run synchronously
on the operation thread; frontends are responsible for moving UI updates to their own
event loop.

Most operations run in the foreground and return once their external process exits. The
webcam sink is the first operation that instead launches its process detached
(`subprocess.Popen` with a new session, not `subprocess.run`) so it can keep feeding
`/dev/videoN` after the CLI invocation that started it returns. Its identity — process ID
and target device — is persisted to a small private state file so a later `stop` can find
and signal it. Linkplane does not re-verify that the PID it signals is still the same
process it started; PID reuse after the tracked process has already exited is a known,
accepted risk rather than something this service defends against.

Machine-readable commands return a versioned envelope:

```json
{
  "schema_version": 1,
  "ok": true,
  "data": {}
}
```

Errors use the same envelope with an `error` object. Schema changes that break consumers
must increment `schema_version`.

The in-process typed contracts (request/result dataclasses, error codes, progress phases,
and the `cancel=` token) are versioned separately as `CONTRACT_VERSION` in
`operations.py`; `docs/api-contracts.md` spells out the additive-vs-breaking policy and
`tests/test_contracts.py` pins the frozen surface. Long-running operations (send, backup,
profile refresh, clipboard sync) are cooperatively cancellable: they check a
`CancellationToken` at item, host, or interval boundaries, emit a `cancelled` progress
event, and return a typed `cancelled` error, so the future background runtime can stop a
job without killing the process.

Status telemetry is intentionally partial. Individual failed probes produce a `null`
section and an entry in `data.issues`; the operation and JSON envelope remain successful
when the device itself was reached. Consumers must not treat a missing optional telemetry
section as a disconnected device.

## Security

- ADB authorization and SSH keys remain the transport trust roots.
- Private keys and generated device data are never stored in the repository.
- Device profiles are written atomically with private file permissions.
- Background operation state, such as the webcam sink's tracked PID, is written
  atomically with private file permissions, the same as device profiles.
- Wireless ADB pairing codes are read interactively and sent over stdin, not process arguments.
- User-controlled remote arguments are shell-quoted.
- Automatic fallback happens only before an operation and never after partial execution.
- Cross-transport fallback must not occur for an explicitly selected device.
- Sensitive capabilities such as clipboard and camera require explicit user actions.

## Evolution

Current core operations share the application-service boundary. New workflows should add
typed contracts before CLI, GUI, or API adapters so frontends remain thin renderers over
the same behavior.
