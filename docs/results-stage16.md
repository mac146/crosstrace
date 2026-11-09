# Stage 16 results: OBI plus matching against our join, with concurrent agents in different conditions

Predictions: [predictions-stage16.md](predictions-stage16.md), committed before any code or run. Harness: [eval/obi_conc.py](../eval/obi_conc.py)
(`python -m eval.obi_conc`; `--no-record` re-scores recorded runs). Real OBI v1.0.0 (stats mode and printer) ran next to our collector (same install and
checks as Stage 15; signature not verified). One API server, C agents in lockstep, every agent requesting /shipping/1../shipping/4; agent kinds
interleave: H healthy, N source address 127.0.0.2 with netem latency on that address, S source address 127.0.0.3 with the server delaying answers to that
address. Each agent is scored against its own truth (top-1; H: none, N: net_latency, S: slow_api). C = 9 (12 runs, 108 agent-runs) and C = 24
(8 runs, 192 agent-runs), healthy calibration at the same C (8 runs). OBI evidence was available for every call by exact port (864/864 and 1536/1536
pairs); no crashed agents and no collector warnings.

## Results (per-agent top-1)
| C | arm | overall | H (none) | N (net_latency) | S (slow_api) |
|---|---|---|---|---|---|
| 9 | spans only | 44.4% | 100% | 33.3% | 0% |
| 9 | **v3 (our join)** | **100%** [97, 100] | 100% | 100% | 100% |
| 9 | pool / pool_port (host-wide) | 61.1% | 100% | 83.3% | 0% |
| 9 | **obi_real+flows (order matching)** | **69.4%** [60, 77] | 100% | 58.3% | 50.0% |
| 9 | **obi_real+port (exact, client port)** | **100%** [97, 100] | 100% | 100% | 100% |
| 24 | spans only | 45.8% | 100% | 12.5% | 25.0% |
| 24 | **v3 (our join)** | **99.5%** [97, 100] | 98.4% | 100% | 100% |
| 24 | pool / pool_port (host-wide) | 45.3% | 98.4% | 37.5% | 0% |
| 24 | **obi_real+flows (order matching)** | **63.0%** [56, 70] | 98.4% | 53.1% | 37.5% |
| 24 | **obi_real+port (exact, client port)** | **99.5%** [97, 100] | 98.4% | 100% | 100% |

Intervals treat agent-runs as independent, which they are not (agents of one run share the server and the schedule); they are optimistic.

## Predictions against results
- 12 (v3 at least 90%; healthy false alarms at most 5%): **held** (100% and 99.5%; kind H false alarms 0/36 and 1/64).
- 13 (pooled arms 55-75%, kind S at most 20%): **half wrong**. 61.1% at C = 9, but **45.3% at C = 24**, below the range; kind S was 0% as predicted, and kind N fell
  from 83% to 37.5%. I did not work out why kind N degrades with more agents (untested explanations: more host-wide noise in the pooled maxima, or the healthy
  thresholds rising with load).
- 14 (order matching at least 15 points below v3): **held** (30.6 and 36.5 points below).
- 15 (exact port matching at least 90%): **held** (100% and 99.5%). The joint belief that 14 and 15 both hold (50%) also held.
- Neither "I would be wrong" condition was triggered.

## What this says
- **Under concurrency with agents in different conditions, OBI matched by order does not work** (63-69%, about the host-wide level), as expected: the
  connections of different kinds interleave, so order assigns the wrong RTT and the wrong server duration to many calls.
- **OBI plus an exact match by the client's source port is as good as our join** (100% and 99.5%, the same as v3 to within one agent-run). So the
  diagnostic evidence does not need our correlator; it needs each call matched to its connection.
- **What the correlator actually adds is a way to do that matching without the application declaring its socket.** It uses the process, the thread,
  the time and the connection. `obi_real+port` needs the span to carry its local port (`network.local.port`, an OpenTelemetry attribute that is
  opt-in and which I would not assume common HTTP clients set; here I used the validation-only attribute our agent already records). Our join needed the
  spans' `process.pid` and `thread.id` instead, plus the thread-per-call structure; it fails for async agents on one thread unless `network.local.port`
  is present (Stage 11), so for async code both approaches need the same attribute.
- So the defensible statement of our contribution is now: (1) the benchmark and the pre-registered evaluation, (2) the diagnosis rules, and (3) a matching
  method that works for thread-per-call agents without declaring ports. It is not that kernel-level evidence needs our collector or our join.

## Limits
Same family of limits as Stages 14-15: mock services on loopback, one OBI version and configuration, `print_stats` output parsed by regular expression,
scenario designed to discriminate (agents differ by source address, which real fleets do not necessarily do), lockstep agents as threads of one process,
n = 12 and 8 runs. Not tested: retransmits and packet loss, DNS, async agents with OBI, agents in separate processes or containers, a real provider.
