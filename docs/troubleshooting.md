# Troubleshooting

Start with the two commands that explain most problems:

```sh
linkplane doctor          # host tools, USB access, configuration, phone, capabilities
linkplane setup           # safe to rerun; stops at the first problem with a code and a next step
```

Every failure has a stable code (`LP-<CATEGORY>-<NNN>`); `--json` output carries the same
code. Never use `adb reconnect` to "fix" a USB phone: on some hosts it detaches the device
until the ADB server is restarted. The safe equivalents are below.

## The phone is not detected (`LP-CONNECT-002`)

Setup keeps waiting and shows the phone-side steps. Check, in order:

1. The cable carries data (charge-only cables are the usual cause). Try another cable or port.
2. USB debugging is on: Settings → System → Developer options → USB debugging.
3. The phone is unlocked; some phones only offer USB debugging while unlocked.
4. `linkplane doctor` shows "ADB" ok. If ADB is missing, see below.

## USB debugging is off

Indistinguishable from "not detected" at the ADB level. Enable Developer options (tap Build
number seven times under About phone), then USB debugging, and reconnect the cable.

## "The phone has not authorized this computer" (`LP-AUTH-002`)

The phone sees the computer and is asking. Look at the phone for *Allow USB debugging?* and
tap **Allow**, ticking *Always allow from this computer*. Setup polls and continues by
itself. If the prompt never appears, unplug and replug the cable, or on the phone revoke
USB debugging authorizations (Developer options) and replug so it asks again.

## The phone is "offline" (`LP-CONNECT-001`)

ADB can see the phone but cannot talk to it. Unlock the phone, unplug and replug the cable,
or toggle USB debugging off and on, then wait a moment. Setup keeps polling. If it
persists, `linkplane doctor`; if the phone was connected wirelessly, reconnect it.

## "This computer is not allowed to access the phone over USB" (`LP-AUTH-003`)

This is the **computer**, not the phone: Linux denied ADB access to the USB device (udev).
Tapping Allow on the phone will not help. Install your distribution's ADB udev rules and
replug:

```sh
sudo pacman -S --needed android-udev        # Arch / Omarchy
# Debian, Ubuntu and Fedora ship the rules with the adb package; reinstall it if they are missing
```

If the rules are installed, make sure your user may access the device (a udev rule for the
phone's vendor id, or membership of the `plugdev`/`adbusers` group), then log out and in.
`linkplane doctor` reports this as "USB access".

## More than one phone is connected (`LP-CONNECT-003`)

Setup lists them and asks which one (interactively) or needs `--serial`:

```sh
linkplane devices
linkplane setup --serial DEVICE_SERIAL
linkplane status --serial DEVICE_SERIAL
```

## ADB is not installed (`LP-DEPENDENCY-001`)

Setup shows the exact command for your package manager and offers to run it after you say
yes, or you run it yourself:

```sh
sudo pacman -S --needed android-tools android-udev   # Arch / Omarchy
sudo apt install adb                                 # Debian / Ubuntu
sudo dnf install android-tools                       # Fedora
```

then `linkplane setup` again. Open a new terminal if `adb` is still not found.

## The daemon is not running (`LP-DAEMON-001`)

```sh
linkplane daemon status
systemctl --user status linkplaned
journalctl --user -u linkplaned -n 50
linkplane setup                       # reinstalls or restarts the service as needed
```

Without `systemd --user` (no desktop session, SSH login, WSL) the service cannot be
installed; run `linkplane daemon run` under a supervisor of your choice, and one-shot
commands work regardless.

## The daemon runs an older Linkplane version

`linkplane daemon status` prints the running version; `linkplane --version` the installed
one. `linkplane setup` restarts the service when they differ. For a daemon you started by
hand, stop it (`linkplane daemon stop`) and start it again.

## "Linkplane could not write its configuration" (`LP-CONFIG-004`)

The configuration directory (`~/.config/linkplane`) or file is not writable by your user.
Fix ownership or permissions, or point `LINKPLANE_CONFIG` at another file. A warning about
"Configuration permissions" means the file is readable by other users: `chmod 600` it.

## "The Linkplane configuration is invalid" (`LP-CONFIG-002`)

Setup never overwrites a file it cannot read. Fix or move
`~/.config/linkplane/config.json`; `linkplane doctor` names the problem.

## "This profile name belongs to another phone" (`LP-REQUEST-001`)

A profile is a human name for one physical phone; a second phone never takes over an
existing name. Setup suggests the next free name (`phone-2`), or pass `--name`, or remove
the old profile with `linkplane profiles remove <name>`.

## An optional feature is unavailable

Setup prints one line per missing optional tool and continues; nothing about the basic
path depends on them. `linkplane screen --install` installs scrcpy after confirmation;
`linkplane capabilities` shows what each phone can do and why not. Clipboard and camera
capture need Termux:API on the phone and the SSH provider (`linkplane pair ssh`).

## Rerunning setup

Always safe. Existing profiles, the service, a daemon at the current version, automations
and history are reused; nothing is duplicated and no token is minted unless you pass
`--api-client`. `--dry-run` runs every check without writing.

## Removing Linkplane

`linkplane uninstall` (service only), `linkplane uninstall --purge` (also Linkplane's own
directories; never your backups), `pipx uninstall linkplane` (the software). See
[`install.md`](install.md#uninstall).

## Still stuck

`linkplane doctor --json` and `linkplane setup --json` produce complete, machine-readable
reports that are safe to share: they contain paths and codes, never tokens.
