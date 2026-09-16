# Linkplane

**Make real devices programmable.**

Linkplane makes Android devices programmable from your computer.

It is an open, local-first control plane for real devices: a small background service on
your Linux machine that knows when your phone is connected, keeps track of its state, and
runs the actions you have approved (a verified photo backup, a notification, a file
delivery, screen mirroring, your own script) without you typing every command by hand. One
command, `linkplane`, and a local HTTP API give you and your own programs the same device
interface.

No account. No cloud. Your phone talks to your computer over a USB cable (wireless ADB is
available for advanced users), and everything Linkplane knows stays on that computer.

## Why Linkplane

Using a phone with a computer usually looks like this, every time:

```text
connect the phone → check it → copy something by hand → launch another tool
```

Linkplane is building toward this instead:

```text
phone connects → Linkplane knows → state and events are available
               → approved actions run: files move, notifications appear, tools launch
               → your scripts, bars and programs use one device interface
```

The foundation for that works today: the background service observes connections, battery
and Wi-Fi, and rules can back up new photos whenever the phone connects and tell you when
they are done. Setting that up still means editing a rules file; making it a one-step
choice is the next milestone ([`docs/v0.6-direction.md`](docs/v0.6-direction.md)).

**If you only want to copy an occasional file over USB, you probably do not need
Linkplane.** Your file manager, `adb push`, KDE Connect / GSConnect and LocalSend already
do that well. Linkplane is for the work you would otherwise repeat by hand, or want other
programs to do for you.

It is not trying to replace those tools. Linkplane uses ADB and scrcpy underneath and can
hand a transfer to LocalSend; it is not a cloud photo service, a full phone backup, or a
mass-market phone companion app (yet).

## Who it is for

Right now, in public alpha:

- Linux developers and power users who are comfortable in a terminal
- homelab and automation users who want the phone to be one more scriptable device
- privacy-focused, local-first users who want no account and no cloud
- Android tinkerers who want scripting and control over their own phone

Later, the same control plane is meant to serve everyday desktop users through automatic
backup, a tray menu, wireless connection and multi-device workflows. None of that is a
finished consumer experience today.

## How it fits together

```text
Linux computer (everything below runs here, as your user)
│
├── linkplane           CLI: one-shot commands and setup
├── linkplaned          background service (a systemd user service)
│    ├── state          what is connected, battery, Wi-Fi
│    ├── events         connect, disconnect, battery, Wi-Fi changes
│    ├── rules + jobs   "when this happens, do that", tracked and cancellable
│    ├── audit          what ran, when, and why
│    └── local API      http://127.0.0.1:8741, token-scoped, for your own tools
│
└── providers           ADB (primary) · Termux/SSH (optional) · scrcpy, LocalSend (tools)
         │
    USB cable or your own network
         │
Android phone           nothing Linkplane-specific installed; exposes capabilities via ADB
```

The application layer is managed from the Linux machine. The phone only needs USB
debugging. The background service exists because something has to notice the phone
arriving and react when no terminal is open; it is installed as a `systemd --user` service
so it starts at login and runs as you, without root. Every one-shot command also works
without it. There is no tray icon or window yet; see the roadmap.

## What Linkplane does

| | |
|---|---|
| **See** | `linkplane status`, battery, storage, memory, Wi-Fi; `linkplane devices` |
| **Move files** | `linkplane send` to the phone; `linkplane backup` pulls photos incrementally with SHA-256 verification |
| **Use the phone** | screen mirroring and control, camera preview, audio, a virtual webcam (all via scrcpy); notifications to the phone; ring, vibrate and flash to find it |
| **React** | `linkplane events` streams connect, disconnect, battery and Wi-Fi changes; `linkplane watch` and rule files run actions when they happen, such as a photo backup when the phone connects |
| **Build on it** | a background daemon with a local, token-scoped HTTP API and Server-Sent Events, so dashboards, bars and agents can use the same control plane |

## Quick start

```text
install Linkplane  →  linkplane setup  →  plug in the phone  →  tap "Allow"  →  your phone is ready
```

```sh
linkplane setup
```

Setup checks your computer, walks you through enabling USB debugging on the phone, waits for
it, registers it under a name you choose, installs and starts the Linkplane service, waits
until the phone is observed, and verifies ping, status and battery. It is safe to run again
at any time; it reuses everything that already works.

## Install

Linkplane is a Python 3.11+ application with no third-party Python dependencies. The
recommended way to install it is **pipx**, which gives it its own environment and puts the
`linkplane` command on your PATH.

Choose the section for your operating system and run only that one.

**Arch Linux / Omarchy**

```sh
sudo pacman -S --needed python-pipx android-tools android-udev
pipx install https://github.com/Omgwtfpancake/linkplane/releases/download/v0.5.1/linkplane-0.5.1-py3-none-any.whl
```

**Ubuntu 24.04**

