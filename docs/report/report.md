> **DRAFT.** Written from the stage documents in `docs/`; every number is cited to the stage document that holds it. A first review pass (by the assistant, against those documents) removed unsupported figures and tightened claims; it has not yet been read by the author. Open TODOs are listed at the end.

# Does kernel-level tracing help diagnose slow AI agents? A pre-registered benchmark, with negative results

**Author:** Mayank Kumar Singh (independent)  ·  **Code and data:** https://github.com/mac146/crosstrace (commit: TODO, fill in the hash of the final commit this report describes)

## Abstract

A slow AI agent run is a chain of network calls (model, tools, database, outside APIs). Application-level traces show which call was slow but cannot tell a DNS stall from a slow connection from a slow server, because all three happen inside one HTTP span. We built a small fault-injection benchmark, an eBPF collector, a correlator that aligns kernel events with spans, and rule-based diagnosers, and measured how often the first-ranked cause equals the injected fault. Where the cause lies in the network path, adding kernel evidence raised top-1 accuracy from about 30% to about 97% on two independent sets of 90 runs (mock services, loopback; Section 4.4), with no false accusations in 240 runs per set. That result is largely built into the experiment, and we say so. Two later stages narrow it. First, host-wide counters given the run's maximum server wait (a generous upper bound: real host counters have no such number) match the full per-span join on this benchmark, so what carries the result is per-request kernel timing, not the join to a span. Second, the join clearly beats the strongest cheap baseline we built only when two different problems hit the same destination in one run (100% vs 0%, 20 runs, and again with 8 and 32 simultaneous agents), a scenario we designed to discriminate. The real OpenTelemetry eBPF agent (OBI v1.0.0), run on the same scenarios, names a slow server as well as the join; with only its request timing it is at chance level on network causes, and with its Prometheus-style aggregates it fails when two problems are present, but with its per-connection RTT statistics matched to calls by port and order it comes within one run of the join (95-100%) in these sequential scenarios. Under concurrent agents in different conditions on one server (Stage 16), OBI matched by order drops to 63-69%, while OBI matched exactly by the client's source port is as good as our join (99.5-100%): the correlator's advantage is matching without a declared port, not better evidence. On a real model over the real internet the picture is less favourable: a one-second network fault never crosses a step threshold, and packet loss cannot be told from the path's background retransmits by count (70% detection, 10 runs per group, one path). From Stage 6 on, each stage's predictions were committed to git before its runs, and several were wrong; we report those. We make no claim about production deployments, HTTP/3, streamed responses or multi-host systems.

## 1. Problem and hypothesis

An agent answers one request with a sequence of model calls and tool calls. Application-level tracing (OpenTelemetry spans following the GenAI conventions) shows a timeline, but a slow HTTP call is opaque: DNS lookup, TCP connect, retransmits and server time all sit inside one span. System-level eBPF tools see those quantities but know nothing about the agent.

**Hypothesis.** Joining agent-level spans with kernel-level network events diagnoses injected faults more accurately than agent-level spans alone. "Diagnoses" means the first-ranked cause equals the injected fault (top-1); top-3 and a false-attribution rate are secondary. False attribution means the first-ranked cause points at a component that was healthy.

The question was refined as results came in (Sections 4 and 6): it became *which part* of the combined evidence carries the gain, and *what* the per-span join adds over cheaper alternatives that also use kernel data.

## 2. Related work

A first pass of a few web searches, not a systematic review; "not found" does not mean "does not exist". Reliability markers follow `docs/market-scan.md`: **P** primary or well sourced, **S** secondary (vendor blog or aggregator quoting a report).

