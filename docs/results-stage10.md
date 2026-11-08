# Stage 10 results: v5, a diagnoser for real networks

v5 = v4 plus run-level, per-class evidence for the model connection (`predictions-stage10.md`, committed before any v5 result):
the run's DNS lookup against its healthy threshold; the lower median over the run's model calls of the smoothed RTT against its
healthy threshold ("persistent" = more than half the calls); a slow model call these explain is not also blamed on `slow_model`.
Packet loss is unchanged from v4. Thresholds: the coarse rule over per-run healthy values.

## A. Regression on the mock agent: nothing changed
Across **870 recorded mock evaluations** (slow-API suite, subtle suite under both threshold rules, two-fault suite), v5 gave the
**same first-ranked cause as v4 in every one** (0 differences), as predicted: with no DNS lookup on the model connection and
microsecond loopback RTTs, neither run-level rule can fire there.

## B. Real model, fresh seeds (`rf_b`, 10 runs per group, LangGraph + `z-ai/glm-5.3-flash` via OpenRouter, faults on the real path)
Calibration: 32 healthy real runs (Stage 8's 16 plus 16 recorded in this session). `rf_a` (Stage 8) informed the design and is not
used to judge v5. No run was excluded (the CDN kept its addresses this time). Cost: 202 calls, about **$0.0042**.
Top-1 / false attribution / "none":

| group | spans only | v3 | v4 | **v5** |
|---|---|---|---|---|
| healthy | 100% / 0% / 100% | 50% / 50% / 50% | 100% / 0% / 100% | **100% / 0% / 100%** |
| dns_delay (injected 5,792 ms median) | 0% / 30% / 70% | 10% / 20% / 30% | 10% / 20% / 60% | **100% / 0% / 0%** |
| net_latency (injected 1,319 ms median) | 0% / 40% / 60% | 0% / 30% / 40% | 0% / 30% / 60% | **90% / 0% / 10%** |
| packet_loss (injected 25% median) | 0% / 10% / 90% | 100% / 0% / 0% | 70% / 0% / 30% | **70% / 0% / 30%** |

What the kernel measured (medians): DNS 5,951 ms for the 5,792 ms injected; connect 1,400 ms and max smoothed RTT 1,335 ms for
1,319 ms one-way; retransmits 7 per run in the loss group against 2 in healthy runs; kernel evidence on 100% of model calls.

Predictions: net_latency at least 70% (**90%, right**), dns_delay at least 80% (**100%, right**), packet_loss equal to v4 and
between 30% and 70% (**70%, right, at the top edge**), healthy false alarms at most 20% (**0 of 10, right**; spans-only 0 of 10).
I would have been wrong below 50% / 60% / above 30% respectively; none happened.

## How much this proves, and what it does not
* **The faults were chosen to be unambiguous** (DNS 4-8 s, latency 0.8-1.5 s one-way). The detection floor is set by this path's
  healthy tails, not by the method: the v5 thresholds came out at **DNS 2,333 ms and median RTT 1,012 ms**. A DNS fault under about
  2.3 s, or a latency under about a second, is **undetectable here**; the missed latency run is most likely an injected value near
  the 0.8 s end (not individually verified). The RTT threshold is that high because one of the 32 healthy runs had a median RTT of
  about 960 ms: the path itself was slow for a stretch, the congestion risk named in advance, and it is baked into the calibration.
* **Packet loss is not solved.** v5 and v4 find it in 70% of runs because the loss group's 7 retransmits per run now exceed the
  healthy baseline of 6; this is the same trade-off as before (v3 finds 100% but false-alarms on half of healthy runs). In Stage 8's
  set the same rule found 50%: this 70% is within the noise of 10 runs.
* **Small samples:** 10 runs per group (healthy false-alarm upper bound 28%), 32 calibration runs, one model, one provider, one
  NAT'd network path, one afternoon. Heavy-tailed signals could look different at another time of day or on another network, and
  the thresholds would need recalibrating there.
* A path that is slow for a long time while the model is healthy would be reported as `net_latency`: that is a correct finding (the
  network is slow) but it is indistinguishable from an injected fault by this method.
* Two simultaneous faults on the model connection, a slow remote server (cannot be injected on someone else's model), and agents
  with fewer than three model calls per run (no run-level median) are untested or unsupported.

## Mistakes caught on the way
The first design used the plain median and my unit tests showed that two spiked calls out of four sat at the average of the middle
two and would have been reported as a persistent delay; it is now the lower median (decided before any evaluation, recorded in the
predictions). Two test-construction bugs (a lookup longer than its span; healthy fixtures without response events) were also mine.
