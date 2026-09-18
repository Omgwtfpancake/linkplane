# Onboarding run 003 — 2026-09-17/18 — Ubuntu 24.04 VM (tier 1) — v0.6 candidate

**Result: PASS.** A machine that had never known Linkplane installed the v0.6 candidate,
onboarded a phone with a genuine first-computer authorization, turned automatic
camera-photo backup on by answering one question, and then backed up 647 camera files by
itself — with a success notice when files arrived, silence when nothing changed, one safe
notice when a run failed, and recovery across a reboot. Nothing was released: the candidate
still reports version 0.5.1 and nothing was merged, tagged or published.

| | |
|---|---|
| Candidate | wheel `linkplane-0.5.1-py3-none-any.whl` (sha256 `dc7656df…7b98`), built from the public export of `dev/v0.6` @ `7890927`; reports **0.5.1** because the release bump has not happened |
| Environment | **Tier 1**: fresh QEMU/KVM VM (4 vCPU, 4 GB, new 25 GB disk) from the official Ubuntu 24.04 cloud image (SHA-256 verified), cloud-init user `tester` (sudo, plugdev), headless, SSH |
| Guest | Ubuntu 24.04.5 LTS, kernel 6.8.0-139-generic x86_64, Python 3.12.3 |
| Before install | no pipx, adb, `linkplane`, `~/.android`, `~/.config/linkplane`, `~/.local/state/linkplane`, no user unit, no pipx venvs, no `PYTHONPATH`, no checkout |
| Phone | Samsung SM-S901U, Android 16 (toybox 0.8.12), serial redacted; the VM generated its own ADB key, so the phone asked *Allow USB debugging?* and a human tapped Allow |
| Prerequisites | `sudo apt update && sudo apt install pipx adb` — 5.6 s (pipx 1.4.3, adb 1:34.0.4, `android-sdk-platform-tools-common` udev rules) |
| Install | `pipx install ~/linkplane-0.5.1-py3-none-any.whl` — 3.8 s; `linkplane` resolved to `~/.local/bin/linkplane`; `pipx ensurepath` was not needed |

## Timings

| Event | Clock (guest) | Δ |
|---|---|---|
| prerequisites + candidate install | | 9.4 s |
| `linkplane setup` begins | 00:27:42 | 0 s |
| phone detected (`unauthorized`, guidance shown) | 00:27:46 | 3.1 s |
| **human tapped Allow** → authorized | 00:28:13 | 30.2 s |
| registered, unit installed, daemon running, observed | 00:28:14 | 31.4 s |
| ping 28.2 ms, status, battery | 00:28:14 | 31.7 s |
| backup question asked and answered `y`, folder default accepted | 00:28:15 | 32.4 s |
| **Ready** (`setup took 32s`) | 00:28:15 | 32.4 s |
| first automatic backup finished (647 files, 2.93 GB) | 00:30:16 | 153 s |

Authorization → first backup complete: **123 s**. Of setup's 32 s, about 27 s was the human
reading the prompt and tapping Allow.

## Automatic camera-photo backup

The question came only after ping, status and battery had passed, named the camera folder,
said one-way and that deleting phone photos never deletes the backups, showed the folder, and
defaulted to no. Answering `y` and pressing Enter at `Back up camera photos to
[~/Pictures/Linkplane/phone]` wrote one ordinary rule; no JSON was edited by hand.

| Run | Trigger | Result | Discovery | Duration | Notification |
|---|---|---|---|---|---|
| first | phone attached when the daemon started (`initial`) | 647 files, 2.93 GB copied and verified | batch, 1 call, 0.178 s | 121.0 s | 1 success |
| no change | disconnect + reconnect (`change`) | 0 copied, 647 verified unchanged | batch, 1 call, 0.160 s | 1.76 s | **none** |
| one new photo | disposable test JPEG added, reconnect | exactly 1 copied, 647 skipped | batch, 1 call, 0.233 s | 1.97 s | 1 success |
| failure | temporary destination made read-only after enabling, new photo pending | job failed, no partial file left | batch, 1 call | — | 1 failure: "Automatic camera-photo backup failed / Linkplane cannot write to the backup folder." |
| after reboot | phone attached when the daemon started | 0 copied | batch, 1 call | 2.20 s | none |

`linkplane status` showed `Backup … on, to /home/tester/Pictures/Linkplane/phone` with the
last run, including `failed: Linkplane cannot write to the backup folder.` — the raw adb
error (with its path) appeared only in `automations jobs` and the audit log. `automations
list` showed the rule with its conditional notification and `on_error` step; `automations
presets` showed device, state, folder and last run. The audit log recorded `rules.loaded`,
`job.started`, `job.completed` / `job.failed` and `rule.fired` (`✓backup ✓notify-desktop`,
`✓backup ·notify-desktop`, `✗backup ✓notify-desktop`).

Enabling an unwritable folder was refused up front (`LP-REQUEST-001`, "this user cannot
write to …"), so the runtime failure had to be provoked by making a folder read-only after
enabling it.

## Reboot, rerun, additive behaviour

- **Reboot:** `sudo systemctl reboot`; after login the user unit was `enabled`/`active`, the
  phone was observed, and the automatic run copied nothing in 2.2 s with no notification.
  Setup was not rerun first.
- **Rerun:** `linkplane setup` again took **3.3 s**, asked **no** questions (including no
  backup question), reported "already registered" / "already installed" / backup "on", and
  left `config.json` and `automations.json` byte-identical with one rule.
- **Additive:** the disposable test photo was deleted from the phone; its backup copy stayed,
  so the phone had 647 camera files and the backup folder 648.
- **Existing-user path:** with the same profile and no rule (isolated state), `linkplane
  status` printed `Backup … off (turn on: linkplane automations enable photo-backup)`, and
  that command turned it on — matching `docs/cli-reference.md` and the tester guide.

## Honest notes about this run

- **Notifications were observed through test instrumentation.** A headless server image has
  no notification service, so `libnotify-bin` and a small logging
  `org.freedesktop.Notifications` server were installed as a user service *before* the timed
  run. It is not part of Linkplane or of the user flow; it stands in for a desktop's
  notification daemon and recorded every notice.
- **Disconnects were done at the VM's USB controller** (`device_del` / `device_add`), not by
  pulling the cable: the machine under test saw genuine USB removal and re-insertion
  (`device.disconnected` then `device.connected`, both marked as changes), but a physical
  cable pull was not performed in this run.
- **The phone's camera folder changed between sessions** (652 files earlier the same day,
  647 during this run): photos were removed on the phone by its owner. Discovery matched the
  live count exactly each time.
- **The user service follows the login session.** On this headless VM each SSH command
  started and stopped the `systemd --user` manager, so the daemon stopped between commands;
  a desktop login (or `loginctl enable-linger`) keeps it running. This is the environment,
  not Linkplane, but it means automatic backup only runs while the user's session exists.
- **Not exercised on a device:** a real out-of-space failure (`LP-STORAGE-001`); it is
  covered by hermetic tests. The batch discovery fast path was again only exercised on this
  one phone and toybox version; the per-file fallback is covered by tests.
