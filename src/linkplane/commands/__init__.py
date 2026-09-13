"""CLI command implementations for the v0.1 core commands.

Each module exposes `run(arguments, provider) -> int`. The CLI resolves the provider (the
only place that knows about `--transport`, `--serial`, profiles, and SSH options) and the
command talks to the device purely through the `Provider` interface, so a new provider
never requires touching a command.
"""

from __future__ import annotations

import json
from typing import Any

from linkplane.operations import JSON_SCHEMA_VERSION


def print_envelope(ok: bool, data: dict[str, Any]) -> None:
    print(
        json.dumps(
            {"schema_version": JSON_SCHEMA_VERSION, "ok": ok, "data": data},
            indent=2,
        )
    )
