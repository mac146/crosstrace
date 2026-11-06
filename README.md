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

**How much to trust this**

* The network gain is close to built in by the design of the experiment: the baseline cannot separate those causes
  by construction. What the experiment does show is that the full chain (eBPF collection, clock alignment, join,
  diagnosis) works, and where it breaks (below).
* One rule was changed after seeing results (retransmits now explain a high RTT, because a lost SYN inflates the RTT
  sample). It was judged on set 2, which was recorded afterwards. Details: [docs/results-stage4.md](docs/results-stage4.md).
* `packet_loss` tops out at 90%: in 3 of 30 runs per set the loss never hit a packet that mattered, so there is nothing
  to observe. The diagnoser reaches that ceiling.
* **Overhead target (under 5%) not met.** In-process OpenTelemetry alone adds +7.5% latency [+4.1, +11.3] and ~4 ms of
  CPU to a 63 ms run; the eBPF collector adds no resolvable latency on top but ~8 ms more CPU per run (~20% of one
  core in total with eBPF). No mitigation (sampling, batching) was tried.
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
make test                        # 77 tests; the one live eBPF test is skipped if bpftrace is not usable
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
tests/        77 tests (join, diagnosers, metrics, live eBPF)    docs/         decisions.md (D1-D21), results-stage1..4.md
```

`docs/decisions.md` records every design decision and why, including the ones that were changed after results came in.

## Not covered (out of scope or untested)

GPU and inference-server internals, multi-agent protocols, TLS (kernel sees connections, not contents; plain HTTP
here), faults outside the catalog (slow API server, congestion, flaky resolvers, several faults at once), real model
providers, non-loopback networks, IPv6, libc resolvers (the agent does its own DNS lookups so the probe can see them),
and any mitigation of the tracing overhead.

## License

MIT, see [LICENSE](LICENSE).
