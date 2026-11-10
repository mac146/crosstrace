# Stage 20 predictions: our join vs real OBI under interleaved, reused-connection concurrency, with overhead (written BEFORE any code or run)

## Question (from the proposal for this stage)
Does explicit kernel-to-span correlation outperform OBI's available telemetry when request order can no longer reliably identify which connection belongs to which operation?

## What is new relative to Stages 15, 16 and 19 (so nothing is claimed twice)
Stages 16 and 19 already compared our join with OBI at 9 and 24 agents, but in LOCKSTEP (all agents start each call together) and with a NEW connection per call. New here:
agents are NOT synchronised (random start offsets and think times), connections can be REUSED (keep-alive), 8 and 32 agents, top-2 and false attribution are reported, the missing-evidence rate and the
correlation reliability of each system are measured directly, and the CPU and latency overhead of OBI (never measured before) is compared with our compiled collector's under the same concurrent load.

## Scenario
One mock API server (one destination). C threads (agents) of one process, C in {8, 32}. Each agent starts after a random offset U(0,100) ms and makes 8 sequential calls /shipping/1../shipping/8 with random
think time U(0,30) ms between calls; no barriers. Agent kinds interleave (agent i: H, N, S for i mod 3 = 0, 1, 2): H healthy (default source address); N source address 127.0.0.2 with netem latency of 75-250 ms one way on
that address for the whole run (truth net_latency); S source address 127.0.0.3 for which the server delays every answer by 300-1,000 ms (truth slow_api). All three kinds hit the SAME destination at the same time.
Factor reuse: off (a new connection per call, as in Stages 14-19) or on (one keep-alive connection per agent for its 8 calls). Our compiled collector (with the TCP-send probe, needed for reused
connections) and real OBI v1.0.0 (stats mode and printer, same configuration as Stages 15-16) run side by side on the same runs. Calibration: 6 healthy runs (all kind H) at each (C, reuse). Scored runs, fresh seeds:
C = 8: 12 mixed runs (96 agent-runs); C = 32: 6 mixed runs (192 agent-runs), per reuse setting.
Overhead (separate runs, healthy workload so faults do not distort timings): C = 32, reuse on, 10 rounds in random order of four arms: untraced, our collector, OBI, both; per run the collector process CPU (compiled collector or OBI),
the agent process CPU, and the mean call latency.

## Arms
`spans only`; `v3, our join` (the unchanged correlator: process id, thread id, time and connection; NO declared port); `host-wide` (pooled counters over all agents); `OBI, order matching` (what OBI's telemetry allows without
a declared key: server duration matched by server port, path and order; per-connection RTT line matched by order); `OBI, exact port` (control: both pieces matched exactly by the client's source port, which needs the span to declare its
port; here the validation attribute the harness already records).
Measures per agent-run: top-1, top-2 (the injected cause among the first two; "none" for healthy), false attribution (the first-ranked cause points at a component that was healthy; metrics.py definition).
Per call: evidence present (missing-evidence rate) and, for matching, whether the evidence belongs to the call's own connection (correlation reliability; for our join: every attached event carries the call's true client port and at least one is attached).

## Predictions
1. **Our join stays accurate**: top-1 at least 92% in every one of the four (C, reuse) conditions (70%); healthy agents' false attribution at most 5% (75%); top-2 at least 97% (70%).
2. **OBI matched by order breaks down**: top-1 at most 75% in the two no-reuse conditions (65%) and at most 65% with reuse (60%). With reuse OBI prints one RTT line per CONNECTION, fewer than calls, so order matching supplies no per-call RTT.
3. **OBI matched exactly by client port equals our join**: top-1 at least 95% in all four conditions (60%).
4. Host-wide counters at most 70% (80%); spans only at most 50% (85%).
5. **Missing evidence.** Our collector: at least 99% of calls have kernel evidence attached in every condition and no collector warnings (75%). OBI (exact matching): at least 98% of calls have a server-duration line (70%) and at least
   95% have an RTT line, counting calls on a reused connection through their connection's line (55%).
6. **Correlation reliability.** Our join attaches only the call's own connection's events for at least 99% of calls in all conditions (75%). OBI order matching assigns the call's own connection for at most 70% of calls at C = 32 without
   reuse (65%) and at most 40% with reuse (55%).
7. **Overhead (C = 32, reuse on, 10 paired rounds).** OBI's collector-process CPU per run is at least twice that of our compiled collector (60%). Mean call latency relative to untraced: ours within +5% (70%), OBI within +10% (55%).
I would be wrong, and say so, if our join is below 85% in any condition (the claim that thread/process matching survives interleaving and reuse fails), if OBI matched by order reaches 85% in any condition (order is enough after all), or
if OBI matched exactly is below 85% (its telemetry is not sufficient even with a perfect key).

## How the question will be answered (fixed now)
Yes, with the stated limits, if 1 and 2 both hold: our join stays at 92% or more while order-matched OBI is at 75% or less (65% with reuse). Partly, if our join holds and OBI order is above those bounds but below 85%.
No, if order-matched OBI reaches 85%. Whatever the answer, 3 decides what is credited: if OBI with an exact key equals our join (as in Stages 16 and 17), the result is "explicit correlation beats the telemetry OBI offers by itself, and
equals OBI plus a declared key", which is the honest form of the claim. The join needs process and thread ids on spans (our tracer adds them); a stock stack may not.

## Limits stated in advance
Mock services on loopback; kinds separated by source address (a constructed difference); threads of one process (async agents were Stages 17 and 19); one OBI version and configuration tuned by me to attach; faults as before (latency and
a slow server); agent-runs not independent (shared server and runs). Packet loss and DNS are not part of it.