- **AgentSight** (arXiv 2508.02736, P): eBPF reading of an agent's TLS-encrypted LLM traffic plus its syscalls, linked by process lineage, a 100-500 ms time window and argument matching, judged by a second LLM. It targets security and behaviour (prompt injection, reasoning loops), evaluates three qualitative cases and about 2.9% overhead, and reports no diagnosis accuracy, no comparison with agent-only traces and no network faults (`docs/results-stage13.md`).
- **OpenTelemetry eBPF Instrumentation (OBI, formerly Beyla), Pixie, Odigos** (P for OBI's documentation): eBPF telemetry generators. OBI documents TCP RTT, retransmit and socket I/O metrics plus per-request application spans (kernel 5.8 or later). We found no agent-span joining or scoring in them. We ran OBI v1.0.0 in Stage 15 (Section 6); the earlier "obi" arm of Stage 14 was an emulation of its information content and has been corrected.
- **Microservice RCA benchmarks** such as RCAEval (P): injected faults and top-k scoring, on microservices rather than LLM agents and without kernel traces.
- **Agent observability market** (S): funding concentrates on tracing, evaluation and cost. In LangChain's practitioner survey (1,340 respondents, via vendor blogs) 94% of production agents have some observability and 71.5% trace individual steps and tool calls. Datadog's report (also via vendor blogs) reportedly puts rate-limit errors at nearly a third of LLM call failures in March 2026, a provider-side cause.
- **Protocol limits of kernel probes** (P/S mix): Linux has no in-kernel QUIC, so HTTP/3 is invisible to TCP-level probes; stream-level HTTP/2 or TLS content needs user-space probes or in-process instrumentation.

We found no prior work that scores cross-layer diagnosis of agent runs against injected faults, and no measurement of the "cheap metrics stack" alternative. That is a statement about our searches, not a claim of novelty.

## 3. Method

**Agents and services (`agent/`, `mockservices/`).** A small hand-instrumented agent (model call, DB query, HTTP API call, slow tool) against mock model, API and DNS servers started as real subprocesses on loopback. Later stages add a LangGraph agent (`create_react_agent`) against the mock, and against a real model (`z-ai/glm-5.3-flash` through OpenRouter). Spans follow the GenAI conventions and carry `process.pid` and `thread.id`.

**Fault injection (`faultlab/`).** Seven faults plus healthy (`dns_delay`, `net_latency`, `packet_loss`, `slow_tool`, `slow_db`, `slow_model`, `rate_limit`), later `slow_api`, subtle (20-100 ms) magnitudes and eight fault pairs. Magnitudes come from a seeded generator and were not tuned against either diagnoser. Network faults use `tc netem` on loopback scoped to one port; real-path faults use `tc` on the default interface scoped to the model server's addresses, with guards and guaranteed cleanup.

**Collector (`collectors/`).** (a) A bpftrace program: DNS via `sendto`/`recvfrom` (optionally libc `getaddrinfo`), TCP connect via a `tcp_v4_connect` kprobe, connection completion via an fentry hook on `tcp_finish_connect` (tracepoint as fallback), retransmits, smoothed RTT samples, and optionally every `tcp_sendmsg`. (b) From Stage 12, a compiled C/libbpf CO-RE collector emitting fixed-size binary records through a ring buffer, decoded after the run, same event schema. Both use CLOCK_MONOTONIC.

**Correlator (`correlator/`).** Converts kernel timestamps to the spans' wall clock using monotonic/wall pairs sampled every 100 ms and interpolated only between adjacent pairs (the wall clock stepped by about 3.5 ms against the monotonic clock in a few percent of runs on WSL). Events attach to spans by, in order: a span's declared local port (`network.local.port`), pid + thread + time, the connection's four-tuple, and a port + time fallback. Unattached events are reported with reasons. Lookups are indexed, so cost is linear in events.

**Diagnosers (`diagnosers/`).** All use the agent's steps (model and tool calls) and the same healthy-duration thresholds from calibration runs (coarse: `max(p99, 3 x median) + 50 ms`; tight: `p99 + 5 ms`). *Baseline*: blames the span type that was slow; a slow API step is split at random among the network causes. *Combined* rules are transparent: DNS time above its healthy threshold goes to `dns_delay`; the rest goes to `net_latency` (smoothed RTT above threshold), `packet_loss` (retransmits) or, in v3, `slow_api` (server wait above threshold with everything else healthy). v1 was frozen before any evaluation; v2 (retransmits explain a high RTT) was changed after seeing v1's results; v3 adds the slow-server rule; v4 a healthy retransmit baseline for latent loss; v5 per-class thresholds and run-level medians for real networks. Stage 13 adds evidence arms that feed v3's rules different evidence: *pool* (run-wide maxima, no per-request wait) and *poolw* (plus the run's maximum server wait). Stage 14 adds *pool_port* (pooled per destination port, with server wait) and *obi* (per-request server wait only; an emulation); Stage 15 replaces the emulation with real OBI output (*obi_real*).

