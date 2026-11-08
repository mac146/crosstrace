# Stage 14 predictions: what does the join add beyond the strongest cheap baselines? (written BEFORE the runs)

Stage 13 showed host-wide pooled counters plus the run's maximum server wait match the joined method on this benchmark, whose faults hit
a whole service. Here faults are SELECTIVE, and the baselines are the strongest ones a team could build without our join:

Arms (same rules as v3, same healthy thresholds, same recorded runs; only the evidence differs):
- `v3`        joined: each step's own kernel events (the method).
- `pool`      host-wide counters: max DNS, max RTT, total retransmits for the whole run, no per-request wait (Stage 13).
- `pool_port` metrics per destination service: the same counters AND the maximum server wait, but pooled per destination port
              (what per-upstream dashboards give; needs no per-request join, only the span's server.port).
- `obi`       an EMULATION of an OpenTelemetry eBPF auto-instrumentation agent's information content: a per-request server-side
              duration for every call, but no DNS timing, no RTT, no retransmits. It is NOT the real tool (not installed or run here);
              it takes the per-request wait from our own kernel events. Stated so nobody reads it as a measurement of Beyla/OBI.
- `base`      spans only.

Scenarios (4 sequential API calls per run, mock API; seeded magnitudes: slow server 300-1000 ms, latency 75-250 ms one way; 30 healthy
calibration runs, 20 runs per scenario; no thresholds are tuned on scenario runs):
- S0 none.
- S1 selective slow server: one API server, only calls 2 and 4 are answered slowly.
- S2 selective latency: one API server, a latency window (netem) around calls 2 and 4 only.
- S3 mixed on ONE server: latency windows around calls 1 and 3, slow answers on calls 2 and 4. Truth = {net_latency, slow_api}.
- S4 two servers: calls alternate A, B, A, B; A answers slowly, B has a latency window. Truth = {slow_api, net_latency}.

Predictions (top-1 for S0-S2, "both truths in the top 2" for S3-S4):
1. S0: no arm false-alarms more than 1 of 20 (85%).
2. S1: `v3`, `pool_port`, `obi` at least 90% top-1; `pool` below 50%. (80%)
3. S2: `v3`, `pool`, `pool_port` at least 90%; `obi` below 50%, because it cannot see RTT. (80%)
4. **S3: `v3` at least 85% both-in-top-2; `pool`, `pool_port` at most 30%** (every slow step sees both a high RTT and a high wait from the
   same destination, and the rules put the excess on net_latency, so slow_api never ranks); `obi` at most 50%. (65%: the join's one real
   advantage, and the one where I could be wrong about the join itself, since window timing could confuse it.)
5. **S4: `pool_port` ties `v3` (within 10 points, both at least 85%)**, `pool` at most 30%. (70%) This is the point of the stage: if
   per-destination metrics already separate the faults, the join's advantage is limited to same-destination, same-run mixtures.

Overall belief: the join adds a measurable edge over the strongest cheap baseline only in S3-type situations (different problems at the
same destination in one run, or concurrency that mixes requests); everywhere else per-destination metrics with per-request wait suffice. 60%.
I would be wrong, and say so, if `pool_port` matches `v3` in S3 (the join adds nothing even here) or if `v3` itself is below 70% in S3.

## Part 2: the same comparison under concurrency (written BEFORE the code or the runs)
Setup: C agents (threads of one process, each with its own trace) run the mixed scenario at the same moment against one API server,
in lockstep: before call i every agent waits at a barrier, the latency window (netem on the server port) is opened for calls 1 and 3 and
closed for 2 and 4, the server answers calls 2 and 4 slowly. So every agent has the truth {net_latency, slow_api}. A netem window is per
port, so agents cannot be given different network faults at the same time; that is a limit of the injector, stated here. One collector
records everything. Each agent is diagnosed on its own spans. Host-wide arms (`pool`, `pool_port`, `obi`'s evidence) are built from ALL
agents' HTTP spans, as a shared host's counters would be. Healthy thresholds are calibrated at the same C (12 healthy runs), and 6 more
healthy runs at that C measure false alarms. C = 1 (control), 8, 32; 20 mixed runs at C = 1 and 8, 10 at C = 32.
6. **The join survives**: `v3` finds both causes (both in top 2) for at least 90% of agents at C = 8 (75%) and at least 80% at C = 32 (60%).
   What can break it: the join misattributing events between simultaneous threads, or the collector losing events at 32 connections at once.
7. `pool`, `pool_port` and `obi` stay at or below 30% at every C (85%): they fail for the same structural reason as at C = 1, so
   concurrency cannot make them worse than 0%. Concurrency therefore does NOT widen the gap this experiment can show; it tests whether the join
   keeps its side of it.
8. Healthy runs at C = 8: at most 5% of agents get a false alarm from `v3`; at C = 32 at most 10% (55%): load makes healthy steps slower, and
   calibrating at the same C only partly absorbs that.
I would be wrong if `v3` falls below 80% at C = 8, or loses events (collector warnings) at C = 32.
