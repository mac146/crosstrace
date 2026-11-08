# Stage 13 predictions: does the per-span join beat plain host-level kernel metrics? (written BEFORE the runs)

Why: the literature check (README, "related work") found tools that emit eBPF telemetry (Beyla/OBI, Pixie) and AgentSight (security
focus), but no measured comparison against the obvious cheaper alternative: **an ordinary metrics stack**, i.e. the agent's spans plus
host-level kernel counters for the run (total retransmits, DNS lookup latency, RTT) with NO attribution to a particular span.
If that does as well as our join, the join is not worth its complexity. This stage tests that, on runs ALREADY recorded
(no new runs, no root, no API spend), so nothing can be tuned to the result.

## Arms (same rules as v3, same thresholds, same runs; only the evidence differs)
- `v3`   joined: each HTTP step uses the kernel events the correlator attached to its own span (the current method).
- `pool` host metrics: every HTTP step gets the same run-wide numbers: maximum DNS lookup, maximum smoothed RTT, total retransmits.
         No per-request server wait (a host metric does not have one), so `slow_api` cannot fire.
- `poolw` the generous version: as `pool` plus the run's maximum server wait (not available from real host metrics; an upper bound
         on what a metrics stack could do).
Pooled evidence uses only events the correlator attached to the run's own spans, so it never sees foreign traffic: that favours the
pooled arms (a real host counter also counts other processes). Stated so the comparison errs in the baseline's favour.

## Data (all recorded earlier; calibration splits as in the original evaluations)
slowapi suite: hard_b and hard_c (fresh when they were run), 30 runs per fault; pairs suite: pair_a (8 pairs x 30), calibrated on hardcal;
LangGraph agent: lg_a (5 faults x 30), calibrated on lgcal.

## Predictions
1. **Single network faults** (dns_delay, net_latency, packet_loss in hard_b/hard_c): `pool` is within 5 percentage points of `v3`
   on top-1. With one fault and one slow step the run-wide number is the span's number. Confidence 80%.
2. **slow_api** (server slow, network healthy): `pool` falls to roughly chance (below 50% top-1) while `v3` stays above 90%, because
   naming a slow server needs the per-request wait. `poolw` is within 5 points of `v3`. Confidence 85% for the pool/v3 gap.
3. **Pairs**: the join matters where two faults sit on different paths. In `net_latency+slow_model` the pooled RTT (from the API
   connection) is blamed on the slow model call, so `pool` loses at least 20 points of "both in top 2" against `v3` on that pair
   (70%). Averaged over the 8 pairs `v3` beats `pool` by at least 10 points (65%).
4. **LangGraph runs** (lg_a): same pattern as 1 and 2 (single faults tie, slow_api separates). 70%.
5. **Healthy runs**: false alarms of `pool` are no lower than `v3`'s (pooled maxima can only raise the evidence). 75%.

Overall belief: the join's value is concentrated in (a) naming a slow server and (b) runs where faults are on different paths;
on single network faults it adds nothing over host metrics. 65%.

I would be wrong (and say so) if `pool` matches `v3` on slow_api and on pairs: then the per-span join is not earning its cost on this
benchmark, and the README would need to say it.
