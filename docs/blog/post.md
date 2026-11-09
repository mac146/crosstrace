# Does kernel-level tracing help diagnose slow AI agents? I tested it, wrote down my predictions first, and got a lot wrong

**TL;DR.** I built an open benchmark that injects faults into an AI-agent system (slow DNS, network latency, a slow API server, a slow database, ...) and measures how often a diagnosis names the right cause. Agent-level traces plus kernel-level (eBPF) evidence beat agent-level traces alone where the cause is in the network path. But when I compared against the strongest cheap alternatives, including the real OpenTelemetry eBPF agent (OBI), the story got narrower: the evidence is what matters, and matching each call to its connection matters under concurrency. I committed my predictions to git before every run, and I report the ones I got wrong. Everything is open: https://github.com/mac146/crosstrace (full report at tag `report-v1`).

## The problem

A slow agent run is a chain of network calls: the model, tools, a database, outside APIs. An application-level trace tells you *which* call was slow. It can't tell you *why*, because a DNS stall, a slow connection and a slow server all happen inside one HTTP span. System-level eBPF tools see DNS time, connect time, retransmits and round-trip time, but they know nothing about the agent. The hypothesis: joining the two diagnoses faults better than spans alone.

## What I built

A seeded fault-injection benchmark; an eBPF collector (bpftrace first, then a compiled C/libbpf version that uses about 2.9x less CPU); a correlator that aligns kernel events with spans (clock alignment, indexed matching); rule-based diagnosers; and an evaluation where every stage's predictions, with "I would be wrong if..." conditions, were committed before the runs.

## What held up

- **Kernel evidence works where the cause is in the network path.** Top-1 accuracy on DNS delay, latency and packet loss rose from about 30% (spans only) to about 97% on a fresh set of 90 runs, with no false accusations in 240 runs. Caveat I put in the report: mock services on loopback, and the spans-only baseline can't separate those causes by construction.
- **The join is robust.** 160/160 runs passed integrity checks against ground-truth ports; 5,120/5,120 spans got exactly their own connection with up to 512 simultaneous agent threads.
- **On a real model over the internet**, v5 of the rules found injected DNS delays in 10/10 and latency in 9/10 runs (10 runs per group, one network path: observations, not estimates).

## What I had to narrow

- **What carries the result is per-request kernel timing, not the join itself.** Host-wide counters plus the run's maximum server wait matched the join on single faults.
- **The real OpenTelemetry eBPF agent (OBI v1.0.0):** its per-request server duration names a slow server as well as my join (100%) but is at chance on network causes. With its per-connection RTT matched exactly to each call by client port, it equals my join (99.5-100% with 9 and 24 concurrent agents in different conditions). Matched by connection order, it falls to 63-69%. So the join is not better evidence; what my correlator adds is matching a call to its connection without the application declaring its socket. That works for thread-per-call agents; async agents on one thread need the port declared, and a quick text search of the standard Python HTTP client instrumentations suggests they don't set it.
- **Host-wide counters get worse as agents are added.** I traced this to a single stray retransmit anywhere in a run being pooled into every agent's evidence (rescoring without it fixed it: 64/64 instead of 24/64). Per-call evidence doesn't have this problem.

## What didn't work (kept in the report)

- On a real, NAT'd network path, packet loss can't be told from the path's background retransmits by count: 70% detection at best, and an alternative feature that looked better on the design set failed on fresh data.
- My CPU target was missed (2.9x less, predicted 3.4x); a clock-sampling prediction was wrong; a probe silently missed 0.1% of connections until I found a better hook; a harness bug briefly made a latency fault look like packet loss.
- 13 numbered negative or wrong-prediction results are in the report.

## Limits you should know about

Mock services on loopback, one host, scenarios I designed to discriminate, one OBI version and configuration I tuned to make it attach, and no packet-loss, HTTP/3 or streaming tests. The collector needs root-equivalent access, so it is for dev machines only. I don't know how often slow agent runs in the real world are network problems; the one dataset I found suggests provider-side causes like rate limits dominate.

## What's next

Two follow-ups were pre-registered and are now run (all predictions and results are in the repo): (1) async agents on one thread: assigning calls to connections by start time and duration worked without a declared port (99.5-100%) when calls start in order, but broke down with uneven waits before connecting (gap against a declared port 0, 4, 17 and 21 points at waits of 0, 50, 200 and 500 ms), so declaring the local port is the dependable route; (2) 8 concurrent real agents on a real model and network path: DNS delay detected in 100% and latency in 94% of agent-runs with no false alarms, about USD 0.02. And a question for you: **if you run agents in production, how often are slow or failing runs network or infrastructure problems rather than model or tool problems?** Your last two or three slow-run incidents, anonymized, would help me more than any benchmark row.

Code, benchmark, predictions and the full report: https://github.com/mac146/crosstrace
