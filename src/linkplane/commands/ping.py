from __future__ import annotations

from typing import Any

from linkplane.commands import print_envelope
from linkplane.providers.base import Provider


def run(arguments: Any, provider: Provider) -> int:
    result = provider.ping()
    if arguments.json:
        print_envelope(result.reachable, result.to_dict())
    elif result.reachable:
        print(
            f"Phone reachable via {result.provider} ({result.address})"
            f" in {result.latency_ms:g} ms"
        )
    else:
        detail = f": {result.detail}" if result.detail else ""
        print(f"Phone unreachable via {result.provider} ({result.address}){detail}")
    return 0 if result.reachable else 1
