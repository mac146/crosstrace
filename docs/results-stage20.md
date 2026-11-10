# Stage 20 results: our join vs real OBI under interleaved, reused-connection concurrency, with overhead

Predictions: [predictions-stage20.md](predictions-stage20.md), committed before any code or run. Harness: [eval/obi_vs_join.py](../eval/obi_vs_join.py) (`python -m eval.obi_vs_join`;
`--no-record` re-scores). Threads of one process, NOT synchronised (random start offsets U(0,100) ms and think times U(0,30) ms), one API server, kinds H healthy / N latency on its source address /
S slow server for its source address, 8 calls per agent; connections new per call or one keep-alive connection per agent. Our compiled collector (with the TCP-send probe) and real OBI v1.0.0 ran side
by side. C = 8: 12 mixed runs (96 agent-runs); C = 32: 6 mixed runs (192 agent-runs); 6 healthy calibration runs per condition. A plumbing smoke test ran first (1 run per condition, separate seeds);
it is not part of the results. No crashed agents, no collector warnings.

## Diagnosis (per agent-run; top-1, top-2, false attribution; Wilson intervals treat agent-runs as independent, so they are optimistic)
| condition | arm | top-1 | top-2 | false attribution | H / N / S top-1 |
|---|---|---|---|---|---|
| 8 threads, new connection per call | spans only | 50.0% | 75.0% | 37.5% | 100 / 33 / 0 |
| | **our join** | **100%** [96, 100] | 100% | 0.0% | 100 / 100 / 100 |
| | host-wide counters | 75.0% | 75.0% | 25.0% | 100 / 100 / 0 |
| | **OBI, order matching** | **95.8%** [90, 98] | 100% | 4.2% | 100 / 100 / 83 |
| | OBI, exact client port | 100% | 100% | 0.0% | 100 / 100 / 100 |
| 8 threads, connections reused | spans only | 50.0% | 75.0% | 37.5% | 100 / 33 / 0 |
| | **our join** | **100%** [96, 100] | 100% | 0.0% | 100 / 100 / 100 |
| | host-wide counters | 75.0% | 75.0% | 25.0% | 100 / 100 / 0 |
| | **OBI, order matching** | **67.7%** [58, 76] | 78.1% | 26.0% | 100 / 17 / 96 |
| | OBI, exact client port | 100% | 100% | 0.0% | 100 / 100 / 100 |
| 32 threads, new connection per call | spans only | 46.9% | 68.8% | 34.4% | 100 / 9 / 30 |
| | **our join** | **100%** [98, 100] | 100% | 0.0% | 100 / 100 / 100 |
| | host-wide counters | 68.8% | 68.8% | 31.2% | 100 / 100 / 0 |
| | **OBI, order matching** | **97.4%** [94, 99] | 100% | 2.6% | 100 / 100 / 92 |
| | OBI, exact client port | 100% | 100% | 0.0% | 100 / 100 / 100 |
| 32 threads, connections reused | spans only | 46.9% | 68.8% | 34.4% | 100 / 9 / 30 |
| | **our join** | **100%** [98, 100] | 100% | 0.0% | 100 / 100 / 100 |
| | host-wide counters | 68.8% | 68.8% | 31.2% | 100 / 100 / 0 |
| | **OBI, order matching** | **66.7%** [60, 73] | 81.8% | 21.9% | 100 / 3 / 100 |
| | OBI, exact client port | 100% | 100% | 0.0% | 100 / 100 / 100 |

