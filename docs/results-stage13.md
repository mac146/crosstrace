# Stage 13 results: does the per-span join beat plain host-level kernel metrics?

Predictions: [predictions-stage13.md](predictions-stage13.md), committed before any of this was run. Recorded runs only (no new runs, no
root, no API spend): slowapi suite on hard_b and hard_c (30 runs per fault each), pairs on pair_a (8 pairs x 30), LangGraph agent on lg_a
(5 faults x 30). Reproduce: `python -m eval.run_hard --suite slowapi --split hard_b --seed0 0 --no-record --variants base v3 pool poolw`
(pairs: `--suite pairs --split pair_a`; LangGraph: `--split lg_a --cal-split lgcal`).

Arms: `base` spans only; `v3` joined kernel evidence per span (the method); `pool` the same rules fed run-wide host-metric numbers
(max DNS lookup, max RTT, total retransmits, no per-request server wait); `poolw` = `pool` plus the run's maximum server wait (a generous
upper bound: a host counter has no such number, a per-request tracer or a server-side latency metric does).

## Results (top-1; for pairs, "both faults in the top 2")
| | base | v3 (joined) | pool | poolw |
|---|---|---|---|---|
| dns_delay, net_latency (hard_b, hard_c, lg_a: 6 groups of 30) | 13-30% | 100% | 100% | 100% |
| packet_loss (3 groups) | 10-20% | 93-97% | 93-97% | 93-97% |
| **slow_api** (hard_b, hard_c, lg_a) | 27-37% | **100%** | **27-37%** | 100% |
| healthy runs, false alarms | 0/90 | 0/90 | 0/90 | 0/90 |
| pairs, mean "both in top 2" over 8 pairs (includes the known-flawed rate_limit+slow_db pair, 23.3% in every arm; it does not change the comparison) | 28.7% | **88.3%** | **54.6%** | 88.3% |
| pairs, net_latency+slow_model | 23% | 100% | **0%** | 100% |
| pairs, slow_api+slow_db / dns_delay+slow_api | 30% / 10% | 100% / 100% | **30% / 0%** | 100% / 100% |

## What held and what it means
1. Prediction 1 held: on single network faults the run-wide numbers equal the span's numbers; joined and pooled tie at 100%.
2. Prediction 2 held: without a per-request server wait the pooled arm cannot name a slow server and falls to the spans-only level
   (27-37%, exactly `base`); `poolw` ties `v3`. Same on the LangGraph agent (prediction 4).
3. Prediction 3 held in both parts (0% vs 100% on net_latency+slow_model; 54.6% vs 88.3% mean), prediction 5 held (no healthy false alarms
   in any arm, so nothing to separate them).
4. **The part I did not predict, and the honest conclusion: `poolw` matches `v3` on every group.** What `pool` lacked was not the join
   to a span; it was the per-request server wait. The net_latency+slow_model loss of `pool` was also caused by that missing number: the
   model step's own server wait is what clears the network of blame. With the wait pooled in, nothing is left for the join to add on this benchmark.
   So on this benchmark the supported claim is "per-request kernel timing (server wait, DNS, RTT, retransmits) added to agent spans
   diagnoses far better than spans alone, and plain host-level counters do not suffice". It is NOT "the per-span join is what makes the difference".
5. Why the benchmark cannot show the join's value: each fault hits a whole service (every call to the API, or the resolver). When faults are
   global, pooling loses nothing. The join should matter when the same kind of step is hit selectively (one of several hosts, one request in
   fifty), under concurrency, or when many agents share a machine so host counters mix their traffic; none of that is in this benchmark's faults,
   and the pooled arms here were also given only the run's own events (a real host counter is noisier). That is a limit of what I have measured,
   not evidence the join is useless. A selective-fault benchmark is the next experiment to settle it.

## Related work found (3 searches, no systematic review; "not found" is not "does not exist")
- [AgentSight](https://arxiv.org/html/2508.02736v2): eBPF reading of an agent's TLS LLM traffic plus its syscalls, linked by process tree and a
  100-500 ms window, judged by a second LLM; aimed at security and behaviour (prompt injection, loops), three qualitative cases, about 2.9%
  overhead, no diagnosis accuracy, no comparison with agent-only traces, no network faults.
- Beyla / OpenTelemetry eBPF Instrumentation, Pixie, Odigos: eBPF telemetry generators for HTTP/gRPC/SQL; no agent-span joining or scoring found.
- RCAEval and similar microservice RCA benchmarks: injected faults and top-k scoring, but microservices rather than LLM agents and no kernel traces.
- Vendor blogs describing retransmit counters exported as OTel metrics: illustrations, not measurements.
