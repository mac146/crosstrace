# Stage 18 predictions: many concurrent real agents, a real model, a real network path (written BEFORE any code or run)

Status: **run; results in results-stage18.md.** (Originally: pre-registered before any code or run.) Run it after Stage 17 and only after checking the model account's remaining budget; stop at the first sign of
account or rate-limit trouble (see "Stop rules").

## Why
Everything so far on a real model used one agent at a time (Stages 7, 8, 10, 11: 8-16 runs per group, one NAT'd path). This is the first test of the
whole chain with several real agents at once. It is level 1 of a ladder (level 2: two network namespaces; level 3: two cloud machines); it does not
claim to be a production deployment: one host, one provider, one path.

## Setup
LangGraph agent against the real model (`z-ai/glm-5.3-flash` through OpenRouter, the same as earlier stages; key read in place from the other project's
`.env`, never copied into this repository), tools as before (the mock API and database are local). C = 8 agents per run as threads of one process, started
together. Faults on the real path as in Stage 8/10 (tc on the default interface, scoped to the model server's addresses; cleanup guaranteed): `none`,
`dns_delay`, `net_latency`. The fault affects every agent (a path-level fault cannot be given to one agent only), so agents of one run share a condition.
Calibration: 8 healthy runs at C = 8 (64 agent-runs) for thresholds, then fresh seeds: 8 healthy, 8 `dns_delay`, 8 `net_latency` runs (64 agent-runs each).
The diagnoser is v5 (per-class thresholds, run-level evidence); arms: spans only, v5 joined, v5 on host-wide (pooled) evidence. Compiled collector.
Budget: 256 agent-runs of about 4-5 model calls is roughly 1,200-1,500 calls; at about USD 0.00002 per call (Stage 7: about USD 0.0012 for 60 calls) that is
an estimate of a few cents; the real figure is recorded.

## Stop rules (committed now)
Stop the whole stage if any of these happens: the account reports a spending or credit problem; more than 25% of model calls in a run return an HTTP error
(429 or 5xx) in two consecutive runs; the real-path fault cannot be removed cleanly (the harness guarantees cleanup, and I will check the qdisc afterwards).
Runs with errors are NOT dropped: they are scored as they are and counted.

## Predictions (per agent-run unless stated)
1. At least 90% of agent-runs complete without an HTTP error at C = 8 (65%). If rate limits bite, that is a finding, reported as one.
2. At least 95% of model-call spans carry kernel evidence (the TCP-send probe covers reused connections; Stage 8: 100% single-agent) and the collector
   reports no warnings (75%).
3. Healthy agent-runs: v5 false alarms at most 10% (50%). The weakest prediction: real-network tails are heavy and concurrency adds more.
4. `dns_delay`: v5 top-1 at least 80% (55%). `net_latency`: v5 top-1 at least 70% (50%). (Single-agent Stage 10: 100% and 90%, 10 runs each.)
5. Host-wide pooled evidence is within 10 points of v5 joined on both faults (75%): the fault is the same for every agent, so pooling loses nothing here,
   as in Stage 14's global-fault scenarios. This is a check that I am not claiming a join advantage where there is none.
6. Spans only: at most 20% on both faults (80%), as in Stages 8 and 10.
7. No packet-loss scenario in this stage (the NAT'd path cannot separate loss from background retransmits; Stage 8, 12).
I would be wrong, and say so, if v5 detects fewer than half of either fault (concurrency breaks the real-network result) or if healthy false alarms exceed
25% (the thresholds do not survive concurrency on a real path).

## What would count as a positive result, and what would not
A positive result is v5 at the predicted levels with host-wide evidence close to it: "kernel evidence still detects path-level DNS and latency faults with
8 real concurrent agents; the join is not what carries it". It would not show anything about faults hitting only some agents, about several providers, about
separate hosts, or about production load.
