# Stage 8 results: real DNS, remote-server wait, and faults on the real network path

Setup: LangGraph + `z-ai/glm-5.3-flash` through OpenRouter (TLS to a remote host), a fresh HTTP client per run, WSL2.
Faults were injected on the real path with `tc` (faultlab/real_path.py): `dns_delay` = UDP to the resolver delayed 4-8 s;
`net_latency` = egress to the model server delayed 0.8-1.5 s per packet; `packet_loss` = egress to the model server loses
15-30% of packets. 8 runs per group, 8 healthy runs, thresholds from 16 healthy runs. Predictions: `predictions-stage8.md`
(committed before the run). **n = 8 per group: observations, not accuracy estimates.** Cost: 54 + a few calls, about $0.0015
(running total of real-model spend for the whole project under $0.01). Both interfaces were verified restored after every run.

## What the kernel probes do on the real path (this part works)
| claim | result |
|---|---|
| libc DNS lookups captured (probe on `getaddrinfo`) | **8/8 first model calls in every group** carry the lookup (healthy median 204 ms, was 0% before) |
| injected vs measured DNS delay | 4373->4530, 5705->6113, 4324->4489, 6580->6754, 4001->4196, 4195->4365, 5431->6470, 6204->6395 ms: within 15% in 7 of 8 (worst +19%) |
| evidence on reused connections (TCP-send probe) | **100% of model-call spans** have kernel evidence (was 1% with connect events alone) |
| remote-server wait (client send -> first response bytes) | defined on **100%** of model spans; a first heuristic was wrong (a large TLS ClientHello gave 251 ms for a 5 s call) and was fixed |
| net_latency: injected 991 ms one-way (median) | measured connect 1,060 ms, max smoothed RTT 1,052 ms: the kernel sees the injected delay |
| packet_loss: injected 15-30% | median **4 retransmits per run** vs healthy median 3: **not separable from the network's background rate by count** |

Not validated: remote-server wait against a *known* slow server (the model belongs to someone else, so no slow-server fault
could be injected); only healthy behaviour was observed.

## What the diagnosers do (this part mostly does not)
Top-1 / false attribution / answered "none", thresholds from 16 healthy real runs (step threshold for a model call: 6.5 s):

| group (scored runs) | spans only | v3 | v4 |
|---|---|---|---|
| healthy (8) | 75% / 25% / 75% | 12.5% / 87.5% / 12% | 75% / 25% / 75% |
| dns_delay (8) | 0% / 75% / 25% | 50% / 25% / 0% | 50% / 25% / 25% |
| net_latency (6; 2 not on the used path) | 0% / 17% / 83% | 0% / 17% / 67% | 0% / 17% / 83% |
| packet_loss (8) | 0% / 12% / 88% | **100%** / 0% / 0% | 50% / 0% / 50% |

* **A fault of about a second is invisible to step-duration thresholds on a real model.** A model call takes 1-3 s and varies
  by 2 s, so the model-call threshold is 6.5 s and a 1 s network delay never crosses it: spans-only and v4 answer "none" in 83%
  of net_latency runs and do not name the cause in any run. The kernel plainly measured it (connect 1,060 ms vs healthy median 28 ms),
  but the diagnosers only act on a step that is already slow.
* **The healthy tails of kernel signals on this path are heavy.** Healthy connect: median 28 ms, p90 556 ms, max 1,226 ms;
  healthy lookup: median 204 ms, max 2,283 ms; healthy per-call RTT: median 36 ms, max 1,239 ms (a lost SYN costs a 1 s timeout).
  Calibration "p99" over 16 healthy runs is effectively their maximum, so the RTT threshold became 1,289 ms: **above** the injected
  1 s latency. A single-sample threshold cannot separate a 1 s fault from this network's own spikes.
* **The v3 -> v4 trade-off I flagged is now measured.** v3 finds packet loss in 8 of 8 runs and also false-alarms on 87.5% of
  healthy runs; v4's retransmit baseline (6 per run) removes the false alarms and halves loss detection (50%), because injected
  loss gives 3-10 retransmits against a healthy 0-6. On this network the two are not both achievable by a count threshold.
* dns_delay: v3 and v4 name it in 50% of runs (the model-call rule only looks at DNS when other network evidence is present,
  and the first model call must itself exceed 6.5 s); spans-only never names it (75% false attribution: it blames the model).

## Predictions scored
Right: 1 (libc lookups captured; one of eight lookups was 19% off, so "within 15%" held in 7 of 8), 2 (100% evidence), 3 (wait
defined), 4 (kernel sees the latency), 7 (v4 at most 50% on dns_delay and net_latency), 8 (v4 false alarms at most 25%).
Wrong: 5 (loss runs above the healthy retransmit baseline: only 3 of 8), 6 (spans-only mostly answered "none", it did not blame
the model; its false attribution was high only for dns_delay), "faults applied in at least 90%" (net_latency 6 of 8: the CDN
answered with another address between resolving and connecting; those runs were excluded and counted), spans-only healthy false
alarms 0% (observed 25%: two healthy runs had a slow model call), 9 (no run timed out, so the loss runs did not hit the 60 s limit).

## What would work better (NOT built or tested here)
* Judge the **run, not one sample**: faults persist across a run's four model calls, healthy spikes are sporadic. The median
  per-call RTT is 1,052 ms under latency vs 36 ms healthy, which separates cleanly where the maximum does not.
* Let kernel evidence **raise a finding by itself** (a connect or lookup far above its healthy median) instead of only
  explaining an already-slow step.
* **Thresholds per span class** (a remote model call's lookup is ~200 ms, the local API tool's ~1 ms) and from far more than 16
  healthy runs; heavy-tailed signals need a robust statistic, not p99 of a handful.
* Packet loss is likely undetectable by retransmit count alone on a path with this background rate.
These would be a new diagnoser version tested on fresh seeds; none of it is claimed here.

## Mistakes found on the way (all fixed)
The first real fault run refused to start: my guard against stacking qdiscs looked for the text "prio", which the normal
`eth0` setup contains ("priomap"); it failed safe, but the same test would also have reported a false "failed to restore". Now
matched by qdisc kind with a test using the verbatim `eth0` output. Also: the "largest send" remote-wait heuristic (above), and a
send from another thread being attached by the port+time fallback (found by a unit test, fixed).

## Limits
One model, one provider, one network path (a NAT'd WSL VM); faults are egress-only and one-way; 8 runs per group; healthy
calibration of 16 runs under-samples heavy tails; the diagnosers used were designed on loopback and not changed for this test.
