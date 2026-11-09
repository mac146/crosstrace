# Stage 17 predictions: async agents without a declared port (written BEFORE any code or run)

Status: **pre-registered, not yet run.** Run it before Stage 18 (it needs no API spend and no real network).

## Why
Stage 11: async agents on one thread cannot be joined by process and thread id (0 of 1,280 spans correct at 64 coroutines); they can if each span declares
its socket's local port (`network.local.port`, then 100%). Stage 16: OBI plus an exact match by the client's source port equals our join, and the
Stage 16 follow-up found that stock Python HTTP client instrumentations do not appear to set that attribute. So the one thing our correlator could add over
OBI plus glue is a way to match calls to connections WITHOUT a declared port. For thread-per-call agents it already does (process, thread, time). The open
question is whether anything works for ASYNC agents. This stage tests one specific idea, fixed in advance.

## The idea (assignment by time and duration)
Within one run and one server port, the spans (HTTP client calls) and the connections (the kernel's connect-start .. last-byte activity) are two sets of the
same size. Match them by optimal assignment (Hungarian algorithm) with this cost, fixed now and not tuned afterwards:
`cost(span, connection) = |span.start - connection.connect_start| + |span.duration - connection.active_duration|` (milliseconds, equal weights).
The intuition: calls that face different conditions have different durations, so duration separates them where it matters; calls with equal durations are
interchangeable for diagnosis anyway. The known weakness, predicted below: network latency and a slow server can both make a call take 300-1000 ms, so
duration alone cannot always tell those two kinds apart.

## Scenario (new harness, async)
One thread, an asyncio event loop, C coroutines in lockstep, one API server, the Stage 16 kinds interleaved: H healthy (default source address),
N source address 127.0.0.2 with netem latency on that address (75-250 ms one way), S source address 127.0.0.3 with the server delaying answers to that
address (300-1000 ms). Every coroutine makes 4 sequential calls to /shipping/1../shipping/4 with its own span (same process id and thread id for all).
C = 9 and 24; 12 and 8 mixed runs; healthy calibration at the same C (8 runs). Each agent scored against its own truth (top-1; H none, N net_latency,
S slow_api). The async HTTP client is a small asyncio client written for the harness (the existing client is blocking). Development of the matcher may use at
most 2 throwaway runs; the scored runs are recorded afterwards with fresh seeds.

## Arms (same rules and calibration as v3 unless stated)
- `v3 as is`           our join without any declared port: expected to fail for async (Stage 11).
- `v3 + declared port` the join with the span's local port declared (positive control).
- `v3 + assignment`    the join with the assignment above replacing the process/thread matching: NO declared port. The thing being tested.
- `obi_real+flows`     real OBI evidence (Stage 15/16) matched by server port, path and order (needs OBI; skipped if it is not installed).
- `obi_real+port`      OBI evidence matched exactly by the declared client port (positive control for OBI).
- `pool`               host-wide counters.

## Predictions (per-agent top-1 over all kinds)
1. `v3 as is` at most 45% at both C (75%): at best the kinds that are right by symmetry.
2. `v3 + declared port` at least 95% at both C (80%), `obi_real+port` at least 95% (70%).
3. **`v3 + assignment` at least 70% at both C (60%), and at least 85% at both C (40%).** I do not expect it to be as good as a declared port.
4. Where `v3 + assignment` errs, it is mostly kind N versus kind S (latency versus a slow server confused through similar durations): at least 70% of its
   errors involve exactly that pair (65%). Healthy agents (kind H, durations of a few milliseconds) are almost never mislabelled: false alarms at most 5% (80%).
5. `obi_real+flows` (order) at most 70% (65%): order matching has no more information in async than in threads.
I would be wrong, and say so, if `v3 + assignment` is below 50% (the idea does not work), or if it reaches `v3 + declared port` within 5 points (then a
declared port is not needed for these scenarios and the Stage 16 conclusion changes). Either outcome is reported with all runs.

## What would count as a positive result, and what would not
A positive result is `v3 + assignment` at 85% or more at both C, stated as "without a declared port, in these scenarios, with kinds that differ in duration".
It would not show that the idea works in general: durations can coincide (several calls with similar timing and different causes), and the scenario is built
from the Stage 16 kinds. If it lands between 70% and 85% it is reported as partial, with the confusion table. Below 70% it is a negative result for this idea
and the report's claim about the correlator stays as it is.
