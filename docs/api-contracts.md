# API Contracts

This is a reference for building an API, MCP, or GUI adapter directly against Linkplane's
application services — the same typed functions the CLI calls, without going through the CLI
process or its text/JSON rendering. It is the concrete deliverable behind the roadmap item
"Stabilize and version typed operation contracts for external interfaces" (see
`docs/roadmap.md`, "Foundation", and `docs/product-progress-brief.md`, "Reliable Core").

Every fact below was read directly from the source in `src/linkplane/` as it stands today
(schema version 1, contract version 1), not inferred from names. Where a detail looked like it
could be an implementation accident rather than an intentional contract, that is called out
rather than asserted as stable.

## Stability policy (contract version 1)

The typed service surface is **frozen** as of `linkplane.operations.CONTRACT_VERSION = 1`.
Concretely, the frozen surface is:

- Every `Request`/`Result`/`Outcome`/`Change`/`Item` dataclass catalogued in this document,
  plus the shared `OperationResult`, `OperationError`, `ProgressEvent`, and the embedded
  `models.py`/`dependencies.py` types: each pinned field keeps its **name, relative order,
  and type**.
- The set of `OperationError.code` values (closing table) and what each one means.
- The `(operation, phase)` progress pairs listed per service, and the documented contents of
  `details` at each phase.
- The `cancel=` keyword and the cancellation semantics described in "Cancellation" below.

What may change **without** a version bump (additive growth):

- New fields on any dataclass, provided they have defaults so existing constructors and
  positional consumers keep working.
- New `OperationError.code` values and new `(operation, phase)` pairs. A generic consumer
  should treat an unknown code as `operation_failed` and ignore unknown phases.
- New keyword-only parameters on service functions, with defaults.
- New service functions and new modules.

What **requires** bumping `CONTRACT_VERSION` (a breaking change):

- Renaming, removing, reordering, or changing the type of a pinned field.
- Adding a required (default-less) field to a `Request`.
- Removing a code or phase, or changing what an existing one means.
- Renaming or removing a service function or the `cancel=`/`progress=` keywords.

`tests/test_contracts.py` pins this surface mechanically: it snapshots every frozen field list,
the exact error-code set, and the `cancel=` signature of every cancellable service, so an
accidental break fails a test rather than silently shipping. A deliberate break updates the
snapshot, bumps `CONTRACT_VERSION`, and records the change in this document.

`CONTRACT_VERSION` and `JSON_SCHEMA_VERSION` are independent: the first versions the in-process
typed surface, the second versions only the CLI's `--json` envelope (next section). Neither
implies the other.

## Versioning policy

`linkplane.operations.JSON_SCHEMA_VERSION` (currently `1`) versions exactly one thing: the
shape of the top-level JSON envelope that every `--json` CLI command prints — the
`schema_version` / `ok` / `data` (or `error`) wrapper described below. It does **not** version
the contents of any individual command's `data` object. A command's `data` payload can gain new
fields over time without bumping `JSON_SCHEMA_VERSION`, as long as that growth is additive
(existing fields keep their meaning and type). `JSON_SCHEMA_VERSION` only needs to increment
when the *envelope itself* changes in a way that breaks consumers — e.g. renaming
`schema_version`/`ok`/`data`/`error`, or changing the error object's shape.

