# Stage 6 results: harder tests, a loaded machine, a third-party framework, overhead work

All numbers are console output on WSL2 (kernel 6.6.87.2), loopback, mock services unless stated, 2026-11-06 to 2026-11-08.
95% Wilson intervals where shown. Diagnosers: **base** = spans only; **v2** = Stage 4 combined diagnoser; **v3** = v2 plus one
rule (slow server), written after the first slow-API suite and **frozen before every later suite**. Predictions for each
suite were committed to git before the run: `predictions-stage6.md`, `-6b.md`, `-6c.md`. Seeds and splits are fixed in the
Makefile (`make reproduce-stage6`).

## 1. A slow API server with a healthy network (new fault `slow_api`)
v2 has no way to name a slow server. Predicted 0% / 100% false attribution; observed exactly that.

| slow_api, 30 runs | top-1 | blamed the healthy network |
|---|---|---|
| base, set A (seeds 3000s) / set B (4000s) | 26.7% / 36.7% | 73.3% / 63.3% |
| v2, A / B | 0% / 0% | 100% / 100% |
| **v3, B (fresh seeds, rule written after A)** | **100% [89-100]** | **0%** |

The v3 rule: server wait above its healthy threshold while DNS, RTT and retransmits are all healthy. The network faults
(dns_delay 100%, net_latency 100%, packet_loss 93%) and healthy runs (0/30 false alarms) were unchanged.

## 2. Subtle faults, 20-100 ms (v3 vs base, 30 runs per fault, 60 healthy runs)
Two threshold rules: **coarse** (Stage 1-5: `max(p99, 3x median) + 50 ms`) and **tight** (`p99 + 5 ms`).

| top-1 | coarse base | coarse v3 | tight base | tight v3 |
|---|---|---|---|---|
| dns_delay | 17% | 63% | 33% | 100% |
| net_latency | 13% | 70% | 17% | 100% |
| slow_api | 3% | 50% | 20% | 100% |
| slow_tool | 0% | 0% | 100% | 100% |
| slow_model | 0% | 0% | 100% | 100% |
| slow_db | 83% | 83% | 100% | 100% |
| healthy false alarms | 0/60 | 0/60 | 0/60 | 0/60 |

Two of my predictions were wrong: I expected the tight rule to false-alarm on 3-30% of healthy runs and that no rule would
give both high detection and zero false alarms. On an idle machine it gave both. The coarse rule is blind below its
50 ms margin (slow_tool and slow_model at 0%), as predicted.

## 3. Two faults at once (8 pairs x 30; success = both injected faults are the two top-ranked causes)
| pair | base | v3 |
|---|---|---|
| dns_delay + slow_db | 30% | 100% |
| net_latency + slow_model | 23% | 100% |
| slow_api + slow_db | 30% | 100% |
| dns_delay + slow_api (same step) | 10% | 100% |
| dns_delay + packet_loss (same step) | 10% | 93% |
| packet_loss + slow_tool | 3% | 90% |
| slow_model + slow_tool | 100% | 100% |
| rate_limit + slow_db | 23% -> **73%** after the fix below | 23% -> **73%** |

False attribution for v3: 0% on every pair; base 13% on dns_delay+packet_loss only.
* **A flaw in my test, found and fixed.** In the first run `rate_limit+slow_db` was invalid: the DB lock started with the run
  and expired during the 429 backoff, before the query. The lock now starts at the first query. After the fix both
  diagnosers find both causes in 73% of runs.
* **A real v3 weakness (not fixed, v3 frozen).** On a 429 the rule adds `slow_model` at half the retry delay as a hedge; it
  outranks a real slow database in 8 of the 30 runs (`rate_limit` first, `slow_model` second, `slow_db` pushed to third; the
  other 22 runs found both). A v4 would attribute the retry delay entirely to `rate_limit`; it would need fresh seeds.
* Same-step pairs did better than I predicted (60-80%); different-step pairs matched the prediction (at least 80%).

## 4. A busy machine (subtle suite, all 10 cores saturated by busy loops)
| tight rule | healthy false alarms | v3 top-1 dns / latency / slow_api |
|---|---|---|
| calibrated under the same load | 0/60 | 100% / 100% / 100% |
| calibrated on the idle machine, used under load | **13/60 (22%)** | 100% / 100% / 100% |
| coarse rule (either) | 0/60 | 60-73% / 60-67% / 50-70% |

