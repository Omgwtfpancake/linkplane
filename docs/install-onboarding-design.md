# Linkplane — Install & Onboarding v0.1 Design

> Design pass, 2026-09-12, against commit `5e43912` (package `0.4.0`, milestone `api-v0.1`
> at `ff6492b`, 548 hermetic tests). Founder-reviewed and approved with decisions D1–D8
> (recorded in §28 as "Decided"). **Slices 0 and 1 (§27) are implemented**: `linkplane
> setup` exists as designed in §8–§9 (differences noted *(Slice 1)*); `linkplane
> uninstall`, the clean-machine proof and the README are not. Every claim about
> current behaviour was read from the code; where this document and the code later
> disagree, the code is right and this document has drifted. Implementation discoveries
> that changed the design are marked *(Slice 0)*.

**Guiding principle:** *A new user should experience Linkplane as a product, not as an
architecture project.* The person this document is written for has never seen the
repository, does not know what ADB is, and wants their phone to do something useful from
their Linux desktop in the next few minutes.

---

## 1. Goal

Answer one question with evidence from the repository:

> How does a person who did not build Linkplane install it on a clean Linux machine,
> connect an Android phone, and perform the first useful action without understanding
> Linkplane internals?

Target experience:

```text
clean Linux machine
   → install Linkplane           (one command)
   → linkplane setup             (one guided flow)
        dependencies checked
        phone connected and authorized
        device registered
        daemon installed and running
        device observed
        health verified
   → first useful action succeeds
```

### North-star metric: Time to First Useful Action (TTFUA)

*From "Linkplane is not installed on this machine" to "Linkplane has performed a useful,
non-destructive, non-privacy-sensitive action with the user's phone".*

- The clock starts before the install command is typed and stops when the first action's
  result is on screen. Phone-side steps (Developer options, the authorization prompt) are
  inside the clock: the user experiences them as part of setup.
- Acceptable first useful actions, all read-only or harmless: `linkplane status`, a phone
  notification (`linkplane notify`), a small `linkplane send` of a file the user chose, a
  `linkplane backup --dry-run` that proves photo backup is possible. A destructive or
  privacy-sensitive operation (real backup, clipboard read, camera) never counts.
- Long-term target: **under 5 minutes** on a supported machine and a normal phone.
- **First measurement (Slice 2, 2026-09-12, `docs/testing/onboarding-run-001.md`):**
  83 s on a fresh-HOME approximation with a real first-ever Allow tap (54 s of it human,
  20 s operator gap, ~8 s Linkplane); 2.9 s after uninstall + reinstall; 0.5 s rerun.
  This is a clean-*environment* number on the development machine, not yet the
  clean-*machine* baseline, which needs a fresh OS or user account
  (`docs/clean-machine-onboarding.md` tiers 1–2).
- `linkplane setup` will print its own elapsed time at the end. That is the host-side
  portion of TTFUA and the part Linkplane can actually shorten.

## 2. Non-goals

Not part of this milestone, and onboarding must not become a vehicle for them:

```text
desktop GUI · MCP · SDK · cloud service · remote/LAN access · multi-user accounts
device farm · native Android agent · Windows · macOS · a mobile Linkplane app
plugin marketplace · AI execution · a public package release (PyPI/AUR) inside this pass
analytics, telemetry, crash upload, account registration
```

Also out of scope: wireless ADB and Termux/SSH as *first* onboarding paths (§12 keeps them
as follow-ups), automatic modification of udev rules or group membership (§18), and a
broad refactor of the import-time dependency bindings noted in §27.4.

## 3. Current installation reality

Read from `pyproject.toml`, `Makefile`, `README.md`, `service.py`, and the author's machine.

| Fact | Evidence |
|---|---|
| Package metadata exists and is minimal | `pyproject.toml`: setuptools backend, `name = "linkplane"`, `version = "0.4.0"`, `requires-python = ">=3.11"`, `dependencies = []`, one console script `linkplane = "linkplane.cli:main"` |
| Zero third-party Python dependencies | `dependencies = []`; the README's "Development" section confirms stdlib only |
| Nobody installs it | The repository has **no git remote** (`git remote -v` is empty). It has never been published anywhere. |
| The author's "install" is a launcher over the checkout | `~/.local/bin/linkplane` is a 3-line `sh` script that exports `PYTHONPATH=…/Linkplane/src` and runs `python -m linkplane`. No venv, no pip, no pipx, no uv on the machine. |
| The README has no install section | Its first heading after the intro is "Status command"; "Development" mentions `PYTHONPATH=src python -m linkplane` and that "an installable entry point is defined in `pyproject.toml`". `docs/core-v0.1-plan.md` records `pip install -e .` working once. |
| systemd unit installer exists and works | `service.py`: writes `~/.config/systemd/user/linkplaned.service`, `daemon-reload`, `enable --now`; `ExecStart` is the absolute path of whatever `shutil.which("linkplane")` finds at install time, else `<sys.executable> -m linkplane`. Verified on the author's host per `docs/roadmap.md`. |
| Nothing in the code cares how it was installed | `paths.py` uses XDG directories only; no reference to a venv, site-packages, or the launcher. |
| The package's Python floor is real | 3.11 features (`X | None` in runtime annotations, `tomllib`-era stdlib) — Ubuntu 22.04 (3.10) is out. |

So today "install Linkplane" means: clone a repository that is not public, and either write
the launcher by hand or `pip install -e .` into something. There is no path a stranger can
follow. **Publishing the repository is the first prerequisite of every option in §4.**

## 4. Recommended v0.1 installation method

### 4.1 Options evaluated

Criteria from the brief: user complexity, Python version handling, isolation, upgrade,
uninstall, PATH, systemd user-service compatibility, dependency installation, support
burden, security, reproducibility, fit for an open-source GitHub project.

| Option | Verdict | Why |
|---|---|---|
| **pipx** (`pipx install git+<repo>@<tag>`, later `pipx install linkplane` from PyPI) | **Primary for v0.1** | In every target distro's repositories (`python-pipx` on Arch, `pipx` on Debian 12 / Ubuntu 24.04 / Fedora). Own venv per app (isolation, no PEP 668 fights), `~/.local/bin/linkplane` shim (stable path for the systemd unit), `pipx upgrade` / `pipx uninstall` are the whole lifecycle. Works from a git URL before any PyPI release exists. Uses the distro Python, which is ≥ 3.11 everywhere in the supported set. Boring and well understood. Cost: the user needs `pipx` (and `git` while installs come from a git URL) — two distro packages. |
| `uv tool install` | Documented secondary | Same shape as pipx and faster, and it can fetch a Python if the distro's is too old. But `uv` is not in Debian/Ubuntu 24.04 repositories, and its official install is `curl \| sh` — exactly the pattern §26 refuses as a primary path. It would be chosen for fashion, not for the supported set. Mention it for users who already have it. |
| Python venv + `pip install` | Developer path | Correct and dependency-free but four manual steps (create, activate, install, put on PATH) and the unit's `ExecStart` would point into a venv the user must not delete. This is what contributors do (`pip install -e .`); it is not what a first user should be asked to do. |
| `pip install --user` / system pip | Rejected | Blocked by PEP 668 ("externally managed environment") on Arch, Debian 12+, Ubuntu 23.04+, Fedora 38+. `--break-system-packages` is not advice to give a first user. |
| Bootstrap/installer script (`curl … \| sh`) | Not in v0.1; safer model defined in §26 | Hides what runs, invites `sudo`, and Linkplane has nothing a script would do that pipx does not already do. If one ever exists it is a thin wrapper over the documented pipx commands, downloaded to a file, checksum-verified, readable, never piped to a shell. |
| Native distro package (AUR `linkplane` / `linkplane-git`, later `.deb`/RPM) | **Next after v0.1**; the natural Arch primary once a public release exists | The founding vision names `yay -S … && … setup` as the target. It is the best experience on Arch: `pacman` installs `android-tools` as a dependency and `android-udev`/`scrcpy` as optional ones, `/usr/bin/linkplane` is on PATH for everyone, uninstall and upgrade are the package manager's. Cost: Arch-only, needs an AUR account and a PKGBUILD to maintain, and (for a non-`-git` package) a public source tarball, i.e. a public release tag. Debian/Fedora packaging is far more work and premature. |
| Single-file (`zipapp` `.pyz`) or binary (PyInstaller) | Later | A stdlib-only package zips into one `linkplane.pyz` trivially, which is attractive for "download one file". But PATH, upgrade, and uninstall become manual, the systemd unit must embed `python3 /path/linkplane.pyz`, and there is no integrity story without a release pipeline. Reconsider when there is a release pipeline. |