## Missing evidence and correlation reliability (per call; 768 calls at C = 8, 1,536 at C = 32, per condition)
- **Missing evidence: none.** Our collector had kernel events for 100% of calls; OBI printed a server duration for 100% of calls and a connection RTT line for 100% (matched exactly; with reuse a call uses its connection's line).
- **Our join attached only the call's own connection's events for 100% of calls** in all four conditions.
- **OBI order matching assigned the call's own connection** for 87.5% / 77.0% (C = 8, new / reused) and 85.0% / 83.0% (C = 32) of server durations, but only 35.0% / 0.0% and 30.9% / 0.0% of per-connection RTT lines (with reuse there are fewer lines than calls, so none is assigned).
  The share of calls matched to their own connection was therefore low for RTT, yet diagnosis stayed at 96-97% without reuse (see below).

## Overhead (32 threads, keep-alive, healthy workload, 10 paired rounds, about 1,000 calls per run; the OpenTelemetry spans are in every arm)
| arm | mean call latency | vs none (paired, 95% interval) | agent-process CPU ms/call | collector-process CPU ms/call | added CPU ms/call (agent delta + collector) |
|---|---|---|---|---|---|
| none | 38.44 ms | - | 0.799 | - | - |
| our compiled collector | 38.46 ms | +0.0% [-0.4, +0.4] | 0.810 | 0.090 | 0.100 |
| OBI (stats mode and printer) | 38.50 ms | +0.2% [-0.3, +0.6] | 0.762 | 0.054 | 0.016 |
| both | 38.67 ms | +0.6% [-0.0, +1.1] | 0.900 | 0.104 + 0.055 | 0.259 |
CPU is read in 10 ms ticks, about 0.01 ms per call over this workload, so differences of a few hundredths of a millisecond per call are at the noise level (the agent-process CPU under OBI was lower than under none). The two collectors do
different work: ours records every RTT sample and every TCP send; OBI records HTTP requests and per-connection statistics.

## Predictions against results
1. Our join at least 92% in every condition, healthy false attribution at most 5%, top-2 at least 97%: **held** (100% in all four, false attribution 0%, top-2 100%).
2. OBI order matching at most 75% without reuse and at most 65% with reuse: **wrong without reuse** (95.8% and 97.4%) and **narrowly wrong with reuse** (67.7% and 66.7%).
3. OBI matched exactly at least 95%: **held** (100% in all four).
4. Host-wide at most 70% (**wrong at 8 threads: 75.0%**; held at 32: 68.8%); spans only at most 50% (**held**, 50.0% and 46.9%).
5. Missing evidence (ours at least 99%, no warnings; OBI durations at least 98%, RTT at least 95%): **held** (100% and zero warnings throughout).
6. Reliability: our join at least 99% **held** (100%); OBI order matching's own-connection share at most 70% (no reuse, 32 threads) and at most 40% (reuse): **not met for server durations** (85% and 83%), met for RTT lines (30.9% and 0%).
   I did not say in the pre-registration which of the two my bound meant, which is a flaw in how I wrote it.
7. OBI's collector CPU at least twice ours: **wrong, in the other direction** (OBI process 0.054 vs ours 0.090 ms/call; total added 0.016 vs 0.100, the former within noise). Latency, ours within +5% and OBI within +10%: **held** (+0.0% and +0.2%).
Of the three "I would be wrong if" conditions, one **was triggered**: OBI matched by order reached at least 85% in two conditions (the two without reuse). The other two were not (our join never below 100%; OBI exact never below 100%).

## The pre-registered answer to the question
- **With a new connection per call and unsynchronised agents: no.** Order-matched OBI reached 95.8% and 97.4%, against 100% for our join. Explicit kernel-to-span correlation did not outperform OBI's telemetry here by more than a few points.
- **With reused connections: yes, clearly.** Our join stayed at 100% while order-matched OBI fell to 67.7% and 66.7% (agents with a network fault were not recognised: 17% and 3%), because OBI prints one RTT line per connection, fewer than calls, and order cannot
  assign them. OBI matched exactly by the client port equals our join (100%).
- **So the honest form of the claim** is the one the pre-registration named for outcome 3: explicit correlation beats the telemetry OBI offers by itself when connections are reused or evidence cannot be assigned by order, and equals OBI plus a declared key.

## What this changes in earlier claims
- The Stage 15-16 statement that OBI matched by connection order "breaks down under concurrency" (63-69%) holds only for the lockstep scenarios of Stage 16 (agents calling together, 4 calls each, a new connection per call). With unsynchronised agents making 8 calls
  each and a new connection per call, order matching gave 96-97%. The result depends on the scenario, which the report now says.
- A possible explanation, NOT tested: per-agent diagnosis aggregates 8 calls, and only the slow calls (kinds N and S) use evidence, so wrongly assigned evidence on the many healthy calls does not matter and enough slow calls get roughly right evidence;
  in Stage 16 the lockstep timing made every slow call collide with others. The measured share of calls matched to their own connection was low for RTT (31-35%) while diagnosis stayed high, which is why diagnosis accuracy should not be read as matching accuracy.
- The CPU prediction was wrong: at this load OBI cost no more CPU than our collector. I had not measured OBI's overhead before.

## Limits
Mock services on loopback; threads of one process; kinds separated by source address (a constructed difference); 8 calls per agent; one OBI version and a configuration I tuned to attach; our join needs process and thread ids on spans (our tracer adds them);
the exact-port control needs a declared port (the harness's validation attribute); overhead of one workload (healthy, 32 threads, keep-alive), 10 rounds, 10 ms CPU ticks; agent-runs not independent; faults only latency and a slow server; no packet loss, DNS, async agents or
real providers in this stage.
