# Stage 6b predictions: loaded machine (written BEFORE the run)

Test: the subtle suite (20-100 ms faults, 60 healthy runs, 30 per fault) recorded while 10 busy-loop processes saturate
all 10 cores, diagnosers v3 and spans-only. Two threshold calibrations are compared:
* calibrated UNDER load (healthy runs recorded under the same load), the fair setting;
* calibrated on the idle machine (the Stage 6 thresholds), the "you tuned it in the lab" setting.

Predictions, in advance:
1. **Tight rule, idle-calibrated, under load: many false alarms.** At least 30% of healthy runs get a non-"none" answer.
   Healthy 1-2 ms steps stretch several-fold under CPU contention and the idle thresholds are p99 + 5 ms.
2. **Tight rule, load-calibrated: some false alarms, 3-25%,** because load jitter is bursty and 60 calibration runs
   under-sample its tail. Detection of subtle faults drops: v3 top-1 on dns_delay and net_latency between 50% and 90%.
3. **Coarse rule: no false alarms under either calibration** (its 50 ms margin absorbs the jitter) and still blind to
   the smallest faults, as in the idle run.
4. v3 keeps its edge over spans-only on network/server causes, even if both lose accuracy.
5. The collector may lose events under load (bpftrace output backlog). I expect to see some warnings but no systematic
   failure of the join.

I would be wrong if the tight rule stayed at 0 false alarms and over 90% detection under saturation.
