# Stage 7 predictions: v4 = v3 + a healthy retransmit baseline for the latent-loss rule (written BEFORE the runs)

v4's only change: the "latent loss" rule (report packet_loss when retransmits are seen although no step was slow) now fires
only when the run's retransmit count exceeds the largest count seen in the healthy calibration runs (nearest-rank p99 of
total retransmits per healthy run), instead of at least one. Written after v3 gave 5 false alarms in 8 healthy real runs.

Two tests, both on fresh seeds, neither used to design v4:

**A. Real model, healthy runs (16 fresh runs, seeds 8500+, LangGraph + z-ai/glm-5.3-flash via OpenRouter, calibration = the same
12 healthy real runs as before).**
1. v3 false alarms again at 40% or more (the first sample gave 5/8).
2. v4 false alarms at most 25% (4 of 16). The baseline is the maximum over only 12 healthy runs, so it under-samples the tail;
   I expect it to be 3-4 retransmits and to miss a few outliers. I would be wrong if v4 stays above 25%.
3. Spans-only stays at 0 false alarms.

**B. Mock agent, slow-API suite (5 groups x 30, seeds 4500+): a regression check.**
4. On a healthy loopback the calibrated baseline is 0, so v4 and v3 give the **same first-ranked cause on every run**
   (zero disagreements). Any disagreement is a surprise I will report.

**Not testable now, stated so it is not forgotten:** the price. A baseline above 0 hides the mild end of real packet loss
(injected loss gave 1-3 retransmits per run). Injecting loss on the real network would need `tc` on the real interface,
which I will not do. So v4's real-network detection of packet loss is untested; only its false-alarm side is.
