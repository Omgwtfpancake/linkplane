# Onboarding run 002 — 2026-09-13 — Ubuntu 24.04 VM (tier 1)

**Result: PASS.** The first true clean-machine lifecycle: a machine that had never known
Linkplane installed it from the public repository with pipx, ran `linkplane setup` against
the real phone with a genuine first-computer authorization, kept the service across a
reboot, reran idempotently, survived the failure drills, uninstalled, purged without touching
user data, and removed the package. One product defect was found by a drill in the first
attempt, fixed in the public repository, and the run was restarted from a fresh machine.

| | |
|---|---|
| Environment | **Tier 1**: fresh QEMU/KVM virtual machine (4 vCPU, 4 GB), Ubuntu 24.04.5 LTS official cloud image (SHA-256 verified), cloud-init user `tester` (sudo, plugdev), headless, SSH login session (`systemd --user` running) |
| Host | the development workstation runs QEMU only; the phone is passed through by USB vendor/product id; the host ADB server was stopped first |
| Guest before install | kernel 6.8.0-139-generic x86_64, Python 3.12.3, no pipx, no adb, no git-installed Linkplane, no `~/.config/linkplane`, `~/.local/state/linkplane`, `~/.android`, no service unit, no pipx venvs |
| Phone | Samsung SM-S901U, Android 16, `device_id: redacted`; a **fresh ADB key** was generated in the VM (verified different from the workstation's key), so the phone showed *Allow USB debugging?* for this machine |
| Software | public repository commit `f9cca447b5d1` (main), installed with `pipx install "git+https://github.com/Omgwtfpancake/linkplane.git@f9cca447b5d1"`; `which linkplane` → `~/.local/bin/linkplane`; `linkplane --version` → 0.4.0 |
| Prerequisites (before the timer) | `sudo apt-get update && sudo apt-get install -y pipx adb` — 13 s; installs pipx 1.4.3, adb 1:34.0.4 and `android-sdk-platform-tools-common` (the udev rules); `git` was already present in the cloud image; `pipx ensurepath` once |
| Private/local source | not used: no `PYTHONPATH`, no checkout, no wheel copied in, no seeded keys or state |
| Order followed | prerequisites first, then the phone connected (as `docs/install.md` says), then install, then setup |

## Timings (final run, wall clock in the guest)

| Event | Clock | Δ from T0 |
|---|---|---|
| T0 `pipx install git+…` begins | 01:38:23.93 | 0.0 s |
| T1 installed | 01:38:31.37 | 7.4 s |
| T2 `linkplane setup` begins | 01:38:31.48 | 7.6 s |
| T3 phone first detected (`unauthorized`; guidance shown) | 01:38:35.20 | 11.3 s |
| T4 USB authorization completed (human tapped Allow) | 01:38:36.20 | 12.3 s |
| T5 device registered as `phone` (default) | 01:38:36.21 | 12.3 s |
| T6 daemon installed (user unit) | 01:38:36.33 | 12.4 s |
| T7 daemon running (0.4.0, started) | 01:38:37.33 | 13.4 s |
| T8 phone observed (battery 46 %) | 01:38:37.33 | 13.4 s |
| **T9 first useful action: Ping ✓ 32 ms** | **01:38:37.36** | **13.4 s** |
| status ✓, battery ✓, ready | 01:38:37.74 | 13.8 s |

| Component | Seconds |
|---|---|
| install (pipx from GitHub, builds the wheel in the VM) | 7.4 |
| setup: checks before the phone | 0.1 |
| ADB server start + first device poll | 3.6 |
| **human: reading the prompt and tapping Allow** | **1.0** (the tester was already holding the phone) |
| Linkplane: register, install + start unit, wait for the daemon | 1.1 |
| Linkplane: observe, ping | 0.03 |
| **Clean-machine TTFUA (T9 − T0)** | **13.4** |

Setup reported `(setup took 6s)`. Linkplane's own share of the 13.4 s is about 4.8 s; the
rest is pipx building and installing the package and the human tap.

## First attempt, and the defect it found

The first attempt attached the phone to the VM *before* `adb` was installed. The device node
therefore predated the udev rules that ship with Ubuntu's `adb` package, and setup stopped
correctly with **`LP-AUTH-003`** ("This computer is not allowed to access the phone over
USB"), telling the user to replug rather than to approve anything on the phone. After one
replug (detach/reattach at the VM level) setup completed in 2 s; the phone authorized the
new key within the seconds between the replug and the run. That attempt reached ping in
47 s of wall clock, most of it the operator gathering evidence between the diagnosis and
the replug. Following the documented order (install `adb`, then plug in) avoids it entirely.

The failure drill "daemon stopped, then rerun setup" then found a **product defect**: with
the unit installed but inactive after `linkplane daemon stop`, setup waited and stopped
with `LP-DAEMON-001` instead of starting the service it had installed. Per the defect rule
the run was abandoned as a qualification, the fix was made in the public repository
(`f9cca44`, "Start an installed but inactive service during setup", with a regression test;
660 hermetic tests), pushed, and the run restarted from a fresh machine against that commit.
No VM was patched by hand.

## Persistence

`sudo reboot`; SSH login 18 s later: the user manager brought `linkplaned.service` up
(new pid), `linkplane devices --observed` showed the phone connected, `linkplane status`
printed its telemetry. No passthrough action was needed after the reboot: QEMU kept the
device attached across the guest restart.

## Rerun

`linkplane setup --json`: ok, **0.304 s**, profile reused (`created: false`), unit already
installed, daemon at 0.4.0 not restarted or started, no API client, `config.json` unchanged.

## Failure drills

| Drill | Outcome |
|---|---|
| phone disconnected (device detached at the VM) | `linkplane status` → `LP-CONNECT-001` "Phone unreachable" naming both providers; `linkplane setup` showed the phone guidance and waited; SIGINT → "interrupted", nothing written |
| daemon stopped (`linkplane daemon stop`) | `status` still works (one-shot commands do not need the daemon); `linkplane setup` → "Daemon running: started; pid …", phone observed, Ready — the fix under test |
| optional dependencies absent (no scrcpy, notify-send, v4l2-ctl) | one warning line; setup continued |
| unauthorized | exercised for real by the fresh key in the main run |

Polish noted, not a defect: with a USB-only profile and the phone unplugged, `status`
appends the SSH provider's hints ("the phone is on the same network", …) to the ADB ones;
the message is truthful but noisier than it needs to be.

## Uninstall

`linkplane uninstall` (JSON): daemon `was_running: true, stopped: true`, unit removed,
`/run/user/1000/linkplane` removed, `~/.config/linkplane` and `~/.local/state/linkplane`
retained; `systemctl --user is-active` → inactive; `linkplane --version` still 0.4.0;
second run: every line "not installed / not running / nothing to remove / kept".

## Purge

Planted `~/Documents/keep.txt` and `~/Documents/PhoneBackup/IMG_0001.jpg` with a
`.linkplane-manifest.json`, and pointed `config.json`'s `backups` at `$HOME`, `/` and that
folder. `linkplane uninstall --purge` on a real terminal listed exactly
`~/.config/linkplane (2 files)` and `~/.local/state/linkplane (3 files)`, asked for a typed
`yes`, removed those two, and nothing else: the file, the photo, the manifest and the home
directory survived. Second purge: "nothing to remove".

## Software removal

`pipx uninstall linkplane` → the `linkplane` command is gone, no pipx venvs remain. Nothing
Linkplane-related is left on the machine except `~/.android` (adb's, not Linkplane's).

## Qualification

Every gate of `docs/clean-machine-onboarding.md` §9 passed on the final run with no source
edit, no manual configuration, and no command outside the documented ones on the happy
path. **Ubuntu 24.04 LTS is recommended for promotion to Supported** (founder decision; the
support tables are unchanged until then).
