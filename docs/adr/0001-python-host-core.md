# 0001 — Python, standard library only, for the host core

**Status:** Accepted (2026-09-10)

## Context

Linkplane's host side has been Python from the first prototype. The Core v0.1 brief
(`docs/core-v0.1-brief.md`) asks for a small, maintainable product that one person can keep
moving, running on one Linux PC against one Android phone, with room to grow into a control
plane later.

## Decision

The host implementation stays in Python (≥ 3.11) with **no runtime dependencies** beyond the
standard library. Process orchestration is `subprocess` with argument arrays; concurrency is
`threading`; persistence is JSON files under `~/.config/linkplane/`; tests are `unittest`.

## Alternatives considered

- **Rust or Go rewrite** for a single static binary. Rejected: nothing in v0.1 is
  performance-bound (every hot path is an external process anyway), and a rewrite would
  discard ~7,600 tested lines for no product gain.
- **Adopting a CLI/async framework** (click, typer, asyncio transports). Rejected for now:
  argparse and synchronous subprocess calls are adequate, and each dependency is a packaging
  liability for the future `yay -S linkplane` goal.

## Consequences

Packaging stays trivial (`pyproject.toml`, setuptools). Anything that needs true streaming or
high concurrency later (event bus, daemon) must be designed as threads over blocking calls,
or justify a dependency in a new ADR.
