# Run inside WSL/Linux (see README.md). Targets marked ROOT need passwordless sudo for tc and bpftrace
# (scripts/setup_sudoers.sh, once) and change network settings on the loopback interface: dev WSL/VM only.
# pipefail: `make reproduce` pipes through tee, which otherwise hides a failing step (exit code 0 although it failed)
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c
VENV ?= $(HOME)/.venvs/crosstrace
PYTHON ?= $(VENV)/bin/python
N ?= 30
REPRO_DIR ?= $(HOME)/.crosstrace-repro

.PHONY: venv test run-healthy
venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -q

run-healthy:
	$(PYTHON) scripts/run_healthy.py

# --- Stage 1: baseline only (agent spans). ROOT for the two netem faults; -noroot skips them. ---
.PHONY: eval-baseline eval-baseline-noroot
eval-baseline:
	$(PYTHON) -m eval.run_baseline --n $(N)
eval-baseline-noroot:
	$(PYTHON) -m eval.run_baseline --n $(N) --faults none dns_delay slow_tool slow_db slow_model rate_limit

# --- Stages 2-3 checks. ROOT. ---
.PHONY: stage2-check stage3-check
stage2-check:
	$(PYTHON) -m eval.stage2_check --k 5
stage3-check:
	$(PYTHON) -m eval.stage3_check --k 20

# --- Stage 4. ROOT. About 15 minutes each for the two eval targets. ---
.PHONY: eval-full eval-full-fresh overhead
eval-full:
	$(PYTHON) -m eval.run_full --n 30 --cal 30 --split full --seed0 1000
eval-full-fresh:
	$(PYTHON) -m eval.run_full --n 30 --cal 30 --split full2 --seed0 2000
overhead:
	$(PYTHON) -m eval.overhead --rounds 12

# --- Stage 6: harder evaluations. ROOT. Seeds and splits are fixed so reruns are comparable. ~10-15 min each. ---
# slowapi-a: v2 only (before the slow_api rule existed); slowapi-b: fresh seeds, all variants.
.PHONY: eval-slowapi-a eval-slowapi-b eval-subtle eval-pairs venv-real eval-real
eval-slowapi-a:
	$(PYTHON) -m eval.run_hard --suite slowapi --split hard_a --seed0 3000 --variants base v2
eval-slowapi-b:
	$(PYTHON) -m eval.run_hard --suite slowapi --split hard_b --seed0 4000 --variants base v2 v3
eval-subtle:
	$(PYTHON) -m eval.run_hard --suite subtle --split sub_a --seed0 5000 --variants base v3
eval-pairs:
	$(PYTHON) -m eval.run_hard --suite pairs --split pair_a --seed0 7000 --variants base v3

# --- Stage 6: real-model realism check. Needs an API key (see eval/real_model.py) and costs a few cents. ---
venv-real:
	$(VENV)/bin/pip install -q -r requirements-real.txt
eval-real:
	$(PYTHON) -m eval.real_model --cal 12 --n 8 --variants base v3

# --- Stage 6 extras ---
# eval-load: subtle faults with all cores busy (needs ~15 min). eval-langgraph: a third-party agent framework (LangGraph)
# against the local mock model, ~15 min, needs `make venv-real`. reproduce-stage6 runs every Stage 6 suite (~2 hours).
.PHONY: eval-load eval-langgraph reproduce-stage6
eval-load:
	$(PYTHON) -m eval.run_hard --suite subtle --split sub_load --seed0 6000 --variants base v3 --load 10 --cal-split hardcal_load
eval-langgraph:
	$(PYTHON) -m eval.run_hard --suite slowapi --split lg_a --seed0 9000 --variants base v3 --llm-kind langgraph-mock --cal-split lgcal
reproduce-stage6:
	rm -rf $(REPRO_DIR)-s6 && mkdir -p $(REPRO_DIR)-s6
	( for t in eval-slowapi-a eval-slowapi-b eval-subtle eval-pairs eval-load eval-langgraph; do 	    CROSSTRACE_DATA=$(REPRO_DIR)-s6 $(MAKE) $$t || exit 1; done ) 2>&1 | tee $(REPRO_DIR)-s6/report.txt

# --- Stage 8-9 ---
# eval-real-faults: faults on the REAL network path to a real model (ROOT; touches eth0 and lo, see faultlab/real_path.py).
#   Needs CROSSTRACE_ENV_FILE=<env file with OPENROUTER_API_KEY> and `make venv-real`; costs about a cent.
# eval-concurrency: the join with up to 512 agent threads (ROOT, ~1 minute).
.PHONY: eval-real-faults eval-concurrency
eval-real-faults:
	$(PYTHON) -m eval.real_faults --n 10 --cal 16 --cal-split realcal3 --cal-extra realcal2 --split rf_b --seed0 12000 --variants base v3 v4 v5
eval-concurrency:
	$(PYTHON) -m eval.concurrency --configs 1x1 1x8 1x32 4x8 8x8 8x16 8x32 16x16 16x32 --total 160

# --- The whole experiment from an empty database, one command (ROOT, about 40 minutes, keep the machine idle). ---
# Fault parameters are seeded and reproduce exactly; timings and therefore exact percentages vary a little from
# run to run (see README, "Reproducibility"). The full report is also saved to $(REPRO_DIR)/report.txt.
.PHONY: reproduce
reproduce:
	rm -rf $(REPRO_DIR) && mkdir -p $(REPRO_DIR)
	( $(MAKE) test && \
	  CROSSTRACE_DATA=$(REPRO_DIR) $(MAKE) eval-full && \
	  CROSSTRACE_DATA=$(REPRO_DIR) $(MAKE) eval-full-fresh && \
	  $(MAKE) overhead ) 2>&1 | tee $(REPRO_DIR)/report.txt
