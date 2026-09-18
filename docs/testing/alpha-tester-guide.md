# Alpha tester guide (first session)

Thank you for trying Linkplane. This is an alpha: it has passed a clean-machine test, but
you are among the first people to use it without the author in the room. This session is
bounded to about 30 minutes and never touches your photos, clipboard, or camera.

## You need

- **Arch Linux / Omarchy** or **Ubuntu 24.04** (other systemd distributions may work; say
  which you used).
- An Android phone, a USB cable that carries data, and a minute to enable Developer options
  → USB debugging. Nothing is installed on the phone.
- A terminal. No Python knowledge needed.

## 1. Install

Pick the block for your operating system and run only that one. Install `adb`
**before** plugging the phone in.

Arch Linux / Omarchy:

```sh
sudo pacman -S --needed python-pipx android-tools android-udev
pipx install https://github.com/Omgwtfpancake/linkplane/releases/download/v0.5.1/linkplane-0.5.1-py3-none-any.whl
linkplane --version
```

Ubuntu 24.04:

```sh
sudo apt update
sudo apt install pipx adb
pipx install https://github.com/Omgwtfpancake/linkplane/releases/download/v0.5.1/linkplane-0.5.1-py3-none-any.whl
linkplane --version
```

If `linkplane` is not found, run `pipx ensurepath` once and open a new terminal.

## 2. Set up

```sh
linkplane setup
```

Follow the screen. When the phone asks *Allow USB debugging?*, tap **Allow** and tick
*Always allow from this computer*. Note how long it took and anything you had to read twice.

## 3. Try these (all safe)

```sh
linkplane notify "Hello from Linkplane"   # look at the phone: that is Linkplane talking to it
linkplane status                          # battery, storage, memory (Wi-Fi appears when the phone's Wi-Fi is on)
echo "Hello from Linkplane" > /tmp/linkplane-hello.txt
linkplane send /tmp/linkplane-hello.txt   # lands in the phone's Download folder
linkplane events                          # unplug and replug the phone, watch; Ctrl+C
linkplane doctor
linkplane setup                           # again: it should change nothing and finish fast
```

## 4. Uninstall (end of session, or keep it!)

```sh
linkplane uninstall            # stops and removes the service; keeps your configuration
linkplane uninstall --purge    # also removes Linkplane's own config/state (asks first)
pipx uninstall linkplane
```

## If something fails

Capture and include in your report:

```sh
linkplane doctor --json > doctor.json
linkplane setup --json > setup.json      # if setup was the problem
linkplane --version; python3 --version; adb version | head -1
```

Both JSON files contain paths and error codes, never tokens. Please **replace your phone's
serial number** with `DEVICE_SERIAL` before posting; it is the only identifier in there.

## Automatic camera-photo backup (new in v0.6)

This is the feature to try. It copies new photos and videos from the phone's camera folder
(`/sdcard/DCIM/Camera` only) to this computer whenever the phone connects. It is one-way:
nothing on the phone is changed or deleted, and deleting photos from the phone never deletes
their backups.

Setup asks about it only when it registers a new phone, and the default answer is no. **If
your phone was already set up before v0.6, setup will not ask**; turn it on yourself:

```sh
linkplane status                                   # "Backup ... off (turn on: ...)"
linkplane automations enable photo-backup          # folder: ~/Pictures/Linkplane/<your phone's name>
linkplane automations enable photo-backup --destination ~/Pictures/PhoneTest   # or a folder you choose
```

Then unplug and replug the phone. Expect one desktop notification if new photos were copied,
nothing if there was nothing new, and one "Automatic camera-photo backup failed" notice if it
could not run. Check with `linkplane status` and `linkplane automations jobs`. Turn it off
with `linkplane automations disable photo-backup` (your backed-up photos stay).

## Report

Open an issue at https://github.com/Omgwtfpancake/linkplane/issues (templates: bug, setup
problem, feature) or send the form below.

### Feedback form

```text
OS / version:
Phone (model only):
Install: worked / failed — command used:
Setup: worked / failed — where it stopped (line or error code):
Time from install command to "Your phone is ready" (rough):
What was confusing:
First command that failed (if any) and its output:
doctor.json / setup.json attached (serial replaced):
What felt useful:
What felt unnecessary:
Would you keep it installed?  yes / no / not yet — why:
```

## Privacy

Linkplane is local-first: no account, no telemetry, nothing leaves your computer unless you
send it (a file you `send`, a backup you pull). Your report is the only thing that travels.
