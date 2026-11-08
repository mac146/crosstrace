# Stage 6d predictions: a real model over the real internet (written BEFORE the run)

Setup: the LangGraph agent with `z-ai/glm-5.3-flash` through OpenRouter (TLS to a remote host), tools and database local.
12 healthy runs for calibration, then 8 runs each of: healthy, slow_tool (300-1000 ms), slow_db (300-1000 ms); kernel collector
on port 443, DNS port 53 as well as the stub. Diagnosers base and v3 (frozen). Network faults are NOT injected (that would mean
running `tc` on the real network). n = 8 per group is tiny; these are observations, not accuracy estimates.

From a single smoke run (4 calls: 2.2 s, 0.9 s, 0.8 s, 1.7 s): healthy model calls take about 1-2 s and vary by a factor of 2-3.

Predictions, in advance:
1. **Mock-calibrated thresholds fail on the real model.** The mock-calibrated "chat" threshold is about 55-60 ms. At least
   80% of healthy real runs will get a non-"none" answer (a false alarm), for both diagnosers.
2. **Real-calibrated thresholds mostly cope.** With the thresholds rebuilt from 12 healthy real runs, healthy false alarms
   stay at or under 25% (2 of 8), because 12 runs under-sample the tail of model latency.
3. **Local faults are still found.** slow_tool and slow_db are 300+ ms on steps that do not depend on the model's latency
   (healthy: about 1 ms and 50 ms), so top-1 is at least 85% for both diagnosers under real calibration, with base and v3
   equal (the kernel adds nothing for local faults). Some misses will be a noisy model call outranking the fault.
4. **Kernel coverage is partial.** Only the first model call of a run gets connect evidence (the HTTP client reuses its
   connection, seen in the smoke run); the real hostname's DNS lookup is not captured. I expect fewer than 40% of model-call
   spans to carry connect evidence.
5. **Latent loss.** On healthy real connections I expect at least one TCP retransmit across the 12 + 8 healthy runs (one
   was seen in a failed smoke run), which the "latent loss" rule would report as packet_loss, a false alarm.

I would be wrong if: mock-calibrated thresholds mostly work (1), real-calibrated healthy false alarms exceed 25% (2), local
faults are found in under 70% of runs (3), or more than 60% of model-call spans carry connect evidence (4).
