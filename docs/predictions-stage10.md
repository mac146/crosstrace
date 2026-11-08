# Stage 10 predictions: v5, a diagnoser for real networks (written BEFORE any v5 result was computed)

v5 = v4 + `run_level`: for the model connection, per-class healthy thresholds and run-level evidence, even when no step crossed
its duration threshold: (a) the run's DNS lookup above its healthy threshold -> `dns_delay`; (b) the lower median over the run's
model calls of the smoothed RTT above its healthy threshold -> `net_latency` ("persistent" = more than half the calls); (c) a slow
model call that (a)/(b) explain is not also blamed on `slow_model`. Thresholds: the coarse rule (`max(p99, 3 x median) + 50`)
over per-run values from healthy runs, at least 5 of them. Packet loss is unchanged from v4 (the real-path data say count alone
cannot separate it). Design inputs: the rf_a runs and healthy tails seen in Stage 8, so **rf_a is a design set and is not used
to judge v5**. One design change was made while writing unit tests, before any evaluation: the lower median instead of the
median (an even number of calls with half spiked sat at the average of the two).

## A. Regression on the mock agent (recorded runs only, nothing re-recorded)
Sets: slow-API suite hard_c (150 runs), subtle suite sub_a (coarse and tight rules), two-fault suite pair_a.
1. **v5 gives the same first-ranked cause as v4 on every run** (0 differences): the mock has no DNS lookups on the model
   connection (IP address) and loopback RTT is microseconds, so neither run-level rule can fire. Any difference is a surprise.

## B. Real model, fresh seeds (rf_b, seeds 12000+, 10 runs per group, faults as in Stage 8)
Calibration: 32 healthy real runs (the 16 of Stage 8 plus 16 new ones recorded in this session). Variants base, v3, v4, v5.
2. **net_latency**: v5 top-1 at least 70% (v4 and spans-only about 0% on rf_a). Runs where the fault was not on the used path are
   excluded and counted, as before.
3. **dns_delay**: v5 top-1 at least 80% (injected 4-8 s against a healthy lookup tail of about 2.3 s).
4. **packet_loss**: v5 equals v4 (no new rule): between 30% and 70% top-1, with v3 higher and false-alarming on healthy runs.
5. **Healthy runs**: v5 false alarms at most 20% (2 of 10); spans-only at most 25%.
6. v5 never makes a previously correct local-fault answer worse (slow_tool, slow_db are not part of this run; stated so).

I would be wrong if v5 gets under 50% on net_latency, under 60% on dns_delay, or false-alarms on more than 30% of healthy runs.

Risks I already see, stated in advance: a path that is slow for a stretch of time (congestion) would look like a persistent
delay and false-alarm healthy runs; a DNS fault under about 2.3 s, or a latency under about the healthy median RTT threshold,
is undetectable by construction; two faults at once on the model connection are untested; one model and one network path.
