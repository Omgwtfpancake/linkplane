> **Note (2026-09-10):** the project was renamed from PhoneBridge to **Linkplane** after this
> document was written. It is kept verbatim; read `PhoneBridge` as `Linkplane` throughout.

# Vision

This is the founding product vision for PhoneBridge, in the founder's own words. It's the
source document behind `docs/product-progress-brief.md`'s "The Vision" section and much of
its commercial-direction thinking — kept here in full because the summarized version
necessarily drops the reasoning and the concrete examples that make the strategy legible.
When the two disagree on emphasis, this document is the more authoritative statement of
intent; `product-progress-brief.md` is the living status snapshot against it.

## Positioning: orchestrator, not competitor

PhoneBridge is a privacy-first Android ↔ Linux command and automation layer. The
competitive landscape already has strong tools: scrcpy is excellent at mirroring and
control — audio, clipboard, camera, keyboard/mouse, gamepads. LocalSend already provides
extremely easy encrypted local file transfer, with millions of downloads. PhoneBridge
should not try to beat either at their specialty. Instead, **PhoneBridge should become the
glue between Android and Linux** — orchestrating proven open-source components rather than
rewriting them, at least initially.

```text
                         PHONEBRIDGE
                              │
             ┌────────────────┴────────────────┐
             │                                 │
          Android                            Linux
             │                                 │
             └──────── Secure Bridge ──────────┘
                              │
       ┌──────────┬───────────┼──────────┬──────────┐
       │          │           │          │          │
     Screen     Files      Clipboard   Phone      Events
     Control    Transfer      Sync      Status    Automation
       │          │           │          │          │
     scrcpy    Native/       Native     Native     Native
              existing
```

The user doesn't necessarily need to know which component handles what. They just know:

```sh
phonebridge screen
phonebridge send ~/Downloads/movie.mp4
phonebridge clipboard
phonebridge camera
phonebridge webcam
phonebridge audio
phonebridge notify "Dinner's ready"
phonebridge status
```

**The key distinction is "take on those features" versus "rewrite those projects."**
PhoneBridge can absolutely give users screen control, file transfer, clipboard sync,
audio, camera access, and more — but as a unified layer that uses proven open-source
components underneath, not a from-scratch reimplementation.

There's product value in this even where PhoneBridge writes none of the underlying
codec/transport code. Example: running `phonebridge screen` when scrcpy isn't installed
should not print `ERROR: scrcpy not found`. It should offer to install it, configure it,
and report progress in PhoneBridge's own voice:

```text
PhoneBridge Screen

scrcpy is required for high-performance
Android screen control.

Install it now? [Y/n]

✓ Galaxy S22 connected
✓ Wireless ADB configured
✓ Audio enabled

Opening phone...
```

And there's value in simplifying scrcpy's large option surface into presets. Instead of
requiring a user to learn `scrcpy --video-codec=h265 --max-size=1920 --max-fps=60
--audio-source=playback ...`, they use `phonebridge screen --quality high` or
`phonebridge game`, and PhoneBridge configures everything intelligently underneath.

### Where PhoneBridge builds its own identity

The layer above stops being "just a wrapper" once it adds things the individual programs
don't provide together:

- `phonebridge backup photos` — detect the phone, find today's photos, transfer only new
  ones, verify them, and organize them into `~/Pictures/Phone/2026/09/08/`.
- `phonebridge webcam` — scrcpy already provides Linux V4L2 webcam output; PhoneBridge
  makes that feature dramatically easier to reach.
- `phonebridge game` — low-latency mirroring, audio, and controller support configured
  together (scrcpy already supports forwarding attached gamepads).
- Combined automation:

  ```text
  phonebridge automate
  WHEN
    Phone connects to home Wi-Fi

  DO
    Backup photos
    Sync clipboard
    Sync downloads

  THEN
    Notify desktop when complete
  ```

### Progressive dependency strategy

1. **Stage 1** — integrate existing open-source tools where licenses and distribution
   models permit it. Get something useful into people's hands.
2. **Stage 2** — build native functionality where integration is poor, or where
   PhoneBridge can make something significantly better.
3. **Stage 3** — potentially replace more dependencies with PhoneBridge-native
   implementations, if doing so gives meaningful performance, UX, security, or packaging
   advantages.

"100% written by us" is explicitly not a goal in itself. Users care that PhoneBridge is
easy, fast, reliable, secure, private, and works — not whether PhoneBridge personally
wrote the H.265 streaming implementation.

## The capability list

| Capability | Priority | Example |
|---|---|---|
| One-command setup | 🔴 Critical | `phonebridge setup` |
| Auto-discovery/pairing | 🔴 Critical | Finds phone automatically |
| Device status | 🔴 Critical | `phonebridge status` |
| Self-diagnostics | 🔴 Critical | `phonebridge doctor` |
| File transfer | 🔴 Critical | `phonebridge send photo.jpg` |
| Clipboard sync | 🔴 Critical | Phone ↔ Linux |
| Notifications | 🔴 Critical | Phone notifications on desktop |
| Phone commands | 🔴 Critical | ring, battery, volume, etc. |
| Multiple phones | 🟠 Important | `phonebridge use galaxy-s22` |
| Remote connectivity | 🟠 Important | Works away from home |
| Waybar integration | 🟠 Important | Battery/status in your bar |
| Desktop notifications | 🟠 Important | Native Linux alerts |
| Automation/events | ⭐ Differentiator | `phonebridge watch ...` |
| CLI/API | ⭐ Differentiator | Scripts can control the bridge |
| Plugins | ⭐ Long-term | Extend PhoneBridge |