**Metrics (`eval/metrics.py`).** Top-1, top-3, false attribution, and for pairs "both injected faults in the top two". 95% Wilson intervals.

**Pre-registration discipline.** From Stage 6 on, a `docs/predictions-stage*.md` file with numeric predictions and "I would be wrong if" conditions was committed to git before each run, and results were scored against it. Rules written after seeing results are marked as such and judged on fresh seeds. Stages 1-4 predate those files: the v1 rules were frozen in code before evaluation, but there is no separate predictions file for them (TODO: state the commit that froze v1).

## 4. Results

All runs are on WSL2 (kernel 6.6.87.2), loopback, mock services, unless stated. Each subsection cites the stage document that holds the numbers.

### 4.1 Baseline (`results-stage1.md`)
30 runs per fault, 30 healthy calibration runs. The spans-only baseline is 100% top-1 on faults confined to one span type (`slow_tool`, `slow_db`, `slow_model`, `rate_limit`, healthy) and 26.7-30.0% on the three network-path faults (about chance, 1/3). Those non-network faults are 300 ms or more against 1-50 ms healthy steps, so they are easy by design. In 23% of `packet_loss` runs (7/30) the loss was never visible.

### 4.2 Do the raw kernel events show the faults? (`results-stage2.md`)
Five seeded runs per fault (a qualitative check). Injected vs measured: DNS delay 804 ms vs 805.1 ms, connect time under a 200 ms-per-direction latency 400 ms vs 400.3 ms, response gap for `slow_model` 313 ms vs 314.6 ms. `slow_tool` and `slow_db` are not visible in the kernel (only as a quiet gap) and need the span that covers it.

### 4.3 The join (`results-stage3.md`)
Over 160 runs (20 per fault), **160/160 passed all join-integrity checks** against ground truth the agent records (the real client port of each socket, never read by the correlator). The first version passed 16/40; the checks found neighbour stealing, late teardown retransmits, float precision in the clock map and wall-clock steps, all fixed. One refinement of the check itself was made after seeing a failure (post hoc). The wall clock stepped by more than 1 ms in 7/160 runs, all still passing.

### 4.4 Baseline vs combined (`results-stage4.md`)
Top-1 on the three network-path faults together (n = 90 per set):

| | spans only | v1 (frozen first) | v2 (changed after set 1) |
|---|---|---|---|
| set 1 (seeds 1000-1029) | 26.7% [19-37] | 90.0% [82-95] | 96.7% [91-99] (optimistic: designed after this set) |
| set 2 (seeds 2000-2029, fresh) | 30.0% [22-40] | 92.2% [85-96] | **96.7% [91-99]** |

On the other four faults (120 runs per set) and healthy runs, all diagnosers are 100% with zero disagreements. False attribution was 0/240 for all three diagnosers on both sets. `packet_loss` tops out at 90%: in 3 of 30 runs per set the loss hit nothing that mattered. **This result is close to guaranteed by the design** (the baseline cannot separate those causes by construction); what it demonstrates is that the chain works end to end.