### 4.2 Recommendation

**pipx, installing from the public Git repository at a pinned tag**, is the one primary v0.1
path. The README shows exactly this, per distro:

```sh
# Arch / Omarchy
sudo pacman -S --needed python-pipx git android-tools android-udev
pipx install "git+https://github.com/<owner>/linkplane.git@<tag>"
pipx ensurepath        # once; then open a new terminal
linkplane setup
```

```sh
# Ubuntu 24.04 / Debian 12 (expected to work, §5)
sudo apt install pipx git adb
pipx install "git+https://github.com/<owner>/linkplane.git@<tag>"
pipx ensurepath
linkplane setup
```

Notes that are part of the decision, not afterthoughts:

- `<tag>` must be an **installable name**. `docs/versioning.md` says milestone tags
  (`core-v0.1`, `api-v0.1`) are "not installable by name" and a public release `vX.Y.Z` is
  "the only thing that gets a package and support". Handing tester #1 an install command
  therefore *is* the first public release. The onboarding milestone should close by cutting
  the first public tag (§28, decision 7); until then internal testing installs from a
  commit hash.
- `adb` is listed in the install line rather than left to `setup` because on a clean
  machine it is the one dependency without which nothing works, and installing it needs
  `sudo` anyway; `setup` still detects and offers it (§19) for users who skipped the line.
- `pipx ensurepath` edits the user's shell rc to add `~/.local/bin`. That is a visible,
  reversible, user-owned change and the only PATH manipulation in the whole flow.
- The systemd unit will point at `~/.local/bin/linkplane` (the pipx shim), which survives
  `pipx upgrade`, so upgrading never breaks the daemon's `ExecStart` (§20).
- Developer install stays as it is (`git clone`, `PYTHONPATH=src`, or `pip install -e .`
  in a venv) and is documented under "Development", not in the quick start.

## 5. Supported Linux boundary

The smallest boundary that is honest is the set of environments that will actually be
tested with a phone before tester #1 arrives.

| Tier | Environments | Meaning |
|---|---|---|
| **Supported** | **Arch Linux, including Omarchy** (systemd, Python 3.12+, `android-tools` + `android-udev`, Wayland or X11 desktop) | Clean-machine test (§22) run with a real phone; failures are bugs; documented in the README quick start. This is the author's platform and the only one with a paired phone today. |
| **Expected to work** | Ubuntu 24.04 LTS, Debian 12, Fedora 40+ (all systemd, Python ≥ 3.11, `adb` packaged with udev rules) | Install + `setup` + daemon exercised in a clean VM **without** a phone (or with USB passthrough when practical); phone path believed identical because ADB is ADB. Not advertised as supported until a real-phone run exists. |
| **Unsupported / not tested** | Ubuntu 22.04 and older (Python 3.10), non-systemd distros (Void, Alpine, Devuan, Gentoo/OpenRC), WSL (no USB without `usbipd`; no `systemd --user` by default), NixOS, immutable/atomic desktops (Silverblue, SteamOS), containers | `setup` still runs on non-systemd hosts but stops at the daemon phase with an explicit "install the daemon with your init system by hand" (`service.py` already raises `LP-DEPENDENCY-001` for a missing `systemctl`); nothing else is promised. |

Dependency-install helpers (§19) know `pacman`, `apt-get`, `dnf`, and `brew`; that a helper
exists for a package manager does **not** make the distro supported. `brew` plans stay as
they are but macOS is a non-goal.

Founder decision 3 (§28): whether to spend a VM cycle promoting Ubuntu 24.04 to *supported*
before tester #1, or to invite an Arch tester first.

## 6. Dependency inventory

Everything the code shells out to or requires, from `dependencies.py`, `doctor.py`,
`service.py`, `actions.py`, `clipboard.py`, `camera.py`, `webcam.py`, `transfer.py`,
`notification.py`, `devices.py`, and the phone-side scripts in `legacy/termux/`.

| Dependency | Where | Classification | Modelled today | Notes |
|---|---|---|---|---|
| Python ≥ 3.11 | host | **Required for Linkplane core** | `requires-python`; `doctor` prints the version | stdlib only |
| `adb` (`android-tools` / `adb`) | host | **Required for Android setup** | `adb_dependency_plan` (pacman/apt/dnf/brew) | the primary provider; every first-use path needs it |
| USB access rules for ADB (`android-udev` on Arch; bundled with Debian/Ubuntu/Fedora `adb`) | host | **Required for Android setup** on Arch | `ADB_USB_RULES_PACKAGES` + `usb_rules_install_hint` *(Slice 0)*; detected at runtime as `LP-AUTH-003` | without it `adb devices` shows `no permissions` |
| `systemd --user` (`systemctl`) | host | Required for daemon *installation*; optional for one-shot commands | `service._systemctl` checks `shutil.which("systemctl")` | `XDG_RUNTIME_DIR` (socket dir) comes from the systemd session too |
| `ssh` (OpenSSH client) | host | Optional provider (Termux/SSH) | `ssh_dependency_plan`; `doctor` warns | never needed on the USB path |
| `scrcpy` (with the options `scrcpy_compatibility` probes) | host | Required only for capabilities: `screen.control`, camera preview, audio, webcam | `scrcpy_dependency_plan` + feature probe; no `dnf` package | compatibility is by option probing, not version number |
| `v4l2loopback` kernel module (`-dkms`) | host | Capability only (webcam) | `v4l2loopback_dependency_plan` (sysfs check) | needs a kernel module load; may need `sudo modprobe` |
| `v4l2-ctl` (`v4l-utils`) | host | Capability only (webcam) | `v4l2_ctl_dependency_plan` *(Slice 0)* | |
| `localsend-cli` | host | Optional fallback provider for `send` | `localsend_dependency_plan`; no package on any manager | manual install only; never required |
| `wl-copy`/`wl-paste` or `xclip` | host | Capability only (desktop clipboard) | `wayland_clipboard_dependency_plan`, `x11_clipboard_dependency_plan` | one of the two per session type |
| `notify-send` (`libnotify`) | host | Capability only (`notify-desktop` rule action; `watch --notify`) | `notify_send_dependency_plan` *(Slice 0)*; `doctor` "Desktop notifications" | present on every desktop in the supported set |
| `sudo` | host | Only to run a package-manager install command | `dependency_plan` prefixes it | Linkplane itself never needs it (§18) |
| `git`, `pipx` | host | Install-time only | n/a | §4 |
| Developer options + USB debugging + the authorization prompt | phone | **Required for Android setup** | detected via `adb devices` state | no software installed on the phone |
| Termux + `openssh` + `legacy/termux/phone-status-json.sh` | phone | Optional provider (SSH) | `probe_ssh_endpoint`; `doctor` "Remote scripts" | advanced path only |
| Termux:API (`termux-clipboard-get/set`, `termux-camera-photo`, `termux-notification`) | phone | Capability only: `clipboard.*` and `camera.capture` run **through the SSH provider** and need it there (`clipboard.py`, `camera.py`; the ADB serial only foregrounds Termux — corrected *(Slice 0)*: the draft said "even over ADB"); `notify.post` over ADB does **not** (uses `cmd notification post`) | probed over SSH in `devices.py`; catalogue entry `termux-api`; ADB provider detail `TERMUX_API_DETAIL` | never part of first-use |
| `make`, `unittest` | host | Developer only | `Makefile` | |

### 6.1 Gaps the inventory exposed *(all three closed in Slice 0; kept as the record)*

1. **USB permission rules are not a dependency Linkplane knows about.** On Arch without
   `android-udev`, `adb devices -l` prints `<serial> no permissions (user in plugdev group;
   are your udev rules wrong?) …`. `parse_adb_devices` takes the second whitespace field as
   the state, so the state becomes the word `no`; `select_device` raises "no authorized ADB
   device; found <serial> (no)"; `errors.classify` matches `no authorized` and returns
   **`LP-AUTH-002` "The phone has not authorized this computer"** with hints about USB
   debugging. That is the wrong diagnosis for a host-side udev problem and would send a
   first user to the phone to fix the computer. Setup needs a correct detection (§17, §27
   Slice 0).
2. `notify-send` and `v4l2-ctl` are used but have no `DependencyPlan`, so `doctor` cannot
   report them and `setup` cannot either.
