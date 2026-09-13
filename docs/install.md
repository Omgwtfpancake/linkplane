# Installing Linkplane

Linkplane is a Python 3.11+ program with no third-party Python dependencies. It runs on a
Linux desktop with `systemd --user` and talks to Android phones through `adb`.

**Status:** version 0.5.1 (public alpha), published as a GitHub pre-release with a
wheel and an sdist. There is no PyPI package yet, so installs use the release wheel URL.
Nothing below requires root except installing system packages with your distribution's
package manager.

## Supported platforms

| Status | Platform | Meaning |
|---|---|---|
| **Supported** | Arch Linux, including Omarchy | verified with a real phone by the clean-machine procedure (`docs/testing/onboarding-run-001.md`) |
| **Supported** | Ubuntu 24.04 LTS | the full clean-machine lifecycle on a fresh VM with a real phone: public-source pipx install, USB Android onboarding, daemon install and start, device observation, first useful action, real reboot and persistence, setup rerun, uninstall, bounded purge, pipx removal (`docs/testing/onboarding-run-002.md`) |
| Expected to work | other current systemd-based distributions (Debian 12+, Fedora 40+, …) with Python ≥ 3.11 and an `adb` package | untested |
| Not supported | distributions without `systemd --user` (one-shot commands work; `linkplane daemon run` needs your own supervisor), WSL, macOS, Windows | |

## Prerequisites

| Need | Why | Arch / Omarchy | Debian / Ubuntu | Fedora |
|---|---|---|---|---|
| Python ≥ 3.11 | the runtime | `python` | `python3` | `python3` |
| pipx | installs Linkplane into its own environment and onto your PATH | `python-pipx` | `pipx` | `pipx` |
| adb | Android Debug Bridge, how Linkplane reaches the phone | `android-tools` | `adb` | `android-tools` |
| USB access rules | lets your user open the phone over USB | `android-udev` | included with `adb` | included with `android-tools` |
| systemd user session | runs the background service | part of the desktop login | | |

Choose the section for your operating system; do not run both sets of commands. Install
`adb` before plugging the phone in, so its udev rules apply to the device.

### Arch Linux / Omarchy

```sh
sudo pacman -S --needed python-pipx android-tools android-udev
```

### Ubuntu 24.04

Run this instead:

```sh
sudo apt update
sudo apt install pipx adb
```

`linkplane setup` detects a missing `adb`, shows the exact command for your package manager,
and runs it only if you say yes. It never installs anything without asking, and it never
needs root itself.

## Install

```sh
pipx install https://github.com/Omgwtfpancake/linkplane/releases/download/v0.5.1/linkplane-0.5.1-py3-none-any.whl
linkplane --version           # linkplane 0.5.1
```

If `linkplane` is not found after installation, run `pipx ensurepath` once and open a new
terminal; it adds `~/.local/bin` to your PATH. If the command is already found, there is
nothing to do.

`which linkplane` should print `~/.local/bin/linkplane`. The same result from the tagged
source, if you prefer (needs `git`): `pipx install "git+https://github.com/Omgwtfpancake/linkplane.git@v0.5.1"`.
Once Linkplane is on PyPI the artifact becomes the package name (`pipx install linkplane`).
Other isolation tools that produce a console script on PATH (for example `uv tool install`)
work the same way but are not the documented path.

Do not `pip install` into the system Python: modern distributions refuse it, and Linkplane's
service unit needs a stable executable path, which pipx provides.

## Set up the phone

```sh
linkplane setup
```

Phone side, once:

1. Settings → About phone → tap **Build number** seven times (enables Developer options).
2. Settings → System → Developer options → **USB debugging** on.
3. Connect the phone with a USB *data* cable.
4. Tap **Allow** when the phone asks about this computer (tick *Always allow*).

Setup then registers the phone (default name `phone`), installs and starts the
`linkplaned` user service, waits for the phone to be observed, and verifies ping, status and
battery. Useful flags: `--dry-run` (check everything, write nothing), `--name`, `--serial`
(when several phones are connected), `--no-daemon`, `--api-client ID` (also create a
read-only local API token, shown once), `--json`. Run it again whenever you like: existing
profiles, the service, history and automations are reused, and a service running an older
Linkplane version is restarted. If anything stops it, `linkplane setup` says what happened,
gives the stable error code and the next step; see [`troubleshooting.md`](troubleshooting.md).

Basic setup needs nothing installed on the phone. It does **not** need Termux, Termux:API,
an SSH server, or a Linkplane app.

## Optional components

Basic setup never fails because one of these is missing; the related capability is simply
reported as unavailable (`linkplane doctor` and `linkplane capabilities` show which). The
authoritative list is the dependency catalogue in `src/linkplane/dependencies.py`.

| Component | Where | Unlocks | Install |
|---|---|---|---|
| scrcpy | computer | `screen`, camera preview, `audio`, `webcam` | `linkplane screen --install`, or your package manager |
| v4l2loopback + v4l-utils (`v4l2-ctl`) | computer | `webcam` | package manager |
| libnotify (`notify-send`) | computer | desktop notifications from rules and `watch --notify` | package manager |
| wl-clipboard or xclip | computer | desktop side of clipboard sync | package manager |
| OpenSSH client | computer | the optional Termux/SSH provider | package manager |
| LocalSend CLI | computer | `send` fallback when ADB is unavailable | manual |
| Termux + openssh + `legacy/termux/phone-status-json.sh` | phone | the SSH provider (`linkplane pair ssh`) | Termux |
| Termux:API | phone | `clipboard` read/write/sync and `camera capture` (these run through the SSH provider) | Termux |

## Upgrade

```sh
pipx install --force https://github.com/Omgwtfpancake/linkplane/releases/download/vX.Y.Z/linkplane-X.Y.Z-py3-none-any.whl   # the newer release wheel
pipx upgrade linkplane                    # once installs come from PyPI
linkplane setup                           # verifies, and restarts the service if it runs an older version
```

The service unit points at `~/.local/bin/linkplane`, which pipx keeps stable across
upgrades, so the unit never needs rewriting; `linkplane setup` restarts the running daemon
when its version differs from the installed one.

## Uninstall

Three commands, three different scopes; none of them touches your backups or any file you
created:

| Command | Removes | Keeps |
|---|---|---|
| `linkplane uninstall` | the `linkplaned` user service (stopped, disabled, unit deleted) and its runtime socket and discovery file | configuration, profiles, API clients, history, audit, jobs, automations, the software |
| `linkplane uninstall --purge` | additionally `~/.config/linkplane`, `~/.local/state/linkplane` and `$XDG_RUNTIME_DIR/linkplane`, after listing them and asking you to type `yes` (`--force` skips the prompt for scripts) | backup destinations, transferred files, everything outside those three directories, the software |
| `pipx uninstall linkplane` | the software installation | everything else |

`linkplane uninstall` does not remove the Python package: whatever installed Linkplane
removes it. A daemon that is not the service's process (for example one you started with
`linkplane daemon run` in a terminal) is reported and left alone. Both commands are safe to
repeat; absence is not an error. `--dry-run` shows what would happen.

## Development install

```sh
git clone <repository>
cd linkplane
PYTHONPATH=src python -m linkplane --version    # run the checkout
make test                                       # hermetic: no phone, no ADB server
```

or, in a virtual environment, `pip install -e .`. The developer checkout is not the
user path: the documented product is the pipx-installed console script.
