# Contributing

Linkplane is a small alpha project; contributions are welcome and the bar is simple.

- **Python**: 3.11 or newer, standard library only (no third-party runtime dependencies).
- **Tests**: `make test` runs the hermetic suite and must pass without a phone, an ADB
  server, systemd, sudo, or network. Anything that talks to hardware belongs in
  `tests/device/` (run with `make test-device`), never in the normal run. A change to
  behaviour comes with a test.
- **Injected seams**: external tools are reached through runner/factory parameters that
  default to `None` and are resolved at call time, so tests can patch them.
- **Errors** carry stable `LP-` codes (`docs/api-contracts.md`); add a code rather than a
  new prose error, and never remove or renumber one.
- **Identifiers**: use placeholders (`DEVICE_SERIAL`, `192.0.2.10`, `/home/user`) in docs,
  tests and examples. Never commit a real phone serial, address, token, key, or config file.
- **Pull requests**: one focused change, a description that says what and why, and the
  suite green. Documentation changes for user-visible behaviour go in the same PR.

Design context lives in `docs/adr/` and `docs/current-state.md`; read them before changing
the provider, event, or API models.
