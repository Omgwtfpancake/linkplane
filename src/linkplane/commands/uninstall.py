"""`linkplane uninstall [--purge]` -- render `linkplane.uninstall`, ask before deleting."""

from __future__ import annotations

import sys
from typing import Any

from linkplane import uninstall as uninstall_module
from linkplane.commands import print_envelope
from linkplane.operations import CancellationToken, cancel_on_interrupt

MARK = {"ok": "✓", "warning": "!", "error": "✗"}


def _confirm(question: str) -> bool:
    print()
    print(question)
    try:
        answer = input("Type 'yes' to continue: ")
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def run(arguments: Any) -> int:
    interactive = sys.stdin.isatty() and not arguments.json
    options = uninstall_module.UninstallOptions(
        purge=arguments.purge,
        force=arguments.force,
        dry_run=arguments.dry_run,
        interactive=interactive,
        unit_dir=arguments.unit_dir,
        socket_path=arguments.socket,
    )
    seams = uninstall_module.UninstallSeams(confirm=_confirm if interactive else None)
    with cancel_on_interrupt(CancellationToken()) as cancel:
        result = uninstall_module.run_uninstall(options, seams, cancel=cancel)
    if arguments.json:
        print_envelope(result.ok, result.to_dict())
        return 0 if result.ok else 1
    print("Linkplane Uninstall" + (" (dry run)" if result.dry_run else ""))
    for step in result.steps:
        print(f"  {MARK.get(step.status, '·')} {step.name:<24} {step.summary}")
        if step.fix and step.status != "ok":
            print(f"    {step.fix}")
        elif step.fix and step.name == "Configuration and state":
            print(f"    {step.fix}")
        if step.code and step.status == "error":
            print(f"    Error: {step.code}")
    print()
    print(result.software_hint)
    return 0 if result.ok else 1