## Automations: the actual selling point

One feature in that list could become PhoneBridge's real differentiator.

```sh
phonebridge watch battery --below 20 \
  --notify "Phone battery low"

phonebridge watch connected \
  --run "~/scripts/phone-home.sh"

phonebridge watch charging \
  --run "notify-send 'Phone is charging'"
```

Taken further, `phonebridge events` produces a live stream other programs can subscribe
to:

```text
10:31:04  phone.connected
10:31:05  wifi.connected
10:34:17  battery.changed       73%
10:38:02  notification.received Discord
10:41:27  charging.started
```

Which means someone could write:

```python
from phonebridge import Phone

phone = Phone()

print(phone.battery)
print(phone.storage)

phone.notify("Compile finished!")
phone.send("build.zip")
```

That's not merely another KDE Connect alternative — it's an Android integration platform
for Linux hackers, developers, and automation enthusiasts.

## Installation is what will make or break this

The Termux/SSH/keys/scripts/packages/configuration setup that got the prototype working is
fine for development. It is unacceptable for a consumer product. The target experience is
turning this:

```text
Install Termux
Install OpenSSH
Generate key
Find IP
Copy key
Create script
Install packages
Configure service
Start API
...
```

into this:

```sh
yay -S phonebridge
phonebridge setup
```

```text
Phone:

Pair with "Sam-PC"?

      [PAIR]

Computer:

✓ Galaxy S22 discovered
✓ Secure connection established
✓ PhoneBridge service installed
✓ Phone paired

Try:

    phonebridge status
```

That installation experience matters more than adding another 30 commands. LocalSend's
own zero-configuration pitch — install, open, select the nearby device, send — and its
adoption numbers are the reference point for how much frictionless setup is worth.

## Where AI fits

Not "put AI in it because everything needs AI." Instead: expose PhoneBridge's functions
cleanly enough that AI agents can operate them —
`get_phone_status`/`send_phone_notification`/`transfer_file_to_phone`/`get_latest_photo`/
`ring_phone`/`get_battery` and so on. Then someone using an AI agent on Linux can say:

- "Send the PDF I just created to my phone." → PhoneBridge handles it.
- "Tell me when my phone reaches 90% charge." → PhoneBridge watches the event.
- "When my phone connects to my home Wi-Fi, back up today's photos." → PhoneBridge runs
  the automation.

That's a more interesting category than "yet another phone companion app."

## Three audiences, three layers

Originally the target audience was "Linux power users + Android + terminal." That's too
narrow. The real shape is three layers over the same underlying services:

```text
Normal users        → GUI
Linux enthusiasts    → CLI
Developers/AI        → API
```

```text
┌────────────────────────────────────────────┐
│ PhoneBridge                   Galaxy S22 ● │
├────────────────────────────────────────────┤
│                                            │
│  🔋 76%        💾 31 GB free               │
│                                            │
│  [ Mirror Phone ]   [ Send Files ]         │
│                                            │
│  [ Photos ]         [ Clipboard ]          │
│                                            │
│  [ Use Camera ]     [ Find Phone ]         │
│                                            │
├────────────────────────────────────────────┤
│ AUTOMATIONS                                │
│                                            │
│ ✓ Backup photos when connected             │
│ ✓ Alert PC when battery < 20%              │
│ ○ Sync clipboard                           │
│                                            │
└────────────────────────────────────────────┘
```

Someone doesn't have to be a Linux terminal expert to use PhoneBridge (GUI). Power users
still get `phonebridge status --json` (CLI). Developers get `PhoneBridge("galaxy-s22")`
(API/Python). Eventually AI agents get an API/MCP-style interface over the same services.

**Product principle:** PhoneBridge should make ten separate Android/Linux utilities feel
like one product.

## Roadmap by phase

- **0.1 — Foundation.** `setup`, `status`, `battery`, `ping`, `doctor`.
- **0.2 — Actually useful.** `send`, `receive`, `clipboard`, `notify`, `ring`, `devices`.
- **0.3 — Desktop integration.** Waybar, native notifications, background daemon,
  automatic reconnect, discovery, multiple devices.
- **0.4 — Automation.** `events`, `watch`, `hooks`, plus Python/API integration.
- **0.5 — Ecosystem.** scrcpy integration, plugins, remote connections, agent/API
  integration, polished installers/packages.

Only after people are actually using it does it make sense to start deciding what belongs
in a paid version (see `product-progress-brief.md`'s "Commercial Direction" for current
thinking there).

## Status against this vision

As of `docs/roadmap.md` and `docs/product-progress-brief.md`'s current state, PhoneBridge
already has a real git repository, a typed application-service architecture, and a working
CLI covering most of 0.1–0.3 (status, doctor, send, clipboard, notify, devices, screen,
camera, webcam, audio, find, pairing/profiles, endpoint refresh including a bounded
cold-start scan) plus the very beginning of 0.4 (structured progress events exist for every
operation; `watch`/`events`/hooks and the background daemon itself do not yet). The
"orchestrate, don't rewrite" principle is already the codebase's working default — `screen`,
`camera preview`, `audio`, and `webcam` are all thin, dependency-checked wrappers around
scrcpy, exactly as described above. What's not yet built: one-command `yay`-style
packaging and `phonebridge setup`'s guided pairing UX (0.1's stated hardest problem), the
GUI layer, the Python/API surface (`from phonebridge import Phone`), and everything under
0.4/0.5's automation and ecosystem phases.