This is the same policy `docs/architecture.md` already states ("Schema changes that break
consumers must increment `schema_version`"); this document exists to spell out the boundary
between "envelope" and "payload" precisely, since that boundary is what a generic API/MCP
client needs to code against.

`JSON_SCHEMA_VERSION` lives in `operations.py` (not `cli.py`) specifically so application-service
modules that print their own envelope (`find.py`, `webcam.py`, `profiles.py`) can import it
without a circular dependency on `cli.py`. Before commit `2413e69`, three of those modules
hardcoded the literal `1` instead of importing the constant; the literals happened to still
agree, but nothing enforced that. `tests/test_schema_version.py` now exercises the real
`--json` code path of every command that builds its own envelope to guard against that drift
recurring.

Calling a service function directly (rather than through `--json`) sidesteps the envelope
question entirely — you get the `OperationResult` described below, and no JSON schema version
applies to it at all. The envelope only matters for the CLI's own JSON rendering.

## The shared JSON envelope

Only eight CLI commands currently build a `--json` envelope at all: `status`, `find`,
`devices`, `doctor`, `profiles list`, `profiles refresh`, `webcam start`, and `webcam stop`.
`screen`, `audio`, `send`, `backup`, `camera capture`, `camera preview`, `notify`, `clipboard`,
`pair usb`/`wireless`/`ssh`, `profiles remove`, and `profiles default` all have typed
`Request`/`Result` dataclasses (documented below) but no `--json` output today — their CLI
wrappers only print human-readable text. This is one of the concrete reasons to call the
service functions directly: several typed results are reachable today only that way, not
through any CLI JSON path.

### Success

Built by `cli.py`'s `main()` (or, for `find`/`webcam start`/`webcam stop`/`profiles
list`/`profiles refresh`, by the command's own module) as
`{"schema_version": JSON_SCHEMA_VERSION, "ok": True, "data": <result>.to_dict()}`. Real output
from `linkplane status --json` on this machine:

```json
{
  "data": {
    "battery": {
      "health": "good",
      "level": 96,
      "powered_by": ["usb"],
      "status": "charging",
      "temperature_c": 31.1,
      "voltage_mv": 4245
    },
    "device": {
      "android": "16",
      "kernel": "5.10.236-android12-9-31998796-abS901USQSAGZH3",
      "manufacturer": "samsung",
      "model": "SM-S901U",
      "product": "r0qsqw",
      "serial": "DEVICE_SERIAL"
    },
    "issues": [],
    "memory": {
      "available_bytes": 2578579456,
      "total_bytes": 7571677184,
      "used_percent": 65.9
    },
    "storage": {
      "available_bytes": 13501853696,
      "path": "/sdcard",
      "total_bytes": 111975297024,
      "used_bytes": 98339225600,
      "used_percent": 88
    },
    "transport": "adb",
    "uptime_seconds": 71124
  },
  "ok": true,
  "schema_version": 1
}
```

(`status` is the one command that sorts its JSON keys; the others below do not, so do not rely
on key order for any command.)

### Error

Built once, centrally, in `main()`'s `except` handler in `cli.py`:

```python
except (BridgeError, KeyError, TypeError, ValueError) as error:
    if getattr(arguments, "json", False):
        print(json.dumps({
            "schema_version": JSON_SCHEMA_VERSION,
            "ok": False,
            "error": {"type": type(error).__name__, "message": str(error)},
        }, indent=2))
```

Real output from `linkplane status --transport ssh --serial foo --json` (an intentionally
invalid combination that fails validation before touching any device — safe to run):

```json
{
  "schema_version": 1,
  "ok": false,
  "error": {
    "type": "BridgeError",
    "message": "--serial cannot be used with the SSH transport"
  }
}
```

**This error object is `{"type", "message"}` plus, since Core v0.1, `"code"` and optionally
`"hints"`.** `type` is the Python exception class name that reached `main()` — `BridgeError`,
`LinkplaneError`, `OperationCancelled`, occasionally `KeyError`/`TypeError`/`ValueError` if
something unexpected slipped through. `code` is the stable ``LP-<CATEGORY>-<NNN>`` identifier
from `linkplane.core.errors` (`LP-CONNECT-001`, `LP-TIMEOUT-001`, …): exact for a
`LinkplaneError`, `LP-CANCELLED-001` for a cancellation, and otherwise derived by
`errors.classify()` from the message of a plain `BridgeError`. `hints` is a list of short
"check that …" strings when the code has any. Both keys are additive; the envelope's
`schema_version` is unchanged. Note that this `code` is a different vocabulary from
`OperationError.code` (`invalid_request`, `dependency_missing`, …) — `errors.OPERATION_CODE_MAP`
maps the latter onto the former.

The reason: every CLI command wrapper (`get_status`, `screen`, `audio`, `send`, `backup`,
`capture_photo`, `preview_camera`, `webcam_start`, `webcam_stop`, `notify`, `find_phone`,
`clipboard`, `pair_usb`, `pair_wireless`, `pair_ssh`, `list_profiles`, `remove_profile`,
`set_default_profile`, `refresh_profiles`) follows the same pattern:

```python
result = some_service_function(SomeRequest(...))
if result.error is not None:
    raise BridgeError(result.error.message)
```

The specific `OperationError.code` is discarded at this point — only its `message` text
survives, re-wrapped in a generic `BridgeError` that `main()` then reports as `"type":
"BridgeError"`. **A consumer that shells out to `linkplane <command> --json` cannot recover
`OperationError.code` from the output; only a consumer calling the service function directly
gets the structured code.** This asymmetry is exactly what makes direct service calls more
useful than CLI JSON for programmatic error handling, and it is worth designing an API/MCP
adapter to call services directly rather than to shell out and parse `error.type`.

## The shared service contract (`operations.py`)

Every service function below returns an `OperationResult[T]`, a frozen, generic dataclass:

```python
@dataclass(frozen=True)
class OperationResult(Generic[T]):
    value: T | None = None
    error: OperationError | None = None
```

`__post_init__` enforces that exactly one of `value`/`error` is set (constructing one with
both or neither raises `ValueError`). Build one with `OperationResult.success(value)` or
`OperationResult.failure(code, message)`; read one with the `.ok` property (`True` iff
`error is None`) or by checking `.value`/`.error` directly.

```python
@dataclass(frozen=True)
class OperationError:
    code: str
    message: str
    # .to_dict() -> {"code": str, "message": str}
```

`code` is a short machine-matchable string (`"invalid_request"`, `"transport_unavailable"`,
etc. — the full set is in the closing table below); `message` is a human-readable string not
meant to be pattern-matched.

Operations may also emit structured progress through an optional callback, run synchronously
on the calling thread:

```python
ProgressCallback = Callable[[ProgressEvent], None]

@dataclass(frozen=True)
class ProgressEvent:
    operation: str
    phase: str
    message: str
    current: int | None = None
    total: int | None = None
    unit: str | None = None
    item: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    # .to_dict() -> all of the above
```

`operation` identifies which service emitted the event (e.g. `"screen"`, `"backup"`,
`"pair_wireless"`); `phase` identifies the step within that operation (e.g. `"started"`,
`"item_completed"`, `"completed"`) — the exact set of `operation`/`phase` pairs each module can
emit is listed per module below. `current`/`total`/`unit` are for progress bars over a known
item count (e.g. files backed up); `item` names the specific item in flight; `details` carries
operation-specific structured data, notably a copy of the in-progress or final result via
`.to_dict()` at `"started"` and `"completed"` phases in most modules. `report_progress(callback,
event)` is the internal helper every service uses to invoke the callback only when one was
passed — it is not itself a contract surface.

### Cancellation

Long-running services accept a keyword-only `cancel: CancellationToken | None = None`:

```python
class CancellationToken:
    cancelled: bool            # property
    reason: str | None         # property; the first reason passed to cancel()
    def cancel(self, reason: str = "operation cancelled") -> None
    def wait(self, timeout: float) -> bool   # sleep, returning True early if cancelled
    def raise_if_cancelled(self) -> None     # raises OperationCancelled
```

The token is thread-safe: create it on any thread, pass it in, and call `cancel()` from
another thread, a signal handler, or a timer. Cancellation is **cooperative**: a service checks
the token at its natural boundaries (before each file, host, or polling interval) and finishes
the step in flight first — an `adb push` or `adb pull` that has already started runs to
completion, and an in-flight subnet-scan probe finishes its (short, bounded) timeout. When a
service observes the token it emits one `("<operation>", "cancelled")` progress event
describing how far it got, then returns `OperationResult.failure("cancelled", reason)`. The
`reason` is whatever was passed to `cancel()`.

Services that accept `cancel=` today, and what a cancelled run leaves behind:

| Service | Boundaries checked | State after cancel |
|---|---|---|
| `transfer.send_files` (ADB path) | before `mkdir`, before each push, and after a push that exits non-zero | Items already pushed stay on the phone; nothing partial is retried or cleaned up. `("send", "cancelled")` carries `current`/`total` items sent. LocalSend hands the whole batch to one external process and has no boundary; it ignores the token. |
| `backup.backup_photos` | before discovery, after discovery, before each pull | Every completed file is already recorded in the manifest, so the next run resumes exactly where this one stopped. `("backup", "cancelled")` carries the partial `BackupResult` (with `downloaded` set) in `details`. |
| `profiles.refresh_profile_endpoints` | before each profile, and inside `--scan` between probes | The config file is untouched: changes are applied only after every profile is examined. `("profile_refresh", "cancelled")` carries the outcomes gathered so far in `details["outcomes"]`. |
| `profiles.scan_for_profile` | before each probe and between probe completions | Raises `OperationCancelled` directly (it returns a plain `str | None`, not an `OperationResult`). Queued probes short-circuit; running ones finish their timeout. |
| `clipboard.use_clipboard` / `sync_clipboards` (`action="sync"`) | every polling interval (the token's `wait` replaces `time.sleep`, so cancel is immediate) | **Documented exception:** stopping is the only way a sync ends, so a cancelled sync is its normal completion — it returns `OperationResult.success` with the accumulated `updates` count, exactly as Ctrl+C does, not a `cancelled` error. |

Services not in that table (`status`, `screen`, `audio`, `camera`, `webcam`, `notify`, `find`,
`devices`, `doctor`, pairing, `profiles list/remove/default`) are either single-shot or hand
control to one foreground external process (scrcpy) and do not accept `cancel=`. Passing an
unknown keyword to them raises `TypeError`, so check the table rather than passing it blindly.

The CLI wires this in for `send`, `backup`, `profiles refresh`, and `clipboard sync`: the first
Ctrl+C cancels the token (the operation stops at its next boundary and the process exits with
status 130), a second Ctrl+C raises `KeyboardInterrupt` and force-quits (also 130). The helper
that does this, `operations.cancel_on_interrupt`, is a CLI convenience, not a contract surface.
In `--json` mode a cancelled command prints the standard error envelope with
`"type": "OperationCancelled"`.

A note on injectable parameters: nearly every service function accepts keyword-only seams like
`adb_factory`, `ssh_factory`, `process_runner`, `command_runner`, `dependency_factory`,
`compatibility_factory`, `state_loader`/`state_saver`, `foreground_runner`, `code_reader`, and
similar. These exist so tests can substitute fakes for real ADB/SSH/subprocess/filesystem
access — they are not part of the intended external API surface. An API/MCP/GUI adapter should
call these functions with only the `request` object (and, for `notify`/`camera
capture`/`clipboard`, the transport arguments those specific functions require positionally —
see their sections below) and let the defaults run real commands.

## Shared embedded types

These dataclasses are not themselves service results; they appear *inside* several modules'
`Request`/`Result` dataclasses and are documented once here rather than repeated per module.

### `models.py`

```python
@dataclass(frozen=True)
class Capability:
    name: str
    status: str          # "ready" | "needs_dependency" | "unverified" | "unavailable"
    detail: str | None = None

@dataclass(frozen=True)
class Endpoint:
    transport: str
    address: str
    state: str
    device_id: str
    capabilities: tuple[Capability, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # .to_dict()

@dataclass(frozen=True)
class Device:
    id: str
    name: str
    manufacturer: str | None
    model: str | None
    android: str | None
    capabilities: tuple[Capability, ...]
    endpoints: tuple[Endpoint, ...]
    # .to_dict()

@dataclass(frozen=True)
class Check:
    name: str
    status: str           # "ok" | "warning" | "error" (see doctor.py)
    summary: str
    fix: str | None = None
    # .to_dict()

@dataclass(frozen=True)
class DiscoveryIssue:
    backend: str
    error: str
    # .to_dict()

@dataclass(frozen=True)
class DiscoveryResult:
    devices: tuple[Device, ...]
    issues: tuple[DiscoveryIssue, ...] = ()
    # no .to_dict() — cli.py assembles the devices/doctor JSON payload by hand from this
```

`Capability.status` values in current use (from `devices.py`/`doctor.py`): `ready`,
`needs_dependency`, `unverified`, `unavailable` — matching `docs/architecture.md`'s
"Capabilities" section. Current capability `name`s in use: `status`, `send`, `backup`,
`notify`, `screen`, `camera`, `clipboard` (per-endpoint, from `devices.py` and `clipboard.py`'s
Termux probe).

### `dependencies.py`

```python
@dataclass(frozen=True)
class DependencyPlan:
    executable: str
    package: str | None
    available: bool
    executable_path: str | None
    package_manager: str | None
    install_command: tuple[str, ...] | None
    required_executables: tuple[str, ...] = ()
    missing_executables: tuple[str, ...] = ()
    requires_privilege: bool = False
    install_error: str | None = None
    # .to_dict()

@dataclass(frozen=True)
class ScrcpyCompatibility:
    screen_supported: bool
    camera_supported: bool
    audio_supported: bool = True
    missing_screen_options: tuple[str, ...] = ()
    missing_camera_options: tuple[str, ...] = ()
    missing_audio_options: tuple[str, ...] = ()
    probe_error: str | None = None
    webcam_supported: bool = True
    missing_webcam_options: tuple[str, ...] = ()
    # .to_dict()
```

`DependencyPlan` reports whether a host executable (e.g. `adb`, `scrcpy`, `ssh`,
`localsend-cli`, `wl-copy`/`xclip`, `v4l2loopback`) is available, and if not, the detected
package manager and the exact install command Linkplane would run.
`ScrcpyCompatibility.*_supported` is derived by probing `scrcpy --help` output for the specific
CLI options each feature (screen mirroring, camera source, audio, V4L2 webcam sink) actually
needs — not inferred from `scrcpy`'s mere presence.

These embed directly into several results: `ScreenResult.dependency`,
`AudioResult.dependency`, `CameraPreviewResult.dependency`,
`WebcamStartResult.scrcpy_dependency`/`.loopback_dependency`. `ScrcpyCompatibility` itself does
not appear inside a `Result` dataclass — it is computed and consulted internally (to decide
whether to fail with `dependency_missing`) but not returned to the caller.

## Per-service contracts

All `Request`/`Result` dataclasses below are `@dataclass(frozen=True)`.

### `status.py` — read device status

One line: read battery/memory/storage/uptime telemetry over ADB or Termux/SSH, tolerating
individual probe failures.

```python
@dataclass(frozen=True)
class StatusRequest:
    transport: str = "auto"            # "auto" | "adb" | "ssh"
    serial: str | None = None
    serial_from_profile: bool = False

@dataclass(frozen=True)
class TelemetryIssue:
    component: str
    error: str

@dataclass(frozen=True)
class StatusResult:
    transport: str
    device: dict[str, Any]
    battery: dict[str, Any] | None
    memory: dict[str, Any] | None
    storage: dict[str, Any] | None
    uptime_seconds: int | None
    issues: tuple[TelemetryIssue, ...] = ()
    # .to_dict(); .from_dict(value) reconstructs from the raw transport dict
```

`device`/`battery`/`memory`/`storage` are free-form dicts assembled by the transport layer
(`transports.py`'s `AdbTransport.status()`/`SshTransport.status()`), not further typed here.
Per `docs/architecture.md`: "Status telemetry is intentionally partial" — an individual failed
probe yields `null` for that section plus an entry in `issues`, and the overall
`OperationResult` is still a success if the device itself was reached. A missing optional
section must not be read as "device disconnected."

```python
def read_status(
    request: StatusRequest,
    *,
    ssh_factory: Callable[[], SshTransport] | None = None,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
) -> OperationResult[StatusResult]
```

- Error codes: `invalid_request` (unsupported `transport` value; `serial` combined with
  `transport="ssh"`), `transport_unavailable` (ADB or SSH probe failed; for `transport="auto"`
  the message concatenates both failures when both were tried).
- Progress events: none. `read_status` never calls `report_progress`.

### `screen.py` — mirror the phone screen (scrcpy)

One line: launch `scrcpy` against a device with a quality preset, waiting for it to exit.

```python
@dataclass(frozen=True)
class ScreenRequest:
    serial: str | None = None
    quality: str = "balanced"          # "low" | "balanced" | "high" | "game"
    audio: bool = True
    record: str | None = None
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False

@dataclass(frozen=True)
class ScreenResult:
    device: str
    serial: str
    quality: str
    command: tuple[str, ...]
    dependency: DependencyPlan
    dry_run: bool
    exit_code: int | None = None
    # .to_dict()
```

```python
def launch_screen(
    request: ScreenRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    process_runner: Callable[..., Any] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[ScreenResult]
```

This is a foreground operation: on success, it has already run `scrcpy` to completion via
`subprocess.run` (or `process_runner`) and `exit_code` is scrcpy's real exit code — it does not
return early while mirroring is still active. `dry_run=True` returns the planned `command`
without dependency checks or execution and without setting `exit_code`.

- Error codes: `invalid_request` (unknown `quality`), `transport_unavailable` (no ADB device
  selectable, or no serial resolvable), `dependency_missing` (scrcpy missing, or present but
  missing the screen-mirroring options `ScrcpyCompatibility.screen_supported` checks for),
  `operation_failed` (the scrcpy process itself failed to start/raised `OSError`, or a
  `BridgeError` from transport/dependency plumbing).
- Progress events: `("screen", "started")`, `("screen", "command")` (dry run only),
  `("screen", "completed")`.

### `audio.py` — forward or record phone audio only

One line: the same `scrcpy` launch pattern as `screen.py`, but with `--no-video` and an audio
source/codec/record-format instead of a video quality preset.

```python
@dataclass(frozen=True)
class AudioRequest:
    serial: str | None = None
    source: str = "playback"           # one of AUDIO_SOURCES (11 values, see below)
    record: str | None = None
    record_format: str | None = None   # one of AUDIO_RECORD_FORMATS if set
    codec: str | None = None           # one of AUDIO_CODECS if set
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False

@dataclass(frozen=True)
class AudioResult:
    device: str
    serial: str
    source: str
    command: tuple[str, ...]
    dependency: DependencyPlan
    dry_run: bool
    exit_code: int | None = None
    # .to_dict()
```

`AUDIO_SOURCES = ("output", "playback", "mic", "mic-unprocessed", "mic-camcorder",
"mic-voice-recognition", "mic-voice-communication", "voice-call", "voice-call-uplink",
"voice-call-downlink", "voice-performance")` — verified against scrcpy 4.1's `--help`, per the
module's own comment. `AUDIO_CODECS = ("opus", "aac", "flac", "raw")`.
`AUDIO_RECORD_FORMATS = ("m4a", "mka", "opus", "aac", "flac", "wav")` — only formats valid for
an audio-only (`--no-video`) recording; if `record` is set without `record_format`, the format
is inferred from the file extension and rejected if it doesn't match one of these.

```python
def launch_audio(
    request: AudioRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    process_runner: Callable[..., Any] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[AudioResult]
```

Foreground, blocking, same as `launch_screen` — `exit_code` is real scrcpy exit status on
success.

- Error codes: `invalid_request` (unknown `source`/`codec`, or an unresolvable
  `record`/`record_format` combination), `transport_unavailable`, `dependency_missing` (scrcpy
  missing or `ScrcpyCompatibility.audio_supported` is false), `operation_failed`.
- Progress events: `("audio", "started")`, `("audio", "command")` (dry run only), `("audio",
  "completed")`.

### `transfer.py` — send files to the phone

One line: push local files/directories to the phone over ADB, or hand them to the LocalSend CLI.

```python
DEFAULT_DESTINATION = "/sdcard/Download"

@dataclass(frozen=True)
class SendRequest:
    paths: tuple[str, ...]
    transport: str = "auto"            # "auto" | "adb" | "localsend"
    destination: str = DEFAULT_DESTINATION
    serial: str | None = None
    dry_run: bool = False

@dataclass(frozen=True)
class SendItem:
    path: str
    size: int

@dataclass(frozen=True)
class SendResult:
    transport: str                     # "adb" | "localsend"
    items: tuple[SendItem, ...]
    total_bytes: int
    destination: str | None            # None for localsend
    device: str | None                 # None for localsend
    serial: str | None                 # None for localsend
    dry_run: bool
    # .to_dict()
```

```python
def send_files(
    request: SendRequest,
    *,
    progress: ProgressCallback | None = None,
) -> OperationResult[SendResult]
```

Unlike most other modules, `send_files` has no `adb_factory` injection seam — it constructs
`AdbTransport(request.serial)` directly. With `transport="auto"` and the default destination,
an ADB failure falls back to LocalSend automatically; an explicit `transport="adb"` or a
non-default `destination` disables that fallback (and combining `transport="localsend"` with a
non-default `destination`, or with `serial` set, is itself an `invalid_request`).

- Error codes: `invalid_request` (unsupported `transport`, a source path that doesn't exist,
  `destination` not absolute or containing a newline, or a `localsend`/`serial`/`destination`
  combination that isn't allowed), `transport_unavailable` (ADB unselectable when ADB was
  required or `serial` was given), `dependency_missing` (LocalSend CLI not installed),
  `operation_failed` (mkdir/push/LocalSend process failure), `cancelled` (the `cancel` token
  was set; ADB path only).
- Progress events: `("send", "started")`, `("send", "command")` (dry run only), `("send",
  "item_started")`, `("send", "item_completed")` (ADB path only — LocalSend hands the whole
  batch to one external process, so there is no per-item progress there), `("send",
  "cancelled")` (ADB path only; `current`/`total` = items sent so far / total), `("send",
  "completed")`.
- Cancellation: accepts `cancel=`; see "Cancellation" above.

### `backup.py` — incremental verified photo backup

One line: pull new/changed files from an Android directory (default `/sdcard/DCIM/Camera`) to
a local directory, skipping files whose size/mtime/SHA-256 already match a JSON manifest, and
verifying every downloaded file's SHA-256 against the phone's own `sha256sum` before publishing
it.

```python
DEFAULT_SOURCE = "/sdcard/DCIM/Camera"
DEFAULT_DESTINATION = "~/Pictures/Linkplane"
MANIFEST_SCHEMA_VERSION = 1     # unrelated to JSON_SCHEMA_VERSION — see note below

@dataclass(frozen=True)
class BackupRequest:
    destination: str = DEFAULT_DESTINATION
    source: str = DEFAULT_SOURCE
    serial: str | None = None
    dry_run: bool = False

@dataclass(frozen=True)
class BackupResult:
    transport: str                     # always "adb" today
    device: str
    serial: str
    source: str
    destination: str
    discovered: int
    pending: int
    pending_bytes: int
    downloaded: int
    skipped: int
    pending_files: tuple[str, ...]     # relative paths
    dry_run: bool
    # additive (v0.6 development, unreleased)
    preserved: tuple[str, ...] = ()    # existing local files with no manifest record that were not overwritten
    adopted: int = 0                   # such files byte-identical to the phone's, now recorded instead of re-downloaded
    downloaded_bytes: int = 0
    verified_bytes: int = 0            # bytes re-hashed to confirm already backed-up files
    discovery_seconds: float = 0.0     # measurement for the unchanged-file strategy
    unchanged_check_seconds: float = 0.0
    duration_seconds: float = 0.0
    # .to_dict()
```

Safety (v0.6 development): before downloading anything, `backup_photos` checks that the
destination's filesystem has room for every pending file plus the largest one replacing an
existing local copy, and otherwise fails with `operation_failed` / `LP-STORAGE-001` without
creating the destination. It never overwrites a local file that no manifest entry records,
and refuses to write through a symbolic link below the destination root.

`MANIFEST_SCHEMA_VERSION` versions the on-disk `.linkplane-manifest.json` file this module
writes into the backup destination — a completely separate version number from
`JSON_SCHEMA_VERSION`; do not conflate the two. A manifest with a mismatched
`MANIFEST_SCHEMA_VERSION`, or one that names a different device serial or source path, makes
`backup_photos` fail rather than silently mixing backups.

```python
def backup_photos(
    request: BackupRequest,
    *,
    progress: ProgressCallback | None = None,
) -> OperationResult[BackupResult]
```

No `adb_factory` seam here either — constructs `AdbTransport(request.serial)` directly.
`downloaded` is `0` in a dry run (nothing was actually pulled) even though `pending` is
computed for real.

- Error codes: `invalid_request` (source not an absolute Android path, contains a control
  character, or resolves outside itself via `..`), `transport_unavailable` (no ADB device
  selectable), `operation_failed` (discovery/stat/checksum/pull/manifest I/O failure;
  `error_code` `LP-STORAGE-001` when the destination lacks free space),
  `cancelled` (the `cancel` token was set).
- Progress events: `("backup", "discovering")`, `("backup", "started")`, `("backup",
  "item_pending")` (dry run only), `("backup", "item_started")`, `("backup", "item_completed")`,
  `("backup", "cancelled")` (`details` = the partial `BackupResult.to_dict()` with `downloaded`
  = files completed before the cancel), `("backup", "completed")`.
- Cancellation: accepts `cancel=`; a cancelled backup is resumable because the manifest is
  saved after every file. See "Cancellation" above.

### `camera.py` — Termux photo capture, and scrcpy camera preview

This module has two independent operations.

#### Capture (`capture_camera_photo`)

One line: take a photo via `termux-camera-photo` over SSH, base64-transfer it back, decode and
save it locally, then delete the remote temp file.

```python
TERMUX_TMP = "/data/data/com.termux/files/usr/tmp"

@dataclass(frozen=True)
class CaptureRequest:
    output: str | None = None          # default: ~/Pictures/Linkplane/capture-<timestamp>.jpg
    camera_id: int = 0
    force: bool = False                # overwrite an existing output file
    foreground: bool = True            # bring Termux to the foreground on the paired ADB device first
    dry_run: bool = False

@dataclass(frozen=True)
class CaptureResult:
    transport: str                     # always "ssh"
    endpoint: str                      # "user@host"
    camera_id: int
    output: str
    remote_path: str
    foreground_serial: str | None
    command: tuple[str, ...]
    bytes_written: int | None          # None until the photo is actually saved
    dry_run: bool
    # .to_dict()
```

```python
def capture_camera_photo(
    request: CaptureRequest,
    transport: SshTransport,
    adb_serial: str | None,
    *,
    foreground_runner: Callable[[str], None] = foreground_termux,
    progress: ProgressCallback | None = None,
) -> OperationResult[CaptureResult]
```

Note the shape: `transport` is a required positional `SshTransport` **instance**, not an
injectable factory — the caller must already have a configured SSH transport (in the CLI, via
`configured_ssh_context`) before calling. `adb_serial` is also required positionally (it can be
`None`, but `foreground=True` with `adb_serial=None` is an `invalid_request`).

The saved JPEG is validated: `decode_photo` requires a well-formed base64 payload whose decoded
bytes start with the JPEG SOI marker (`\xff\xd8`) and end with EOI (`\xff\xd9`); a malformed
transfer fails as `operation_failed` rather than writing a corrupt file.

- Error codes: `invalid_request` (negative `camera_id`, output path already exists without
  `force`, output path is a directory, or `foreground=True` with no `adb_serial`),
  `transport_unavailable` (building the remote command failed), `operation_failed` (foreground
  bring-up failed, capture/transfer/decode/save failed). No `dependency_missing` — capture
  doesn't touch scrcpy.
- Progress events: `("camera_capture", "started")`, `("camera_capture", "command")` (dry run
  only), `("camera_capture", "foreground")`, `("camera_capture", "capturing")`,
  `("camera_capture", "transferring")`, `("camera_capture", "completed")`.

#### Preview (`preview_phone_camera`)

One line: launch `scrcpy --video-source=camera` for a live preview/recording, same
foreground-blocking pattern as `screen.py`.

```python
@dataclass(frozen=True)
class CameraPreviewRequest:
    serial: str | None = None
    quality: str = "balanced"          # "low" | "balanced" | "high" | "motion"
    facing: str | None = None          # e.g. "back" | "front" — mutually exclusive with camera_id
    camera_id: str | None = None
    audio: bool = True
    torch: bool = False
    record: str | None = None
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False

@dataclass(frozen=True)
class CameraPreviewResult:
    device: str
    serial: str
    selection: str                     # "ID <camera_id>" or the resolved facing value
    quality: str
    command: tuple[str, ...]
    dependency: DependencyPlan
    dry_run: bool
    exit_code: int | None = None
    # .to_dict()
```

```python
def preview_phone_camera(
    request: CameraPreviewRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    process_runner: Callable[..., Any] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[CameraPreviewResult]
```

- Error codes: `invalid_request` (unknown `quality`, or both `facing` and `camera_id` set),
  `transport_unavailable`, `dependency_missing` (scrcpy missing, or
  `ScrcpyCompatibility.camera_supported` is false), `operation_failed`.
- Progress events: `("camera_preview", "started")`, `("camera_preview", "command")` (dry run
  only), `("camera_preview", "completed")`.

### `webcam.py` — Linux V4L2 webcam sink

Two operations that manage a detached background process, plus its persisted state.

#### Start (`start_webcam`)

One line: launch `scrcpy --v4l2-sink=<device>` detached (`subprocess.Popen`, new session) so it
outlives the calling process, and persist its PID/device/serial to a small JSON state file so a
later `stop` can find it.

```python
@dataclass(frozen=True)
class WebcamStartRequest:
    serial: str | None = None
    device: int | None = None          # explicit /dev/videoN; auto-detected via v4l2-ctl if omitted
    facing: str | None = None          # mutually exclusive with camera_id
    camera_id: str | None = None
    extra_arguments: tuple[str, ...] = ()
    install: bool = False
    dry_run: bool = False
    state_path: str | None = None      # default: $LINKPLANE_WEBCAM_STATE or ~/.config/linkplane/webcam.json

@dataclass(frozen=True)
class WebcamStartResult:
    device: str
    serial: str
    video_device: str                  # "/dev/videoN"
    command: tuple[str, ...]
    scrcpy_dependency: DependencyPlan
    loopback_dependency: DependencyPlan
    pid: int | None                    # None until the process actually starts (also None on dry run)
    dry_run: bool
    # .to_dict()
```

```python
def start_webcam(
    request: WebcamStartRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    scrcpy_dependency_factory: Callable[[], DependencyPlan] | None = None,
    loopback_dependency_factory: Callable[[], DependencyPlan] | None = None,
    dependency_handler: Callable[[bool], None] | None = None,
    compatibility_factory: Callable[[DependencyPlan], ScrcpyCompatibility] | None = None,
    command_runner: Callable[..., str] = run_command,
    process_factory: Callable[..., Any] = subprocess.Popen,
    process_alive: Callable[[int], bool] | None = None,
    state_loader: Callable[[str | None], dict[str, Any] | None] | None = None,
    state_saver: Callable[[dict[str, Any], str | None], Path] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[WebcamStartResult]
```

Unlike `screen`/`audio`/`camera preview`, this returns as soon as the detached process has
launched and its state file is written — it does not block for the webcam session's duration.
It refuses to start a second sink: if state from a previous `start` still names a live PID,
this fails with `already_running` rather than launching a second `scrcpy` process. Per
`docs/architecture.md`, Linkplane does not re-verify that a later-signaled PID is still the
same process (PID reuse after the tracked process exited is a known, accepted risk).

The CLI's `webcam_start(arguments)` wrapper only forwards `serial`, `device`, `install`, and
`dry_run` from CLI flags into `WebcamStartRequest` — `facing`, `camera_id`, `extra_arguments`,
and `state_path` are real request fields with no corresponding CLI flags today, reachable only
by constructing `WebcamStartRequest` directly.

- Error codes: `invalid_request` (both `facing` and `camera_id` set, or negative `device`),
  `transport_unavailable`, `dependency_missing` (no v4l2loopback sink device found/resolvable,
  scrcpy missing, or `ScrcpyCompatibility.webcam_supported` is false), `already_running` (state
  file names a PID that is still alive), `operation_failed` (state file read/write failure, or
  process launch `OSError`/`BridgeError`).
- Progress events: `("webcam_start", "started")`, `("webcam_start", "command")` (dry run only),
  `("webcam_start", "completed")`.

#### Stop (`stop_webcam`)

One line: read the persisted state, `SIGTERM` the tracked PID if it looks alive, and clear the
state file.

```python
@dataclass(frozen=True)
class WebcamStopRequest:
    state_path: str | None = None
    dry_run: bool = False

@dataclass(frozen=True)
class WebcamStopResult:
    pid: int
    video_device: str
    serial: str | None
    dry_run: bool
    # .to_dict()
```

```python
def stop_webcam(
    request: WebcamStopRequest,
    *,
    state_loader: Callable[[str | None], dict[str, Any] | None] | None = None,
    state_clearer: Callable[[str | None], None] | None = None,
    process_terminator: Callable[[int], None] | None = None,
    process_alive: Callable[[int], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[WebcamStopResult]
```

- Error codes: `not_found` (no state file / nothing tracked), `operation_failed` (state file
  unreadable or corrupt — in the corrupt case the state file is cleared as a side effect before
  returning the error; termination `OSError`; state-clear failure).
- Progress events: `("webcam_stop", "started")`, `("webcam_stop", "completed")`.

### `notification.py` — post a notification to the phone

One line: post a notification via `cmd notification post` over ADB, or `termux-notification`
over SSH.

```python
@dataclass(frozen=True)
class NotificationRequest:
    message: str
    title: str = "Linkplane"
    notification_id: int = 8765
    transport: str = "auto"            # "auto" | "adb" | "ssh"
    serial: str | None = None
    dry_run: bool = False
    serial_from_profile: bool = False

@dataclass(frozen=True)
class NotificationResult:
    transport: str                     # "adb" | "ssh"
    device: str                        # ADB model name, or "user@host" for SSH
    serial: str | None                 # None for SSH
    title: str
    notification_id: int
    message: str
    command: tuple[str, ...]
    dry_run: bool
    # .to_dict()
```

```python
def notify_phone(
    request: NotificationRequest,
    *,
    ssh_factory: Callable[[], SshTransport] | None = None,
    adb_factory: Callable[[str | None], AdbTransport] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[NotificationResult]
```

Note `ssh_factory` here is a zero-argument *factory* (unlike `camera.py`/`clipboard.py`, which
take an already-constructed `SshTransport` instance) — the caller supplies a callable that
builds one on demand, only invoked if the SSH path is actually taken.

- Error codes: `invalid_request` (unsupported `transport`, or `serial` combined with
  `transport="ssh"`), `transport_unavailable` (ADB unselectable when ADB was required or an
  explicit non-profile `serial` was given; message concatenates both failures when
  `transport="auto"` fell back to SSH and that also failed), `operation_failed` (SSH
  notification command itself failed, or the ADB shell command failed).
- Progress events: `("notify", "started")`, `("notify", "command")` (dry run only), `("notify",
  "completed")`.

### `find.py` — locate the phone

One line: ring the phone at maximum volume (restoring the previous level afterward), pulse
vibration, flash the camera torch, and post a "Locate this phone" notification, any subset of
which can be disabled.

```python
RING_STREAM = 2   # AudioManager.STREAM_RING

@dataclass(frozen=True)
class FindPhoneRequest:
    serial: str | None = None
    duration: float = 5.0
    ring: bool = True
    torch: bool = True
    vibrate: bool = True
    message: str = "Locate this phone"
    install: bool = False
    dry_run: bool = False

@dataclass(frozen=True)
class FindPhoneResult:
    device: str
    serial: str
    duration: float
    ring: bool
    torch: bool
    vibrate: bool
    notification_command: tuple[str, ...]
    ring_get_command: tuple[str, ...] | None
    ring_set_command: tuple[str, ...] | None
    torch_command: tuple[str, ...] | None
    vibrate_command: tuple[str, ...] | None
    dry_run: bool
    ring_applied: bool = False
    ring_restored: bool = False
    note: str | None = None            # e.g. "unable to read ring volume; skipping ring: ..."
    # .to_dict()
```

```python
def locate_phone(
    request: FindPhoneRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
    command_runner: Callable[..., str] = run_command,
    process_runner: Callable[..., Any] = subprocess.run,
    dependency_factory: Callable[[], DependencyPlan] = scrcpy_dependency_plan,
    progress: ProgressCallback | None = None,
) -> OperationResult[FindPhoneResult]
```

Ring volume is read before being raised and restored in a `finally` block even if a later step
(the torch flash) fails — "never leave the ringer raised" per the module's own comment.
`dependency_factory`/scrcpy is only consulted (and can only fail with `dependency_missing`)
when `torch=True`; ring and vibrate never need scrcpy.

Real dry-run output (`linkplane find --dry-run --json`, run against the connected device):

```json
{
  "schema_version": 1,
  "ok": true,
  "data": {
    "device": "SM S901U",
    "serial": "DEVICE_SERIAL",
    "duration": 5.0,
    "ring": true,
    "torch": true,
    "vibrate": true,
    "notification_command": ["adb", "-s", "DEVICE_SERIAL", "shell", "..."],
    "ring_get_command": ["adb", "-s", "DEVICE_SERIAL", "shell", "cmd", "media_session", "volume", "--stream", "2", "--get"],
    "ring_set_command": ["adb", "-s", "DEVICE_SERIAL", "shell", "cmd", "media_session", "volume", "--stream", "2", "--set", "15", "--show"],
    "torch_command": ["scrcpy", "--serial", "DEVICE_SERIAL", "--video-source=camera", "--camera-torch", "--no-playback", "--time-limit=5"],
    "vibrate_command": ["adb", "-s", "DEVICE_SERIAL", "shell", "cmd", "vibrator_manager", "synced", "-f", "-B", "-d", "linkplane-find", "oneshot", "5000"],
    "dry_run": true,
    "ring_applied": false,
    "ring_restored": false,
    "note": null
  }
}
```

- Error codes: `invalid_request` (`duration <= 0`, or all of `ring`/`torch`/`vibrate` disabled),
  `transport_unavailable`, `dependency_missing` (scrcpy required for `torch` and unavailable),
  `operation_failed` (notification/ring/vibrate/torch command failure).
- Progress events: `("find", "started")`, `("find", "notified")`, `("find", "ring")`, `("find",
  "vibrate")`, `("find", "torch")`, `("find", "completed")`.

### `devices.py` — device discovery

One line: probe ADB and Termux/SSH endpoints, capability-probe each connected one, and group
endpoints into logical `Device`s by shared `device_id`.

```python
@dataclass(frozen=True)
class DiscoveryRequest:
    ssh: SshTransport
    ssh_device_id: str | None = None
    additional_ssh: tuple[tuple[SshTransport, str], ...] = ()
    adb_identities: tuple[tuple[str, str], ...] = ()
    allowed_adb_serials: frozenset[str] | None = None
    issues: tuple[DiscoveryIssue, ...] = ()
```

```python
def scan_devices(request: DiscoveryRequest) -> OperationResult[DiscoveryResult]
```

`DiscoveryResult` (defined in `models.py`, documented above) is the result type: `devices:
tuple[Device, ...]`, `issues: tuple[DiscoveryIssue, ...]`. A per-backend discovery failure
(e.g. `adb devices` itself erroring) is folded into `issues` rather than failing the whole
operation — `scan_devices` only returns an `OperationResult.failure` for genuinely unexpected
parsing errors, not for "adb isn't reachable right now."

There is also a convenience wrapper, `discover_devices(ssh, ssh_device_id=None, *,
additional_ssh=(), adb_identities=None, allowed_adb_serials=None) -> DiscoveryResult`, used by
the CLI. It builds a `DiscoveryRequest` and calls `scan_devices`, but **raises `BridgeError`
directly instead of returning an `OperationResult`** — it is not itself typed the same way as
the rest of this document's service functions. `scan_devices` is the typed core; prefer it for
a direct integration.

- Error codes (from `scan_devices`): `operation_failed` only — for an unexpected
  `KeyError`/`TypeError`/`ValueError` while parsing `adb devices` output, or an unexpected
  `BridgeError`/`KeyError`/`TypeError`/`ValueError` while discovering SSH endpoints or grouping
  endpoints into devices. Ordinary discovery problems (no adb, an individual ADB probe or SSH
  endpoint being unreachable) surface as a `DiscoveryIssue` entry or an `Endpoint.error`/`state`
  inside a *successful* result, not as an `OperationResult.failure` — a normal "adb isn't
  installed" or "phone is offline" does not fail the whole call.
- Progress events: none.

Real output shape (`linkplane devices --json`; device model/manufacturer/serial are this
machine's actual paired phone):

```json
{
  "schema_version": 1,
  "ok": true,
  "data": {
    "devices": [
      {
        "id": "DEVICE_SERIAL",
        "name": "samsung SM-S901U",
        "manufacturer": "samsung",
        "model": "SM-S901U",
        "android": "16",
        "capabilities": [
          {"name": "backup", "status": "ready", "detail": "/sdcard/DCIM is readable"},
          {"name": "camera", "status": "ready", "detail": "scrcpy camera source"},
          {"name": "notify", "status": "ready", "detail": "Android shell API"},
          {"name": "screen", "status": "ready", "detail": "scrcpy screen options available"},
          {"name": "send", "status": "ready", "detail": "/sdcard/Download is writable"},
          {"name": "status", "status": "ready", "detail": "ADB telemetry available"}
        ],
        "endpoints": ["... one Endpoint per transport, same shape as models.py above ..."]
      }
    ],
    "issues": []
  }
}
```

Note `cli.py` sets the envelope's `"ok"` to `not discovery.issues` for the `devices` command
specifically — i.e. `ok` can be `false` even though the call itself succeeded, if any backend
reported a `DiscoveryIssue`. This is a real, if slightly unusual, use of the envelope's `ok`
field to mean "no partial-discovery issues," not strictly "the operation itself succeeded."

### `doctor.py` — readiness diagnostics

One line: run a fixed battery of environment/dependency/capability checks and return them as a
flat list, reusing an already-run `DiscoveryResult`.

```python
@dataclass(frozen=True)
class DoctorRequest:
    discovery: DiscoveryResult

@dataclass(frozen=True)
class DoctorResult:
    checks: tuple[Check, ...]
    # .to_dict() -> {"checks": [check.to_dict() for check in checks]}
```

```python
def run_diagnostics(request: DoctorRequest) -> OperationResult[DoctorResult]
```

`Check` (from `models.py`, documented above) has `status` in `{"ok", "warning", "error"}` in
current use. The fixed check set, in emission order: `Python`, `ADB`, `SSH client`, `SSH
configuration` (only if a discovery `ssh` issue exists), `Device`, `Screen`, `LocalSend`,
`Desktop clipboard`, `Termux SSH` (only if at least one SSH endpoint was discovered),
`Capabilities`. `cli.py` sets the envelope's `ok` to `not any(check.status == "error" for check
in checks)` — same "computed from payload contents" pattern as `devices`.

- Error codes: `operation_failed` only, for an unexpected exception while building checks.
- Progress events: none.

### `clipboard.py` — phone/desktop clipboard bridge

One line: get/set the Termux clipboard over SSH, optionally through the desktop clipboard
(Wayland `wl-copy`/`wl-paste` or X11 `xclip`, auto-selected by session type), or run a
continuous bidirectional sync loop.

```python
@dataclass(frozen=True)
class ClipboardRequest:
    action: str                        # "get" | "set" | "pull" | "push" | "sync"
    text: str | None = None            # required for "set", forbidden otherwise
    interval: float = 1.0              # "sync" only; must be > 0
    prefer: str = "desktop"            # "desktop" | "phone" — sync conflict resolution
    foreground: bool = True
    dry_run: bool = False

@dataclass(frozen=True)
class ClipboardResult:
    action: str
    transport: str                     # always "ssh"
    endpoint: str                      # "user@host"
    desktop_backend: str | None        # "Wayland" | "X11" | None (only set for pull/push/sync)
    foreground_serial: str | None
    commands: tuple[tuple[str, ...], ...]
    text: str | None                   # only populated for "get"
    bytes_transferred: int | None
    updates: int                       # only meaningful for "sync"
    dry_run: bool
    # .to_dict()
```

```python
def use_clipboard(
    request: ClipboardRequest,
    transport: SshTransport,
    adb_serial: str | None = None,
    *,
    foreground_runner: Callable[[str], None] = foreground_termux,
    backend_factory: Callable[[], DesktopClipboard | None] = desktop_clipboard,
    progress: ProgressCallback | None = None,
) -> OperationResult[ClipboardResult]
```

Like `capture_camera_photo`, `transport` is a required positional `SshTransport` instance, not
an injectable factory. `DesktopClipboard` (a small internal dataclass:
`name`/`read_command`/`write_command`) never appears in `ClipboardResult` itself — only its
`.name` string does, via `desktop_backend`.

**`action="sync"` blocks until stopped.** `use_clipboard` calls `sync_clipboards`, which loops
polling both clipboards every `interval` seconds until either it catches `KeyboardInterrupt`
or the `cancel` token passed to `use_clipboard(..., cancel=token)` is set; either way it
returns the accumulated `updates` count as a **success**. This is the one documented exception
to the "cancel returns a `cancelled` error" rule (see "Cancellation" above): stopping is how a
sync normally ends. An in-process API/MCP adapter should run `sync` on a worker thread and call
`token.cancel()` to stop it; the token's `wait` replaces `time.sleep`, so the stop takes effect
immediately rather than after the next interval.

- Error codes: `invalid_request` (unsupported `action`; `text` missing for `set` or present for
  a non-`set` action; `interval <= 0`; unsupported `prefer`; `foreground=True` with no
  `adb_serial`), `transport_unavailable` (building the remote command failed),
  `dependency_missing` (no desktop clipboard backend available, or `backend_factory` itself
  raised), `operation_failed` (foreground bring-up failed, or the actual
  get/set/pull/push/sync `BridgeError`).
- Progress events: `("clipboard", "started")`, `("clipboard", "watching")` (`sync` only, once
  polling begins), `("clipboard", "updated")` (`sync` only, once per synchronized change),
  `("clipboard", "completed")`.

### `profiles.py` — pairing and named device profiles

Seven operations share this module: three pairing methods, plus profile list, remove, and
default selection, and wireless endpoint refresh. All read/write the JSON config file at `resolve_config_path(path)`
(from `transports.py`), using `update_config` for a `flock`-guarded atomic read-modify-write
where anything is saved.

```python
@dataclass(frozen=True)
class DeviceProfile:
    name: str
    device_id: str
    adb_serials: tuple[str, ...]
    preferred_adb_serial: str | None
    ssh: dict[str, Any] | None
    # .adb_serial property -> preferred_adb_serial or the first of adb_serials, or None
```

`DeviceProfile` is not itself a `Result` field for the pairing operations (those return
`PairingResult`), but it is what `ProfileListResult` is built from.

#### USB pairing (`pair_usb_device`)

```python
@dataclass(frozen=True)
class UsbPairingRequest:
    name: str
    serial: str | None = None
    make_default: bool = False
    dry_run: bool = False
    config_path: str | None = None

def pair_usb_device(
    request: UsbPairingRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[PairingResult]
```

- Error codes: `invalid_request` (bad profile name, or the profile name is already bound to a
  different `device_id`), `dependency_missing` (ADB not installed), `transport_unavailable` (no
  ADB device selectable, or no serial resolvable), `operation_failed` (config save failure).
- Progress events: `("pair_usb", "started")`, `("pair_usb", "completed")`.

#### Wireless ADB pairing (`pair_wireless_device`)

```python
@dataclass(frozen=True)
class WirelessPairingRequest:
    name: str
    pair_address: str                  # "HOST:PORT"
    connect_address: str               # "HOST:PORT"
    make_default: bool = False
    dry_run: bool = False
    config_path: str | None = None

def pair_wireless_device(
    request: WirelessPairingRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
    command_runner: Callable[..., str] = run_command,
    code_reader: Callable[[], str] = pairing_code,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[PairingResult]
```

`code_reader` defaults to `pairing_code`, which reads the six-digit wireless pairing code
interactively via `getpass` and raises `BridgeError` immediately if stdin isn't a TTY. A
non-interactive caller (an API/MCP/GUI adapter) **must** supply its own `code_reader` that
returns the code some other way — the interactive default is unusable outside a terminal. Per
`docs/architecture.md`'s security notes, the code is read over stdin (never a process argument)
and redacted from progress messages and error text before either is surfaced.

- Error codes: `invalid_request` (bad profile name, malformed `pair_address`/`connect_address`,
  device-identity conflict, non-interactive `code_reader` failure, or a code that isn't exactly
  six digits), `dependency_missing` (ADB not installed), `operation_failed` (the actual
  `adb pair`/`adb connect`/`select_device`/save sequence failed — message has the code
  redacted).
- Progress events: `("pair_wireless", "started")`, `("pair_wireless", "paired")`,
  `("pair_wireless", "connected")`, `("pair_wireless", "completed")`.

#### Termux SSH pairing (`pair_ssh_device`)

```python
@dataclass(frozen=True)
class SshPairingRequest:
    name: str
    host: str
    user: str
    port: int = 8022
    identity_file: str | None = None
    serial: str | None = None          # mutually exclusive with device_id
    device_id: str | None = None
    make_default: bool = False
    dry_run: bool = False
    config_path: str | None = None

def pair_ssh_device(
    request: SshPairingRequest,
    *,
    adb_factory: Callable[[str | None], AdbTransport] = AdbTransport,
    ssh_factory: Callable[[str, str, int, str | None], SshTransport] = SshTransport,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
) -> OperationResult[PairingResult]
```

If `device_id` is not given, this associates the profile with the currently-selectable ADB
device's serial (requiring ADB); passing `device_id` explicitly skips ADB entirely — this is
the path used when Termux can't read the hardware serial, per `docs/architecture.md`'s "Device
identity" section.

- Error codes: `invalid_request` (bad profile name, missing host/user, bad port, both
  `serial`+`device_id` set, empty `device_id`, or `paired_device_id`/`ssh_factory`/`command`
  construction failure), `dependency_missing` (ADB not installed, only when `device_id` was not
  given), `transport_unavailable` (ADB device unselectable, or no device id resolvable at all),
  `operation_failed` (the SSH readiness probe returned an unexpected response, or the save
  failed).
- Progress events: `("pair_ssh", "started")`, `("pair_ssh", "completed")`.

#### Shared pairing result

```python
@dataclass(frozen=True)
class PairingResult:
    method: str                        # "usb" | "wireless" | "ssh"
    profile: str
    device_id: str
    device: str | None                 # None until the device is actually reached (wireless)
    adb_serial: str | None
    ssh_endpoint: str | None
    config_path: str
    commands: tuple[tuple[str, ...], ...]
    pairing_output: str | None = None  # wireless only
    connection_output: str | None = None  # wireless only
    dry_run: bool = False
    # .to_dict()
```

None of `pair usb`/`pair wireless`/`pair ssh` expose `--json` in the CLI today, so this
`PairingResult` is currently reachable only by calling the service functions directly.

#### List profiles (`read_profiles`)

```python
@dataclass(frozen=True)
class ProfileListRequest:
    config_path: str | None = None

@dataclass(frozen=True)
class ProfileListResult:
    default_device: str | None
    profiles: tuple[DeviceProfile, ...]
    # .to_dict() -> {"default_device": ..., "devices": {name: {device_id, adb_serial,
    #                adb_serials, ssh}}}  -- NOT a plain asdict(); see below

def read_profiles(request: ProfileListRequest) -> OperationResult[ProfileListResult]
```

`ProfileListResult.to_dict()` is a hand-built transform, not `dataclasses.asdict(self)` — it
reshapes the list of profiles into a `devices` dict keyed by profile name, with each profile
represented as `{"device_id", "adb_serial", "adb_serials", "ssh"}` (`adb_serial` is the
`DeviceProfile.adb_serial` *property* value, derived, not a stored field). This is the shape a
consumer calling `--json` actually sees; a consumer calling `read_profiles` directly and
inspecting `.value.profiles` gets `DeviceProfile` instances instead.

Real output from this machine (`linkplane profiles list --json`, no profiles configured
here):

```json
{
  "schema_version": 1,
  "ok": true,
  "data": {
    "default_device": null,
    "devices": {}
  }
}
```

- Error codes: `operation_failed` only (config load/parse failure, or `default_device` present
  but not a string).
- Progress events: none.

#### Remove profile (`remove_device_profile`)

```python
@dataclass(frozen=True)
class ProfileChangeRequest:
    name: str
    config_path: str | None = None

@dataclass(frozen=True)
class ProfileChangeResult:
    action: str                        # "remove" | "default"
    profile: str
    config_path: str
    # .to_dict()

def remove_device_profile(
    request: ProfileChangeRequest,
    *,
    progress: ProgressCallback | None = None,
) -> OperationResult[ProfileChangeResult]
```

Removing the profile that is currently `default_device` also clears `default_device` from the
config, as a side effect of the same atomic update.

- Error codes: `not_found` (no such profile), `operation_failed` (config save failure, or
  `devices` config isn't a JSON object). No `--json` CLI path exists for this command today.
- Progress events: `("profile_remove", "started")`, `("profile_remove", "completed")`.

#### Set default profile (`select_default_profile`)

```python
def select_default_profile(
    request: ProfileChangeRequest,
    *,
    progress: ProgressCallback | None = None,
) -> OperationResult[ProfileChangeResult]
```

Same `ProfileChangeRequest`/`ProfileChangeResult` as removal, with `action="default"`.

- Error codes: `invalid_request` (bad profile name), `not_found` (no such profile),
  `operation_failed` (config save failure). No `--json` CLI path exists for this command today.
- Progress events: `("profile_default", "started")`, `("profile_default", "completed")`.

#### Refresh endpoints (`refresh_profile_endpoints`)

One line: for one profile or all of them, ask a currently-reachable ADB link (typically USB)
for the device's live Wi-Fi address, and update a drifted SSH host or verify/save a corrected
wireless ADB alias — the "Dynamic refresh" roadmap item under Foundation.

```python
@dataclass(frozen=True)
class EndpointRefreshRequest:
    name: str | None = None            # None = all profiles
    config_path: str | None = None
    dry_run: bool = False

@dataclass(frozen=True)
class ProfileEndpointChange:
    field: str                         # "ssh_host" | "adb_serial"
    previous: str
    current: str
    # .to_dict()

@dataclass(frozen=True)
class ProfileRefreshOutcome:
    profile: str
    discovered_address: str | None
    changes: tuple[ProfileEndpointChange, ...]
    note: str | None = None            # e.g. "no reachable ADB endpoint to query" / "already up to date"
    # .to_dict()

@dataclass(frozen=True)
class EndpointRefreshResult:
    config_path: str
    dry_run: bool
    profiles: tuple[ProfileRefreshOutcome, ...]
    # .to_dict()

def refresh_profile_endpoints(
    request: EndpointRefreshRequest,
    *,
    command_runner: Callable[..., str] = run_command,
    adb_locator: Callable[[str], str | None] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancellationToken | None = None,
) -> OperationResult[EndpointRefreshResult]
```

A profile with no currently-reachable ADB alias, or whose reachable device has no active Wi-Fi
route, is skipped with a `note` explaining why — not treated as an error for the whole
operation, consistent with this being an opportunistic refresh (per `docs/architecture.md`:
"refreshes a profile only when at least one of its aliases is already reachable").

- Error codes: `invalid_request` (bad profile name), `not_found` (named profile doesn't exist),
  `operation_failed` (config load/save failure), `cancelled` (the `cancel` token was set;
  the config file is never written in that case).
- Progress events: `("profile_refresh", "started")`, `("profile_refresh", "scanning")` (with
  `scan=True`, once per profile that is about to be swept), `("profile_refresh",
  "scan_planned")` (dry run with `scan=True`), `("profile_refresh", "skipped")`,
  `("profile_refresh", "refreshed")`, `("profile_refresh", "unchanged")`, `("profile_refresh",
  "cancelled")` (`current`/`total` = profiles examined / targeted; `details["outcomes"]` = the
  outcomes gathered so far), `("profile_refresh", "completed")`. (`"refreshed"` vs.
  `"unchanged"` is chosen per-profile based on whether that profile actually had changes.)
- Cancellation: accepts `cancel=`, forwarded into `scan_for_profile`; see "Cancellation" above.

## All `OperationError.code` values

Every distinct code any service function can return, across the whole codebase (from grepping
every `OperationResult.failure("...", ...)` call site):

| Code | Meaning | Returned by |
|---|---|---|
| `invalid_request` | The request itself is malformed or self-contradictory (bad enum value, mutually exclusive fields both set, missing required combination) — fails before touching any device or host resource. | `status`, `screen`, `audio`, `transfer`, `backup`, `camera` (capture & preview), `notification`, `find`, `clipboard`, `profiles` (all three pairing methods, `refresh_profile_endpoints`, `select_default_profile`) |
| `transport_unavailable` | An ADB or SSH transport/device could not be reached or selected. | `status`, `screen`, `audio`, `transfer`, `backup`, `camera` (capture & preview), `webcam` (start), `notification`, `find`, `profiles` (`pair_usb_device`, `pair_ssh_device`) |
| `dependency_missing` | A required host executable/package/kernel module is missing or incompatible (scrcpy, v4l2loopback, LocalSend CLI, a desktop clipboard backend, ADB). | `screen`, `audio`, `transfer`, `camera` (preview), `webcam` (start), `find`, `clipboard`, `profiles` (all three pairing methods) |
| `not_found` | A named resource (webcam sink state, device profile) does not exist. | `webcam` (stop), `profiles` (`remove_device_profile`, `select_default_profile`, `refresh_profile_endpoints`) |
| `already_running` | An operation refuses to start a second instance of a singleton background process. | `webcam` (start) only |
| `operation_failed` | Everything else: an external command/process failed, I/O failed, or an unexpected condition was hit while the operation was otherwise valid and its dependencies were in place. | every module in this document |
| `cancelled` | The caller's `CancellationToken` was set and the operation stopped at its next boundary; `message` is the reason passed to `cancel()`. Not a fault. See "Cancellation". | `transfer` (ADB path), `backup`, `profiles` (`refresh_profile_endpoints`) |

As covered above, this `code` is only visible to a caller that inspects `OperationResult.error`
directly — the CLI's own `--json` error envelope currently collapses all of these down to a
generic `"type": "BridgeError"` (or occasionally `KeyError`/`TypeError`/`ValueError`, or
`OperationCancelled` for a cancelled run) before printing, discarding `code` entirely. A generic API/MCP error handler that wants to switch on
`code` needs to call the service function itself, not shell out to `linkplane ... --json` and
parse its error output.


## Core v0.1 additions (additive, contract version 1 unchanged)

Everything in this section was added on 2026-09-10 for `docs/core-v0.1-brief.md` without
changing any pinned field above. After a session of real-device use they are now **pinned**
in `tests/test_contracts.py` too: `CapabilityReport`, `PingResult`, `BatteryReading`,
`StatusResult` (now defined in `core/telemetry.py`, re-exported from `status.py`), the
additive `Check.code`, the capability catalogue and status vocabulary, the PB code set, and
the `Provider` interface. All additive; contract version stays 1.

`models.Check` gained `code: str | None = None` (the PB code of a failing `doctor` check;
`null` for passing/informational checks). `read_status` results carry
`operation="device.status"`, `provider`, and `resource_id`; its failures keep the frozen
`transport_unavailable` category and add `error_code`/`hints`.

### `OperationResult` / `OperationError` provenance fields

```python
@dataclass(frozen=True)
class OperationError:
    code: str                       # short category, as before
    message: str
    error_code: str | None = None   # stable LP-… identifier (linkplane.core.errors)
    hints: tuple[str, ...] = ()

@dataclass(frozen=True)
class OperationResult(Generic[T]):
    value: T | None = None
    error: OperationError | None = None
    operation: str | None = None    # capability name, e.g. "battery.read"
    resource_id: str | None = None  # logical device id / profile name
    provider: str | None = None     # "adb", "ssh", …
    warnings: tuple[str, ...] = ()
    # .success is an alias of .ok; .success_with(value, **provenance) sets the new fields
```

No existing service sets the provenance fields yet; `to_dict()` of a result is unchanged
because results are rendered via their `value`.

### `core/errors.py` — `LinkplaneError`

`LinkplaneError(code, message, hints=())` extends `BridgeError` (so every existing
`except BridgeError` still catches it) and adds `.code`, `.hints`, `.title`, and
`.to_dict() -> {"type", "message", "code", "hints"}`. Codes and titles are the module-level
constants in `linkplane.core.errors`; `classify(error, provider=None)` wraps a plain
`BridgeError`. Current codes:

| Code | Meaning |
|---|---|
| `LP-CONFIG-001` / `-002` / `-003` / `-004` | configuration missing / invalid / device profile not found / could not be written |
| `LP-CONNECT-001` / `-002` / `-003` | unreachable / no device connected / more than one device |
| `LP-AUTH-001` / `-002` / `-003` | authentication failed / device has not authorized this computer / the Linux host may not access the USB device (udev) |
| `LP-TIMEOUT-001` | the phone did not respond in time |
| `LP-PROVIDER-001` / `-002` / `-003` | command failed / malformed output / partial data |
| `LP-CAPABILITY-001` / `-002` | unsupported / unavailable |
| `LP-DEPENDENCY-001` | host program missing |
| `LP-REQUEST-001` | invalid request |
| `LP-CANCELLED-001` | cancelled |
| `LP-STATE-001` | already running |

### `core/capability.py` — `CapabilityReport`

```python
@dataclass(frozen=True)
class CapabilityReport:
    name: str                          # from CATALOGUE, e.g. "battery.read"
    status: str                        # supported | unsupported | unavailable | permission-denied | provider-error
    detail: str | None = None
    requirements: tuple[str, ...] = () # Android capability classes A–F/X, where known
    metadata: dict[str, Any] = {}
```

`CATALOGUE` is the ordered tuple of every capability name; `Provider.capabilities()`
returns one report per entry in that order.

### `providers/base.py` — `Provider`, `PingResult`, `BatteryReading`

```python
class Provider(ABC):
    name: ClassVar[str]                        # "ssh", "adb"
    address: str                               # property: serial or user@host:port
    def ping(self) -> PingResult               # never raises for an unreachable device
    def status(self) -> StatusResult
    def battery(self) -> BatteryReading
    def capabilities(self) -> tuple[CapabilityReport, ...]

@dataclass(frozen=True)
class PingResult:
    reachable: bool; provider: str; address: str
    latency_ms: float | None = None; detail: str | None = None

@dataclass(frozen=True)
class BatteryReading:
    level: int; status: str; health: str; powered_by: tuple[str, ...]; provider: str
    temperature_c: float | None = None; voltage_mv: int | None = None
```

`providers.select_provider(transport, *, serial, serial_from_profile, ssh_factory,
adb_factory)` is the CLI's provider resolution and raises `LinkplaneError`.

### New `--json` envelopes

- `linkplane battery --json`: `data` = `BatteryReading.to_dict()` plus `"address"`.
- `linkplane ping --json`: `data` = `PingResult.to_dict()`; `ok` mirrors `reachable`;
  exit status 1 when unreachable.
- `linkplane capabilities --json`: `data` = `{"provider", "address", "capabilities":
  [CapabilityReport.to_dict(), …]}`.


## Core v0.2 additions (slice 1, additive; not yet pinned)

- `core/events.py::Event(type, device, ts, provider, data, seq)` with
  `to_dict() -> {..., "schema": "linkplane.event/1"}` and `from_dict()`. `type` is one of
  `EVENT_TYPES` (closed, append-only catalogue). `linkplane events --json` emits one such
  object per line — **not** the `schema_version` envelope.
- `core/state.py::DeviceState(device, provider, connection, address, battery, wifi_ssid,
  last_seen, updated)` and `diff(previous, current, *, low_battery=20, fresh=False)`.
- `history.py`: `HistoryWriter`, `read_history`, `last_seq`; file
  `$XDG_STATE_HOME/linkplane/events.jsonl` (`LINKPLANE_HISTORY` overrides).
- `observe.py::Observer(sink, *, identities, interval, low_battery, cancel, tracker,
  battery_poller, wifi_poller, poll_wifi_enabled)`.

`Event` and `DeviceState` are pinned in `tests/test_contracts.py` (slice 2 used them).

## Core v0.2 additions (slice 2, additive)

- `daemon.py`: `Daemon(socket_path, state_path, history_path, identities, interval,
  low_battery, poll_wifi, observer_factory, write_history)`; client helpers `request(op,
  path, **fields)`, `subscribe(path, *, types, devices, cancel)`, `is_running(path)`,
  `connect(path)`; `resolve_socket_path()`, `resolve_state_path()`.
- Control protocol `linkplane.daemon/1` (newline-delimited JSON over a Unix socket, one
  request per connection): `status` → `{ok, protocol, pid, started, socket, state_file,
  interval, events_seen, subscribers, devices}`; `state` → `{ok, devices}`; `stop` →
  `{ok, stopping}`; `subscribe {types?, devices?}` → `{ok, subscribed, protocol}` then one
  `Event.to_dict()` per line. Errors: `{ok: false, code, message}`.
- Registry snapshot `linkplane.state/1`: `{schema, updated, daemon: {pid, started,
  socket} | null, devices: {name: DeviceState.to_dict()}}`.
- New codes: `LP-DAEMON-001` not running, `LP-DAEMON-002` already running,
  `LP-DAEMON-003` protocol.
- CLI: `daemon run|status|stop`; `events --follow --socket --type --device`.

## Core v0.2 additions (slice 3, additive)

- `service.py`: `install(unit_dir, extra_arguments, dry_run, runner)`,
  `uninstall(unit_dir, dry_run, runner)` → `ServiceResult(action, unit_path, unit_text,
  commands, dry_run)`; `unit_text(command)`, `daemon_command(extra)`, `resolve_unit_dir()`.
- CLI: `daemon install [--interval N] [--no-wifi] [--unit-dir D] [--dry-run] [--json]`,
  `daemon uninstall [--unit-dir D] [--dry-run] [--json]`; `daemon run` handles SIGTERM as a
  cooperative stop. `devices --observed [--json]`: `data` is the `linkplane.state/1`
  snapshot (`{schema, updated, daemon, devices}`), not the discovery shape.


## Automation additions (slice A, additive; not yet pinned)

- `core/automation.py`: `Automation(name, when, do, device, conditions, enabled, allow,
  cooldown_seconds, on_initial, continue_on_error)`, `Step(action, options)`,
  `parse_automation(record)`, `parse_automations(config)` (file schema
  `{"schema_version": 1, "automations": [...]}`), `matches(rule, event)`,
  `condition_holds(actual, expected)`, `fill(template, context)`, `Engine(rules,
  run_step, clock).handle(event) -> [Firing]`, `Firing(automation, event, outcomes)`,
  `StepOutcome(action, ok, detail, data, skipped)`. Errors: `LP-CONFIG-002` for an invalid
  rule or file.
- `actions.py`: `run_step(step, event, context, rule) -> StepOutcome`; `notify_desktop`,
  `notify_phone`, `run_shell` (refuses without `"run"` in the rule's `allow`).
- CLI: `watch <event> [--if COND]... [--device NAME] [--notify MSG]... [--urgency U]
  [--notify-phone MSG]... [--run CMD]... [--cooldown S] [--on-initial] [--interval N]
  [--low-battery N] [--no-wifi] [--socket P]`; `--run` implies `allow: ["run"]`.


## Automation additions (slice B, additive)

- `automations.py`: `load_rules(path) -> LoadedRules(path, active, blocked)` (missing file
  = no rules; blocked = rules using a privileged action without `allow`), `AuditWriter`,
  `read_audit(path, limit)`, `resolve_automations_path()`, `resolve_audit_path()`.
- Audit records (`linkplane.audit/1`, JSON Lines): `{schema, ts, kind, ...}` with kinds
  `daemon.started`, `daemon.stopped`, `rules.loaded`, `rules.blocked`, `rules.error`,
  `automation.fired`, `automation.skipped`, `automation.error`; fired/skipped carry the
  `Firing.to_dict()` fields (`automation`, `event`, `ok`, `outcomes`).
- Daemon protocol: `reload` → `{ok, path, loaded, active, blocked}`; `status` gains
  `automations: {path, loaded, active, blocked, fired}`. `Daemon(...)` gains
  `automations_path`, `audit_path`, `step_runner`.
- CLI: `daemon reload`, `daemon run --automations F --audit F`, `automations list [--file F]
  [--json]`, `automations log [--file F] [-n N] [--follow] [--json]`.


## Automation additions (slice C, additive)

- `jobs.py`: `JobRunner(records_dir, retry_delays=(10, 30, 90), sleep, write_records)`
  with `run(*, automation, action, device, call) -> JobRecord`, `cancel_device(device,
  reason)`, `cancel_all(reason)`, `running()`, `wait_idle(timeout)`; `read_jobs(dir, limit)`;
  `resolve_jobs_dir()` (`$XDG_STATE_HOME/linkplane/jobs`, `LINKPLANE_JOBS`).
  `JobRecord(id, automation, action, device, state, started, finished, attempt, progress,
  result, error)`; states `running | succeeded | failed | cancelled | skipped`. Only
  `transport_unavailable` failures are retried.
- `actions.py`: `run_step(..., jobs=None)`; `backup_job`, `send_job`, `clipboard_sync_job`
  build the existing `BackupRequest` / `SendRequest` / `ClipboardRequest` and run through
  the job runner; the job result's keys join the rule context for later steps.
- `core/automation.py`: `Engine.select(event)` and `Engine.fire(rule, event)` split out of
  `handle()` so the daemon can run firings on a pool.
- Daemon: `Daemon(..., jobs, max_firings=4)`; firings run on a pool; `device.disconnected`
  cancels that device's jobs (audited as `jobs.cancelled`); stop cancels all and waits up
  to 10 s; `status` gains `jobs: [{device, action}]`.
- CLI: `automations jobs [--dir D] [-n N] [--json]`, `watch --backup DIR`.


## Refinement pass additions (additive; contract version 2 unchanged)

- `Event` gains `event_id` (opaque, unique), `correlation_id` (defaults to the event's own
  id: the root of a causal chain), and `source` (`"observer"`). Pinned. Pre-refinement
  history lines load with ids assigned.
- `JobRecord.state` vocabulary is `running | retrying | completed | failed | cancelled |
  skipped` (`completed` replaces the pre-refinement `succeeded`; `retrying` is new);
  `JobRecord.correlation_id` added. `JobRunner(on_transition=...)` is called on every state
  change. See `docs/state-machines.md`.
- `Firing.to_dict()` gains `rule` (the preferred term; `automation` stays) and
  `correlation_id`. `core.automation.Rule` is an alias of `Automation`.
- Audit schema `linkplane.audit/2`: `{schema, audit_id, ts, kind, actor, source, decision,
  device?, rule?, job_id?, action?, correlation_id?, details}`; kinds `daemon.started|stopped`,
  `rules.loaded|error`, `rule.blocked|fired|skipped|error`, `job.started|retrying|completed|
  failed|cancelled|skipped`, `jobs.cancelled`. Readers accept v1 lines.

### Preferred terms for frozen fields

| Frozen field | Preferred term (API) | Note |
|---|---|---|
| `StatusResult.transport`, `Endpoint.transport`, `SendResult.transport`, `NotificationResult.transport`, `CaptureResult.transport`, `ClipboardResult.transport` | `provider` | value is a provider name (`adb`, `ssh`) |
| `Firing.automation`, `JobRecord.automation`, audit `rule` | `rule` | both keys are written |
| `Event.type` / `Event.device` / `Event.ts` / `Event.seq` | `event_type` / `device_id` / `timestamp` / `sequence` | the API may present the preferred names; the wire `Event` keeps the pinned ones |
| audit `kind` | `kind` | kept; `decision` carries the outcome |


## Local API — Slice 0 additions (additive; contract version 2 unchanged)

Prerequisites for the local HTTP API (`docs/local-api-design.md` §24, founder-approved
2026-09-11). No endpoint exists yet.

- `registry.py`: `Registry(config, states)` with `devices() -> [DeviceRecord]`,
  `resolve(id_or_alias) -> DeviceRecord | None`, `target(device) -> Target(serial, ssh)`,
  `identities()`; `profile_ssh_config(config, profile)` (moved verbatim from the CLI);
  `DeviceRecord.to_dict()` is the API's Device projection: `{device_id, name, observed,
  default, connection, provider, address, last_seen, providers[], state}` — never
  `identity_file`. **Canonical identity is `device_id`** (`DeviceProfile.device_id`); the
  profile name and `serial:<x>` are aliases. `cli.profile_identities` now delegates here.
- `clients.py` (`linkplane.clients/1`, `~/.config/linkplane/clients.json`, 0600,
  `LINKPLANE_CLIENTS`): `Client(client_id, client_name, client_type, scopes, token_sha256,
  created)`, `create_client` (token shown once, SHA-256 stored, atomic write),
  `revoke_client`, `load_clients`, `authenticate` (constant-time), `expand_scopes`
  (`read` shorthand; no wildcard; `shell.execute` reserved), `write_token_file`. Scope
  vocabulary `SCOPES` = `READ_SCOPES` + `CONTROL_SCOPES` + the capability catalogue.
- CLI: `clients create <id> [--name] [--type] [--scope S ...] [--token-file] [--file] [--json]`,
  `clients list`, `clients revoke <id>`.
- `api/actions.py`: `ActionSpec(name, execution, providers, request_type, parameters,
  required, fixed, job_action)`, `ACTIONS`, `JOB_ACTIONS` (declarative: `files.send→send`,
  `backup.photos→backup`, `clipboard.sync→clipboard-sync`), `spec_for`,
  `validate_parameters`, `build_request`; `SEAM_FIELDS` are never accepted from clients.
- `api/security.py`: `STATUS_FOR_CODE` / `status_for` (design §15), `check_bind_address`
  (loopback only), `valid_host_header`, `origin_allowed`, `check_request_headers`,
  `bearer_token` (header only; URL tokens refused), `correlation_id_from`, `error_body`
  (`{"error": {type, message, code, hints, correlation_id, details}}`), `RedactingFilter`.
- Catalogue: `device.find` and `clipboard.sync` appended (ADB reports `device.find`
  supported; SSH reports `clipboard.sync` supported when both Termux clipboard commands
  exist). `COMMANDS` maps them to `find` / `clipboard sync`.
- `OperationResult.to_dict() -> {value, error, operation, resource_id, provider, warnings}`.
- `JobRecord.actor: str | None` (`rule:<name>` / `client:<id>`); `JobRunner.run(...,
  actor=, on_started=)`; `JobRunner.cancel(job_id, reason) -> bool`. Ids of jobs without a
  rule use the actor label (`…-client-gui-backup-3`).
- `AuditWriter.write(..., source=)`; `job()` takes the actor from the record when set and
  writes `source: "api"` for `client:` actors; an empty rule is omitted.
- Daemon `status` gains `last_seq` (newest history sequence, `null` without history) and
  `api` (`null` until a listener exists).
- Codes: `LP-CLIENT-001` not authenticated, `LP-CLIENT-002` scope not granted,
  `LP-RESOURCE-001` no such resource, `LP-INTERNAL-001` internal, `LP-API-001` listener
  could not bind. Pinned in `tests/test_contracts.py`.


## Local API — Slice 1 (additive; contract version 2 unchanged)

The HTTP API itself is specified by `docs/local-api-design.md` and `docs/openapi.json`
(protocol `linkplane.api/1`, paths `/v1/…`); ADR 0012 records the decisions. In-process
additions:

- `api/server.py`: `ApiServer(bind, port, daemon, clients_path, config_path,
  allowed_origins, keepalive, adb_factory, runtime)` (a `ThreadingHTTPServer`; raises
  `LP-REQUEST-001` for a non-loopback bind and `LP-API-001` when the port is taken),
  `ApiHandler`, `ClientStore` (re-reads `clients.json` on mtime change), `job_dict()`
  (`JobRecord.to_dict()` + `rule` alias), `write_discovery()` (`api.json`).
- `api/execute.py`: `ActionRuntime(jobs, audit, adb_factory).execute(spec, parameters,
  ActionContext) -> Result dict | JobRecord`; `select(target, spec)` (declared providers
  in `auto` order, never `adb_factory(None)`), `check_capability`, `RUNNERS` (one thin
  call per action into the existing service), `capability_reports`,
  `effective_capabilities`, `audit_parameters` (no clipboard text; messages truncated).
  `ActionError` is `api.security.ApiError`: a `LinkplaneError` with `details`, an optional
  fixed HTTP `status`, and `not_implemented`.
- The immediate Result projection: `{action, device_id, device, provider, actor,
  correlation_id, started, finished, value, warnings}` where `value` is the service
  result's `to_dict()` (`OperationResult.to_dict()["value"]`).
- `Daemon(config_path, clients_path, api_enabled=False, api_bind="127.0.0.1",
  api_port=8741, api_origins=(), api_keepalive=15.0, adb_factory=None)`; properties
  `api`, `api_address`, `api_error`, `audit`, `history_enabled`, `jobs_dir`,
  `api_discovery_path`; methods `last_seq()`, `observed_states()`, `new_subscriber()`.
  `describe()["api"]` is `{url, protocol, streams}` or `null`. `daemon run` enables the
  API by default (`--api-bind`, `--api-port`, `--api-origin`, `--no-api`, `--clients`);
  `daemon install` accepts `--api-port` / `--no-api`; `daemon status` prints the URL.
- `JobRunner.running_id(device, action)`.
- Audit kinds written by the API: `action.requested` (`started`), `action.completed`,
  `action.failed`, `action.blocked`, `access.blocked`, `api.started`, `api.stopped`,
  `api.error`; all `source: "api"`, actor `client:<id>` (or `daemon` for lifecycle).


## Local API — Slice 2 (additive; contract version 2 unchanged)

- Routes: `POST /v1/jobs/{id}/cancel` (`jobs.cancel`), `GET /v1/rules` (`rules.read`),
  `POST /v1/rules/reload` (`rules.reload`, no body), `GET /v1/audit` (`audit.read`).
- `api/projections.py`: `DeviceIdResolver` (registry name → canonical `device_id`, null when
  unresolvable), `job_dict` (`action` = public capability name, `job_action` = the record's
  name, `rule`, `device_id`), `event_dict` (+ `device_id`), `audit_dict` (redacted, +
  `device_id`, public `action`/`job_action`), `rule_dict` / `rules_dict` (no path; `run`
  command text omitted).
- `api/actions.py`: `PUBLIC_ACTIONS` (`backup→backup.photos`, `send→files.send`,
  `clipboard-sync→clipboard.sync`), `public_action()`.
- `api/security.py`: `SENSITIVE_KEYS`, `redact_record()` (deep copy; never mutates).
- `api/openapi.py`: `build_spec()`, `routes_served()`, `python -m linkplane.api.openapi`
  writes `docs/openapi.json`; `tests/test_openapi.py` pins file == generator.
- `automations.py`: `LoadedRules.blocked_rules` (additive), `read_audit_reverse(path,
  chunk_size)` (newest first, bounded memory).
- Daemon: `audit_file` property. Audit kinds: `job.cancel` (`started` | `skipped` |
  `blocked`), `rules.reload` (`loaded` | `error`).
- Limits pinned: audit default 100 / max 1000; jobs 50 / 500; events 100 / 1000.


## Onboarding foundations — Slice 0 (additive; contract version 2 unchanged)

Design: `docs/install-onboarding-design.md`; founder decisions D1–D8 (2026-09-12).

- New codes: `LP-AUTH-003` the Linux host may not open the phone's USB device (adb's
  `no permissions` state; udev rules / group membership — fixed on the computer, not by
  tapping Allow), `LP-CONFIG-004` the configuration file or directory could not be
  created, locked, or written. Both carry hints; API status 503 and 500 respectively.
- `transports.parse_adb_devices`: the state is the full adb state phrase (`device`,
  `unauthorized`, `offline`, `no permissions`); adb's `* daemon …` chatter and the header
  are skipped wherever they appear; only identifier-keyed `key:value` tokens become
  details. `ADB_STATE_*` constants; `describe_blocked_adb_state(serial, state)`.
  `AdbTransport.select_device` diagnoses blocked devices by severity (no permissions,
  then unauthorized, then offline) and `core.errors.classify` maps each message to
  `LP-AUTH-003`, `LP-AUTH-002`, `LP-CONNECT-001` (with `OFFLINE_HINTS`).
- `AdbTransport(serial, runner=None)`: the runner is resolved at call time.
- Daemon socket `status` (and `/v1/health`) → adds `version` (the package version of the
  running process); `linkplane daemon status` prints it.
- Pairing identity: `paired_device_id(..., strict=True)` for USB pairing and for SSH
  pairing whose identity comes from a USB serial or `--device-id`; a same-named profile
  belonging to a different `device_id` is refused with `LP-REQUEST-001` and hints naming
  `next_free_profile_name(name, taken)` (`name`, `name-2`, …) and the remove command. Wireless
  pairing onto a profile that has a hardware serial verifies `getprop ro.serialno`
  after `adb connect` and disconnects on mismatch. `OperationResult` failures from the
  pairing and profile services carry `error_code`/`hints` when a `LinkplaneError` was the
  cause, and the `pair` CLI re-raises them with the code intact.
- Dependency catalogue (`dependencies.py`): `Dependency(name, purpose, where,
  required_for, summary, plan_factory)` with purposes `core-required`, `android-base`,
  `capability-optional`, `provider-optional`, `developer-only` (`PURPOSES`; the first two
  are `BLOCKING_PURPOSES`) and `where` `host` | `phone`; `DEPENDENCIES` /
  `dependency_catalogue()`, `dependencies_for(capability)`, `dependency_report(finder)` →
  `DependencyReport(statuses)` with `required_missing`, `optional_missing`, `base_ready`
  (true whenever nothing blocking is missing: optional tools never block the USB path).
  New plans: `notify_send_dependency_plan`, `v4l2_ctl_dependency_plan`,
  `systemd_dependency_plan`; package data `NOTIFY_SEND_PACKAGES`, `V4L2_UTILS_PACKAGES`,
  `ADB_USB_RULES_PACKAGES` (+ `USB_RULES_BUNDLED_WITH_ADB`); `usb_rules_install_hint(finder)`.
  `host_dependency_plans()` now also returns the v4l2-ctl, notify-send and systemctl plans.
  Nothing here executes a package manager; `install_dependency` remains the only thing that does.
- Doctor (additive checks): `USB access` (error `LP-AUTH-003` for `no permissions` with
  the udev hint; warning `LP-AUTH-002` for `unauthorized`; warning `LP-CONNECT-001` for
  `offline`; absent when no ADB endpoint is blocked), `Configuration permissions` (warning
  when the config file is group/other-readable), `Desktop notifications` (`notify-send`,
  warning with the distro install hint). Helpers `usb_access_checks(discovery)`,
  `configuration_permission_check(path)`. Existing check names and statuses unchanged.
- ADB provider: `clipboard.read`, `clipboard.write`, `clipboard.sync`, `camera.capture`
  stay `unsupported` but carry `providers.adb.TERMUX_API_DETAIL` (they run through the
  SSH provider and need Termux:API on the phone) instead of "not implemented".
- Call-time-resolved seams (`None` defaults): `service.install/uninstall(runner)`,
  `dependencies.scrcpy_compatibility(runner)`, `devices.discover_adb(runner)`,
  `profiles.pair_usb_device/pair_ssh_device(adb_factory)`,
  `profiles.pair_wireless_device(adb_factory, command_runner, code_reader)`.


## Onboarding — Slice 1, `linkplane setup` (additive; contract version 2 unchanged)

- `setup.run_setup(options, seams, progress, cancel)` → `SetupResult(ok, status, steps,
  timing, device, profile, daemon, api_client, next_steps, dry_run)`; `steps` are
  `SetupStep(phase, Check)` with phases `preflight, dependencies, device_detection,
  device_authorization, device_registration, daemon_installation, daemon_start,
  observation_verification, first_use_verification, api_client, complete`
  (`setup.PHASES`). A phase failure ends the run with `status: "stopped"` and an `error`
  step carrying the stable `LP-` code; `OperationCancelled` propagates from waits.
- `SetupOptions(config_path, name, serial, install_daemon, unit_dir, socket_path,
  daemon_arguments, api_client, clients_path, dry_run, interactive, device_wait,
  daemon_wait, observe_wait, poll_interval)`; `SetupSeams` holds every external touch
  point (finder, environ, adb listing, transport/provider factories, dependency installer,
  service installer, daemon command/status/restart, client store, clock/sleep, prompts).
- `--json` envelope `data`: `{ok, status, dry_run, steps[], device{serial, model},
  profile{name, device_id, created, default}, daemon{requested, available, unit_path,
  installed, running, version, restarted}, api_client{client_id, scopes, created, token} |
  null, timing{setup_started, first_use_succeeded, elapsed_seconds}, next_steps[]}`. The
  token appears once, only when a client was created in this run.
- Progress events: `ProgressEvent("setup", <phase>, message, details={"step": …} |
  {"guidance": [lines]})`.
- No new codes. Setup stops with `LP-DEPENDENCY-001`, `LP-CONNECT-002/003`,
  `LP-AUTH-002/003`, `LP-CONNECT-001` (offline), `LP-REQUEST-001` (name conflict),
  `LP-CONFIG-002/004`, `LP-DAEMON-001`, `LP-PROVIDER-*`, or the client-store codes.


## SSE shutdown ordering (2026-09-12, behaviour fix; contract version 2 unchanged)

- Pinned: daemon stop closes every event stream (HTTP SSE and the control-socket
  `subscribe`) **after** delivering every event produced before shutdown, including
  `observer.stopped`. Mechanism: the daemon offers each subscriber the `None` sentinel
  once the observer has unwound; stream loops end only on that sentinel or a departed
  client, never on the cancellation flag; `Daemon.add_subscriber` hands a subscriber that
  arrives after the sentinel its own sentinel at once (`Daemon.subscribers_closed`);
  `ApiServer.close()` waits up to `STREAM_DRAIN_TIMEOUT` (2 s, condition-driven) for open
  streams to end before shutting the listener (`ApiServer.wait_streams_drained`).
- Test seam: `ApiServer.stream_hooks["on_idle"]` (called when a stream's queue wait times
  out), beside the existing `after_subscribe` / `after_replay` hooks.


## Onboarding — Slice 2, `linkplane uninstall` (additive; contract version 2 unchanged)

- `uninstall.run_uninstall(options, seams, cancel)` → `UninstallResult(ok, steps,
  daemon_was_running, daemon_stopped, unmanaged_daemon, unit_path, unit_removed,
  runtime_removed, configuration_retained, purge_requested, purge_performed,
  purge_targets[PurgeTarget(path, exists, files, bytes, refused)], removed, retained,
  warnings, software_hint, dry_run)`; `--json` envelope `data` is its `to_dict()`.
- `UninstallOptions(purge, force, dry_run, interactive, unit_dir, socket_path, stop_wait,
  poll_interval)`; `UninstallSeams(finder, daemon_status, unit_main_pid, service_uninstall,
  confirm, clock, sleep)`.
- Purge invariant: targets are exactly `uninstall.owned_roots()` = `paths.config_dir()`,
  `paths.state_dir()`, `paths.runtime_dir()`; `refuse_reason(root)` rejects a root that is
  not named `linkplane`/`phonebridge`, is `/`, `$HOME` or above it, or is a symlink;
  `remove_tree` never follows a symlink. Nothing is ever read from configuration to pick a
  target. A daemon answering on the socket blocks purge (`LP-STATE-001`); non-interactive
  purge requires `--force` (`LP-REQUEST-001` otherwise); an unmanaged daemon (no unit, or
  MainPID ≠ the socket's pid) is reported and left alone.