3. The `Termux:API` requirement for clipboard and camera capture is documented nowhere a
   user would look; `capabilities` reports `clipboard.read` as supported by the ADB provider
   only if the probe succeeds, which is fine, but onboarding must never imply clipboard is
   part of the base experience.

## 7. Required vs optional dependencies

What `linkplane setup` treats as blocking versus informational:

| Tier | Members | Setup behaviour |
|---|---|---|
| **Required (blocking)** | Python ≥ 3.11 (already true if `linkplane` runs), `adb`, USB access to the phone, a writable config directory | Setup stops at the failing phase with the LP error, the fix, and "run `linkplane setup` again" |
| **Required for the daemon phase** | `systemctl --user`, `XDG_RUNTIME_DIR` | Missing → daemon phases are skipped with an explicit notice; everything one-shot still works; setup ends "complete (daemon not installed)" |
| **Optional, reported, never installed by default** | `scrcpy`, `v4l2loopback`, `v4l2-ctl`, `wl-clipboard`/`xclip`, `notify-send`, `ssh`, `localsend-cli` | One line each in the dependency phase: present/absent and what it unlocks. No prompt, no install. Existing per-command `--install` flags (`screen --install`, …) keep doing the on-demand install for the capability that needs it. |
| **Phone-side optional** | Termux, Termux:API | Never mentioned by `setup`; documented under advanced providers (§12) |

The rule the brief asks for, stated as a requirement: **`setup` must not install, or offer
to install, a dependency that only a capability needs.** The only install it may ever
offer is `adb` (§19).

## 8. Guided setup flow

### 8.1 Entry point *(Slice 1: built as described; flags are `--name`, `--serial`, `--config`, `--no-daemon`, `--api-client ID`, `--dry-run`, `--non-interactive`, `--json`, plus `--api-port`/`--no-api` for the unit; no `--yes`, no `--install`: a package install always needs an interactive yes)*

One new top-level command: **`linkplane setup`**. It is the first command the README tells a
user to run and the only one they must know. It composes existing services and never
re-implements them:

| Phase | Reuses |
|---|---|
| preflight | `paths.config_dir()`, `sys.version_info`, `shutil.which("systemctl")`, `os.environ["XDG_RUNTIME_DIR"]` |
| dependencies | `dependencies.host_dependency_plans()` (+ the two missing plans), `dependency_install_hint`, `install_dependency` |
| device detection / authorization | `AdbTransport.select_device` semantics through `devices.discover`/`parse_adb_devices`, polled |
| registry | `profiles.pair_usb_device`, `profiles_from_config`, `save_paired_profile` |
| daemon install / start | `service.install`, `daemon.is_running`, `daemon.request("status")` |
| observation | `daemon.request("status")["devices"]` |
| health / first use | `doctor.provider_checks` (ping, status, capabilities) via the same `resolve_provider` the CLI uses |

`--json` emits one envelope with a `phases` array (each `{phase, status, code?, detail,
fix?}`), the same `Check` shape `doctor` already prints, so a future GUI can drive the same
flow. `--dry-run` runs every check and prints every change it *would* make (unit text,
config path, profile name) without writing anything — the same convention `pair` and
`daemon install` already follow.

Alternatives considered and rejected: `linkplane doctor --fix` (turns doctor into an
installer, §16), `linkplane pair usb` as the entry (it does one phase and assumes ADB is
ready), a `linkplane init` alias (two names for one thing).

### 8.2 What the user sees (illustrative, derived from the phases in §9; the built output has the same lines with `✓`/`!`/`·`/`✗` marks and section headings "Checking this computer…", "Connect your Android phone with USB.", "Setting up Linkplane…", "Verifying connection…")

```text
$ linkplane setup

Linkplane setup

  ✓ Python 3.12.3
  ✓ Configuration directory   ~/.config/linkplane
  ✓ ADB                       1.0.41 (android-tools)
  · scrcpy not installed      needed for screen, camera preview, audio, webcam — linkplane screen --install
  · notify-send available     desktop notifications from rules

Connect your phone

  On the phone: Settings → About phone → tap "Build number" seven times,
  then Settings → System → Developer options → turn on "USB debugging".
  Plug the phone into this computer with a USB cable.

  ⠋ waiting for a phone …
  ✓ Phone detected            Samsung SM-S901U (DEVICE_SERIAL)
  ! The phone is asking whether to allow USB debugging from this computer.
    Tap "Allow" (you can tick "Always allow").
  ✓ Authorized

  ✓ Device registered         profile "phone" (default) → ~/.config/linkplane/config.json

Background service

  ✓ Daemon installed          ~/.config/systemd/user/linkplaned.service
  ✓ Daemon running            pid 41213, local API http://127.0.0.1:8741/v1
  ✓ Phone observed            connected, battery 84%

  ✓ Health check              ping 12 ms · status ok · 9 of 11 capabilities supported

Your phone is ready.  (setup took 1m 48s)

Try:
  linkplane status
  linkplane notify "Hello from Linkplane"
  linkplane send ~/Pictures/photo.jpg
```

Every `✓`/`!`/`·` line maps to a phase or check in §9; nothing is decorative. Failures
print the same title/detail/fix/code block the CLI already renders for `LP-` errors.

## 9. Setup state machine

Phases run in order. Each phase's **entry condition** is evaluated against the real system
every run (no setup-state file, §10), so a re-run skips straight through phases that are
already satisfied.

| # | Phase | Entry condition | Success condition | Recoverable failures (code) | User instruction | Retry | Persisted |
|---|---|---|---|---|---|---|---|
| 0 | **preflight** | always | Python ≥ 3.11; `config_dir()` exists or can be created (0700); `state_dir()` writable; `XDG_RUNTIME_DIR` set (else note "daemon phases unavailable") | config dir not writable (`LP-CONFIG-002` with the path); no `systemctl` → daemon phases marked *skipped*, not failed | "Fix permissions on <path>" / "daemon needs systemd; see docs" | safe | creates the config/state directories only |
| 1 | **dependencies** | preflight ok | `adb` available | `adb` missing (`LP-DEPENDENCY-001`); package manager unknown (`LP-DEPENDENCY-001`, install_error text) | show the exact install command; offer to run it (§19); otherwise "install adb and run setup again" | safe | nothing |
| 2 | **device detection** | `adb` available | `adb devices -l` lists exactly one device in any state | none listed (`LP-CONNECT-002`): keep polling up to 120 s with the phone-side instructions on screen; more than one (`LP-CONNECT-003`): list them and ask for `--serial`; `no permissions` (new detection, §17) | Developer options / USB debugging / cable text; udev fix text on Arch | safe; polling is the retry | nothing |
| 3 | **device authorization** | one device listed | its state is `device` | `unauthorized` (`LP-AUTH-002`): poll while telling the user to tap Allow; `offline` (`LP-CONNECT-001`): poll with "unlock / replug / wait" guidance *(Slice 1: founder decision, offline polls like unauthorized)*; `no permissions` (`LP-AUTH-003`): stop at once with the host-side fix; timeout: the code of the last state seen plus "run `linkplane doctor`" | "Tap Allow on the phone" / host guidance | safe; polling is the retry; detection and authorization share one 120 s window | nothing (the phone stores the authorization) |
| 4 | **profile / registry** | authorized serial known | a profile whose `device_id == serial` exists and is the default (or a default exists) | name collision with a *different* device (§10 — must not alias); config invalid (`LP-CONFIG-002`) | "Profile 'phone' belongs to another phone; using 'phone-2' / pass `--name`" | safe | **config.json** (0600): profile, `default_device` |
| 5 | **daemon installation** | systemd available; not `--no-daemon` | unit exists at `resolve_unit_dir()/linkplaned.service` **and** its `ExecStart` resolves to the current `linkplane` executable | `systemctl` failure (`LP-PROVIDER-001` from `service.py`); unit dir not writable | "systemd --user is not available in this session (SSH login without a session bus?)" | safe: existing matching unit is left alone; mismatched `ExecStart` → rewrite with notice | **unit file** |
| 6 | **daemon start** | unit installed | `daemon.is_running()` and `request("status")` answers within 10 s | not running (`LP-DAEMON-001`): show `systemctl --user status linkplaned` and `journalctl --user -u linkplaned -n 20`; API bind failure is **not** a failure of this phase (`LP-API-001` is logged by the daemon and reported as a warning line) | "The service did not start; here is its log" | safe: `enable --now` is idempotent | socket, `state.json`, `api.json` (by the daemon) |
| 7 | **observation verification** | daemon running | `request("status")["devices"]` contains the registered `device_id` with `connection == "connected"` within 15 s | device not observed (`LP-CONNECT-002` with "the daemon does not see the phone yet"); typically `adb track-devices` needs a second | "Replug the phone; if it persists run `linkplane events`" | safe | nothing (the daemon writes `state.json`) |
| 8 | **optional API client** | `--api-client <id>` given | client exists (created, or already present) | id exists (`LP-STATE-001`): reuse, never mint another token; invalid id (`LP-REQUEST-001`) | token printed once with the same "shown once" notice `clients create` prints | safe | **clients.json** (0600) |
| 9 | **first-use verification** | provider resolvable | `provider.ping()` reachable, `provider.status()` without issues, capabilities queried | unreachable (`LP-CONNECT-001`), partial telemetry (`LP-PROVIDER-003`, warning only) | doctor's existing fixes | safe, read-only | nothing |
| 10 | **complete** | phases 0–4, 9 ok (5–7 ok or skipped) | summary printed with elapsed time and "Try:" | — | — | — | nothing |

