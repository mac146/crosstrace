# Stage 7 result: v4, a healthy retransmit baseline for the latent-loss rule

Predictions: `predictions-stage7.md`, committed before both runs. v4 differs from v3 in one rule: the latent-loss rule reports
packet loss only when the run's TCP retransmits exceed the largest count seen in the healthy calibration runs (instead of at
least one). It was written after v3 raised 5 false alarms in 8 healthy real runs (`results-stage6.md` section 6).

## A. Real model, 16 fresh healthy runs (LangGraph + `z-ai/glm-5.3-flash` via OpenRouter, seeds 8500+; calibration = the same 12 healthy runs)
| diagnoser | false alarms on healthy runs |
|---|---|
| spans only | 0/16 |
| v3 | **11/16 (68.8%)**, replicating the first sample (5/8) |
| v4 | **1/16 (6.2%)** [1-28%] |

* Prediction 1 (v3 at least 40%): right. Prediction 2 (v4 at most 25%): right. Prediction 3 (spans-only 0): right.
* **The baseline v4 learned is 5 retransmits per run.** Healthy runs on this network show 0-6 per run (calibration:
  0 0 0 0 0 1 2 2 2 3 4 5; this set: 0 0 0 0 0 1 1 2 2 2 2 2 3 3 5 6). The one v4 false alarm is the run with 6, one above the
  calibration maximum: with only 12 calibration runs the tail is under-sampled, as predicted.
* Cost: 60 model calls, 26,602 + 3,340 tokens, about **$0.0012** (running total for the whole real-model work about $0.004).

## B. Regression check on the mock agent (slow-API suite, 5 groups x 30, seeds 4500+)
Prediction 4 held: v3 and v4 gave the **same first-ranked cause in all 150 runs** (0 differences); the loopback baseline is 0,
so v4 behaves exactly like v3 there. Results for both: slow_api 100%, dns_delay 100%, net_latency 100%, packet_loss 96.7%,
healthy 0/30 false alarms (spans only: 30%, 27%, 20%, 20% on the four faults).

## What this does NOT show
* **The price of v4 is untested and probably real.** With a baseline of 5, a run needs 6 or more retransmits to count as
  packet loss. Injected loss on loopback typically gave 1-3 per run (sometimes more), so on this network v4 would stay silent
  about mild packet loss. I did not inject loss on the real network (that would mean running `tc` on a real interface), so the
  detection side of the trade-off is unmeasured. Spans still flag a loss that visibly slows a step, so only latent loss, not
  slow-step loss, is affected.
* The baseline needs calibration on the deployment network; 12 runs under-sample its tail, and a different network, time of day
  or provider will have a different background rate.
* Small samples (16 healthy runs), one model, one provider, one network path.
