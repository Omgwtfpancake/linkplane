# Linkplane CLI reference

Every command, with the flags and output shapes as they are today. The first-run path is in
[`install.md`](install.md); problems are in [`troubleshooting.md`](troubleshooting.md);
the design of each area is under `docs/adr/`. Examples use placeholders: `DEVICE_SERIAL`
for a phone's ADB serial, `192.0.2.10` and `phone.local` for a phone's address.

Every command that prints data supports `--json` (envelope with `schema_version`, `ok`,
`data`); add `--debug` before any command for diagnostic logging on stderr. Failures carry a
stable `LP-<CATEGORY>-<NNN>` code (`docs/api-contracts.md`).

## Status command

With an Android device connected and USB debugging authorized:

```sh
linkplane status
linkplane status --json
linkplane status --transport adb
linkplane status --transport adb --serial DEVICE_SERIAL
```

The default `auto` transport prefers ADB and falls back to SSH when ADB is unavailable.
To force the Termux bridge:

```sh
linkplane status --transport ssh
```

Telemetry sections are collected independently. If one probe fails, human-readable output
marks that section unavailable and shows a warning while retaining the remaining values.
JSON output returns `null` for the failed section and includes component-level details in
the `issues` array.

## Local API

`linkplane daemon run` also serves a local HTTP API on `http://127.0.0.1:8741/v1`
(loopback only; `--api-port`, `--no-api`). Create a client and use its token:

```sh
linkplane clients create waybar --type tool --scope read --scope battery.read
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8741/v1/devices
curl -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
     -d '{"action": "battery.read"}' http://127.0.0.1:8741/v1/devices/<device_id>/actions
curl -N -H "Authorization: Bearer <token>" 'http://127.0.0.1:8741/v1/events/stream?after=0'
```

Routes: `/v1/health`, `/v1/devices[/{id}[/state|/capabilities|/actions]]`,
`/v1/jobs[/{id}[/cancel]]`, `/v1/events`, `/v1/events/stream` (Server-Sent Events,
resumable by `seq`), `/v1/rules[/reload]`, `/v1/audit`. Contract:
`docs/local-api-design.md`, `docs/openapi.json`; decisions: `docs/adr/0012-local-api.md`.

## Core commands: ping, battery, capabilities

Three small commands talk to the phone purely through the provider layer (see
`docs/adr/0002-provider-abstraction.md`) and share the same `--transport`, `--serial`,
SSH, and `--device` options as `status`:

```sh
linkplane ping                    # exit 0 if the phone answers, 1 if not
linkplane battery                 # level, charging state, health, temperature, voltage
linkplane capabilities            # what this phone can do right now, and through which provider
linkplane battery --json
linkplane capabilities --json
```

`capabilities` lists the full catalogue for every device (`device.ping`, `device.status`,
`battery.read`, `storage.read`, `files.send`, `backup.photos`, `notify.post`,
`clipboard.read`, `clipboard.write`, `screen.control`, `camera.capture`) with one of five
statuses: `supported`, `unsupported` (this provider never does it), `unavailable` (it could,
but something is missing right now), `permission-denied`, or `provider-error`.

Failures carry a stable code. Humans see a title, the detail, what to check, and the code;
`--json` consumers get the same code in the error envelope:

```text
Phone unreachable.

ssh: connect to host 192.0.2.229 port 8022: Connection refused

Check that:
  • the phone is on the same network
  • Termux and its SSH server (sshd) are running on the phone
  • the configured host, port, user, and key are correct

Error: LP-CONNECT-001
```

Add `--debug` before any command to log diagnostic detail (and the traceback of a handled
error) to stderr.

## Events

Stream what changes on the phone, as it happens:

```sh
linkplane events                      # human-readable, until Ctrl+C
linkplane events --json | jq .type    # one JSON object per line
linkplane events --interval 10 --low-battery 30 --no-wifi
```

```text
18:52:00  observer.started     *                  sources: adb.track-devices, adb.battery, adb.wifi; every 30s
18:52:00  device.connected     phone              DEVICE_SERIAL  (initial)
18:52:00  battery.changed      phone              100%  (initial)
18:52:03  device.disconnected  phone              DEVICE_SERIAL
18:52:04  device.connected     phone              DEVICE_SERIAL
```

