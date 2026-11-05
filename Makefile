# Run inside WSL/Linux. Override PYTHON to use a venv: make test PYTHON=~/.venvs/crosstrace/bin/python
PYTHON ?= python3

.PHONY: run-healthy test
run-healthy:
	$(PYTHON) scripts/run_healthy.py

test:
	$(PYTHON) -m pytest -q
