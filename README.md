# Linkplane

**Make real devices programmable.**

Linkplane is an open, local-first control plane for real devices, starting with your Android
phone and your Linux desktop. One command, `linkplane`, and one small background service
give you the phone's battery and status, file transfer, verified photo backup, screen
mirroring, notifications in both directions, a "find my phone", a live stream of device
events your own scripts and tools can react to, and a local HTTP API for anything else you
want to build on top.

No account. No cloud. Your phone talks to your computer over a USB cable (or, later, your own
Wi-Fi), and everything Linkplane knows stays on that computer.

## What Linkplane does

| | |
|---|---|
| **See** | `linkplane status`, battery, storage, memory, Wi-Fi; `linkplane devices` |
| **Move files** | `linkplane send` to the phone; `linkplane backup` pulls photos incrementally with SHA-256 verification |
| **Use the phone** | screen mirroring and control, camera preview, audio, a virtual webcam (all via scrcpy); notifications to the phone; ring, vibrate and flash to find it |
| **React** | `linkplane events` streams connect, disconnect, battery and Wi-Fi changes; `linkplane watch` and rule files run actions when they happen |
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

**Arch Linux / Omarchy** (the currently supported platform):

```sh
sudo pacman -S --needed python-pipx android-tools android-udev
pipx install <Linkplane release artifact or source>   # see docs/install.md
pipx ensurepath                                        # once; then open a new terminal
```

Linkplane has not yet been published to PyPI or a public repository; until it is, install it
from the release wheel or source tree you were given, exactly as described in
[`docs/install.md`](docs/install.md). Current package version: **0.4.0, pre-public-release**.

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

The test tiers are described in `tests/README.md`. Contributions are welcome once the
repository is public; until then this is a pre-release.

## License

Linkplane is released under the [MIT License](LICENSE).