Connection changes arrive instantly from `adb track-devices`; battery, charging, and Wi-Fi
are polled on `--interval` (default 30 s) for connected devices. Events are also appended
to `~/.local/state/linkplane/events.jsonl` (`--no-history` to skip). Event types:
`device.connected/disconnected/authorized/unauthorized`, `battery.changed/low/ok`,
`charging.started/stopped`, `wifi.connected/disconnected`, `observer.started/stopped`.
`(initial)` marks an observation rather than a change. See `docs/core-v0.2-design.md`.

## Watch: react to events

One rule from flags, in the foreground:

```sh
linkplane watch battery.low --notify "Phone battery {level}%" --urgency critical
linkplane watch device.connected --run ~/scripts/phone-home.sh
linkplane watch wifi.connected --if ssid=Home --notify-phone "Welcome home"
linkplane watch battery.changed --if "level<15" --if status!=charging --cooldown 600 --notify "Charge me"
```

`--if` conditions are over the event's data: `key=value`, `key!=value`, `key<n`, `key>n`,
`key in a,b`. Messages take `{level}`, `{ssid}`, `{device}`, and any other data field.
Rules fire on changes only; the opening observation is ignored unless `--on-initial`.
`--run` commands get `LINKPLANE_EVENT`, `LINKPLANE_DEVICE`, and `LINKPLANE_DATA`
(JSON) in their environment and are executed as argument lists, never through a shell
string with event text in it. Each firing prints one line per action with ✓ or ✗.
`watch` subscribes to a running daemon when there is one and observes in-process otherwise.

## Automations

Rules that keep working with no terminal open live in `~/.config/linkplane/automations.json`
and run inside the daemon:

```json
{
  "schema_version": 1,
  "automations": [
    {"name": "low-battery", "when": "battery.low",
     "do": [{"action": "notify-desktop", "message": "Phone battery {level}%", "urgency": "critical"}]},
    {"name": "welcome-home", "when": "wifi.connected", "if": {"ssid": "Home"}, "cooldown_seconds": 3600,
     "do": [{"action": "notify-phone", "message": "Welcome home"}]},
    {"name": "phone-home", "when": "device.connected", "device": "phone",
     "do": [{"action": "run", "command": "~/scripts/phone-home.sh"}], "allow": ["run"]}
  ]
}
```

```sh
linkplane automations list          # rules, and which are blocked pending "allow"
linkplane daemon reload             # re-read the file without restarting
linkplane automations log [--follow]   # what fired, when, and how each action went
```

`when` is an event type; `if` is a flat map over the event's data (`"ssid": "Home"`,
`"level": {"below": 15}`, `{"above": n}`, `{"in": [...]}`, `{"not": v}`); `do` runs in
order and stops at the first failure unless `"continue_on_error": true`. A rule that uses
`run` must list `"allow": ["run"]`, or it is loaded as *blocked* and shown as such. Rules
never fire on the opening observation unless `"on_initial": true`. Every firing is written
to `~/.local/state/linkplane/audit.jsonl`.

Long actions are **jobs**: `backup` (`source`, `destination`), `send` (`paths`,
`destination`), and `clipboard-sync` (`interval`, `prefer`). Each runs the same service the
CLI command uses, with a record under `~/.local/state/linkplane/jobs/`, progress, retries
on a lost connection, and cancellation when that device disconnects or the daemon stops.
Job states are `running`, `retrying`, and one of `completed`, `failed`, `cancelled`,
`skipped` (`docs/state-machines.md`). Every event carries an id and a correlation id, and
every rule firing, job record, and audit entry it causes carries the same correlation id.
The job's result feeds later steps, so `{downloaded}` works in a notification after a
backup. One job per device and action at a time; a repeat trigger is recorded as skipped.

```json
{"name": "backup-when-home", "when": "wifi.connected", "if": {"ssid": "Home"}, "cooldown_seconds": 3600,
 "do": [{"action": "backup", "destination": "~/Pictures/Linkplane"},
        {"action": "notify-desktop", "message": "{downloaded} new photos backed up"}]}
```

```sh
linkplane automations jobs          # recent jobs: state, progress, result or error
linkplane watch device.connected --backup ~/Pictures/Linkplane   # the same, from a flag
```

## Daemon

