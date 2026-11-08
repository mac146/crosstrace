# crosstrace

Does joining **agent-level traces** with **OS/network-level traces** find the root cause of a slow AI-agent run
better than agent-level traces alone?

An AI agent answers one request with a chain of network calls (model, tools, database, outside APIs). Tracing tools
that work at the application level show a timeline of spans, but cannot see DNS stalls, connection-setup delays or
TCP retransmits. System-level eBPF tools see those but know nothing about the agent. This repo is a prototype that
captures both, joins them into one timeline, names the most likely cause with evidence, and measures how often that
is right against faults it injected itself.

It contains (a) a reproducible fault-injection benchmark for a small agent, (b) a cross-layer tracer, correlator
and diagnoser, and (c) the evaluation and results below. It is a research prototype, not a monitoring product.

## Result

**For faults whose cause is in the network path, joining the layers helps a lot. For every other fault it makes no
difference. It caused no false accusations in these experiments. The tracing overhead target was missed.**

Network-path faults (`dns_delay`, `net_latency`, `packet_loss`), top-1 accuracy = the diagnoser's first-ranked cause
is the injected fault. 30 runs per fault, 90 per set, both diagnosers scored on the same recorded runs:

| | agent spans only | spans + kernel events |
|---|---|---|
| set 1 (seeds 1000-1029) | 26.7% [19-37] | 90.0% [82-95] (rules frozen before the run) |
| **set 2 (seeds 2000-2029, fresh)** | **30.0% [22-40]** | **92.2% [85-96]**, or **96.7% [91-99]** after one rule change (below) |

(95% Wilson intervals.) The agent-only diagnoser sits near chance (1/3) because DNS, connect delay and
retransmits all happen *inside one HTTP span*; the kernel measures each of them directly (injected vs measured:
DNS delay 804 ms vs 805.1 ms, connect time 400 ms vs 400.3 ms for a 200 ms-per-direction delay).
Over 8 fault types and 240 runs per set, no diagnoser ever blamed a healthy component (0/240, upper 95% bound 2%).
The four non-network faults and the healthy runs are 100% for both diagnosers: spans already suffice there.

**Harder tests (Stage 6, [docs/results-stage6.md](docs/results-stage6.md)).** A slow API *server* with a healthy network:
the Stage 4 diagnoser blamed the network every time (0% right); after one added rule it was right in 100% of 30 fresh-seed
runs. Subtle 20-100 ms faults, two faults at once (90-100% on most pairs; one weakness found, not fixed), a machine with all
cores busy, and a third-party agent framework (LangGraph, 96.7-100% on the same faults against the mock model) all kept the
advantage. A first run against a **real model over the real internet** (small sample) found two real problems: the
combined diagnoser raised false alarms on healthy runs because real connections routinely show TCP retransmits (it
reported them as packet loss), and its server-wait signal cannot be computed for a remote server. Thresholds also have
to be calibrated on the real service. A fourth version (v4) that only reports background-level retransmits as packet loss when
they exceed what healthy runs show cut those false alarms from 69% to 6% on fresh healthy runs, at a cost that is not yet
measured: it would stay silent about mild packet loss on that network ([docs/results-stage7.md](docs/results-stage7.md)).
Where my predictions were wrong, the results document says so. **Not tested:**
production load.

**Stage 8-9 (real network path, real DNS, scale).** Probes for real DNS lookups (libc) and for TCP sends now give kernel
evidence on every model call, including reused connections, and the kernel measured faults injected on the real path to a real
model (a 991 ms latency as a 1,060 ms connect; a 4.9 s DNS delay as 5.3 s). **The diagnosis did not keep up:** on a real model the
calls vary by seconds and healthy kernel signals have heavy tails, so a 1 s network fault never crosses a step threshold (83% of
such runs got no answer), and packet loss is not separable from the path's background retransmits by count
([docs/results-stage8.md](docs/results-stage8.md)). A fifth version (v5: per-class thresholds and run-level medians instead of
single samples) then found the faults on fresh real-network runs: DNS delay 10/10 and latency 9/10 (spans-only: 0/10), with 0/10
false alarms on healthy runs and no change at all on 870 mock evaluations; packet loss stays at 70% and the detection floor is
set by the path's own noise, about 1 s of latency and 2.3 s of DNS on that network, from 10 runs per group on one path
([docs/results-stage10.md](docs/results-stage10.md)).

**Stage 11 (what was still untested).** The correlator was quadratic and is now linear (51,200 tasks in under a second); a sustained
run at 512 simultaneous agents with reused ports had 0 wrong connections, and about 0.1% of connections lost their connect event only
when they overflowed the server's accept queue (cause not established). **Async agents on one thread cannot be joined by pid and
thread id** (0 of 1,280 spans correct at 64 coroutines); they can if the HTTP span records its socket's local port
(`network.local.port`, then 100%), which is an instrumentation requirement. A remote server's wait measured from the client side was
within tolerance on 95 of 96 TLS calls after I fixed a definition that session tickets fooled, and v5 found two simultaneous real
faults in 7 of 8 and 6 of 10 runs ([docs/results-stage11.md](docs/results-stage11.md)). The join itself stayed
correct under load: 5,120 of 5,120 spans got exactly their own connection with up to 512 simultaneous agent threads and no
lost kernel events ([docs/results-stage9.md](docs/results-stage9.md)); async agents on one shared thread and real production
load remain untested.

