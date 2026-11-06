# Stage 4 result: baseline vs combined diagnoser, and the cost of tracing

Everything here is console output of `make eval-full`, `make eval-full-fresh` and `make overhead` on WSL2
(kernel 6.6.87.2), loopback only, mock services, one injected fault per run, 2026-11-06.
95% Wilson intervals in brackets. All runs were recorded with the eBPF collector on; **both diagnosers score the
identical runs**. Step thresholds come from 30 healthy calibration runs (seeds 900000+), same for both diagnosers.

* **base**: spans only (Stage 1 baseline). Cannot split a slow API call into DNS / latency / loss; it lists the three
  with equal score and breaks the tie with a seeded random draw (expected top-1 = 1/3 on those faults).
* **v1**: combined diagnoser. Rules written and frozen before any evaluation (diagnosers/combined.py docstring).
* **v2**: v1 plus ONE rule change made after I saw v1's results on set 1: if a connection had retransmits, a high
  RTT is attributed to the loss, not to latency. Reason: a lost SYN is retransmitted after ~1 s and the kernel's first
  RTT sample includes that wait, so "RTT high" and "retransmits" co-occur under pure packet loss, and v1 split the
  blame equally and broke the tie at random.

## Sets
* **Set 1**: seeds 1000-1029 per fault (same fault parameters as the Stage 1 baseline run). v1 was frozen before
  this ran; v2 was designed after looking at it, so v2's numbers on set 1 are optimistic.
* **Set 2**: seeds 2000-2029, never looked at before the v2 change. This is the clean test of v2 and a replication of v1.

## Headline (30 runs per fault per set; top-1 = first-ranked cause equals the injected fault)

| | set 1 base | set 1 v1 | set 1 v2* | set 2 base | set 2 v1 | **set 2 v2** |
|---|---|---|---|---|---|---|
| dns_delay | 30.0% | 100% | 100% | 33.3% | 100% | 100% |
| net_latency | 26.7% | 100% | 100% | 33.3% | 100% | 100% |
| packet_loss | 23.3% | 70.0% | 90.0% | 23.3% | 76.7% | **90.0% [74-97]** |
| **network faults together (n=90)** | 26.7% [19-37] | 90.0% [82-95] | 96.7% [91-99]* | 30.0% [22-40] | 92.2% [85-96] | **96.7% [91-99]** |
| paired (n=90): diagnoser right & base wrong / base right & diagnoser wrong | | 59 / 2 | 60 / 0* | | 59 / 3 | 60 / 0 |

\* optimistic: designed after seeing this set.

Everything that is not a network fault (slow_tool, slow_db, slow_model, rate_limit; 120 runs per set) and the
healthy runs (30 per set): **base, v1 and v2 are all 100% top-1, with 0 paired disagreements.** The kernel adds
nothing there and takes nothing away.

**False attribution: 0/240 for all three diagnosers on both sets** (upper 95% bound 2%).
All faults together, set 2: base 73.8% [68-79], v1 97.1% [94-99], v2 98.8% [96-100].

## What the numbers do and do not say
* The gain is entirely on the three network-path faults, as predicted before running. Where the cause lives in one
  span type (model, tool, db), spans already suffice.
* **This result is close to guaranteed by the experiment's design**, so do not over-read it: the baseline is
  structurally unable to separate DNS from latency from loss (it sees one HTTP span), and the kernel measures
  those quantities directly. The informative parts are that the real eBPF -> join -> diagnosis chain works end to end,
  that the join was verified against independent ground truth (Stage 3), and the packet_loss and overhead findings.
* packet_loss ceiling: in 3 of 30 runs (set 1) and 3 of 30 (set 2) the loss never hit a packet that mattered, the
  kernel saw 0 retransmits, and nothing anywhere shows a fault. Max possible top-1 is therefore 27/30 = 90%, and
  **v2 reaches exactly that on both sets**. v1 did not (70% / 77%) because of the tie described above.