Rules that apply to every phase:

- A phase that fails **stops** setup (except warnings), prints the LP block, and ends with
  `Run "linkplane setup" again after fixing this.` Exit code 1. Nothing later runs
  half-way.
- Phases 2 and 3 are the only interactive waits. Ctrl+C exits 130 (`OperationCancelled`
  handling already exists) leaving whatever was already written intact and valid.
- `--non-interactive` (for scripts and the clean-machine test) turns every prompt into a
  failure with the code and never runs a package manager.
- Setup never touches: `automations.json`, `events.jsonl`, `audit.jsonl`, `jobs/`, backup
  destinations, SSH keys.

## 10. Idempotence behaviour

Hard requirement: **`linkplane setup` must be safe to run again, at any point, forever.**

| Existing state | Behaviour |
|---|---|
| config directory exists | reused; permissions checked (0700 dir, 0600 file) and *reported*, tightened only with the user's yes (the author's hand-written `config.json` is 0644 today) |
| `config.json` exists and is valid | reused; every profile kept |
| `config.json` invalid | stop with `LP-CONFIG-002`; setup never overwrites a file it cannot parse |
| profile with `device_id == connected serial` exists | reused; if it is not the default and there is no default, it becomes the default; if another default exists, left alone with a note |
| profile named `phone` exists for a **different** `device_id` | **do not alias.** Today `pair_usb_device` → `paired_device_id(name, …, strict=False)` returns the *existing* profile's `device_id` and `save_paired_profile` appends the new serial as an alias of the old phone. That is right for adding a wireless alias to the same phone and wrong for a second phone. Setup calls the strict form (or compares first) and picks the next free name (`phone-2`) unless `--name` is given. |
| legacy top-level `ssh` config only (pre-profiles) | left intact; a USB profile is added beside it (`profiles_from_config` already reads both) |
| pre-rename `~/.config/phonebridge` | resolved by `paths.py` as today; setup reports which directory it is using |
| unit file exists, `ExecStart` matches | verify only ("Daemon installed ✓ (already)") |
| unit file exists, `ExecStart` points at a vanished path (old venv, old launcher) | rewrite with notice, `daemon-reload`, restart |
| daemon already running | verify via socket; if its reported version differs from the installed package (`describe()` carries `version` in `/v1/health`; the socket `status` op should expose the same) → `systemctl --user restart` with notice (§20) |
| daemon running **foreground** in another terminal (no unit) | *(Slice 1)* setup sees it on the socket, verifies its version (a warning rather than a restart when no unit is installed), and uses it for observation; with `--no-daemon` this is the only daemon path |
| API client id already exists | reuse, no new token (`LP-STATE-001` is informational here) |
| device authorized before | phase 3 passes instantly |
| no phone connected on a re-run | setup still verifies phases 0, 1, 5, 6 and reports "waiting for a phone" — useful after a reboot |

State that cannot be reused and why: **an API token**. Tokens are stored only as SHA-256; a
lost token cannot be shown again, only revoked and re-created. Setup says so instead of
minting a duplicate client.

## 11. Android USB/ADB onboarding

ADB over USB is the mandatory first path (founder decision 4, recommended yes):
`ADBProvider` supports `device.ping/status`, `battery.read`, `storage.read`, `files.send`,
`backup.photos`, `notify.post`, `device.find` with **nothing installed on the phone**;
`screen.control` needs only host-side scrcpy. The daemon's observer (`adb track-devices`)
is ADB-only; SSH-only devices are not observed (`docs/current-state.md`).

The user is guided through, in the words setup prints:

1. **Enable Developer options** — "Settings → About phone → tap *Build number* seven times"
   (with the Samsung/Pixel variants in one line each, since the two together cover most
   phones).
2. **Enable USB debugging** — "Settings → System → Developer options → *USB debugging*".
3. **Connect the cable** — "Use a data cable; charge-only cables are the most common
   reason nothing appears."
4. **Accept the prompt** — "Tap *Allow* on the phone; tick *Always allow from this
   computer* so you are not asked again."
5. **Verify** — setup polls; the user never types `adb devices`.

Detection mapping (from `AdbTransport.select_device` and `errors.classify`):

| `adb devices -l` state | Setup shows | Code |
|---|---|---|
| nothing listed | the four steps above, spinner, "still waiting after 30 s? try another cable/port" | `LP-CONNECT-002` (only if the 120 s window expires) |
| `unauthorized` | "The phone is asking for permission — tap Allow" | `LP-AUTH-002` |
| `offline` | "Unplug and plug the phone back in" | `LP-CONNECT-001` |
| `no permissions …` | **host** fix: on Arch `sudo pacman -S android-udev` then replug; on Debian/Ubuntu/Fedora the rules ship with `adb`, so "replug; if it persists see troubleshooting" | new detection (§17) |
| more than one `device` | list them; "unplug the others or run `linkplane setup --serial <x>`" | `LP-CONNECT-003` |

Troubleshooting only (never in the happy path): `adb kill-server`, `adb usb`, the note
from `docs/smoke-test.md` that `adb reconnect` can detach a USB device on some hosts.