**How much to trust this**

* The network gain is close to built in by the design of the experiment: the baseline cannot separate those causes
  by construction. What the experiment does show is that the full chain (eBPF collection, clock alignment, join,
  diagnosis) works, and where it breaks (below).
* One rule was changed after seeing results (retransmits now explain a high RTT, because a lost SYN inflates the RTT
  sample). It was judged on set 2, which was recorded afterwards. Details: [docs/results-stage4.md](docs/results-stage4.md).
* `packet_loss` tops out at 90%: in 3 of 30 runs per set the loss never hit a packet that mattered, so there is nothing
  to observe. The diagnoser reaches that ceiling.
* **Overhead: latency is near the 5% target, CPU is not.** Across three sessions OpenTelemetry alone added +0.6% to +3.5%
  latency (one earlier session read +7.5%) and about 1.3 ms of CPU per 60 ms run; adding the eBPF collector gives
  +2.3% to +5.2% latency and roughly 3 ms of bpftrace CPU per run. A shared provider and in-kernel filtering did not help.
  The spread between sessions is larger than within one, so treat any single number as a range.
  Stage 12: a compiled C/libbpf collector cut the collector's CPU from about 5.1 to 1.7 ms per run (2.9x; I predicted 3.4x) with the
  same join integrity and event completeness ([docs/results-stage12.md](docs/results-stage12.md)).
* Everything ran on loopback, with mock model and API servers, plain HTTP, one injected fault at a time, and faults
  I chose (large, hundreds of ms). Real networks, TLS, concurrent faults and subtle degradation are untested.

## How it works

```
 faultlab ──injects one fault──▶ agent (LLM + 3 tools, OTel GenAI/HTTP/DB spans with pid, tid)
                                      │  spans                    kernel events (bpftrace)
                                      ▼                           DNS, TCP connect, retransmit, RTT
                                 SQLite store ◀───────────────────────────┘
                                      │
                                 correlator: one clock, spans <-> kernel events
                                      │
                       baseline diagnoser (spans)   combined diagnoser (spans + kernel)
                                      └────────── evaluator (accuracy, false attribution, overhead)
```

* **Agent** ([agent/](agent)): a loop with a mock LLM and three tools (SQLite query, external HTTP API, configurable
  slow tool), hand-instrumented with OpenTelemetry. Every span carries `process.pid` and `thread.id`.
* **Mock services** ([mockservices/](mockservices)): deterministic LLM server, API server and a DNS stub, each a
  separate OS process on real sockets. They carry the fault hooks (delay, HTTP 429).
* **Faults** ([faultlab/](faultlab)), seeded and one at a time, ground truth stored per run:

  | fault | mechanism |
  |---|---|
  | `dns_delay` | resolver stub delays its reply (300-1000 ms) |
  | `net_latency` | `tc netem` delay on the API port only, both directions (75-250 ms) **root** |
  | `packet_loss` | `tc netem` loss on the API port only (20-40%) **root** |
  | `slow_tool` | sleep inside the tool (+300-1000 ms) |
  | `slow_db` | another connection holds an exclusive SQLite lock (300-1000 ms) |
  | `slow_model` | mock LLM delays every response (200-600 ms x 4 calls) |
  | `rate_limit` | mock LLM answers HTTP 429 to the first 1-3 requests; client backs off |
  | `none` | healthy control, needed to measure false accusations |

* **Kernel collector** ([collectors/](collectors)): a bpftrace program (`net_events.bt`) wrapped in Python. DNS via
  `sendto`/`recvfrom`, connects via a `tcp_v4_connect` kprobe and `sock:inet_sock_set_state`, plus
  `tcp:tcp_retransmit_skb` and `tcp:tcp_probe` (RTT). Output is JSON lines on CLOCK_MONOTONIC.
  A compiled equivalent ([collectors/ctrace/](collectors/ctrace), C + libbpf; build with `make -C collectors/ctrace`, install with
  `./scripts/setup_sudoers.sh`) is used automatically when installed (`CROSSTRACE_COLLECTOR=bpftrace` forces the bpftrace one).
* **Correlator** ([correlator/](correlator)): converts kernel timestamps to the spans' wall clock using clock pairs
  sampled every 100 ms (the wall clock was seen stepping ~3 ms against the monotonic clock in 4% of runs), then
  attaches events to spans: exact pid/tid + time for DNS and connects, the connection's 4-tuple for RTT and
  retransmits (which carry no pid), port + time as a fallback. Unmatched events are returned with a reason.
