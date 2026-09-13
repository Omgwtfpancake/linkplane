"""`linkplane setup` -- render the guided flow, ask the few questions it has, print the
result. All decisions live in `linkplane.setup`; this module only talks to the human."""

from __future__ import annotations

import sys
from typing import Any

from linkplane import setup as setup_module
from linkplane.commands import print_envelope
from linkplane.operations import CancellationToken, ProgressEvent, cancel_on_interrupt

MARK = {"ok": "✓", "warning": "!", "skipped": "·", "error": "✗"}
SECTION = {
    setup_module.PREFLIGHT: "Checking this computer...",
    setup_module.DEVICE_DETECTION: "Connect your Android phone with USB.",
    setup_module.DAEMON_INSTALLATION: "Setting up Linkplane...",
    setup_module.FIRST_USE_VERIFICATION: "Verifying connection...",
    setup_module.API_CLIENT: "API client...",
}


def _interactive(arguments: Any) -> bool:
    return not arguments.non_interactive and sys.stdin.isatty()


def _ask(question: str, default: str) -> str:
    try:
        answer = input(f"{question} [{default}]: ")
    except EOFError:
        return default
    return answer.strip() or default


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _choose(question: str, choices: list[str]) -> int | None:
    print(question)
    for index, choice in enumerate(choices, start=1):
        print(f"  {index}. {choice}")
    try:
        answer = input(f"Choose 1-{len(choices)} (or Enter to cancel): ").strip()
    except EOFError:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(choices):
        return int(answer) - 1
    return None


class _Renderer:
    def __init__(self) -> None:
        self.section: str | None = None
        self.waiting_shown = False

    def __call__(self, event: ProgressEvent) -> None:
        heading = SECTION.get(event.phase)
        if heading and heading != self.section:
            self.section = heading
            print()
            print(heading)
        if "guidance" in event.details:
            print()
            for line in event.details["guidance"]:
                print(f"  {line}")
            print()
            if event.phase in (setup_module.DEVICE_DETECTION, setup_module.DEVICE_AUTHORIZATION):
                print("  Waiting for your phone... (Ctrl+C to stop)")
            return
        step = event.details.get("step")
        if not step:
            return
        mark = MARK.get(step["status"], "·")
        line = f"  {mark} {step['name']:<26} {step['summary']}"
        print(line)
        if step["status"] in {"warning", "skipped"} and step.get("fix"):
            print(f"    {step['fix']}")


def _elapsed(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def run(arguments: Any) -> int:
    daemon_arguments: list[str] = []
    if getattr(arguments, "api_port", None) is not None:
        daemon_arguments += ["--api-port", str(arguments.api_port)]
    if getattr(arguments, "no_api", False):
        daemon_arguments.append("--no-api")
    interactive = _interactive(arguments)
    options = setup_module.SetupOptions(
        config_path=arguments.config,
        name=arguments.name,
        serial=arguments.serial,
        install_daemon=not arguments.no_daemon,
        unit_dir=arguments.unit_dir,
        socket_path=arguments.socket,
        daemon_arguments=tuple(daemon_arguments),
        api_client=arguments.api_client,
        dry_run=arguments.dry_run,
        interactive=interactive,
    )
    seams = setup_module.SetupSeams(
        ask=_ask if interactive else None,
        confirm=_confirm if interactive else None,
        choose=_choose if interactive else None,
    )
    renderer = None if arguments.json else _Renderer()
    if renderer is not None:
        print("Linkplane Setup")
        print("Make real devices programmable.")
        if arguments.dry_run:
            print("(dry run: nothing will be written)")
    with cancel_on_interrupt(CancellationToken()) as cancel:
        result = setup_module.run_setup(options, seams, progress=renderer, cancel=cancel)

    if arguments.json:
        print_envelope(result.ok, result.to_dict())
        return 0 if result.ok else 1

    if not result.ok:
        failure = result.failure
        print()
        if failure is not None:
            print(f"Setup stopped: {failure.check.summary}")
            if failure.check.fix:
                print(f"  Next step: {failure.check.fix}")
            if failure.check.code:
                print(f"  Error: {failure.check.code}")
        print("  Nothing that was already working has been changed. Run `linkplane setup` again after fixing this.")
        print("  For deeper diagnostics: linkplane doctor")
        return 1

    print()
    if result.dry_run:
        print("Dry run complete; nothing was written.")
    else:
        print(f"Your phone is ready.  (setup took {_elapsed(result.timing.elapsed_seconds)})")
    if result.api_client and result.api_client.get("token"):
        print()
        print(f"API client  {result.api_client['client_id']} ({', '.join(result.api_client['scopes'])})")
        print(f"Token       {result.api_client['token']}")
        print("            shown once; only its hash is stored")
    print()
    print("Try:")
    for command in result.next_steps:
        print(f"  {command}")
    print()
    print(setup_module.ADVANCED_HINT)
    return 0
