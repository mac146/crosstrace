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

## Part 2: the same comparison under concurrency
Harness: [eval/selective_conc.py](../eval/selective_conc.py) (`python -m eval.selective_conc --agents 1 8 32`). C agents (threads, one trace each)
run the mixed scenario in lockstep against ONE API server (latency window for calls 1 and 3, slow answers for calls 2 and 4); truth for every
agent is {net_latency, slow_api}. One collector; each agent diagnosed on its own spans; the host-wide arms pool over ALL agents' HTTP spans.
Thresholds are calibrated at the same concurrency (12 healthy runs) and 6 more healthy runs per level measure false alarms.
Predictions 6-8 (predictions-stage14.md, Part 2) all held.

| agents at once | mixed runs (agent-runs) | v3 joined: both in top 2 | runs where every agent was right | pool / pool_port / obi | healthy false alarms (v3) | HTTP spans with kernel events | collector warnings |
|---|---|---|---|---|---|---|---|
| 1 (control) | 20 (20) | 100% [84, 100] | 20/20 | 0% / 0% / 0% | 0/6 | 104/104 | 0 |
| 8 | 20 (160) | 100% [98, 100] | 20/20 | 0% / 0% / 0% | 0/48 | 832/832 | 0 |
| 32 | 10 (320) | 100% [99, 100] | 10/10 | 0% / 0% / 0% | 0/192 | 2048/2048 | 0 |

- **The join keeps its side of the result under concurrency**: 100% at 8 and at 32 simultaneous agents, with every HTTP span joined to
  kernel events, no orphans and no lost events. Healthy thresholds rose with load (http_api 57 -> 71 -> 102 ms) and calibrating at the same
  load kept healthy false alarms at 0.
- **Concurrency did not widen the gap**, as predicted: the cheap baselines were already at 0% in this scenario, so 0% is also all they can show.
  What this adds is evidence that the join does not degrade where host-wide counters get most mixed.
- Not shown, and not claimed: any case where host-wide or per-destination metrics fail worse than 0%; agents with DIFFERENT faults at the same
  moment (a netem window is per port, so the injector cannot give two agents different network faults on one server).

## Limits specific to part 2
- Lockstep threads in one process on loopback, one server; the 512-thread/16-process join checks (Stages 9 and 12) used a different, simpler workload.
- Agents of one run share a server and a window, so agent-runs are not independent: the intervals are optimistic; the per-run column is the safer count.
- `pool_entries` (host-wide pooling across agents) is exercised only by this integration run, not by a separate unit test.