`linkplaned` keeps observing between commands, so several clients can subscribe at once
and the last known state is always on disk:

```sh
linkplane daemon run                      # foreground; Ctrl+C or `daemon stop` ends it
linkplane daemon status [--json]          # pid, socket, events seen, observed devices
linkplane events --follow [--json] [--type battery.low] [--device phone]
linkplane daemon stop
```

The control socket is `$XDG_RUNTIME_DIR/linkplane/daemon.sock` (mode 0600, this user
only); the registry snapshot is `~/.local/state/linkplane/state.json`, rewritten on every
event. `events --follow` falls back to observing in-process when no daemon is running.
The protocol is newline-delimited JSON (`docs/adr/0008-daemon-control-socket.md`).

To keep it running across logins, install it as a `systemd --user` service:

```sh
linkplane daemon install --dry-run   # show the unit and the systemctl calls first
linkplane daemon install             # writes ~/.config/systemd/user/linkplaned.service, enables and starts it
linkplane daemon uninstall
```

The unit runs `linkplane daemon run` with `Restart=on-failure`; `systemctl --user stop`
ends it cleanly (SIGTERM is handled like Ctrl+C). `linkplane devices --observed` answers
from the snapshot without probing anything.

## Screen control

Linkplane uses scrcpy for low-latency screen mirroring and control. If it is missing,
Linkplane offers to install the correct system package.

```sh
linkplane screen
linkplane screen --quality low
linkplane screen --quality high
linkplane screen --quality game
linkplane screen --record ~/Videos/phone.mkv
```

The presets choose sensible resolution, frame rate, bitrate, latency, and gamepad options.
Advanced scrcpy arguments can be appended after `--`:

```sh
linkplane screen --quality high -- --turn-screen-off
```

Inspect the generated command without opening a window:

```sh
linkplane screen --quality game --dry-run
```

## Audio control

For phone audio without video mirroring, use `linkplane audio`. It runs scrcpy in the
foreground with `--no-video`, so it stays active for as long as you want audio, exactly
like `linkplane screen`.

```sh
linkplane audio
linkplane audio --source mic
linkplane audio --source playback --record ~/Music/session.m4a
linkplane audio --codec flac --record ~/Music/session.flac
```

`--source` selects what scrcpy captures (`playback`, `mic`, `output`, and the other
values `scrcpy --help` documents). `--record` writes an audio-only recording; the format
is inferred from the file extension (`m4a`, `mka`, `opus`, `aac`, `flac`, or `wav`) unless
`--record-format` overrides it. A video-container extension such as `.mp4` or `.mkv` is
rejected instead of producing a broken recording.

```sh
linkplane audio --source mic --record ~/Music/note.wav --dry-run
```

## File transfer

Send files or complete directories directly through ADB. The destination defaults to the
phone's Download directory and is created automatically when necessary.

```sh
linkplane send ~/Downloads/movie.mp4
linkplane send photo.jpg document.pdf
linkplane send ~/Pictures/Trip --destination /sdcard/Pictures
linkplane send build.apk --dry-run
```

When no authorized ADB device is connected, the default `auto` mode opens the existing
LocalSend transfer flow. A transport can also be selected explicitly:

```sh
linkplane send document.pdf --transport adb
linkplane send document.pdf --transport localsend
```

## Photo backup

Incrementally back up the phone camera directory over ADB. The default destination is
`~/Pictures/Linkplane`, and the default phone source is `/sdcard/DCIM/Camera`.

```sh
linkplane backup
linkplane backup ~/Pictures/MyPhone
linkplane backup --source /sdcard/Pictures/Screenshots
linkplane backup --dry-run
linkplane backup ~/Pictures/MyPhone --serial DEVICE_SERIAL
```

Linkplane preserves relative directories and records file size, modification time, and
SHA-256 in `.linkplane-manifest.json`. Every downloaded file is written to a temporary
path and verified against the checksum reported by the phone before it replaces the local
copy. Later runs checksum locally unchanged files and download only new, changed, missing,
or damaged files. Files removed from the phone are never deleted from the backup.

## Camera

Capture a JPEG through the Termux:API camera command. By default, Linkplane briefly
brings Termux to the foreground through the explicitly associated ADB device, captures
camera ID 0, transfers validated image data over SSH, and removes the temporary phone
file.