### 4.5 Harder tests (`results-stage6.md`, `results-stage7.md`)
- *Slow server, healthy network.* v2 blamed the network in 100% of runs (0% right); v3, after one added rule, was 100% [89-100] on fresh seeds.
- *Subtle 20-100 ms faults.* With the tight threshold rule v3 was 100% on `dns_delay`, `net_latency`, `slow_api` (spans only: 33%, 17%, 20%) with 0/60 healthy false alarms; the coarse rule is blind below its 50 ms margin.
- *Two faults at once (8 pairs x 30, both in top two).* v3 100% on four pairs, 93% and 90% on two more, 73% on `rate_limit + slow_db` (after fixing a flaw in my test; the first version of that pair is the flawed 23% in `pair_a`); spans only 3-30%, except 100% on `slow_model + slow_tool` (no network involved).
- *Busy machine.* A tight threshold calibrated idle and used under full CPU load false-alarmed on 13/60 healthy runs (22%); calibrated under the same load, 0/60.
- *Third-party framework (LangGraph, mock model).* v3 96.7-100% on the same faults, 0/30 healthy false alarms; spans stayed attached across worker threads.
- *Real model over the internet (8 runs per group).* Mock-calibrated thresholds are useless there; only about 1% of model spans had connect-event evidence at that point (the client reuses connections; a TCP-send probe raised this to 100% in Stage 8); and v3's latent-loss rule false-alarmed on 5/8 healthy runs because real connections routinely show retransmits. v4 (healthy retransmit baseline of 5 per run) cut false alarms on 16 fresh healthy runs from 11/16 (v3) to 1/16, at an unmeasured cost to detection (`results-stage7.md`).

