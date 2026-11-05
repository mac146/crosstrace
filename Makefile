# Run inside WSL/Linux. Override PYTHON to use a venv: make test PYTHON=~/.venvs/crosstrace/bin/python
PYTHON ?= python3
N ?= 30

.PHONY: run-healthy test eval-baseline eval-baseline-noroot
run-healthy:
	$(PYTHON) scripts/run_healthy.py

test:
	$(PYTHON) -m pytest -q

# Stage 1 exit command. Needs passwordless `tc` (scripts/setup_tc_sudo.sh) for the netem faults.
eval-baseline:
	$(PYTHON) -m eval.run_baseline --n $(N)

# Same, without the two faults that need root.
eval-baseline-noroot:
	$(PYTHON) -m eval.run_baseline --n $(N) --faults none dns_delay slow_tool slow_db slow_model rate_limit