* "Latent loss" (rule 5 of the diagnoser): retransmits are reported as packet_loss even if no step was slow. Among
  packet_loss runs that were not visibly slow, v1/v2 get 67% (set 1, n=9) and 75% (set 2, n=12) right; the baseline
  gets 0% by construction. The cost side of this rule, spurious retransmits on a healthy network, is only tested on 60
  healthy loopback runs (0 retransmits seen). On a real network, background retransmits would produce false alarms.
* One packet_loss run in set 1 hit the agent's 30 s timeout and errored; it is scored, not dropped.

## Cost of tracing (`make overhead`, 12 rounds, A/A-controlled; effects inside the control interval are noise)

Agent workload, per healthy run (480 timed runs per arm):

| arm | latency mean | latency vs untraced (mean of 12 rounds) | main-thread CPU | process CPU | bpftrace CPU |
|---|---|---|---|---|---|
| untraced | 62.9 ms | (reference) | 6.34 ms | 6.35 ms | - |
| otel | 67.7 ms | **+7.5% [+4.1, +11.3]** | 10.28 ms (+63%) | 10.29 ms | - |
| otel + ebpf | 67.9 ms | **+8.2% [+2.9, +13.0]** | 11.01 ms (+83%) | 14.36 ms (+139%) | 4.54 ms/run, 792 events/s |
| untraced-ctrl (A/A) | 62.5 ms | -0.4% [-5.1, +3.6] | 6.21 ms (+4%) | 6.22 ms | - |

* **The brief's target (under 5% added latency and CPU) is not met.** OpenTelemetry in-process tracing alone adds
  about 7.5% latency and 4 ms of CPU per run (about 18 spans per run, roughly 0.2 ms per span with the SDK's
  synchronous processor). I did not try sampling, batching or lighter instrumentation, so this is the cost of the
  straightforward setup, not a floor.
* eBPF's added **latency** is not resolvable on top of OTel's (+8.2% vs +7.5%, intervals overlap almost entirely).
  Its **CPU** is real: bpftrace uses about 4.5 ms per run, and the collector's helper threads in the agent process
  (JSON parsing, clock sampling) another ~3.4 ms. In total, tracing with eBPF burns about 12.5 ms more CPU than
  untraced per 63 ms run (about 20% of one core on top of the agent's own).
* These runs are 63 ms and dominated by a 50 ms sleep. The api_calls workload (20 back-to-back calls of ~2 ms
  each) showed +61.6% [+25.6, +104.7] for OTel and +58.6% for OTel+eBPF, but its A/A control was +14.4% [-15.7, +46.7]:
  that microbenchmark cannot resolve differences below roughly +-30%, so read it only as "per-call overhead is
  large when calls are short".
* The first version of this benchmark treated iterations as independent and showed an A/A difference of +44%; the
  analysis was changed to use rounds as the unit (disclosed in eval/overhead.py), which is why these numbers differ
  from that first draft.
* Not measured: effect on mock-service processes, machine-wide cost of tracing all TCP (bpftrace sees ~80 events
  per run of unrelated loopback traffic here), collector startup (~1 s per recording), memory.

## Limits (carry into the write-up)
* Faults are injected by me, in a closed catalog; the combined diagnoser's rules were written knowing that catalog
  (in-distribution). How it behaves on faults I did not inject (a slow API server, congestion, a flaky resolver,
  several simultaneous faults) is untested. Rule 4 falls back to the baseline's three-way split when the kernel sees
  nothing, so it should degrade to baseline, but that is not measured.
* Loopback only, mock model and mock APIs, plain HTTP (no TLS), sequential single-thread agent, large fault
  magnitudes (hundreds of ms) against 1-50 ms healthy steps. Subtle faults and noisy real networks are the hard case.
* Two latency-affecting faults at once, or loss together with real added latency, is undefined: v2 attributes
  everything to loss when retransmits are present.
* The hypothesis was tested in the form "does the combined diagnoser identify the injected fault more often than the
  agent-only one": yes for network-path faults (about 30% -> 97% top-1, two independent sets of 90 runs each),
  no difference elsewhere, no extra false attributions. Whether this carries over to real deployments is not shown.
