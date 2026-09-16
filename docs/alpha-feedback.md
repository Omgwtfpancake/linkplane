# Alpha feedback

What external testers have told us, recorded as facts and lessons, without names or
private details. Newest round first. Direction decisions that follow from it live in
[`v0.6-direction.md`](v0.6-direction.md); onboarding measurements live in
[`testing/`](testing/).

## Round 1: first external tester (v0.5.0, Arch Linux / Omarchy)

### Positive

- Installation and `linkplane setup` worked and were fast.
- The core first commands worked: `notify`, `status`, `send`, `events`, `doctor`.
- The "Hello from Linkplane" notification on the phone was the memorable moment: the first
  visible proof that the computer is driving the phone.
- The daemon and reconnect behaviour worked: unplugging and replugging the phone showed up
  as events without restarting anything.
- Rerunning `linkplane setup` was fast and changed nothing.

### Friction, already fixed in v0.5.1

- Setup's completion screen suggested `linkplane send <file>`; typed as a bare
  `linkplane send` it failed with an argument error. Setup now prints a complete,
  copy-pasteable example.
- The install instructions showed Arch and Ubuntu prerequisite commands in one block, which
  led to running `apt` on Arch. They are now one section per operating system.
- `pipx ensurepath` was presented as a required step. It is now advised only when
  `linkplane` is not found after installing.

### Product questions the tester asked

- Does Linkplane live in the system tray?
- Does it launch when the phone connects?
- Why a persistent daemon, and why systemd?
- Does it sync files automatically?
- Why use this instead of ordinary USB file transfer?
- What problem does it solve, and who is it for?
- Is the application / control layer managed from the Linux machine?

Answers as of v0.5.1 (now also in the README):

| Question | Answer today |
|---|---|
| Tray? | No. There is no tray or window yet. |
| Launch on connect? | The service starts at login and is already running; it notices the connection within about a second. Rules in `automations.json` can react to it. Nothing visible happens on connect unless a rule says so. |
| Why daemon / systemd? | Something has to observe the phone and react when no terminal is open. A `systemd --user` unit starts it at login as the user, without root. One-shot commands work without it. |
| Automatic sync? | One-way photo backup on connect is possible with a rule and job tracking, but it is not offered during setup and requires hand-editing JSON. There is no two-way sync. |
| Why not ordinary USB transfer? | For an occasional manual copy, ordinary tools are fine and Linkplane adds little. Linkplane is for repeated work: backups that run by themselves, notifications, scripts and programs reacting to the phone. |
| Who is it for? | Currently Linux developers, power users, homelab/automation users, privacy-focused users and Android tinkerers. |
| Where does it run? | On the Linux computer. The phone needs only USB debugging. |

### Product insight

The tester was right: if Linkplane is just a nicer way to copy a file over USB, normal
Linux tools already solve that. The control-plane foundation works (setup, daemon, events,
rules, jobs, audit, API), but the user-facing value is still manual: every useful thing
happens because the user typed a command. The value has to become **automatic and
obvious**: the phone should do something useful *because* it is connected and observed.

The README now leads with the problem, the audience, what Linkplane is not replacing, and
where it runs, because the tester had to ask all of them.