```sh
sudo apt update
sudo apt install pipx adb
pipx install https://github.com/Omgwtfpancake/linkplane/releases/download/v0.5.1/linkplane-0.5.1-py3-none-any.whl
```

Then check `linkplane --version`. If the command is not found, run `pipx ensurepath` once
and open a new terminal.

Current version: **0.5.1, public alpha** (a GitHub pre-release; not on PyPI yet).
Details, upgrade and removal: [`docs/install.md`](docs/install.md).

## Connect your Android phone

You need an Android phone, a USB cable that carries data, and USB debugging:

1. Settings → About phone → tap **Build number** seven times (this turns on Developer options).
2. Settings → System → Developer options → turn on **USB debugging**.
3. Plug the phone into the computer and run `linkplane setup`.
4. When the phone asks *Allow USB debugging?*, tap **Allow** (tick *Always allow from this computer*).

That is all the phone needs. Basic Linkplane does **not** require Termux, Termux:API, an SSH
server, or any Linkplane app on the phone. Some advanced capabilities do; see
[`docs/install.md`](docs/install.md#optional-components).

## First commands

```sh
linkplane status                        # battery, storage, memory, Wi-Fi
linkplane notify "Hello from Linkplane" # a notification on the phone
linkplane send ~/Pictures/photo.jpg     # copy a file to the phone
linkplane backup ~/Phone/Photos --dry-run
linkplane events                        # watch the phone connect, charge, change networks
linkplane screen                        # mirror the screen (needs scrcpy: linkplane screen --install)
linkplane doctor                        # when something is off
```

The full command list is in [`docs/cli-reference.md`](docs/cli-reference.md).

## Features

- **Status and diagnostics**: `status`, `battery`, `ping`, `capabilities`, `devices`, `doctor`.
- **Files**: `send` over ADB (LocalSend CLI as a fallback); `backup` with manifests, resume and verification.
- **Screen, camera, audio, webcam**: presets over scrcpy; `webcam` creates a V4L2 device.
- **Notifications and find**: `notify` posts to the phone; `find` rings, vibrates and flashes the torch.
- **Clipboard**: one-shot and synced clipboard between phone and desktop (needs Termux:API on the phone).
- **Events and automations**: `events`, `watch`, and `automations.json` rules with consent-gated script actions, job tracking, and an audit log.
- **Daemon and API**: `linkplaned` keeps observing between commands; `linkplane clients create` mints a scoped token for the local HTTP API (`http://127.0.0.1:8741/v1`).
- **Profiles**: several phones by name, USB and wireless ADB aliases, an optional Termux/SSH endpoint.

## Privacy

Linkplane is local-first. It needs no account, sends no telemetry, and has no remote
service. The background service listens only on the loopback interface, and every API
client needs a token you create. Actions you invoke can, of course, move data where you
point them: `send` and `backup` copy files between your phone and your computer, the
optional SSH provider connects to your phone over your network, and `screen --install`
style helpers invoke your distribution's package manager only after you confirm. Linkplane
never uploads anything on its own.

## Platform support

| Status | Platform |
|---|---|
| **Supported** | Arch Linux, including Omarchy (verified with a real phone) |
| **Supported** | Ubuntu 24.04 LTS (full clean-machine lifecycle verified on a fresh VM with a real phone: public-source pipx install, USB onboarding, daemon persistence across a reboot, rerun, uninstall, purge, removal) |
| Expected to work | other current systemd-based Linux distributions with Python 3.11+ and `adb` |
| Not supported | distributions without `systemd --user` (one-shot commands still work; the daemon needs another supervisor), WSL, macOS, Windows |

"Supported" is earned by the clean-machine procedure in
[`docs/clean-machine-onboarding.md`](docs/clean-machine-onboarding.md); the evidence for each
platform is under [`docs/testing/`](docs/testing/).

## Documentation

- [`docs/install.md`](docs/install.md), install, upgrade, uninstall, purge
- [`docs/troubleshooting.md`](docs/troubleshooting.md), the common onboarding problems and their codes
- [`docs/cli-reference.md`](docs/cli-reference.md), every command
- [`docs/current-state.md`](docs/current-state.md), what exists, vocabulary, files on disk
- [`docs/v0.6-direction.md`](docs/v0.6-direction.md) and [`docs/alpha-feedback.md`](docs/alpha-feedback.md), where Linkplane is going next and why
- [`docs/architecture.md`](docs/architecture.md) and [`docs/adr/`](docs/adr/), how and why
- [`docs/local-api-design.md`](docs/local-api-design.md) and [`docs/openapi.json`](docs/openapi.json), the local API

## Development

```sh
git clone <this repository>
cd linkplane
make test              # unit + integration; no phone, no ADB server needed
make test-device       # the paired phone (never part of make test)
PYTHONPATH=src python -m linkplane status   # run the checkout directly
```

The test tiers are described in `tests/README.md`. Linkplane is a public alpha; issues and
contributions are welcome (`CONTRIBUTING.md`).

## License

Linkplane is released under the [MIT License](LICENSE).
