# Stage 19 results: does assignment-by-time survive uneven waits before connecting?

Predictions: [predictions-stage19.md](predictions-stage19.md), committed before any code or run. Harness: [eval/async_jitter.py](../eval/async_jitter.py) (`python -m eval.async_jitter`;
`--no-record` re-scores). Stage 17's scenario (24 coroutines on one asyncio thread, kinds H/N/S, real OBI beside our collector) with a random wait, uniform on 0..J ms, between a call's span start and
its connection (inside the span), J in {0, 50, 200, 500} ms; per J 8 mixed runs (192 agent-runs) and 6 healthy calibration runs at the same J. The matcher and its cost are Stage 17's, unchanged.
**Deviation from the pre-registration:** it said "no throwaway runs"; I ran one plumbing smoke test first (1 healthy + 1 mixed run per J, to check the code ran end to end) and looked at its output, which already
showed the matcher degrading at high J. The scored runs use a different seed namespace, so none of them reuses the smoke test's seeds. No crashed agents, no collector warnings.

## Results
Share of calls assigned to their TRUE connection (the declared port is the truth), combined cost / start time only / duration only:
| J (ms) | combined (Stage 17) | start only | duration only |
|---|---|---|---|
| 0 | 97.9% | **100%** | 18.4% |
| 50 | 14.8% | 4.6% | 2.2% |
| 200 | 11.8% | 3.3% | 2.0% |
| 500 | 12.9% | 3.6% | 1.2% |

Per-agent diagnosis top-1 (192 agent-runs per J; control = declared port):
| J (ms) | healthy threshold | spans only | v3 no declared port | **v3 + declared port** | **v3 + assignment** | assignment: start only | assignment: duration only | host-wide | OBI order | OBI exact port |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 96 ms | 45.8% | 66.1% | 99.5% | **99.5%** | 99.5% | 94.3% | 45.3% | 86.5% | 99.5% |
| 50 | 166 ms | 45.8% | 51.0% | 99.0% | **95.3%** | 66.1% | 78.6% | 44.8% | 84.4% | 99.0% |
| 200 | 365 ms | 45.8% | 50.5% | 98.4% | **81.2%** | 67.7% | 69.8% | 56.8% | 84.9% | 98.4% |
| 500 | 797 ms | 44.3% | 46.9% | 93.8% | **72.9%** | 62.0% | 57.3% | 57.3% | 72.4% | 93.8% |

Gap (declared port minus assignment): **+0.0, +3.6, +17.2, +20.8 points** at J = 0, 50, 200, 500. Assignment errors: 1 (J=0, a healthy agent called packet loss), 9, 36, 52; the share that were latency-versus-slow-server
swaps: 0 of 1, 67%, 92%, 77%. Intervals (Wilson, agent-runs treated as independent, so optimistic) are in the console output of `eval.async_jitter`.

## The result against the pre-registered labels
**Partial**: the gap is at most 5 points at J = 50 (3.6) but not at J = 200 (17.2). The method of Stage 17 therefore holds for calls that start in order and for small waits (up to about 50 ms with the kinds as defined), and fails at waits of
200 ms and more. Neither "I would be wrong if" condition was triggered (the gap is not within 5 points at J = 500; it is 0 at J = 0).

## Predictions against results
1. J = 0 replicates Stage 17: diagnosis gap 0 (held), but the true-connection share is **97.9%, not at least 99%** (Stage 17 measured 100% on a different set of runs). The start-only version is exactly 100%, so at J = 0 the duration term of the cost
   adds a few mistakes, none of which changed a diagnosis. **Half wrong.**
2. The share falls with J (at most 80% at J = 200, at most 60% at J = 500): **held, far beyond the bound**: it collapses to 15% already at J = 50. Exact matching is lost as soon as the waits exceed the spacing of the calls; only the kind of each call is
   kept (next point).
3. Diagnosis gap at most 5 at J = 50 (**held**, 3.6), at least 10 at J = 200 (**held**, 17.2), at least 20 at J = 500 (**held**, 20.8, narrowly); at least 70% of the wrong diagnoses at J >= 200 are N-versus-S swaps (**held**, 92% and 77%).
   At J = 50 the share is only 15% but diagnosis is almost intact: the matcher swaps calls of the SAME kind (healthy with healthy), which does not change a diagnosis, and keeps the kinds apart through their durations. From J = 200 the durations of
   N and S calls (which overlap by construction) are no longer separated and the diagnosis breaks.
4. Ablations: start only matches the combined cost at J = 0 within 2 points of share (**missed narrowly**: 100% vs 97.9% = 2.1) and the combined cost beats start-only by at least 5 points of share at J = 200 (**held**: 11.8% vs 3.3%, though both are very low);
   duration only is the worst of the three at J = 0 with at most 70% share (**held**: 18.4%), yet its diagnosis is 94.3% at J = 0 (same-kind swaps again).
5. OBI matched by order at most 75% at J = 200: **wrong** (84.9%; 86.5%, 84.4%, 84.9%, 72.4% across J). The v3-without-declared-port arm at most 60% at J = 200: **held** (50.5%).
6. The declared-port control at least 95% at J <= 50 (**held**), at least 90% at J = 200 (**held**, 98.4%), below 90% at J = 500 (**wrong**: 93.8%). Thresholds rose from 96 to 797 ms but the injected faults (up to 1 s) stayed detectable.

## What this says
- **The Stage 17 claim must be narrowed.** "Assignment on start time and duration matches without a declared port" holds when the calls start in a fixed order (J = 0) and, for diagnosis, with small waits (J = 50), where it loses about 4 points against a declared
  port. With waits of 200 ms or more it loses 17-21 points, and from J = 200 on it is no better than OBI's order matching (81% vs 85% at J = 200, 73% vs 72% at J = 500). A declared port (or an exact match like OBI's by client port) stays at 94-99% at every J.
- **The failure is predictable**: it swaps calls whose durations overlap (here N and S by construction, both 300-1,000 ms), and exact matching is lost at much smaller waits than diagnosis. Whether it would survive larger waits with kinds of clearly
  different durations (not tested) is an open question; the kinds here were built to overlap.
- **Start time is what the matcher uses** (start only: 100% at J = 0); with waits the start is no longer informative and the duration term does the work, only partly.
- **Practical reading:** for async agents that wait unevenly before connecting (asynchronous DNS, pools, TLS, scheduling), do not rely on assignment by time; have the span declare its local port (`network.local.port`) or match by an exact key as OBI's
  per-connection statistics allow. This is an honest limit of the method, and the report says so.

## Limits
Mock services on loopback; waits drawn uniform on 0..J (a real client's waits are not uniform); the kinds' durations overlap by construction; one async client written for the harness; 24 coroutines; 8 runs per J; agent-runs not independent; one OBI version and
configuration; threads and processes were out of scope (process and thread ids already separate those calls); the plumbing smoke test deviation above.
