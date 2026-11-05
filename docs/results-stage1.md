# Stage 1 result: baseline (agent-spans-only) diagnoser

Command: `make eval-baseline` (30 trials per fault, 30 healthy calibration runs, seeds 1000-1029 / 900000-900029).
Run on WSL2 (kernel 6.6.87.2), loopback only, mock services. 95% Wilson intervals in brackets.
Source of numbers: console output of `python -m eval.run_baseline --n 30 --cal 30` on 2026-11-05.

| fault | n | top-1 | top-3 | false-attr | run visibly slow | errors |
|---|---|---|---|---|---|---|
| none | 30 | 100% [89-100] | 100% | 0% [0-11] | 0% | 0 |
| dns_delay | 30 | 30.0% [17-48] | 100%* | 0% | 100% | 0 |
| net_latency | 30 | 26.7% [14-44] | 100%* | 0% | 100% | 0 |
| packet_loss | 30 | 30.0% [17-48] | 76.7% [59-88] | 0% | 77% | 0 |
| slow_tool | 30 | 100% [89-100] | 100% | 0% | 100% | 0 |
| slow_db | 30 | 100% [89-100] | 100% | 0% | 100% | 0 |
| slow_model | 30 | 100% [89-100] | 100% | 0% | 100% | 0 |
| rate_limit | 30 | 100% [89-100] | 100% | 0% | 100% | 0 |

\* By construction: the baseline lists exactly {dns_delay, net_latency, packet_loss} for a slow API step, so top-3 is
trivially 100% when the step is flagged. Top-1 is the informative number.

## Reading it
- The baseline is perfect on faults that sit in one span type, and chance level (~1/3) on the three network-path
  faults, which is the gap the combined diagnoser is meant to close. The hypothesis is only tested on that gap.
- packet_loss: 23% of runs (7/30) showed no visible slowdown (loss 20-40% did not hit a packet that mattered).
  Those runs are scored as misses (the baseline says "none"), so packet_loss top-1/top-3 are depressed for a reason
  no diagnoser could fix from timing alone. Report both overall and conditioned on a visible slowdown in Stage 4.
- Non-network faults are 300 ms+ against 1-50 ms healthy steps, so they are easy by design. Do not read the
  100% as evidence the baseline is strong in realistic conditions.
- Not measured yet: overhead (Stage 4), combined diagnoser (Stage 4).
