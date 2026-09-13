# 0002 — Providers implement capabilities; nothing above them knows the transport

**Status:** Accepted (2026-09-10)

## Context

Before v0.1 the CLI and services reached into `AdbTransport` and `SshTransport` directly
and were duck-typed on `.status()`. The brief's central rule is *capability ≠
implementation*: the public model is `device.battery()`, never `ssh_get_battery()`, so a
future Android agent can replace SSH without the CLI changing.

## Decision

`linkplane.providers.base.Provider` is an abstract class with `address`, `ping()`,
`status()`, `battery()`, and `capabilities()`. A provider wraps exactly one way of reaching
one device endpoint. Errors leave a provider only as `LinkplaneError` (ADR 0005).
`providers.select_provider()` is the single place the CLI turns `--transport`, `--serial`,
and profile/SSH configuration into a provider, preserving the long-standing
`auto | adb | ssh` semantics (ADB first, SSH fallback, explicit choice honoured exactly).
Core commands (`commands/`) receive a `Provider` and never import a transport.

## Alternatives considered

- **A `Protocol` instead of an ABC.** Rejected: the ABC gives a place for shared helpers
  (`describe()`) and makes "forgot to implement `battery`" a construction-time error.
- **One provider per capability** (BatteryProvider, PingProvider…). Rejected for v0.1 as
  speculative; the per-endpoint provider can be split later without changing callers.
- **Migrating every existing service (send, backup, …) onto providers now.** Rejected:
  the brief says refactor only to establish the boundary. Existing services keep calling
  transports; they are reported as capabilities and can migrate one at a time.

## Status update (2026-09-10, later)

`status` now goes through the providers too (`read_status` builds `ADBProvider` /
`SSHProvider` and calls `Provider.status()`), so every core command — `status`, `battery`,
`ping`, `capabilities`, and `doctor`'s provider checks — reaches the phone only through
the interface. `read_status` keeps its own auto/fallback sequencing rather than calling
`select_provider()`, because its fallback triggers on a failed *status read*, not merely on
"no ADB device" — preserving the pre-provider behaviour exactly. The status models live in
`core/telemetry.py` (no provider or transport imports) to avoid an import cycle. The
interface (`address`, `ping`, `status`, `battery`, `capabilities`) is pinned in
`tests/test_contracts.py`.

## Consequences

New capability = new abstract method plus one implementation per provider, plus one
`CapabilityReport`. The contract tests in `tests/test_providers.py` (`ProviderContract`)
must pass for every provider, so a new provider is one subclass supplying fakes.
