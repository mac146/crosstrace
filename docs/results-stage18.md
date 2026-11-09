# Stage 18 results: 8 concurrent real agents, a real model, a real network path

Predictions and stop rules: [predictions-stage18.md](predictions-stage18.md), committed before any code or run. Harness: [eval/real_conc.py](../eval/real_conc.py)
(`CROSSTRACE_ENV_FILE=... python -m eval.real_conc`; `--no-record` re-scores recorded runs). LangGraph agents against the real model
(`z-ai/glm-5.3-flash` via OpenRouter; the key was read in place from the other project's `.env`, never copied), 8 agents per run as threads of one process, started
together; one real-path fault per run (the Stage 8/10 injectors: `tc` on the real interface for latency, on the resolver path for DNS), the same for every agent in the run;
compiled collector. Calibration: 8 healthy runs = 64 completed healthy agent-runs. Scored on fresh seeds: 8 runs x 8 agents for each of none, dns_delay, net_latency (192 agent-runs).
No stop rule was triggered; no run was excluded (the fault was on the path actually used in all 24 scored runs); 0 agent errors and 0 model-call HTTP errors in
256 agent-runs; the network settings were restored cleanly (checked after the smoke test and by the harness's own check at every run).

## Results (per agent-run; v5 = the Stage 10 diagnoser for real networks)
| group | runs / agent-runs | spans only | **v5 joined** | v5 on host-wide (pooled) evidence |
|---|---|---|---|---|
| healthy: false alarms | 8 / 64 | 0/64 | **0/64** | 0/64 |
| dns_delay: top-1 | 8 / 64 | 0% [0, 6] | **100%** [94, 100] | 100% [94, 100] |
| net_latency: top-1 | 8 / 64 | 0% [0, 6] | **94%** [85, 98] | 100% [94, 100] |

Intervals treat agent-runs as independent, which they are not (agents of one run share the fault and the schedule); they are optimistic, so the per-run reading is
safer: DNS 8/8 runs fully right; latency 5/8 runs fully right, the other 3 had one agent out of eight wrong each.
Model-call spans with kernel evidence: 662/662 (100%); collector warnings: 0 of 24 runs. Healthy thresholds from the calibration agents under concurrency:
chat step 12.1 s; model-connection DNS 459 ms and median smoothed RTT 120 ms (Stage 10, one agent at a time, was lower, so concurrency raised the detection floor);
retransmits per healthy agent-run up to 10.
API use: 884 model calls, about 400k input and 79k output tokens, about USD 0.02 at the Stage 7/8 price estimate (the recorded token counts are exact, the price is an estimate).

## Predictions against results
1. At least 90% of agent-runs complete without an HTTP error: **held** (256/256, no 429 or 5xx; 8 simultaneous agents did not trigger rate limits on this account).
2. At least 95% of model-call spans with kernel evidence and no collector warnings: **held** (100%, 0 warnings).
3. Healthy false alarms at most 10% (my weakest prediction, 50%): **held, better than predicted** (0/64).
4. dns_delay at least 80% and net_latency at least 70%: **held** (100% and 94%).
5. Host-wide evidence within 10 points of joined: **held** (0 and 6 points). Host-wide was not worse than joined: on latency it was higher (100% vs 94%).
6. Spans only at most 20%: **held** (0%).
Neither "I would be wrong" condition was triggered.

## What this says, and what it does not
- **The chain survives 8 concurrent real agents on a real model and a real network path**, for path-level DNS and latency faults of 0.8-1.5 s on the Stage 8/10 injectors:
  full detection of DNS delay, 94% of latency, no false alarms, complete kernel coverage, no model errors.
- **The join is still not what carries it, and here pooling did at least as well**: the fault is the same for every agent, so pooling the evidence over all 8 agents loses nothing and
  is slightly more robust than each agent's own (3 of 4 joined misses were an agent answering "none", 1 "packet_loss"; the pooled run-level evidence over 8 agents covered them).
  I did not establish why those four agents missed (their own run-level evidence fell under the threshold, or a stray retransmit; not examined further).
  This is consistent with Stages 13-16: path-level faults need per-request kernel evidence, not a per-call join.
- **Detection floor.** Under concurrency the learned thresholds rose (DNS 459 ms, RTT median 120 ms), so smaller faults than these would not be detected; the injected faults were 0.8-1.5 s (latency)
  and 4-8 s (DNS).

## Limits
One model, one provider, one network path (NAT'd WSL), threads of one process on one host, faults that hit every agent equally (a path-level fault cannot be given to one agent only),
8 runs per group, 8 agents per run (so 24 runs behind 192 agent-runs), one afternoon, no packet-loss scenario (not separable on this path, Stages 8 and 12), rate limits did not bite
(so nothing was learned about 429s under concurrency), and the thresholds were calibrated on the same path and load. It is not a production deployment and says nothing about faults
hitting only some agents, several providers, separate hosts or production load.
