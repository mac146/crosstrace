# Stage 14 results: what the join adds beyond the strongest cheap baselines

Predictions: [predictions-stage14.md](predictions-stage14.md), committed before any code or run. Harness: [eval/selective.py](../eval/selective.py)
(`python -m eval.selective --n 20 --cal 30`; recorded runs are re-scored with `--no-record`). 30 healthy calibration runs, 20 runs per
scenario, 4 sequential API calls per run, 80/80 HTTP spans per scenario had kernel events, 0 orphans, 0 collector warnings.
The compiled collector was used. No thresholds were tuned on the scenario runs.

## Results
| scenario (truth) | base | **v3 joined** | pool (host-wide) | pool_port (per destination) | obi (emulated) |
|---|---|---|---|---|---|
| none: said "none" | 20/20 | 20/20 | 20/20 | 20/20 | 20/20 |
| sel_slow_api: server slow on calls 2,4 (top-1) | 25% | **100%** | 25% | 100% | 100% |
| sel_latency: latency window on calls 2,4 (top-1) | 15% | **100%** | 100% | 100% | **0%** |
| two_servers: A slow, B latency (both in top 2) | 35% | **100%** | **0%** | **100%** | 0% |
| mixed_one_server: latency on calls 1,3, slow server on 2,4 (both in top 2) | 15% | **100%** | **0%** | **0%** | 0% |

Intervals (n=20): 100% = [84, 100], 0% = [0, 16]. Predictions 1-5 all held, including the two I was least sure of (S3 at 65%, S4 at 70%).

## What the join adds, honestly
- **Over host-wide counters** (what a basic metrics stack gives): a lot, as soon as one request or one server differs from another.
- **Over per-destination metrics with a per-request server wait** (`pool_port`, the strongest cheap baseline I built): **nothing, unless two
  different problems hit the SAME destination in the same run.** With the faults on different servers it ties the join at 100%; with a slow
  answer and a latency window on one server it gets 0%, because every slow call sees both a high wait and a high RTT from the same
  destination, and the rules blame the network. Only per-call evidence tells them apart.
- **Over an OBI-style per-request duration only** (`obi`): it names a slow server (100%) but cannot name any network cause (0% on latency).
  The kernel's RTT, DNS and retransmit evidence is what separates network causes; OBI-style timing alone does not.
- So the honest summary: the diagnostic value comes from (1) kernel-level network evidence (RTT, DNS, retransmits) AND (2) per-request
  server timing; the per-span join is the thing that keeps them apart only in the narrow mixed-same-destination case.

## Limits (read before quoting any of this)
- **The mixed scenario was designed by me to discriminate**, from the question "where could per-call evidence matter?". It is a plausible
  situation (two different problems at one service within a run) but not a measured frequency of anything real.
- `obi` is an EMULATION of the information content of an eBPF auto-instrumentation agent, not Beyla/OBI itself, which was not installed or
  run. A real agent also gives server-side spans and propagation that this does not model.
- The runs are sequential (one agent, four calls) on loopback with mock servers. The join was checked separately under 512 concurrent threads
  (Stage 9/12), but the pooled-vs-joined comparison was NOT repeated under concurrency, where host-wide and per-destination counters mix many
  agents' traffic and the gap should grow; that is untested.
- Only latency and slow-server faults were mixed. Packet loss, DNS and the model-call path were not part of this stage.

## A bug found on the way
The first smoke test showed retransmits on calls that only had a latency window. Removing the netem qdisc drops packets still queued in it
(delayed FIN/ACKs); TCP retransmits them, and the flow join attached those retransmits to the call, so a latency fault looked like loss and
`v3` scored 0% on the mixed scenario. The harness now waits for the queue to drain before removing the window. The correlator was right to
attach them (they belong to that connection); the fault injector was the cause. The same effect may have mattered in earlier runs that removed a
qdisc while packets were in flight, but those faults were applied to the whole run, where the retransmits land after the last span.
