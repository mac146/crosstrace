# Stage 17 results: async agents on one thread, without a declared port

Predictions: [predictions-stage17.md](predictions-stage17.md), committed before any code or run. Harness: [eval/async_conc.py](../eval/async_conc.py), matcher:
[correlator/assign.py](../correlator/assign.py) (`python -m eval.async_conc`; `--no-record` re-scores recorded runs). All agents are coroutines of ONE asyncio loop on ONE thread (same
process id and thread id for every span), kinds interleaved as in Stage 16 (H healthy; N source address with netem latency; S source address with the server delaying answers),
real OBI next to our collector. C = 9 (12 runs, 108 agent-runs) and C = 24 (8 runs, 192 agent-runs), calibrated at the same C (8 healthy runs). The matcher was developed with
one throwaway debugging run of 2 mixed runs (6 coroutines; it also exposed a harness bug, the missing DNS lookup, fixed before the scored run); the scored runs are fresh seeds.
The matching cost (|start difference| + |duration difference|, equal weights) was fixed in the pre-registration and not changed. No crashed agents, no collector warnings.

## Results (per-agent top-1)
| C | arm | overall | H (none) | N (net_latency) | S (slow_api) |
|---|---|---|---|---|---|
| 9 | spans only | 44.4% | 100% | 33.3% | 0% |
| 9 | v3, no declared port (as is) | 88.9% [82, 94] | 100% | 86.1% | 80.6% |
| 9 | v3 + declared port (control) | 100% [97, 100] | 100% | 100% | 100% |
| 9 | **v3 + assignment (no declared port)** | **100%** [97, 100] | 100% | 100% | 100% |
| 9 | host-wide | 63.9% | 100% | 91.7% | 0% |
| 9 | OBI, order matching | 70.4% | 100% | 50.0% | 61.1% |
| 9 | OBI, exact port (control) | 100% | 100% | 100% | 100% |
| 24 | spans only | 45.8% | 100% | 12.5% | 25.0% |
| 24 | v3, no declared port (as is) | 48.4% [41, 55] | 96.9% | 20.3% | 28.1% |
| 24 | v3 + declared port (control) | 99.5% [97, 100] | 98.4% | 100% | 100% |
| 24 | **v3 + assignment (no declared port)** | **99.5%** [97, 100] | 98.4% | 100% | 100% |
| 24 | host-wide | 53.6% | 98.4% | 62.5% | 0% |
| 24 | OBI, order matching | 81.8% | 98.4% | 92.2% | 54.7% |
| 24 | OBI, exact port (control) | 99.5% | 98.4% | 100% | 100% |

Directly: the assignment found each call's TRUE connection (the declared port is the truth) for **432/432** calls at C = 9 and **768/768** at C = 24. Its only wrong diagnosis in 300
agent-runs was one healthy agent called packet loss (the same agent the declared-port control also got wrong: a stray retransmit). OBI evidence existed for 864/864 and 1,520/1,536 pairs.
Intervals treat agent-runs as independent, which they are not (same server, same lockstep schedule); they are optimistic.

## Predictions against results
1. `v3 as is` at most 45% at both C: **wrong at C = 9** (88.9%), marginally wrong at C = 24 (48.4%). Our join without a declared port degrades with the number of coroutines (89% -> 48%; Stage 11 saw 0/1,280
   spans at 64 coroutines) but is far from useless at 9; I had assumed the process/thread rule was all there was, and did not account for the flow and port-and-time fallbacks.
2. Declared port at least 95% (v3) and `obi_port` at least 95%: **held** (100% / 99.5%).
3. `v3 + assignment` at least 70% at both C (60%) and at least 85% at both C (40%): **held, both** (100% and 99.5%).
4. Errors mostly N versus S, and healthy false alarms at most 5%: **the false-alarm half held** (0/36 and 1/64); **the N-versus-S half did not materialise**: there was one error in 300 agent-runs, and it was not an N/S confusion.
   The mechanism I predicted (latency and a slow server both making calls take 300-1,000 ms) did not matter, because start times separated the calls (see below).
5. OBI matched by order at most 70%: **wrong** (70.4% at C = 9, 81.8% at C = 24). In this single-thread scenario order carries more information than in Stage 16's threads (63-69%); I did not investigate further.
Of the two "I would be wrong if" conditions, the second **was triggered**: `v3 + assignment` came within 5 points of `v3 + declared port` (0 points). Per the pre-registration this means a declared port is not needed for these
scenarios, and the Stage 16 conclusion changes (next section).

## What this says
- **For these async scenarios, matching by assignment on start time and duration replaces the declared port**, with no loss (100% and 99.5% against 100% and 99.5% for the declared port), where our own join without it gives 89% and 48%
  and OBI matched by order 70% and 82%. This is the positive result the stage was hoping for, and it changes the Stage 16 summary: the correlator CAN match calls to connections without the application declaring its socket,
  even when every span has the same process and thread id. The pooled and by-order arms show it is not trivial.
- **Why it works here, and the main caveat.** The matcher found every connection exactly, including among healthy calls that last only a few milliseconds, so it was almost certainly using the START times: in one asyncio loop the coroutines
  begin their calls in a fixed order, a few tens of microseconds to milliseconds apart, and the kernel's connects follow in the same order. Durations only had to separate the kinds, which differ by hundreds of milliseconds. A single-threaded event
  loop is therefore the easy case for time matching; with threads, several processes, or an event loop whose tasks wait unevenly before connecting (asynchronous DNS, connection pools, TLS), the order of span starts and connects can differ
  and the matching is expected to degrade. That was not tested. The N-versus-S confusion I predicted would be its weak point never appeared because start order resolved it first.
- **What it does not show:** a general method for async agents on a stock stack. It needs process-wide spans with correct start and end times and a kernel collector with connect and last-byte times; the scenario is lockstep with only
  three kinds of calls; the async client is a small one written for the harness (a blocking DNS lookup on the loop thread, one connection per call); the cost was never tuned, so the result may also be partly luck of this scenario.
  The same assignment applied to OBI's printed statistics was not tried.

## Limits
Mock services on loopback, a constructed difference between agents (source addresses), lockstep coroutines of one process, my own asyncio client, n = 12 and 8 runs, one OBI version and configuration, agent-runs not independent,
the correlator's other rules still ran after the assignment (it only supplies ports), and the number of throwaway runs used to debug the matcher (2 mixed runs) was within the pre-registered limit.
