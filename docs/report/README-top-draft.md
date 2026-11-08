# crosstrace

An open research prototype that asks one question: **does joining agent-level traces with kernel-level network traces diagnose slow or
failing AI-agent runs better than agent-level traces alone?** It contains a seeded fault-injection benchmark, an eBPF collector (bpftrace, and a
compiled C/libbpf version), a correlator that aligns kernel events with OpenTelemetry spans, rule-based diagnosers, and the evaluation with its
predictions committed before each run. It is a research prototype, not a monitoring product, and it needs root-equivalent access (dev machines only).

## What we found

- **Kernel evidence helps a lot where the cause is in the network path.** Top-1 accuracy on `dns_delay`, `net_latency` and `packet_loss` rose from
  about 30% (spans only) to 96.7% [91-99] on a fresh set of 90 runs, with 0 false accusations in 240 runs. Mock services on loopback, and largely
  built into the experiment: the spans-only baseline cannot separate those causes by construction.
- **What carries the result is per-request kernel timing, not the per-span join.** Host-wide counters plus a per-request server wait matched the full
  join on every recorded group (Stage 13). The join clearly won only when two different problems hit the same destination in one run (100% vs 0% for
  the best cheap baseline, 20 runs; held at 100% with 8 and 32 simultaneous agents). That scenario was designed by us to discriminate (Stage 14).
- **On a real model over the real internet, it is weaker.** A v5 diagnoser found DNS delays 10/10 and latency 9/10 (spans only: 0/10), but only
  above about 2.3 s of DNS and 1 s of latency on that one path, and packet loss stays at 70% (10 runs per group, one path).
- **Negative and wrong-prediction results are documented**, including a packet-loss rule that failed on fresh data and a compiled collector that
  cut CPU 2.9x against a 3.4x prediction (5.05 to 1.72 ms per 60 ms run).

## What this does not show

Production traffic, multi-host or Kubernetes setups, HTTP/3 or streamed answers, real incident prevalence, or a comparison against the real
OpenTelemetry eBPF agent (our "obi" arm is an emulation). Details and numbers: [docs/report/report.md](docs/report/report.md) and the per-stage
documents in [docs/](docs).

## Run it

Linux or WSL2, kernel 5.8+ with BTF, root. Read the warning below first.

```bash
sudo apt-get install -y bpftrace iproute2 make python3-venv python3-pip curl
./scripts/setup_sudoers.sh       # ROOT, once: passwordless sudo for tc, bpftrace and the compiled collector
make venv && make test
make reproduce                   # ROOT, about 40 minutes, keep the machine idle
```

**Warning.** The harness changes network settings (it adds and removes a `tc` qdisc) and the collector runs `bpftrace` or a compiled eBPF program;
`setup_sudoers.sh` is effectively root for your user. Use a throwaway VM or WSL. API keys for the optional real-model runs go in a git-ignored
`.env`; never commit one.