* **Join validation**: the agent also records the real client port of each socket (used only by the tests). The
  connection the join attaches to a span must be that port: 160/160 runs pass, and hand-built unit tests cover decoy
  processes, concurrent requests, a failed connect, clock offset, drift and clock steps. See
  [docs/results-stage3.md](docs/results-stage3.md) for the bugs this found in the first versions.
* **Diagnosers** ([diagnosers/](diagnosers)): both use the same critical path (the agent's model and tool calls) and
  the same "healthy duration" thresholds from 30 calibration runs. *Baseline*: blames the span type that was slow; a
  slow API call is split three ways at random. *Combined*: transparent rules that split a slow API call using DNS
  time, RTT and retransmits seen by the kernel, and fall back to the baseline's behaviour when the kernel shows nothing.
* **Metrics** ([eval/metrics.py](eval/metrics.py)): top-1, top-3 and false attribution (first-ranked cause points at a
  component that was healthy; the wrong cause inside the right component is a miss, not a false attribution).

## Reproduce

Tested on Windows 11 + WSL2 (Ubuntu 26.04, kernel 6.6.87.2, Python 3.14, bpftrace 0.25). Any Linux with kernel 5.0+
(BTF recommended), root, `bpftrace` and `tc`/`netem` should work; macOS and Windows alone cannot run the kernel part.

```bash
sudo apt-get install -y bpftrace iproute2 make python3-venv python3-pip curl
git clone <this repo> && cd crosstrace
./scripts/00_feasibility.sh      # ROOT: checks eBPF and netem work on this kernel (briefly touches the loopback qdisc)
./scripts/setup_sudoers.sh       # ROOT, once: passwordless sudo for tc and bpftrace. READ THE WARNING BELOW
make venv                        # pinned dependencies (requirements-dev.txt)
make test                        # 119 tests; the one live eBPF test is skipped if bpftrace is not usable
make reproduce                   # ROOT, ~40 min, keep the machine idle: both evaluation sets + overhead
```

**Warnings.** The fault harness changes network settings on `lo` (it adds and removes a `tc` qdisc) and the collector
runs `bpftrace`. Run this only inside a throwaway dev WSL/VM. `setup_sudoers.sh` lets your user run *any* `tc` and
*any* `bpftrace` program as root without a password, which is effectively root; undo it with
`sudo rm /etc/sudoers.d/crosstrace`. If a crash leaves a stale qdisc: `sudo tc qdisc del dev lo root`.
Keep the repo's runtime data (SQLite, venv) on the Linux filesystem; the default is `~/.crosstrace-data` and
`~/.venvs/crosstrace`. SQLite file locking misbehaves on `/mnt/c`.

Smaller entry points: `make run-healthy` (one traced run, no root), `make eval-baseline-noroot` (agent-only baseline
on the six faults that need no root), `make stage3-check` (timelines and join integrity), `make overhead`.
To read one joined timeline, run `make stage3-check` once (it records the runs), then
`python -m eval.stage3_check --show kernel-dns_delay-5000`.

**Reproducibility.** Fault parameters, tie-breaks and run order are seeded and repeat exactly. Timings do not: the
accuracy percentages above will move by a few points between runs (small samples, real kernel timing, whether random
packet loss happens to hit anything), and the overhead numbers depend on the machine. The wide intervals are the honest
statement of that. Healthy-duration thresholds are rebuilt from fresh calibration runs each time.

## Repository layout

```
agent/        target agent, tools, OTel tracing, DNS client      collectors/   bpftrace program, wrapper, SQLite store
mockservices/ mock LLM, API and DNS servers                      correlator/   clock alignment, join, integrity checks, renderer
faultlab/     fault catalog, tc netem controller, run harness    diagnosers/   baseline, combined, healthy-profile thresholds
eval/         metrics, evaluation runners, overhead benchmark    scripts/      feasibility check, sudoers setup, healthy demo
tests/        119 tests (join, diagnosers, metrics, live eBPF)   docs/         decisions.md (D1-D26), results-stage1..4,6.md
```

`docs/decisions.md` records every design decision and why, including the ones that were changed after results came in.
`docs/predictions-stage6*.md` were committed before the Stage 6 runs, so each prediction can be checked against the result.

Optional pieces: `pip install -r requirements-real.txt` adds the LangGraph agent and the real-model adapters. Their
API keys go in a git-ignored `.env` (copy `.env.example`); never commit it, and revoke any key once you have finished with it.

## Not covered (out of scope or untested)

GPU and inference-server internals, multi-agent protocols, TLS end to end (the kernel sees connections, not contents; the
local runs use plain HTTP and a real TLS connection was only glimpsed), faults outside the catalog (slow API server, congestion, flaky resolvers, several faults at once), real model
providers, non-loopback networks, IPv6, libc resolvers (the agent does its own DNS lookups so the probe can see them),
and any mitigation of the tracing overhead.

## License

MIT, see [LICENSE](LICENSE).