```sh
linkplane camera capture
linkplane camera capture ~/Pictures/desk.jpg --camera-id 1
linkplane camera capture --dry-run
linkplane camera capture --no-foreground
```

Existing local files are protected unless `--force` is supplied. If no output is given,
the image is saved under `~/Pictures/Linkplane/` with a timestamped name.

On Android 12 or newer, preview the phone camera through scrcpy:

```sh
linkplane camera preview
linkplane camera preview --facing front --quality high
linkplane camera preview --quality motion --record ~/Videos/phone-camera.mp4
linkplane camera preview --camera-id 2 --torch
linkplane camera preview --dry-run
```

Preview presets control maximum dimensions, frame rate, and bitrate. Additional scrcpy
arguments can be appended after `--`. Camera preview uses microphone audio by default;
pass `--no-audio` to disable it.

## Webcam

Stream the phone camera into a Linux V4L2 device (`/dev/videoN`) that browsers and
video-call apps can open like any other webcam. This uses scrcpy's `--v4l2-sink` output
backed by the `v4l2loopback` kernel module, which must already be loaded (`sudo modprobe
v4l2loopback exclusive_caps=1 card_label="Linkplane"`); Linkplane does not load kernel
modules on its own.

```sh
linkplane webcam start
linkplane webcam start --device 4
linkplane webcam start --dry-run
linkplane webcam stop
```

With no `--device`, Linkplane asks `v4l2-ctl --list-devices` for the first existing
v4l2loopback sink. `start` launches scrcpy detached from the terminal and records its
process ID and sink device at `~/.config/linkplane/webcam.json`; `stop` reads that file,
signals the tracked process, and removes the file. A second `start` while one is already
running is rejected instead of launching a competing scrcpy process. `stop` does not
re-verify that the tracked PID is still the same scrcpy process (PID reuse is a known,
accepted risk); it relies on prompt use of `stop` after `start`.

## Phone notifications

Post a notification from Linux to the connected phone. ADB is preferred automatically,
with the Termux/SSH command as a fallback.

```sh
linkplane notify "Dinner's ready"
linkplane notify "Backup finished" --title "Linkplane Backup"
linkplane notify "Build complete" --id 42 --transport ssh
linkplane notify "Preview only" --dry-run
```

Reusing an ID replaces the prior Linkplane notification when the selected transport
supports stable notification IDs.

## Find phone

Locate a misplaced phone: raises the ring volume to maximum, vibrates, posts an
attention-grabbing notification, and briefly flashes the camera torch, then restores the
original ring volume automatically. ADB only.

```sh
linkplane find
linkplane find --duration 10
linkplane find --no-torch
linkplane find --no-vibrate
linkplane find --no-ring --message "Over here!"
linkplane find --dry-run
```

Vibration uses `adb shell cmd vibrator_manager synced -f -B -d ... oneshot <ms>`. The
`vibrator` shell service most guides reference does not exist on current Android (only
its AIDL binder interface does, with no `cmd`-level frontend); `vibrator_manager` is the
correct, currently-registered service name, confirmed by checking `service list` and by
reading the vibration back out of `dumpsys vibrator_manager`'s own log after triggering
it — not by guessing from documentation. `-f` forces the vibration past Do Not Disturb,
and `-B` runs it in the background so it overlaps with the torch flash instead of
doubling the total wait. Ring volume, vibration, and torch were each verified end to end
against a real device.

## Clipboard

Read or update the phone clipboard explicitly, or move text between the phone and Linux
desktop clipboards:

On Android, Termux may need to be open in the foreground for clipboard access.

```sh
linkplane clipboard get
linkplane clipboard set "Text for the phone"
linkplane clipboard pull
linkplane clipboard push
linkplane clipboard sync
linkplane clipboard sync --interval 0.5 --prefer phone
```

`pull` copies phone to desktop; `push` copies desktop to phone. Clipboard text is carried
over SSH stdin/stdout and is not placed in process arguments. Automatic continuous sync
is opt-in and runs only while the foreground `sync` command is active. It records both
initial values without overwriting either clipboard, then propagates subsequent changes.
If both sides change during one polling interval, the desktop value wins by default;
use `--prefer phone` to reverse that policy. Press Ctrl+C to stop synchronization.

## Pairing and device profiles

Create a named profile from an already authorized USB connection:

```sh
linkplane pair usb phone --default
```

For Android wireless debugging, choose **Pair device with pairing code** on the phone.
Pass its pairing address and the separate connection address shown by Wireless debugging;
Linkplane prompts for the six-digit code without placing it in process arguments:

```sh
linkplane pair wireless phone 192.0.2.229:37123 --connect 192.0.2.229:5555
```

Verify and associate an existing Termux SSH endpoint. With one authorized ADB device
connected, its serial is used as the proven identity automatically:

```sh
linkplane pair ssh phone --ssh-host phone.local --ssh-user u0_a123 \
  --ssh-key ~/.ssh/phone_bridge
```

Pairing writes named profiles atomically to `~/.config/linkplane/config.json` with
private file permissions. A profile can retain USB and wireless ADB aliases alongside its
SSH endpoint:

```json
{
  "default_device": "phone",
  "devices": {
    "phone": {
      "device_id": "DEVICE_SERIAL",
      "adb": {
        "serials": ["DEVICE_SERIAL", "phone.local:5555"],
        "preferred_serial": "phone.local:5555"
      },
      "ssh": {
        "host": "phone.local",
        "user": "u0_a123",
        "port": 8022,
        "identity_file": "~/.ssh/phone_bridge"
      }
    }
  }
}
```

Inspect and manage profiles, or target one from any device command:

```sh
linkplane profiles list
linkplane profiles default phone
linkplane status --device phone
linkplane backup --device phone
linkplane profiles remove old-phone
```

Stored wireless addresses drift when DHCP hands a phone a new lease. `profiles refresh`
uses any currently reachable ADB endpoint (typically USB) to ask the device for its
current Wi-Fi address, then updates a drifted SSH host or verifies and saves a corrected
wireless ADB alias. It never invents an address for a device it cannot already reach.

```sh
linkplane profiles refresh phone --dry-run
linkplane profiles refresh phone
linkplane profiles refresh
```

If *no* known alias for a profile is currently reachable at all (USB unplugged and the
last wireless address gone stale), add `--scan` to sweep the last-known wireless
subnet's other 254 addresses at the same port:

```sh
linkplane profiles refresh phone --scan --dry-run
linkplane profiles refresh phone --scan
```

This only runs for a profile that has both a recorded wireless alias (to anchor the
subnet and the port — the port is never guessed) and a hardware serial learned once over
USB. That hardware serial is the only thing a match is trusted against: a bare
successful `adb connect` proves this host's key was authorized by *some* device before,
not which one, since another already-paired phone on the same network would connect just
as silently. A profile paired only over SSH or only over wireless ADB (no USB serial on
record) cannot be scanned for; `--scan` reports that and does nothing further for it. Any
connection to a candidate that turns out not to match is disconnected immediately, so a
scan doesn't leave stray authorized sessions — or a pending authorization prompt on a
stranger's screen — behind. A sweep takes up to about 15 seconds; `--dry-run --scan`
reports that a scan would run without actually running it, since scanning connects to
other hosts and isn't read-only.

Explicit endpoint flags override an implicit default profile. When `--device` is supplied,
conflicting serial or SSH flags are rejected. Automatic ADB-to-SSH fallback is allowed
only between endpoints explicitly associated with the same profile.

The original single `ssh` object remains supported. SSH values can also be supplied with
`--ssh-host`, `--ssh-user`, `--ssh-port`, and `--ssh-key`, or with the corresponding
`LINKPLANE_SSH_*` environment variables. Set `LINKPLANE_CONFIG` to use a different
configuration file.

SSH status expects `~/phone-status-json.sh` on the Termux device. The original scripts
are retained under `legacy/`.

`device_id` explicitly associates endpoints with one logical phone. Linkplane never
guesses that two endpoints belong to the same physical device.

## Discovery and diagnostics

Inspect logical devices, their endpoints, and currently available capabilities:

```sh
linkplane devices
linkplane devices --json
```

Check host dependencies, device connections, SSH reachability, and aggregate capability
readiness:

```sh
linkplane doctor
linkplane doctor --json
```

Dependency remediation uses deterministic package plans for pacman, apt, dnf, and
Homebrew. Linkplane reports when a tool, such as LocalSend CLI, has no safe automatic
installation path for the detected package manager.