### 4.6 The real network path (`results-stage8.md`, `results-stage10.md`)
Faults injected with `tc` on the real path to a real model (8-10 runs per group, one NAT'd network path: observations, not accuracy estimates). The kernel measured them: a 991 ms one-way latency as a 1,060 ms connect; DNS delay within 15% in 7 of 8 runs. The diagnosers did not keep up at first: a 1 s fault never crosses a step threshold when model calls take 1-3 s (83% of `net_latency` runs got no answer), and healthy kernel signals have heavy tails. v5 (per-class thresholds, run-level lower median RTT, run-level DNS) on fresh seeds (10 per group, 32 healthy calibration runs):

| group | spans only | v3 | v4 | v5 |
|---|---|---|---|---|
| healthy (false alarms) | 0/10 | 5/10 | 0/10 | 0/10 |
| dns_delay (top-1) | 0% | 10% | 10% | **100%** |
| net_latency (top-1) | 0% | 0% | 0% | **90%** |
| packet_loss (top-1) | 0% | 100% | 70% | 70% |

v5 gave the same first cause as v4 on all 870 mock evaluations. The thresholds it learned put the detection floor at about 2.3 s of DNS and about 1 s of latency on this path; faults below that are undetectable here. Two simultaneous real faults (`results-stage11.md`): `dns_delay + net_latency` both in the top two in 7/8 runs, `dns_delay + packet_loss` in 6/10 (spans only 0/8 and 0/10).

### 4.7 Scale, async agents, remote servers (`results-stage9.md`, `results-stage11.md`)
- With up to 512 simultaneous agent threads (16 processes x 32 threads), 5,120/5,120 spans got exactly their own connection (0 wrong, 0 foreign events, no lost connect events) once the mock servers' listen backlog was raised (the first run's task errors were my test servers').
- Sustained churn (about 75,000 connections per run, ephemeral ports reused many times): 0 wrong connections; at 512 threads with backlog 256, 57-94 spans (0.06-0.13%) lacked connection evidence, never wrong evidence (Section 4.8).
- The correlator was quadratic and is now linear: 51,200 tasks (256,000 events) in 0.94 s.
- Async agents on one thread cannot be joined by pid and thread id (0/1,280 spans correct at 64 coroutines); with `network.local.port` on the span the join was 1,280/1,280. That is an instrumentation requirement.
- A known-slow remote server over TLS (server events removed before the join): after redefining the wait as the longest silence after the request, 95/96 calls within max(10%, 30 ms) of the injected delay, against 85/96 for the old definition.

### 4.8 The collector (`results-stage12.md`)
- Missing connect events: a controlled run recording both probes showed the fentry hook saw all 62 connections the tracepoint missed. With fentry, 3 of 74,240 spans (0.004%) lacked connection evidence at 512 threads and backlog 256, for both the bpftrace and the C collector. In the examples inspected the connection's SYN had been retransmitted; the cause of the remaining 3 is not established.
- A compiled C/libbpf collector against the bpftrace collector, 12 rounds of 40 runs, per 60 ms agent run (one session's figures; overhead varies between sessions by more than within one):

| measure | bpftrace (fentry) | compiled C |
|---|---|---|
| collector CPU per run | 5.05 ms | 1.72 ms (2.9x less) |
| latency vs untraced | +3.6% [2.7, 4.4] | +1.6% [-0.6, 3.2] |
| start-up (median of 5) | 0.94 s | 0.31 s |
| join integrity (160 runs) | 100% | 160/160 |
| 512-thread churn, missing connect-ends | 3 / 74,240 | 3 / 74,240 |

The first compiled build joined 0/160 runs because it reported pids as the kernel's global ids, not the caller's pid namespace; the integrity check caught it and the loader now translates them.

Overhead across stages (`results-stage4.md`, `results-stage6.md`): OpenTelemetry alone added +7.5% latency in the first session and -0.3% to +3.5% across three later sessions (about 1.3 ms of CPU per 60 ms run); the eBPF collector added +2.3% to +5.2% latency with the bpftrace collector, with CPU as the missed target until the compiled collector.

## 5. Negative results and wrong predictions

1. **The network gain is built in** (Stage 4): the baseline cannot separate DNS, latency and loss by construction.
2. **v1's `packet_loss` ceiling** (70% and 77%) came from a tie rule I had not anticipated; v2's fix was made after seeing set 1 and judged on set 2.
3. **Two Stage 6 predictions wrong:** I expected the tight rule to false-alarm on 3-30% of healthy runs and that no rule would give both high detection and zero false alarms; on an idle machine it gave both (0/60). Idle-calibrated false alarms under load were 22%, below my predicted 30% or more.
4. **A real flaw in v3**, found and left (v3 was frozen): on a 429 the `slow_model` hedge outranked a real slow database in 8 of 30 runs.
5. **v3 on a real network:** the latent-loss rule false-accused healthy runs (5/8, then 11/16), invisible on loopback where retransmits are 0. v4's fix has a price: detection falls on that path (Stage 8: 50%; Stage 10: 70%, 10 runs).
6. **Real-model step thresholds cannot see a 1 s fault**, and the p99 of 16 healthy runs is effectively their maximum (RTT threshold 1,289 ms, above the injected 1 s). Stage 8 also had several wrong predictions.
7. **Packet loss on the NAT'd path is not separable by retransmit count** (Stage 8), and a better-looking alternative feature on the design set failed on fresh data (6/10 detected, 2/10 healthy false alarms for both rules; predictions 4 and 5 wrong or unconfirmed, Stage 12). Left as a documented limit. An untested idea: spurious-RTO events.
8. **Missing connect events at 512 threads** (Stage 11): three hypotheses (reader dropping lines, bpftrace buffer overflow, the per-socket map filling) were rejected by test, and a speculative fix did nothing and was reverted. Stage 12 then found the probe cause (tracepoint skipped while another BPF program ran; fentry saw all 62). The mechanism remains a hypothesis that fits the data.
9. **CPU target missed twice:** shared provider and in-kernel filtering did not reduce overhead (Stage 6); the compiled collector gave 2.9x against my 3.4x prediction (1.72 ms against at most 1.5 ms).
10. **Clock-sync prediction wrong** (Stage 12, section E): stretching clock sampling was predicted to cut the compiled collector's in-process CPU by at least a third; it fell 12% (0.89 to 0.78 ms). The reader thread, not the sampling, dominates; not split further.
11. **Stage 13's blind spot:** I did not predict that adding only the run's maximum server wait to host-wide counters would match the join on every group.
12. **A harness bug that looked like a result** (Stage 14): removing a netem qdisc dropped queued packets, TCP retransmitted them, and a latency fault looked like packet loss (the join scored 0% on the mixed scenario in the smoke test). Fixed in the harness by draining the queue.
13. **My own validation was once vacuous** (Stage 3): the check reported 80/80 on one-span runs after the agent crashed; it now rejects runs that did not complete.

## 6. What the join adds beyond cheap baselines (`results-stage13.md`, `results-stage14.md`)

**Arms** (same rules as v3, same thresholds, same recorded runs; only the evidence differs): `v3` per-span joined evidence; `pool` run-wide maxima and totals for every step, no per-request wait; `poolw` pool plus the run's maximum server wait (an upper bound: a host counter has no such number); `pool_port` per-destination counters and server wait; `obi` an emulation of an eBPF auto-instrumentation agent's information content (per-request server duration; no DNS, RTT, retransmits). `obi` is not the real tool (it was replaced by real OBI output, `obi_real`, in Stage 15 below).

**Stage 13, recorded runs** (slow-API suite on two fresh-seed sets and on LangGraph; pairs; 30 runs per fault). Single network faults: joined and pooled tie at 100% (packet loss 93-97% for both). `slow_api`: `pool` falls to 27-37% (the spans-only level), `v3` and `poolw` 100%. Mean "both in top two" over the 8 pairs: spans only 28.7%, `v3` 88.3%, `pool` 54.6%, `poolw` 88.3%; this mean includes the known-flawed `rate_limit + slow_db` pair (23.3% in every arm), which does not change the comparison between arms. Healthy false alarms 0/90 in every arm. The pooled arms were given only the run's own events, which favours them.

**Stage 14, selective faults** (four sequential API calls per run; 30 healthy calibration runs; 20 runs per scenario; compiled collector):

| scenario (truth) | spans only | v3 joined | pool | pool_port | obi (emulated) |
|---|---|---|---|---|---|
| slow server on calls 2, 4 (top-1) | 25% | 100% | 25% | 100% | 100% |
| latency window on calls 2, 4 (top-1) | 15% | 100% | 100% | 100% | 0% |
| two servers: A slow, B latency (both in top 2) | 35% | 100% | 0% | 100% | 0% |
| latency on calls 1, 3 and slow server on 2, 4 (both in top 2) | 15% | 100% | 0% | 0% | 0% |

Intervals at n = 20 are 100% = [84, 100] and 0% = [0, 16]; healthy runs: 20/20 "none" in every arm. **Under concurrency** (C agents in lockstep, one server, host-wide arms pooled over all agents): `v3` found both causes for 100% of agent-runs at C = 1, 8 and 32 (20, 20 and 10 mixed runs; every agent right in 20/20, 20/20, 10/10), with 0/6, 0/48 and 0/192 healthy false alarms, all HTTP spans joined and no collector warnings; the cheap baselines stayed at 0%. Healthy thresholds rose with load (http_api 57, 71, 102 ms) and were calibrated at each load.

**Stage 15, real OBI** (`results-stage15.md`; OBI v1.0.0 from the official release, tarball checksum verified, Sigstore signature not verified; same scenarios and seeds, OBI running as root next to our collector; OBI reported all 400 scenario calls after a per-server warm-up). Feeding v3's rules only OBI's per-request server duration (`obi_real`): slow server 100% (OBI's duration matched the injected delay on 118/118 slow calls); latency 15%, two servers 40%, mixed 25% (the equal-split fallback, i.e. chance, not network diagnosis); healthy 0/20 false alarms; `v3` 100% on all of them. Our Stage 14 emulation was too pessimistic on latency (it counted network delay as server time and scored 0%); real OBI is at chance level there, and the comparison with the join is unchanged. One prediction missed narrowly (v3 ahead by 75 points on the mixed scenario, predicted at least 80).

**Stage 15, part 2: OBI with its network statistics** (`results-stage15.md`, predictions committed first; OBI's `stats` mode printed a client-side RTT line for 400/400 scenario calls). `obi_real+net` (run-wide RTT, what OBI's Prometheus metrics hold): latency 100%, two servers 5%, mixed 0%. `obi_real+flows` (each call's own connection RTT, matched by port and order): latency 100%, two servers 100%, mixed 95% [76, 99], healthy 0/20 false alarms; `v3` 100% on all. All five predictions held. So in the sequential case the diagnostic value is carried by per-call RTT plus per-request server timing, which OBI can provide; our correlator is what lets this work without order matching (concurrency, reused connections), but that advantage over OBI plus glue was not measured. Packet loss and DNS were not tested with OBI.

**Stage 16, concurrent agents in different conditions** (`results-stage16.md`, predictions committed first). One API server; 9 and 24 agents in lockstep of three kinds on the same path (healthy; source address with injected latency; source address with a slow-answering server); each agent scored against its own truth; 108 and 192 agent-runs. `v3` 100% and 99.5%; spans only 44%/46%; host-wide pooled arms 61%/45%; `obi_real+flows` (order matching) 69%/63%; `obi_real+port` (exact match by the client's source port, which needs the span to declare its local port) 100%/99.5%; OBI evidence available for every call by exact port. Four of five predictions held; the pooled-arm range was wrong at 24 agents (45%, predicted 55-75%), and a post-hoc follow-up traced the kind-N drop to stray retransmits in other agents' connections being pooled (rescoring with pooled retransmits removed gives 36/36 and 64/64); host-wide evidence therefore degrades with concurrency, per-call evidence does not. So OBI's evidence plus an exact per-call match equals our join; what the correlator adds is a way to match without the application declaring its socket (it uses process, thread, time and connection, and fails for async agents on one thread unless the port is declared).

**What we can honestly say.**
- Over host-wide counters, the join adds a lot as soon as calls or servers differ.
- Over per-destination metrics with a per-request server wait, it adds nothing unless two different problems hit the same destination in one run.
- Over per-request duration alone, kernel RTT, DNS and retransmit evidence is what separates network causes.
- Real OBI behaves as I credited it on slow servers and at chance level on network causes from request timing alone. With its per-connection RTT statistics matched by port and order it reaches 95-100% in sequential scenarios but 63-69% with concurrent agents in different conditions; matched exactly by the client's source port it equals our join (99.5-100%). The per-span join therefore adds a way to match without a declared port, not better diagnostic evidence.
- The mixed scenario was designed by us to discriminate; its real-world frequency is unmeasured.

## 7. Threats to validity and limits

- **Mock services on loopback, plain HTTP, one host and one clock.** The real-model runs use one model, one provider and one NAT'd network path with 8-10 runs per group and 12-32 calibration runs.
- **Injected, in-distribution faults.** The fault catalogue is mine, and the combined rules were written knowing it. Faults outside it (congestion, flaky resolvers, three or more faults) are untested; faults were large relative to healthy steps except in the subtle suite.
- **Sequential or lockstep agents.** Stage 14 concurrency is lockstep threads in one process on one server; a netem window is per port, so different agents cannot be given different network faults at the same moment. Agents of one run share a server and a window, so agent-runs are not independent and the intervals are optimistic; the per-run count is the safer one.
- **Real OBI was run once, in one configuration** I tuned to work on this machine (probes are keyed by executable, so a warm-up was needed; route harvesting disabled; unrelated processes excluded), with `stats` mode added in part 2 (a debugging-style printed output I parsed), and without its client spans or trace propagation or its retransmit and DNS information. The Stage 14 "obi" arm was an emulation and was too pessimistic on latency.
- **The mixed-same-destination scenario was designed to discriminate.** It shows where per-call evidence can matter, not how often it does.
- **Protocol coverage.** All traffic was HTTP/1.x over TCP (plain, or TLS on loopback to a mock). HTTP/3 over QUIC is invisible to TCP-level kernel probes; HTTP/2 streams and streamed (SSE) answers were not tested.
- **Instrumentation requirements.** The join needs `process.pid`, `thread.id` and server address/port on spans (our tracer adds them; whether standard Python SDK setups do was not verified); async agents on one thread need `network.local.port`. OBI plus exact matching needs `network.local.port` too, which a text search of the OpenTelemetry Python requests, httpx, urllib3 and aiohttp-client instrumentations (0.66b2) found none of them setting (not conclusive: shared helpers were not searched). Neither approach works on a stock stack without custom instrumentation.
- **Privileges.** The collector needs root-equivalent access (passwordless `bpftrace`, `tc`, and the compiled collector installed to a root-owned path). It is for development machines and throwaway VMs, not for production as built.
- **Thresholds are per deployment.** They must be calibrated on the deployment's own healthy runs and load; mock-calibrated thresholds were useless on a real model, and an idle-calibrated tight threshold false-alarmed on 22% of healthy runs under load.
- **Unmeasured:** production traffic and fleets, multi-host systems and clock skew between hosts, machine-wide collector cost, the prevalence of network-level causes in real agent incidents (the market scan found no figure; one dataset reportedly puts provider-side rate limits at nearly a third of LLM call failures), and any user study of time-to-diagnosis.

## 8. Reproducibility

- Fault parameters, tie-breaks and run order are seeded and repeat exactly; timings do not, so percentages move by a few points between runs (README "Reproducibility"). Healthy-duration thresholds are rebuilt from fresh calibration runs each time.
- `make reproduce` (root, about 40 minutes, idle machine) records both evaluation sets and the overhead benchmark from an empty database; the other harnesses have their own commands in the Makefile and in each stage document (`python -m eval.selective --n 20 --cal 30`, `python -m eval.selective_conc --agents 1 8 32`). TODO: confirm whether `make reproduce` covers the Stage 13-14 results or add targets for them.
- Every stage from 6 on has `docs/predictions-stage*.md` committed to git **before** its runs, with "I would be wrong if" conditions, and each results document scores them.
- Tests: 260 passed, 1 skipped at the last full run (TODO: refresh at the final commit); live eBPF tests need `scripts/setup_sudoers.sh`.
- The real-model runs cost a few cents in total (TODO: sum the exact figure); the API key was kept outside the repository (`.env`, git-ignored). Those runs cannot be reproduced without your own model access.

## Appendix: decision log

`docs/decisions.md` records every design decision and why, D1-D41, including the ones changed after results came in (for example D32 v5, D36 a reverted fix, D38 fentry for connect-end, D40 and D41 the narrowing of the join's claim).

---

## Open TODOs
- Commit hash of the final version (author and repository URL are filled in), refreshed test count.
- Author review: read every limit and headline claim once; the assistant's pass could not judge what a skeptical reader would question.
- State which commit froze the v1 rules (Stages 1-4 have no predictions files).
- Confirm `make reproduce` coverage of Stages 13-14.
- Sum the real-model spend.
- Stage 15-16: packet loss, DNS and async agents with OBI are untested; Stage 16 agents differ by source address (a constructed difference); the stock-instrumentation check of `network.local.port` and thread/process ids was by text search only; the Sigstore signature of the OBI release was not verified (no cosign).
- Exact figures for Stage 12 predictions 6 and 7 are not in `results-stage12.md`.
