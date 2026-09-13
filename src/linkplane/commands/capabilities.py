from __future__ import annotations

from typing import Any

from linkplane.commands import print_envelope
from linkplane.core.capability import (
    COMMANDS,
    PERMISSION_DENIED,
    PROVIDER_ERROR,
    SUPPORTED,
    UNAVAILABLE,
    UNSUPPORTED,
    CapabilityReport,
)
from linkplane.providers.base import Provider

MARKS = {
    SUPPORTED: "✓",
    UNSUPPORTED: "✗",
    UNAVAILABLE: "○",
    PERMISSION_DENIED: "!",
    PROVIDER_ERROR: "?",
}


def render(reports: tuple[CapabilityReport, ...], provider: Provider) -> None:
    print("Linkplane Capabilities")
    print(f"Provider    {provider.name} ({provider.address})")
    print()
    width = max(len(report.name) for report in reports) + 2
    for report in reports:
        mark = MARKS[report.status]
        line = f"{report.name.ljust(width)}{mark}  {report.status}"
        if report.status != SUPPORTED and report.detail:
            line += f"  — {report.detail}"
        elif report.status == SUPPORTED and report.name in COMMANDS:
            line += f"  (linkplane {COMMANDS[report.name]})"
        print(line)
    print()
    print("✓ supported   ✗ unsupported by this provider   ○ unavailable right now")
    print("! permission denied   ? could not be determined")


def run(arguments: Any, provider: Provider) -> int:
    reports = provider.capabilities()
    if arguments.json:
        print_envelope(
            True,
            {
                "provider": provider.name,
                "address": provider.address,
                "capabilities": [report.to_dict() for report in reports],
            },
        )
    else:
        render(reports, provider)
    return 0
