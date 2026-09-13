# Onboarding run 001 — 2026-09-12

**Result: PASS** on a *fresh-HOME approximation* (procedure tier 3), the highest tier
available without root on the development machine. Not a fresh OS: system packages were
already present. This is therefore the first clean-**environment** TTFUA, not yet the
clean-**machine** baseline.

| | |
|---|---|
| Host | Arch Linux (Omarchy), kernel 7.2, systemd 261, Python 3.14.7, adb 1.0.41 (android-tools + android-udev installed) |
| pipx | not installed on the host; pipx 1.17.2 bootstrapped from PyPI into a venv inside the fresh HOME (host has no `pip`/`setuptools`; root was unavailable for `pacman -S python-pipx`) |
| Environment | `HOME` + `XDG_CONFIG_HOME/STATE/DATA/CACHE` under a scratch directory; `PATH=$HOME/.local/bin:/usr/bin:/bin` (developer launcher and checkout invisible); fresh `~/.android` (new ADB key); `XDG_RUNTIME_DIR` the real session's |
| Phone | Samsung SM-S901U, Android 16, USB data cable; `device_id: redacted` |
| Software | wheel `linkplane-0.4.0-py3-none-any.whl` built from commit `d357344`; `pipx install <wheel>`; `which linkplane` → `~/.local/bin/linkplane` (pipx shim); `linkplane --version` → 0.4.0 |
| Linkplane state before | none in the fresh HOME; the real user's `~/.config/linkplane`, `~/.local/state/linkplane` snapshotted (sha256) and restored afterwards: identical |

## Deviations from a true clean machine

1. The unit had to be installed into the real user manager's directory
   (`linkplane setup --unit-dir ~/.config/systemd/user`), because a fresh HOME has no
   systemd user manager of its own. The daemon therefore ran with the real user's
   environment and wrote to the real state directory (restored from snapshot afterwards).
   The product path (`daemon install`, `enable --now`) was exercised for real; only the
   directory was pointed at.
2. pipx came from PyPI via a bootstrap venv, not from `pacman`.
3. No logout/login or reboot (not practical for the same session); persistence was checked
   with `is-enabled` + `systemctl --user restart`.

No Linkplane source was edited, no configuration was edited by hand, and no command
outside the documented ones was needed on the happy path.

## Measurement A — first-ever setup (fresh ADB key, real Allow tap)

| Event | Clock | Δ from install start |
|---|---|---|
| installation start (`pipx install`) | 18:57:02.0 | 0.0 s |
| installation done | 18:57:05.6 | 3.6 s |
| *(orchestration gap: operator typing the next command)* | | +20.4 s |
| setup start | 18:57:26.8 | 24.8 s |
| phone detected (unauthorized) | 18:57:29.8 | 27.8 s |
| authorization complete (human tapped Allow) | 18:58:24.1 | 82.1 s |
| device registered | 18:58:24.1 | 82.1 s |
| daemon installed | 18:58:24.4 | 82.4 s |
| daemon running (0.4.0) | 18:58:25.4 | 83.4 s |
| device observed (battery 47 %) | 18:58:25.4 | 83.4 s |
| **first useful action: Ping ✓ 32 ms** | **18:58:25.4** | **83.4 s** |
| status ✓, battery ✓, ready | 18:58:25.8 | 83.8 s |

**TTFUA (A) = 83 s** wall clock, of which:

| Source of delay | Seconds |
|---|---|
| package installation (pipx, from a local wheel) | 3.6 |
| operator gap between install and `linkplane setup` | 20.4 |
| setup's own checks before the phone | 0.0 |
| ADB server start + first device poll | 3.0 |
| **human: reading the guidance and tapping Allow on the phone** | **54.2** |
| Linkplane: register, install + start unit, observe | 1.3 |
| Linkplane: ping, status, battery | 0.4 |

Setup's own report: `(setup took 59s)`. Linkplane's share of A, excluding the two human
intervals, is about **8 s** (3.6 install + 3.0 detection + 1.7 processing).

## Measurement B — after uninstall --purge, pipx uninstall, reinstall

Phone already authorized for this environment's key (no tap). `pipx install` 1.1 s
(cached build), setup 1.81 s: **B = 2.9 s** from install start to first useful action.
Registered again as `phone` (config had been purged), unit reinstalled, daemon 0.4.0.

## Measurement C — already-installed rerun

`linkplane setup --json`: **C = 0.503 s**; every phase "already …", no prompt, no restart,
`created: false`. (The earlier development-machine rerun, 0.405 s, is a separate
already-configured measurement.)

## Persistence

`is-enabled` → enabled; `is-active` → active; `systemctl --user restart` → active again
with a new pid; `linkplane daemon status` shows Version 0.4.0 and the phone connected;
`linkplane status` from the pipx install prints the phone's telemetry. Reboot/relogin: not
practical (recorded).

## Failure drills

| Drill | Outcome |
|---|---|
| adb missing (PATH without adb; pacman + sudo visible; non-tty) | stopped: `LP-DEPENDENCY-001`, "install it with: sudo pacman -S --needed android-tools; then run `linkplane setup` again"; no config written |
| no matching phone (`--serial NOPE`), SIGINT after 6 s | guidance printed, "linkplane: interrupted", exit via cooperative cancel, no config created |
| unauthorized | run A: guidance shown once, polled 54 s, continued after the tap |
| optional tool absent (no scrcpy on PATH) | one `!` line; setup continued |
| rerun / daemon already running | measurement C |

## Uninstall proof

`linkplane uninstall` (JSON): daemon `was_running: true, stopped: true`, unit removed,
`systemctl --user is-active` → inactive, `pipx list` still shows linkplane 0.4.0,
`~/.config/linkplane/config.json` retained. Finding: the emptied runtime directory was
left behind (the daemon had removed its own socket and discovery file); fixed in the same
slice so uninstall removes the empty directory.

## Purge proof

Planted `~/Pictures/PhoneBackup/IMG_0001.jpg` + `.linkplane-manifest.json`, and
`config.json` entries `backups: [$HOME, /, ~/Pictures/PhoneBackup]`. `linkplane uninstall
--purge --force` removed exactly `~/.config/linkplane` and `$XDG_RUNTIME_DIR/linkplane`
(the fresh HOME had no state directory: the daemon's state lived in the real user's, see
deviation 1); the photo, manifest and home survived; a second purge reported "nothing to
remove". The symlink-escape case could not be planted in this environment and is covered
by the hermetic tests.

## Product findings

1. Uninstall left an empty runtime directory (fixed).
2. While a phone is `unauthorized`, adb reports no model, so the detection line reads
   "Android device (…, unauthorized)"; the model appears once authorized. Cosmetic.
3. The real manager's daemon named the phone by its bare serial (no profile in the real
   config); setup matched it by address as designed.

## Support matrix

No change proposed: the run was on Arch/Omarchy (already Supported). Ubuntu 24.04 remains
the validation target; it needs a tier 1 or 2 environment, which this machine cannot
provide without root.