Predictions: idle-calibrated false alarms at least 30% (observed 22%: right direction, smaller); load-calibrated 3-25% with
weaker detection (observed 0% and none: wrong). Busy-loop contention is mild for a mostly-sleeping agent: the HTTP-step
threshold only went from 10 ms to 21 ms. Noisy neighbours, disk contention and real network jitter are not tested. I did not
check whether the collector dropped events under load. **Calibrate thresholds where you deploy.**

## 5. A third-party agent framework: LangGraph 1.2 (`create_react_agent`) against the local mock model
Spans wrap the model call and each tool in the framework's own threads; the diagnosers and correlator know nothing about it.
Slow-API suite, 30 per fault, base vs v3, thresholds calibrated on LangGraph healthy runs:

| | base top-1 | v3 top-1 |
|---|---|---|
| slow_api | 26.7% | 100% |
| dns_delay | 30.0% | 100% |
| net_latency | 13.3% | 100% |
| packet_loss | 20.0% | 96.7% |
| healthy false alarms | 0/30 | 0/30 |

* Spans stayed children of `invoke_agent` across LangGraph's worker threads, and tool spans ran on different thread ids
  (for example 516, 518, 522) while the join still attached DNS and connect events to the right span.
* Coverage: 100% of 596 model-call spans and 150 API-tool spans had connect evidence and RTT samples; no pid-pinned event
  was left unattached.
* Predictions: 1 (parenting works), 2 (at least 95% attached), 4 (accuracy at the mock-agent level), 6 (no false alarms)
  held. **3 (connection reuse costs evidence) is inconclusive**: the mock server closes every connection, so the client
  reconnected each time. **5 (hidden client retries) was not tested.** 2 of the 150 runs errored and are scored as is.
* Limits: the model was the mock, over plain HTTP; instrumentation was written by me with knowledge of the join; the
  `create_react_agent` entry point is deprecated in LangGraph 1.x in favour of `langchain.agents.create_agent`.

## 6. A real model and the real internet: NOT done
The OpenCode key listed models but could not generate: paid models returned 402 (no credit) and free models returned 429
("free models work in OpenCode"), so no tokens were spent. Two failed single runs still showed, with n = 2 and failed model
calls only:
* the kernel sees the TLS connection to port 443 (connect in 4-7 ms) and its RTT samples; contents are invisible, as expected;
* **DNS lookups of the real hostname were not captured**: the probe watches how my own resolver client sends queries, not libc;
* **a TCP retransmit appeared on a healthy real connection**, which the "latent loss" rule would count as packet loss.

These are observations, not a test. A real-model run needs credit or another key (`make eval-real` for the Anthropic adapter;
`agent/langgraph_agent.py` for an OpenAI-compatible endpoint).

## 7. Overhead work (agent workload, 12 rounds, A/A-controlled, 3 sessions)
Per-run overhead versus the same round's untraced block; the range is across sessions (Stage 4's session read +7.5% for OTel).

| arm | latency | main-thread CPU | bpftrace events/s |
|---|---|---|---|
| OTel, new provider per run (original) | -0.3% to +2.3% | +20% to +36% | - |
| OTel, one shared provider | +0.6% to +3.5% | +26% to +44% | - |
| + eBPF, all TCP | +2.3% to +5.2% | +44% to +58% (process: +97% to +115%) | 890-990 |
| + eBPF, ports filtered, pure-ACK RTT dropped | +4.7% to +7.8% | +68% to +95% | 480-500 |

* **Hypothesis rejected:** a shared provider did not cut OTel overhead (the profile showed provider creation costs ~0.3 ms per
  run, but the benchmark could not see a gain). **Filtering halved the events and did not reduce bpftrace CPU (about 3 ms per
  run either way) and was slightly worse on latency in 4 of 4 sessions;** I have no explanation.
* **Latency target (under 5%) is met by OTel alone and borderline with eBPF; the CPU target is not met.** OTel adds roughly
  1.3 ms of CPU per 60 ms run. A real fix likely needs a compiled collector (libbpf) and out-of-process parsing.
* Between-session spread (0.6% to 7.5% for one configuration) is larger than the within-session intervals, so any single
  overhead number from this repo should be read as a range.

## What is still untested
A real model and TLS end to end; real network faults; real DNS resolution paths; production load; an agent whose spans I did
not write; concurrency beyond a few threads; three or more faults; faults outside the catalogue.
