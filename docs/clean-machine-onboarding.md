# Clean-Machine Onboarding Procedure

The repeatable proof that a person who has never had Linkplane can install it, connect a
phone, and get a first useful action -- and the way *Time to First Useful Action* (TTFUA)
is measured. Design: `docs/install-onboarding-design.md` §22. Each run is recorded under
`docs/testing/onboarding-run-NNN.md`.

> Installation is not proven until Linkplane succeeds somewhere that has never known
> Linkplane.

## 1. Environment

In order of fidelity; use the best one available and say which was used.

| Tier | Environment | Proves |
|---|---|---|
| **1** | Fresh install of a supported distro (VM with USB passthrough, or a spare machine) | everything, including the systemd user service across a reboot |
| **2** | Fresh Linux user account on an existing machine (`useradd -m -G adbusers`, `loginctl enable-linger`, log in on the desktop) | everything except a truly clean OS: system packages already present |
| **3** | Fresh `$HOME` for an existing user (own XDG tree, own ADB key, PATH without the developer checkout) | install, setup, phone, first use, uninstall, purge; **the systemd unit must be placed in the real user manager's directory** (`--unit-dir ~/.config/systemd/user`) because a HOME cannot host its own manager, and the daemon then uses the real user's state directory -- snapshot and restore it |
| partial | container without systemd or USB | install and CLI only; never the final proof |

A run at tier 3 is labelled *fresh-HOME approximation*; it does not by itself earn a
distro the Supported status (§16 of the Slice 2 brief). **Status: run 001 (tier 3, Arch, fresh HOME) passed; run 002 (tier 1, Ubuntu 24.04 VM
with USB passthrough, installed from the public repository) passed — see
`docs/testing/onboarding-run-002.md`.**

## 2. Preconditions

Host prerequisites (record versions; these are not Linkplane state):

```text
Python ≥ 3.11        python3 --version
pipx                 pipx --version   (Arch: sudo pacman -S python-pipx)
adb                  adb version      (Arch: sudo pacman -S android-tools android-udev)
systemd --user       systemctl --user status   (a desktop login session)
```

Linkplane state that must **not** exist before the clock starts:

```text
no linkplane on PATH            which linkplane            → nothing
no ~/.config/linkplane          no ~/.local/state/linkplane
no ~/.config/systemd/user/linkplaned.service
no $XDG_RUNTIME_DIR/linkplane   no clients, history, audit, jobs
```

Phone: USB debugging enabled, plugged in with a data cable, **this computer not yet
authorized** (a fresh environment has a fresh ADB key, so the phone will ask).

## 3. Install (the clock starts here)

```sh
pipx install linkplane                            # after a public PyPI release
pipx install <public repository or release wheel> # once the repository is public: the tag under test
pipx install ./linkplane-0.4.0-py3-none-any.whl   # before publication: the wheel built from the sanitized public export
pipx ensurepath                                   # once; open a new terminal if PATH changed
linkplane --version
```

The environment must not have the private development checkout: install from the public
source or a release artifact. Before publication, build the wheel from the public export
(`tools/public-export.sh`, then `pip wheel --no-deps -w dist <export>`) and carry only the
wheel to the test machine. `which linkplane` must print the pipx shim
(`~/.local/bin/linkplane`). Never set `PYTHONPATH`, never run from a checkout: the point is
the installed console script.

## 4. Setup

```sh
linkplane setup
```

Expected flow (illustrative; the marks are `✓ ! · ✗`):

```text
Checking this computer...       Linkplane, Python, Configuration, Android platform tools,
                                Optional features (warning lines only)
Connect your Android phone...   guidance, then "Android device detected (…, unauthorized)"
                                → on the phone: tap Allow → "USB debugging authorized"
                                "Device registered as "phone" (default)"
Setting up Linkplane...         Daemon installed, Daemon running (pid, version), Device observed
Verifying connection...         Ping, Status, Battery, Ready
Your phone is ready.  (setup took …)
```

Nothing may be edited by hand. If any command outside `linkplane setup` was needed to
reach "Your phone is ready", it is onboarding friction and goes in the run record.

## 5. Timing method

Wall-clock timestamps (`date +%s.%N`, or `ts`-style prefixes on setup's output with
`PYTHONUNBUFFERED=1`) at:

```text
installation start         the moment `pipx install …` is typed
installation done
setup start                the moment `linkplane setup` is typed
phone detected             "Android device detected"
authorization complete     "USB debugging authorized"
device registered
daemon running
device observed
first useful action        "Ping" ✓  (the first proven phone communication)
```

**TTFUA = first useful action − installation start.** Also report setup's own
`(setup took …)` and, separately, the human portions (tapping Allow, reading, typing) so
Linkplane's share can be improved without confusing it with the person's.

Collect three where the environment allows, labelled exactly:

```text
A  clean-environment first-ever setup (fresh ADB key, real Allow tap)
B  same environment after `linkplane uninstall --purge` + `pipx uninstall` + reinstall
C  already-installed rerun of `linkplane setup`
```

## 6. Persistence

```sh
systemctl --user is-enabled linkplaned.service     # enabled
systemctl --user restart linkplaned.service        # comes back with a new pid
linkplane daemon status                            # Version, Device … connected
linkplane status                                   # from the installed command
```

At tier 1 or 2 also log out and back in (or reboot): the daemon returns and the phone is
observed when connected. Record "not practical" otherwise.

## 7. Failure drills (safe, reversible)

| Drill | How | Expected |
|---|---|---|
| adb missing | run setup with adb off PATH but the package manager visible | stops with `LP-DEPENDENCY-001` and the exact install command; nothing written |
| phone absent | `linkplane setup --serial NOPE`, Ctrl+C after a few seconds | guidance shown, "interrupted", no config created |
| unauthorized | a fresh environment's first run | guidance, then authorized after the tap |
| optional tool absent | no scrcpy on PATH | one `!` line, setup continues |
| rerun | `linkplane setup` again | every phase "already …", no prompt, no restart |

Never break host udev rules or revoke the phone's authorizations to manufacture a case.

## 8. Uninstall and purge checks

```sh
linkplane uninstall
```

Expect: daemon stopped, unit removed, `systemctl --user is-active linkplaned` → inactive,
`pipx list` still shows linkplane, `~/.config/linkplane/config.json` still present.

Then plant user data that must survive -- e.g. `~/Pictures/PhoneBackup/IMG_0001.jpg` with a
`.linkplane-manifest.json` beside it, and a hostile `backups` entry in `config.json`
pointing at `$HOME` and `/` -- and run:

```sh
linkplane uninstall --purge        # answer "yes" (non-interactive: --force)
linkplane uninstall --purge        # again: "nothing to remove"
pipx uninstall linkplane
```

Expect: only `~/.config/linkplane`, `~/.local/state/linkplane` and
`$XDG_RUNTIME_DIR/linkplane` removed; the photos, manifest, home and everything else
untouched.

## 9. Pass / fail

Pass requires all of: pipx console script used; setup reaches "Your phone is ready" with no
manual intervention; unit enabled and survives a restart; ping/status/battery succeed;
rerun idempotent; uninstall leaves config and software; purge removes only the three
roots; TTFUA recorded with its components. Any hand edit, any command outside the
documented ones on the happy path, or any file outside the three roots removed by purge is
a fail and a product defect to file.
