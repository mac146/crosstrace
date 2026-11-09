# Stage 19 predictions: does assignment-by-time survive uneven waits before connecting? (written BEFORE any code or run)

Status: **run; results in results-stage19.md** (one plumbing smoke test was run first, contrary to the "no throwaway runs" line below; disclosed there).

## Why
Stage 17: matching async agents' calls to connections by optimal assignment on start time and duration recovered 99.5-100% without any declared port. I flagged the main caveat: in one
asyncio loop the coroutines start their calls in a fixed order and the kernel's connects follow in the same order, so start time alone orders the calls. That is the easy case. Real async
clients wait unevenly between starting a call and connecting (asynchronous DNS, connection pools, TLS, scheduling). This stage destroys the start order by exactly that, and measures how
fast the matcher degrades. Threads and processes are out of scope on purpose: for thread-per-call agents process and thread ids already separate the calls (Stages 3 and 9), so the assignment
only matters where they cannot, which is async.

## Scenario
Stage 17's scenario (24 coroutines in one asyncio loop on one thread, kinds H/N/S interleaved, one server, source-address faults, real OBI next to our collector) with one change: after a call's span
starts and before its connection is opened, the coroutine awaits a random delay, uniform on 0 to J ms, drawn independently per call from a seeded generator. The delay is INSIDE the span (a
real client's wait is). J in {0, 50, 200, 500} ms; per J: 8 mixed runs (192 agent-runs) and 6 healthy calibration runs at the same J (the delay raises healthy step durations, so thresholds are
calibrated per J). Fresh seeds, no throwaway runs. The matcher and its cost are exactly Stage 17's (equal weights, never tuned); the old results are not re-scored.

## Arms
`v3 + declared port` (control), `v3 + assignment` (the Stage 17 matcher), two ablations of the same matcher on the same recorded runs: `assignment, start time only` (weights start 1, duration 0) and
`assignment, duration only` (weights 0, 1); `v3, no declared port` (as is); `OBI, order matching` and `OBI, exact port` (control); host-wide; spans only.
Primary measure: the share of calls assigned to their TRUE connection (the declared port is the truth), overall and by kind. Secondary: per-agent diagnosis top-1 and the GAP between `v3 + assignment` and
`v3 + declared port` at the same J (the control itself loses power as J rises, because healthy step times rise; the gap isolates the matching).

## Predictions
1. J = 0 replicates Stage 17: the true-connection share at least 99% and a diagnosis gap of at most 3 points (80%).
2. The true-connection share (all kinds) falls as J rises: at most 80% at J = 200 and at most 60% at J = 500 (65%). The main reason: healthy calls are nearly identical in duration, so their starts and
   delays order them arbitrarily, and the matcher swaps them. Swapped healthy calls do not change a diagnosis.
3. The diagnosis gap is at most 5 points at J = 50 (65%), **at least 10 points at J = 200 (55%)** and at least 20 points at J = 500 (55%). The mechanism: durations of N and S calls overlap (both 300-1,000 ms),
   so once starts no longer order the calls the matcher swaps N and S calls, which flips the diagnosis. At least 70% of the matcher's wrong diagnoses at J >= 200 are N-versus-S swaps (65%).
4. Ablations: start-only matches the combined cost at J = 0 (within 2 points of true-connection share, 80%) and is worse than the combined cost by at least 5 points of share at J = 200 (50%); duration-only
   is the worst of the three at J = 0, at most 70% share (70%).
5. `OBI, order matching` at most 75% diagnosis at J = 200 (60%). `v3, no declared port` (as is) at most 60% at J = 200 (60%).
6. The declared-port control at least 95% at J <= 50, at least 90% at J = 200 (50%) and below 90% at J = 500 (60%): thresholds rise with J.
I would be wrong, and say so, if the diagnosis gap is at most 5 points at J = 500 (the matcher is robust to waits that large) or more than 5 points at J = 0 (the Stage 17 result does not replicate).

## What counts as positive, partial and negative (fixed now)
Positive for the matcher: gap at most 5 points at J = 200 (waits of the size of a typical TLS or pool wait). Partial: at most 5 points at J = 50 only. Negative: the gap opens at J = 50 or
the J = 0 replication fails. A negative or partial result is reported as the limit of the Stage 17 method, and the report's claim ("matches without a declared port in a single-threaded event
loop whose calls start in order") is narrowed accordingly.
