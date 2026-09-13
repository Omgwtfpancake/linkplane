# Test tiers

| Tier | Where | Needs | Run |
|---|---|---|---|
| unit | `tests/test_*.py` | nothing external; transports and subprocess are faked | `make test` (or `PYTHONPATH=src python -m unittest discover -s tests`) |
| integration | `tests/integration/` | nothing external; drives `linkplane.cli.main()` across services and providers with fakes at the subprocess/transport seam | included in `make test`; alone: `make test-integration` |
| device | `tests/device/` | the paired phone reachable (ADB, and SSH for the clipboard round trip) | `make test-device` — **never** part of the normal run |
| smoke | `tests/device/test_release_smoke.py` | the paired phone over ADB; the one release procedure (`docs/smoke-test.md`) | `make smoke` |

`tests/device/` deliberately has no `__init__.py`, so `unittest discover -s tests` does not
descend into it; it is only reached by pointing discovery at it directly. Device tests skip
themselves with a clear reason when the phone or a required backend is absent rather than
failing, so a laptop without the phone can still run `make test-device` to see the skips.

`tests/test_contracts.py` (unit) pins the frozen contract surface; a deliberate breaking
change updates its snapshot and bumps `CONTRACT_VERSION` (see docs/api-contracts.md).