**Wireless ADB** stays out of initial onboarding: it needs a pairing code typed from the
phone, two addresses, and its alias drifts with DHCP (`profiles refresh` exists to repair
that). It is documented as the *next* step after USB setup succeeds ("Want to unplug the
cable? `linkplane pair wireless …`"), and USB pairing first is what gives `--scan` the
hardware serial it trusts.

## 12. Advanced provider onboarding

| Path | Phone-side | Host-side | When |
|---|---|---|---|
| **USB ADB** (default) | Developer options, USB debugging, Allow | `adb` (+ udev rules on Arch) | `linkplane setup` |
| **Wireless ADB** | Wireless debugging on; pairing code | none extra | `linkplane pair wireless <name> <pair-addr> --connect <addr>` after USB setup; documented as optional |
| **Termux / SSH** | Termux, `pkg install openssh`, `sshd`, `~/phone-status-json.sh` from `legacy/termux/`, an SSH key | `ssh`, a key pair | `linkplane pair ssh …`; `doctor` already checks identity file, reachability, and remote scripts. Advanced/fallback only; not observed by the daemon. |
| **Termux:API capabilities** | Termux:API app + `pkg install termux-api` | none | unlocks `clipboard.*` and `camera.capture` over either transport; documented per command, never in setup |
| **scrcpy features** | nothing (scrcpy pushes its own server over ADB) | `scrcpy` (+ `v4l2loopback`, `v4l2-ctl` for webcam) | `linkplane screen --install` etc. |
| **future native agent** | an app | — | out of scope (vision §28) |

Each advanced path gets its own short doc section rather than a place in `setup`; a future
`linkplane setup ssh` subcommand is a possible home if demand appears.

## 13. Daemon installation / start behaviour

All from `service.py` and `commands/daemon.py`; setup reuses them unchanged and adds no
second service-management path.

| Aspect | Current behaviour | Setup's use |
|---|---|---|
| Unit location | `$XDG_CONFIG_HOME/systemd/user/linkplaned.service` (`--unit-dir` override) | same |
| Unit content | `Type=simple`, `ExecStart=<abs linkplane> daemon run [--interval --no-wifi --api-port --no-api]`, `Restart=on-failure`, `RestartSec=5`, `After=default.target`, `WantedBy=default.target`, `Documentation=file://<package>/README.md` | same; setup passes `--no-api` through if asked |
| `ExecStart` resolution | `shutil.which("linkplane")` at install time, else `sys.executable -m linkplane` | with pipx this is `~/.local/bin/linkplane`; **setup must run with `~/.local/bin` on PATH** (it will, after `pipx ensurepath` and a new shell) or the unit would embed the venv interpreter instead — still works, but breaks silently on `pipx reinstall`. Setup warns if the resolved path is inside a pipx venv rather than the shim. |
| Enable/start | `daemon-reload`, then `enable --now` | idempotent; re-running is a no-op |
| Restart policy | on failure after 5 s; clean SIGTERM handling verified by `test_sigterm_stops_the_daemon_cleanly` | — |
| Lifetime | starts at login (`default.target`); stops at logout unless `loginctl enable-linger <user>` | setup **mentions** linger as optional for "keep observing while logged out"; never runs it (it may need root) |
| Config path | the daemon reads `config.json` via `paths.py` at start; `daemon reload` reloads rules only; profile changes need a restart | setup restarts the daemon after phase 4 changes the config **only if** the daemon was already running before setup |
| Logs | stderr → journal: `journalctl --user -u linkplaned` | printed in the phase-6 failure text |
| Uninstall | `disable --now`, unlink unit, `daemon-reload`; `LP-CONFIG-003`-coded error if no unit | reused by `linkplane uninstall` (§21) |
| No systemd | `LP-DEPENDENCY-001` "systemctl is not installed; install the daemon with your init system by hand" | daemon phases skipped with that text; one-shot commands and `linkplane events` (in-process observer) still work |
| Port collision | `_start_api` catches `LP-API-001`, logs, audits `api.error`, **daemon keeps running**; `daemon status` shows "API not running" | reported as a warning with `daemon install --api-port <n>` as the fix |

Two small things setup needs from the daemon that do not exist yet: the socket `status`
reply should carry the daemon's package `version` (the HTTP `/v1/health` already does),
and `daemon status` should print it — both additive.

## 14. API-client provisioning decision

**No API client is created by CLI-only setup.**

Evidence: ADR 0012 — "the CLI calls typed services directly"; `cli.py::main` routes every
command to services or the Unix socket, never to `http://127.0.0.1:8741`. The daemon starts
its API with an empty client list (`load_clients` returns `()` when `clients.json` is
absent) and every request gets 401 until a client exists; that is the intended secure
default. Nothing in the daemon requires credentials internally.

Therefore:

```text
linkplane setup               → no token, no clients.json
linkplane setup --api-client waybar --scope read
                              → creates it via clients.create_client, prints the token once
future GUI / MCP / SDK        → provision their own client when first launched
```

Setup's summary mentions the API in one line ("Local API: http://127.0.0.1:8741/v1 — create
a client with `linkplane clients create` when a tool needs it").

## 15. Configuration / state ownership

Everything Linkplane creates on the user's machine, from `paths.py`, `docs/current-state.md`,
`clients.py`, `service.py`, `backup.py`:

| Item | Path | Class | Created by | Uninstall default |
|---|---|---|---|---|
| Device profiles, default device, legacy `ssh` | `~/.config/linkplane/config.json` (0600, dir 0700) | configuration | pair / setup | keep |
| Webcam settings | `~/.config/linkplane/webcam.json` | configuration | webcam | keep |
| Automation rules | `~/.config/linkplane/automations.json` | configuration (user-authored) | user | keep |
| API clients (token digests) | `~/.config/linkplane/clients.json` (0600) | **credential** | clients create / setup `--api-client` | keep (purge removes) |
| SSH identity for Termux path | user-chosen (`~/.ssh/…`) | **credential**, user-owned | user / pair ssh | **never touched** |
| Observed state snapshot | `~/.local/state/linkplane/state.json` | runtime state | daemon | keep (purge removes) |
| Event history | `~/.local/state/linkplane/events.jsonl` | history | daemon / events | keep (purge removes) |
| Audit log | `~/.local/state/linkplane/audit.jsonl` | history | daemon | keep (purge removes) |
| Job records | `~/.local/state/linkplane/jobs/*.json` | history | daemon | keep (purge removes) |
| Control socket, API discovery | `$XDG_RUNTIME_DIR/linkplane/daemon.sock`, `api.json` (0600) | runtime (ephemeral) | daemon | removed by daemon stop |
| systemd unit | `~/.config/systemd/user/linkplaned.service` | configuration (installer-owned) | daemon install / setup | **removed** by uninstall |
| pipx venv + shim | `~/.local/share/pipx/venvs/linkplane`, `~/.local/bin/linkplane` | software | pipx | removed by `pipx uninstall` |
| Backup destination + manifest | user-chosen directory; `.linkplane-manifest.json` (or legacy `.phonebridge-manifest.json`) inside it | **user data** | backup | **never touched, not even by purge** |
| Sent/received files | user-chosen paths, `/sdcard/Download/` on the phone | user data | send | never touched |
| Phone-side authorization key | on the phone | phone state | Android | never touched (user revokes in Developer options) |

Cache: none today. Setup writes nothing outside this table.

## 16. Install vs Setup vs Doctor responsibilities

```text
INSTALL   pipx (or a distro package) puts the `linkplane` executable on the machine.
          Linkplane code never installs Linkplane.

SETUP     linkplane setup — gets Linkplane connected and operational: creates the
          config, registers the phone, installs and starts the daemon, verifies.
          Composes existing services. Interactive. Idempotent. May offer exactly one
          system-package install (adb), with approval.

DOCTOR    linkplane doctor — diagnoses an existing installation and prints fixes.
          Read-only. Never installs, never writes config.
```

Anti-duplication rules:

- The dependency and health checks are **one implementation**. Today `doctor.build_checks`
  and `configuration_checks`/`provider_checks` return `Check` records; setup's phases 0, 1
  and 9 call those same functions and render the results as phases. If a check is missing
  (udev, `notify-send`, config permissions), it is added to `doctor` and setup gets it for
  free.
- `doctor` gains no `--fix`. The one thing people will ask it to fix — a missing `adb` — is
  setup's job.
- The dependency model *(Slice 0, as built)*: `DependencyPlan` stays the detect/install
  half; a separate catalogue record `Dependency(name, purpose, where, required_for,
  summary, plan_factory)` is the "what is it for" half, with purposes `core-required`,
  `android-base`, `capability-optional`, `provider-optional`, `developer-only` (the founder's
  classes) rather than a two-valued tier. `dependency_report()` joins them and answers
  `base_ready`. Version is still handled separately by `doctor.executable_version` (and by
  option probing for scrcpy); no dependency has a numeric floor that matters.

## 17. Common failure UX

Format for every failure, already produced by `render_error`: **title** (from
`errors.TITLES`), detail, "Check that / Fix" hints, and the code. Setup adds "Can Linkplane
fix it?" and "Run setup again?" columns as behaviour, not prose.

| Failure | What happened (title) | Next step for the user | Auto-fix | Retry safe | Code |
|---|---|---|---|---|---|
| adb not installed | "A required program is not installed." | the exact package-manager command for this distro | **offer** (approval required, §19) | yes | `LP-DEPENDENCY-001` |
| adb too old | not a real case: no minimum ADB version is enforced anywhere in the code; `doctor` shows `adb version` for humans | — | — | — | — |
| phone not connected | "No phone connected." | the four phone steps; wait up to 120 s | no | yes (polling) | `LP-CONNECT-002` |
| USB debugging disabled | indistinguishable from "not connected" at the ADB level; the waiting text covers both | same text | no | yes | `LP-CONNECT-002` |
| device unauthorized | "The phone has not authorized this computer." | "Tap Allow on the phone" | no | yes (polling) | `LP-AUTH-002` |
| USB permission (`no permissions`) | "This computer is not allowed to access the phone over USB." *(Slice 0)* | the `usb_rules_install_hint` for this distro (Arch: `sudo pacman -S --needed android-udev`), replug | no (needs root; command shown) | yes | `LP-AUTH-003` |
| multiple devices | "More than one phone is connected." | list; `--serial` | no | yes | `LP-CONNECT-003` |
| daemon cannot install (no systemd) | "A required program is not installed." (systemctl) | run `linkplane daemon run` under your supervisor; setup marks daemon phases skipped | no | yes | `LP-DEPENDENCY-001` |
| daemon cannot install (systemctl error, unit dir unwritable) | "The phone command failed." — `service.py` reuses `LP-PROVIDER-001` for systemctl failures, which reads oddly; acceptable for v0.1, note as debt | the systemctl stderr, `journalctl` hint | no | yes | `LP-PROVIDER-001` |
| daemon cannot start | "The Linkplane daemon is not running." | `systemctl --user status linkplaned`, `journalctl --user -u linkplaned -n 20` | no (setup shows the log) | yes | `LP-DAEMON-001` |
| daemon already running elsewhere | "The Linkplane daemon is already running." | stop it or `--no-daemon` | no | yes | `LP-DAEMON-002` |
| configuration not writable | "Linkplane could not write its configuration." with the path and the OS error *(Slice 0)* | fix permissions | no | yes | `LP-CONFIG-004` |
| configuration corrupt | same title | fix or move the file; setup never overwrites | no | yes | `LP-CONFIG-002` |
| port collision | "The local API could not start." (warning; daemon runs) | `daemon install --api-port <n>` | no | yes | `LP-API-001` |
| API disabled (`--no-api` unit) | not a failure; summary says "Local API: disabled" | — | — | — | — |
| profile already exists (same phone) | not a failure; reused | — | — | — | — |
| profile name taken by another phone | "The request is invalid." naming the owning device and the next free name *(Slice 0: pairing is strict; setup will pre-check and pick `next_free_profile_name`)* | `--name` to choose | yes (name) | yes | `LP-REQUEST-001` |
| dependency unavailable on this distro (e.g. scrcpy on `dnf`, `localsend-cli` anywhere) | only ever a `·` informational line in setup; the per-command `--install` raises "automatic installation is not supported with dnf" | manual install link | no | — | `LP-DEPENDENCY-001` (per command) |
| phone reachable but telemetry partial | "Some phone data could not be retrieved." (warning) | `linkplane status` for detail | no | yes | `LP-PROVIDER-003` |
| `XDG_RUNTIME_DIR` unset (SSH login, no session) | daemon phases skipped: "no user session; log in on the desktop and run setup again" | as stated | no | yes | `LP-DEPENDENCY-001` |

No installer-only prose errors: every row above ends in an existing code, a proposed
additive code, or "not a failure".

## 18. Privilege model

| Needs | Who | Notes |
|---|---|---|
| **Linkplane itself: never root.** | normal user | pipx venv in `$HOME`; config/state under XDG dirs; unit is `systemd --user`; socket in `$XDG_RUNTIME_DIR`; ADB talks to the phone as the user. |
| Installing `adb`, `android-udev`, `scrcpy`, `v4l2loopback` | the OS package manager, via `sudo` | `dependency_plan` already prefixes `sudo` for pacman/apt/dnf and refuses with a clear `install_error` when `sudo` is absent. Setup prints the full command and asks; the sudo prompt the user then sees is the OS's, and setup says so in the words the brief asks for: *"Your package manager may ask for your password to install adb. Linkplane itself does not need root."* |
| Loading the `v4l2loopback` module | root | webcam only; outside setup |
| `loginctl enable-linger` | may need root depending on polkit | mentioned, never run |
| udev rules / groups | root | never modified by Linkplane; command shown |

Setup runs no privileged command silently, ever. `--non-interactive` never runs one at all.

## 19. Dependency installation policy

Decision (founder decision 5, recommended): **detect → explain → offer the exact supported
command → run it only after an explicit "yes" → verify.**

- Only `adb` is ever offered by `setup`. Everything else is reported with the command that
  installs it on demand (`linkplane screen --install`, which already exists and already
  asks the same way).
- The offered command is the one `dependency_plan` computes (`sudo pacman -S --needed
  android-tools`, `sudo apt-get install adb`, `sudo dnf install android-tools`), shown
  verbatim before the prompt. On Arch the offer includes `android-udev` (§6.1).
- `install_dependency` already runs the command, checks the exit code, and re-verifies the
  executable; setup reuses it. A failed install is `LP-DEPENDENCY-001` with the exit code;
  setup never retries a package manager on its own.
- No package manager known (`install_error = "no supported package manager was found"`) →
  the manual instruction and the docs link; setup stops.
- `--non-interactive` / `--no-install`: never invoke; `--install`: pre-approve (for the
  clean-machine test only, documented as such).
- Never: `pip install` anything, edit shell rc files (pipx does that, visibly, once), touch
  `/etc`, load kernel modules, or add the user to groups.

## 20. Upgrade strategy

For the recommended path:

```sh
pipx upgrade linkplane                 # once installs come from PyPI
pipx install --force "git+https://github.com/<owner>/linkplane.git@<newer-tag>"   # while they come from git
linkplane setup                        # verifies and restarts the daemon if its version changed
```

- `pipx upgrade` re-resolves the spec the package was installed from; a git spec pinned to a
  tag never "upgrades", hence the `--force` form with the new tag until PyPI exists. The
  README states this plainly.
- The shim path `~/.local/bin/linkplane` is stable across upgrades, so the unit's
  `ExecStart` stays valid. The **running** daemon keeps executing old code until restarted:
  setup's phase 6 compares the daemon's reported version with `linkplane.__version__` and
  restarts on mismatch; `linkplane daemon status` shows both. A future `linkplane update`
  command would only wrap `pipx upgrade` + that restart; not built now.
- Config and state formats are versioned (`linkplane.state/1`, `linkplane.event/1`,
  `linkplane.clients/1`, `CONTRACT_VERSION`); an upgrade that changes one must read the
  old form. `paths.py` already demonstrates the pattern with the rename.
- Downgrade: `pipx install --force …@<older-tag>`; state files written by a newer version
  may be refused by the older one with `LP-CONFIG-002` — documented, not prevented.
- An AUR package later inherits the distro's own upgrade path; the restart-on-version-change
  rule in setup stays the same.

## 21. Uninstall / purge strategy

Two different things, never conflated:

```text
remove software      pipx uninstall linkplane          (or pacman -R linkplane later)
remove Linkplane's   linkplane uninstall [--purge]      (new, small)
own service/state
```

`linkplane uninstall` (new command, built on `service.uninstall`):

1. Stop and disable the daemon, remove the unit, `daemon-reload` (existing code; tolerate
   "no unit installed" instead of failing, since `service.uninstall` raises today).
2. Remove `$XDG_RUNTIME_DIR/linkplane/` (socket, `api.json`) if the daemon left it.
3. Print what is **kept** and where: config, profiles, clients, state, history, audit, jobs
   — with the exact paths from §15 — and the one command that removes the software
   (`pipx uninstall linkplane`). Linkplane cannot reliably delete its own running
   executable and should not try.
4. `--purge`: after listing every file it will delete and reading an explicit `yes`, remove
   `~/.config/linkplane` and `~/.local/state/linkplane` (and only the legacy `phonebridge`
   directories if `paths.py` resolved to them). **Never** follows a backup destination,
   never deletes a `.linkplane-manifest.json` (it lives in the user's backup directory,
   beside their photos), never touches `~/.ssh`, never touches the phone. `--purge` is
   refused in `--non-interactive` mode without `--yes`.
5. Exit 0 even when there was nothing to remove; `--json` reports what was removed and kept.

Backed-up photos are user data. The purge code path must be written so it cannot reach them
even by misconfiguration: it deletes only the two fixed directories, never a path read from
a config or manifest.

## 22. Clean-machine test plan

### 22.1 Environments

| Environment | What it proves | Practicality |
|---|---|---|
| **A. Clean VM with USB passthrough** (QEMU/virt-manager, Arch ISO or `archinstall` minimal + a desktop session, later Ubuntu 24.04 cloud image) | the whole flow including the phone and `systemd --user` | the primary proof; needs the phone passed through (`usb-host` device) or a second physical machine |
| **B. Fresh Linux user account on the author's machine** | user-level flow: pipx install, setup, daemon, uninstall | fast; system packages are already present, so it cannot prove the `adb` install step |
| **C. Container (podman) with `--device /dev/bus/usb`** | install + CLI + pairing; **no systemd** | cheap, automatable; the daemon phases are expected to be *skipped* here, which is itself a test of that path |
| **D. A second physical machine** | everything, plus reboot | when available; the reboot step is only really meaningful here or in A |

### 22.2 Preconditions (all environments)

```text
no ~/.config/linkplane, no ~/.local/state/linkplane, no ~/.config/phonebridge
no ~/.config/systemd/user/linkplaned.service
no ~/.local/bin/linkplane, no pipx venv named linkplane
phone: USB debugging off, this computer NOT in its authorized list
        (Developer options → Revoke USB debugging authorizations)
a stopwatch
```

### 22.3 Procedure (the release procedure for the milestone, like `docs/smoke-test.md`)

1. Start the clock. Follow the README quick start **literally**, copying commands.
2. `linkplane setup` — record each phase's outcome and any deviation from §8.2.
3. First useful action: `linkplane status`, then `linkplane notify "Hello"`. Stop the clock
   → **TTFUA**.
4. `linkplane doctor` — all ok, no warnings other than optional dependencies.
5. `linkplane setup` again — every phase "already"; nothing rewritten (compare mtimes of
   `config.json` and the unit).
6. Log out and in (or reboot in A/D) — `linkplane daemon status` answers; `devices
   --observed` shows the phone.
7. Unplug/replug — `linkplane events --follow` shows disconnected/connected.
8. `linkplane uninstall` — unit gone, `systemctl --user status linkplaned` says not found,
   config and state still present. `pipx uninstall linkplane` — shim gone.
9. `linkplane uninstall --purge` (reinstall first) — the two directories gone; a backup
   directory created in step 3b (`linkplane backup --dry-run` then a real one into
   `/tmp/lp-backup-test`) is **untouched**.
10. Failure drills, each from a clean state: no `adb` installed; phone unplugged; USB
    debugging off; authorization declined; two phones; `systemctl --user` masked; port
    8741 occupied. Each must match the §17 row.

### 22.4 Automated coverage

- **Hermetic tier (must not regress):** every setup phase is a function taking injected
  probes (finder, runner, transport factory, systemctl runner, clock) with **`None`
  defaults resolved at call time** — the pattern restored in `find.py`/`profiles.py` on
  2026-09-12 — so `tests/test_setup.py` drives all phases, all failure rows, and the
  idempotence table without ADB. The shim verification (ADB server stopped, logging `adb`
  first on PATH, zero invocations) is re-run before the milestone closes and its result
  recorded in the report.
- **Integration tier:** `linkplane setup --dry-run --json` and `--non-interactive` through
  `cli.main()` with fakes at the transport/systemctl seams.
- **Device tier (`tests/device/`, never in `make test`):** setup phases 2–4 and 9 against
  the real phone with a temporary config; the daemon phases with `--unit-dir` pointing at a
  temp directory in `--dry-run` only (a temp unit dir cannot be enabled through the real
  `systemctl --user`, so the live daemon phase stays a manual step in 22.3).

### 22.5 TTFUA measurement protocol

One run per environment, phone prepared as in 22.2, executed by someone other than the
author when possible. Record: environment, distro, wall-clock TTFUA, `setup`'s own elapsed
time, phase where the most time went, every place the user had to read something twice.
The first three runs establish the baseline; only then does optimization start.

## 23. External tester readiness gate

All of the following, before inviting tester #1:

| Gate | Evidence required |
|---|---|
| Repository public on GitHub | URL in the README; `git remote` set |
| One documented supported distro | Arch/Omarchy section in the quick start |
| One recommended install command | the pipx line, pinned to an installable tag (§4.2) |
| One guided setup command | `linkplane setup` passes 22.3 steps 1–7 |
| Clean uninstall | 22.3 steps 8–9 |
| Doctor | `linkplane doctor` clean after setup; udev and config-permission checks present |
| Clear troubleshooting path | `docs/troubleshooting.md` (or a README section) with every §17 row |
| Clean-machine test passed | 22.3 completed in environment A or D, result recorded like `docs/smoke-test.md` |
| Real Android test passed | `make smoke` on the milestone commit |
| README quick start | §24 in place |
| No manual config editing on the happy path | 22.3 completed without opening an editor |
| Hermetic suite green with the shim | recorded count and zero ADB invocations |
| TTFUA baseline recorded | at least one measured run |

The tester should not need the author for the happy path. Anything they *do* need the
author for becomes a §17 row or a README line before tester #2.

## 24. README quick-start proposal

Replace the current opening (which jumps straight to `linkplane status` flags) with:

```markdown
# Linkplane

**Make real devices programmable.** Linkplane connects your Android phone to your Linux
desktop — locally, over a USB cable or your own Wi-Fi, with no account and no cloud.

Check the battery, send files, back up photos with verification, mirror the screen, push a
notification to the phone, find it when it is lost, and let scripts react when it connects
or runs low — all from one command, `linkplane`, and one background service that keeps
watching.

## Install (Arch Linux / Omarchy)

    sudo pacman -S --needed python-pipx git android-tools android-udev
    pipx install "git+https://github.com/<owner>/linkplane.git@<tag>"
    pipx ensurepath   # once; then open a new terminal

Other distributions: see docs/install.md (Ubuntu 24.04, Debian 12, Fedora are expected to
work; they are not yet tested with a phone).

## Connect your phone

    linkplane setup

Setup checks your computer, walks you through enabling USB debugging on the phone, registers
it, starts the Linkplane service, and verifies everything. Run it again any time; it never
undoes a working setup.

## First things to try

    linkplane status                       # battery, storage, memory, Wi-Fi
    linkplane notify "Hello from Linkplane"
    linkplane send ~/Pictures/photo.jpg
    linkplane backup ~/Phone/Photos --dry-run
    linkplane screen                       # needs scrcpy: linkplane screen --install

Something wrong? `linkplane doctor` explains what and how to fix it.

## Upgrade / remove

    pipx upgrade linkplane        (or pipx install --force …@<newer-tag>)
    linkplane uninstall           # stops and removes the service; keeps your config
    pipx uninstall linkplane
```

Everything that is in the README today (status flags, API, events, watch, automations,
daemon internals, pairing details, architecture links) moves below a "## Reference" or
into `docs/` — kept, not deleted.

## 25. Privacy / security behaviour

Unchanged by onboarding and stated in the README's first screen:

- Local-first: no account, no telemetry, no crash upload, no remote service, no network
  listener beyond `127.0.0.1` (the API refuses non-loopback binds by design, ADR 0012).
- Setup sends nothing anywhere. It reads the phone's model and serial over ADB and stores
  them in `config.json` (0600). The phone's authorization key never leaves the phone.
- Optional anonymous install metrics are explicitly **not** proposed; if ever considered
  it is a separate founder decision with an opt-in.

## 26. Packaging security considerations

| Concern | Position |
|---|---|
| Download integrity | Installs come from the public repository over HTTPS at a **tag**; tags should be signed (`git tag -s`) and the README should show the tag's commit hash so a moved tag is detectable. PyPI later adds a wheel checksum; the release page lists SHA-256 for any artifact. |
| Dependency trust | Zero Python dependencies — the wheel is exactly the repository. System dependencies (`adb`, `scrcpy`, …) come from the distro, never from Linkplane. |
| Shell installer risks | No `curl \| sh`, and never `curl \| sudo sh`. If a bootstrap script is ever added: downloaded to a file, checksum printed and verified, short enough to read, runs only the documented pipx/pacman commands, refuses to run as root, never sets PATH itself. |
| PATH manipulation | Only `pipx ensurepath`, run by the user, adding `~/.local/bin`. Linkplane never edits rc files. The unit uses an absolute `ExecStart`, so PATH does not matter to the daemon. |
| Token/config permissions | `clients.json` 0600 via `O_CREAT 0600` + atomic replace; `config.json` 0600, dir 0700 (`profiles.save_config`); socket and `api.json` 0600. Setup/doctor add a permissions check for pre-existing files (the author's own config is 0644 today). |
| Running untrusted commands | Setup executes only: the computed package-manager command (shown, approved), `systemctl --user` with fixed arguments, `adb` with fixed arguments, and Linkplane's own services. No string from a config file, a phone, or the network is ever executed. `run` actions in rules stay consent-gated as today. |
| Privilege escalation | Linkplane never elevates; `sudo` appears only inside the displayed package-manager command. The daemon runs as the user, not as a system service. |
| Supply chain of the AUR package (later) | PKGBUILD builds from the tagged source with a pinned `sha256sums`; `-git` variant clearly labelled unstable. |

## 27. Recommended implementation slices

Derived from what exists: pairing, service install, doctor checks, and dependency plans are
done; what is missing is the udev detection, two dependency plans, a composed `setup`, an
`uninstall`, docs, and the clean-machine proof.

### Slice 0 — foundations (small, testable, no new UX) — **done 2026-09-12**

- `dependencies.py`: `notify-send` and `v4l2-ctl` plans; additive `required_for` and
  `tier` fields on `DependencyPlan`; `host_dependency_plans()` includes them.
- `transports.parse_adb_devices`: keep the full state text for `no permissions`;
  `select_device` raises a distinguishable message; `errors.classify` maps it to the new
  additive `LP-AUTH-003` with per-distro hints (Arch: `android-udev`). `CONTRACT_VERSION`
  unchanged (additive code).
- `doctor`: checks for USB permission state, config file/dir permissions, and the daemon's
  version; socket `status` and `daemon status` carry `version`.
- Testability seams that setup will drive: `service.install/uninstall(runner=…)` and
  `AdbTransport.__init__(runner=…)` move to the `None`-default call-time pattern (they are
  the two remaining import-time bindings on setup's path; the rest stay as debt, §27.4).
- Hermetic tests for all of the above; shim verification re-run.

### Slice 1 — `linkplane setup` — **done 2026-09-12** (`setup.py`, `commands/setup.py`, `tests/test_setup.py`, `tests/integration/test_setup_cli.py`)

- `setup.py` service with the phases of §9 as injectable functions returning `Check`s;
  `commands/setup.py` renders text/`--json`; flags `--name`, `--serial`, `--no-daemon`,
  `--api-client`, `--scope`, `--dry-run`, `--non-interactive`, `--install`/`--no-install`,
  `--unit-dir`, `--api-port`, `--no-api`.
- Idempotence table (§10) as tests; failure rows (§17) as tests; the strict-profile fix.
- Elapsed-time line.

### Slice 2 — `linkplane uninstall`, clean-machine proof, TTFUA baseline — *(uninstall built 2026-09-12: `uninstall.py`, `commands/uninstall.py`, `tests/test_uninstall.py`; non-interactive purge uses the existing `--force` convention; see `docs/clean-machine-onboarding.md` for the proof)*

- `uninstall [--purge] [--yes] [--json]` per §21, with a test that proves purge cannot
  reach a backup destination.
- `docs/onboarding-test.md`: the 22.3 procedure and the recorded result of the first run in
  environment A (or D); `tests/device/test_onboarding_smoke.py` for the device-tier part.
- First TTFUA measurements (three runs) recorded in the doc.

### Slice 3 — README and tester #1 — *(3A done 2026-09-12: identifiers sanitized, README restructured per §24, `docs/install.md`, `docs/troubleshooting.md`, `docs/cli-reference.md`, `tools/public-export.sh` with a build + pipx verification from the export; 3B = publication after founder review)*

- README restructured per §24; `docs/install.md` (per-distro lines, upgrade, uninstall,
  expected-to-work notes); `docs/troubleshooting.md` from §17.
- Repository published; tag signed; install line pinned.
- Readiness gate (§23) walked and recorded; tester #1 invited.

### 27.4 Technical debt recorded here

- Import-time default bindings of injectable dependencies: Slice 0 converted the ones on
  setup's path (`AdbTransport`, `service.install/uninstall`, `scrcpy_compatibility`,
  `discover_adb`, the three `pair_*_device` services). Remaining: `SshTransport`
  (`transports.py`), `clipboard.py`, `webcam.py`, `profiles.scan_for_profile` /
  `refresh_profile_endpoints`' inner helpers, `devices.discover_ssh`. None leaks hardware
  access (verified with the ADB shim on 2026-09-12); they are converted when a test needs
  to patch them, never in a sweep.
- `service.py` reports systemctl failures as `LP-PROVIDER-001` ("The phone command
  failed."), which is the wrong title for a host service error; a `LP-SERVICE-001` would be
  more honest. Additive; not needed for v0.1.
- `docs/product-progress-brief.md` still says "Current version: 0.9.0"; `docs/roadmap.md`
  still calls Core v0.1 the immediate milestone. Both are stale relative to
  `docs/current-state.md` and should be refreshed in Slice 3.

## 28. Founder decisions required

**1. Publish the repository** — *Decided: approved in principle; publishing waits for the publication-safety review (`docs/publication-readiness.md`).*
- Question: Where does the public source live, and under which owner/name?
- A: `github.com/<owner>/linkplane`, public, MIT (as `pyproject.toml` says).
- B: keep private and hand testers a tarball.
- Recommended: **A**. Every install method in §4 assumes a public URL; a tarball has no
  upgrade path and no integrity story.
- Alternative's consequence: no pipx line, no AUR later, tester #1 depends on the author.

**2. Primary installation method** — *Decided: A (pipx); AUR deferred until a public source release exists.*
- A: `pipx install git+…@<tag>` (later PyPI) — one shape across the supported and
  expected-to-work set.
- B: AUR `linkplane-git` / `linkplane` as the primary, matching the vision's `yay -S`
  target; pipx as secondary.
- Recommended: **A** for v0.1, with B as the first native package once a public release
  exists. pipx works on every target distro today with no package to maintain; AUR is
  Arch-only and is better added after the flow is proven.
- Alternative's consequence: the README's only install line would be Arch-only, and the
  first non-Arch tester has nothing.

**3. Initial supported distro set** — *Decided: A; Ubuntu 24.04 is the validation target and becomes supported only after a complete clean-machine test.*
- A: Arch Linux / Omarchy only (tested with a phone); Ubuntu 24.04, Debian 12, Fedora
  "expected to work".
- B: Arch + Ubuntu 24.04 both *supported*, which requires a phone run in an Ubuntu VM with
  USB passthrough before tester #1.
- Recommended: **A** now; promote Ubuntu to supported when a phone run has happened
  (Slice 2 can include it if the VM setup is quick).
- Alternative's consequence: a support promise without a test behind it.

**4. USB ADB as the mandatory first onboarding path** — *Decided: A.*
- A: USB first; wireless ADB and Termux/SSH as documented follow-ups.
- B: let setup offer wireless pairing inline.
- Recommended: **A**. USB is the only path with zero phone-side software, it gives the
  hardware serial that identity and `--scan` rely on, and it has the fewest failure modes.
- Alternative's consequence: a pairing-code prompt, two addresses, and DHCP drift inside
  the first five minutes.

**5. Whether setup may invoke a package manager** — *Decided: A (exact command shown, package named, explicit confirmation; detection/instruction mode always available).*
- A: detect → explain → offer the exact command → run only on explicit yes; `adb` only.
- B: detect and print only; the user runs the command themselves.
- Recommended: **A**. The command is shown verbatim, the sudo prompt is the OS's, and it
  saves a copy-paste on the critical path. `--non-interactive` never installs.
- Alternative's consequence: one more manual step and a second terminal for every user
  without `adb`, which on a clean machine is every user.

**6. API client during setup** — *Decided: A.*
- A: none by default; `--api-client` opt-in.
- B: always create a `local-cli` read-scoped client.
- Recommended: **A**. The CLI does not use the API; a token nobody uses is a credential to
  lose. GUI/MCP provision their own later.
- Alternative's consequence: every install carries an unused bearer token in `clients.json`.

**7. Milestone name and the first public tag** — *Decided: `onboarding-v0.1`; package stays 0.4.0 through implementation; `0.5.0` and any public tag are separate later founder decisions.*
- Question: what closes this work, and does closing it cut the first public release?
- A: milestone tag `onboarding-v0.1` on the closing commit (fits `docs/versioning.md`:
  `<section>-vX.Y`, annotated, not installable by name); package `0.4.0 → 0.5.0` in a
  separate commit ("MINOR increments once per shipped section"); **and** the founder cuts
  `v0.5.0` on the version commit as the first public alpha release, because the tester's
  install line needs an installable name and versioning policy says only `vX.Y.Z` is one.
- B: `onboarding-v0.1` and `0.5.0` only; testers install from a commit hash.
- Recommended: **A**, with the release labelled alpha in the README and the changelog
  stating the supported boundary of §5.
- Alternative's consequence: tester instructions contain a 40-character hash and policy
  says the install is unsupported.

**8. Additive error code for USB permission failures** — *Decided: A, implemented in Slice 0 as `LP-AUTH-003`.*
- A: add `LP-AUTH-003` ("This computer may not access the phone over USB") with distro
  hints.
- B: reuse `LP-AUTH-002` with extra hints.
- Recommended: **A**. `LP-AUTH-002`'s title says the *phone* has not authorized the computer,
  which is false here and sends the user to the wrong device. Codes are additive; contract
  version unchanged.
- Alternative's consequence: the most common Arch clean-machine failure keeps a misleading
  title.

Everything else in this document (flag names, phase order, file layout, which functions to
reuse) is an implementation detail the repository already answers and is not put to the
founder.
