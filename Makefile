# Test tiers -- see tests/README.md.
PYTHON ?= python3
export PYTHONPATH := src

.PHONY: test test-integration test-device smoke

# Normal run: unit + integration, no phone needed.
test:
	$(PYTHON) -m unittest discover -s tests -q

test-integration:
	$(PYTHON) -m unittest discover -s tests/integration -t tests -q

# Requires the paired phone. Never part of `make test`.
test-device:
	$(PYTHON) -m unittest discover -s tests/device -t tests/device -v

# The release smoke test alone (docs/smoke-test.md). Requires the paired phone.
smoke:
	$(PYTHON) -m unittest discover -s tests/device -t tests/device -p 'test_release_smoke.py' -v
