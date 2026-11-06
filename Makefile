# Run inside WSL/Linux. Override PYTHON to use a venv: make test PYTHON=~/.venvs/crosstrace/bin/python
PYTHON ?= python3
N ?= 30

.PHONY: run-healthy test eval-baseline eval-baseline-noroot
run-healthy:
	$(PYTHON) scripts/run_healthy.py

test:
	$(PYTHON) -m pytest -q

# Stage 1 exit command. Needs passwordless `tc` (scripts/setup_sudoers.sh) for the netem faults.
eval-baseline:
	$(PYTHON) -m eval.run_baseline --n $(N)

# Same, without the two faults that need root.
eval-baseline-noroot:
	$(PYTHON) -m eval.run_baseline --n $(N) --faults none dns_delay slow_tool slow_db slow_model rate_limit

# Stage 3 exit check: join spans with kernel events, print timelines, run join integrity checks.
# Needs passwordless sudo for bpftrace and tc (scripts/setup_sudoers.sh).
.PHONY: stage2-check stage3-check
stage2-check:
	$(PYTHON) -m eval.stage2_check --k 5
stage3-check:
	$(PYTHON) -m eval.stage3_check --k 20

# Stage 4. Need passwordless sudo for bpftrace/tc (scripts/setup_sudoers.sh). ~15 min each for the eval-full targets.
.PHONY: eval-full eval-full-fresh overhead
eval-full:
	$(PYTHON) -m eval.run_full --n 30 --cal 30 --split full --seed0 1000
eval-full-fresh:
	$(PYTHON) -m eval.run_full --n 30 --cal 30 --split full2 --seed0 2000
overhead:
	$(PYTHON) -m eval.overhead --rounds 12
