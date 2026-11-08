"""Stage 4: what does tracing cost? Latency and CPU, untraced vs OTel vs OTel + eBPF.

    python -m eval.overhead --rounds 12

REQUIRES passwordless sudo bpftrace (scripts/setup_sudoers.sh). Do not run anything else heavy meanwhile:
the eBPF arm traces the whole machine and the measurement is sensitive to load.

Design
- Arms: `untraced` (NoopTracing); `otel-legacy` (OpenTelemetry with a NEW TracerProvider per run, the original
  design); `otel` (one shared provider, the current default); `otel+ebpf-all` (shared OTel plus the bpftrace
  collector recording all TCP on the machine); `otel+ebpf-filtered` (same, but the kernel program only records the
  services' ports and drops RTT samples on pure ACKs); and `untraced-ctrl`, an identical second untraced arm (A/A control).
- Each round runs every arm once, in an order shuffled with a fixed seed. The first iterations of each block are
  discarded as warm-up.
- Unit of analysis = the ROUND. Iterations inside one block share scheduling luck, so treating them as independent
  gave confidence intervals that were far too narrow (the first version of this benchmark showed an A/A difference of
  +44% on the api_calls workload). Per round, each arm's block mean is divided by that round's `untraced` block mean;
  the table reports the mean ratio and a 95% bootstrap interval over rounds. The A/A control row shows what "no
  effect" looks like on this machine: effects inside its interval cannot be resolved.
- Workloads: `agent` (one full healthy agent run per iteration; ~60 ms, dominated by a 50 ms sleep) and `api_calls`
  (20 back-to-back DNS+connect+GET calls per iteration, where per-call overhead is not hidden by sleeps).
- Latency = wall time per unit (run or call). CPU is reported twice: `main-thread` (time.thread_time: the agent
  and its tracing) and `process` (time.process_time: also includes the collector's helper threads that parse
  bpftrace output and sample clocks). bpftrace CPU = its /proc utime+stime over the block per unit; clock-tick
  resolution is 10 ms, so it is only meaningful over whole blocks. Mock service CPU is not counted.
- Collector startup (~1 s) is a one-off cost per recording, excluded here.
"""
import argparse
import os
import random
import statistics
import sys
import tempfile
import time

from agent.agent import run_agent
from agent.llm_client import LLMClient
from agent.tools import Tools, seed_db
from agent.tracing import NoopTracing, Tracing
from collectors.ebpf import KernelCollector, available
from mockservices.launcher import services

TASK = "What is customer 42's order total and shipping status?"
ARMS = ["untraced", "otel-legacy", "otel", "otel+ebpf-all", "otel+ebpf-filtered", "untraced-ctrl"]
# Stage 12: what does each part of the collector cost? (ports filtered and pure-ACK RTT dropped in all of them)
STAGE12_ARMS = ["untraced", "otel", "ebpf-tracepoint", "ebpf-fentry", "ebpf-fentry+send", "untraced-ctrl"]
STAGE12D_ARMS = ["untraced", "otel", "ebpf-c", "ebpf-c-slowsync", "untraced-ctrl"]      # diagnostic only
STAGE12C_ARMS = ["untraced", "otel", "ebpf-fentry", "ebpf-c", "ebpf-c+send", "untraced-ctrl"]
COLLECTOR_ARMS = {"ebpf-c": {"backend": "c"},
                  "ebpf-c-slowsync": {"backend": "c", "sync_interval_s": 1000},                                 # compiled collector (C + libbpf), decoded after the run
                  "ebpf-c+send": {"backend": "c", "tcp_send": True},
                  "ebpf-tracepoint": {"connect_end": "tracepoint"},          # the collector as it was before Stage 12
                  "ebpf-fentry": {"connect_end": "fentry"},                  # the new default (reliable under load)
                  "ebpf-fentry+send": {"connect_end": "fentry", "tcp_send": True}}   # plus the TCP-send probe
WORKLOADS = ("agent", "api_calls")
TICK = os.sysconf("SC_CLK_TCK")
WARMUP = 5


def bpftrace_cpu_s(sudo_pid):
    """CPU seconds used so far by the bpftrace process started via sudo (child of sudo_pid)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                raw = f.read()
        except OSError:
            continue
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        fields = raw[raw.rindex(")") + 2:].split()      # fields[0] = state, [1] = ppid, [11] = utime, [12] = stime
        if comm in ("bpftrace", "ctrace") and int(fields[1]) == sudo_pid:
            return (int(fields[11]) + int(fields[12])) / TICK
    return None


class Workloads:
    def __init__(self, svc, db):
        self.svc, self.db = svc, db

    def agent(self, tracing):
        run_agent(TASK, LLMClient(self.svc.llm_url, tracing),
                  Tools(self.db, self.svc.api_url, self.svc.dns_server, tracing), tracing)
        return 1

    def api_calls(self, tracing, n=20):
        tools = Tools(self.db, self.svc.api_url, self.svc.dns_server, tracing)
        for _ in range(n):
            tools.http_api("/shipping/42")
        return n


def run_block(fn, factory, iterations):
    """Per-unit [(latency_ms, process_cpu_ms, main_thread_cpu_ms)], warm-up discarded.
    A fresh tracing object per iteration, as every real run would have."""
    out = []
    for i in range(iterations + WARMUP):
        tracing = factory()
        p0, m0, t0 = time.process_time_ns(), time.thread_time_ns(), time.perf_counter_ns()
        units = fn(tracing)
        t1, m1, p1 = time.perf_counter_ns(), time.thread_time_ns(), time.process_time_ns()
        if i >= WARMUP:
            out.append(((t1 - t0) / 1e6 / units, (p1 - p0) / 1e6 / units, (m1 - m0) / 1e6 / units))
    return out


def bootstrap_mean_ci(xs, seed=0, n=4000):
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def pctile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def collect(rounds, agent_iters, api_iters):
    """blocks[(workload, arm)] = list over rounds of dict(lat, proc, main, bt_ms, events, secs, samples)"""
    blocks = {(w, arm): [] for w in WORKLOADS for arm in ARMS}
    with tempfile.TemporaryDirectory() as tmp, services() as svc:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        wl = Workloads(svc, db)
        plan = {"agent": (wl.agent, agent_iters), "api_calls": (wl.api_calls, api_iters)}
        for rnd in range(rounds):
            order = ARMS[:]
            random.Random(rnd).shuffle(order)
            for arm in order:
                factory = {"untraced": NoopTracing, "untraced-ctrl": NoopTracing,
                           "otel-legacy": lambda: Tracing(shared=False)}.get(arm, Tracing)
                collector = None
                if arm in COLLECTOR_ARMS:
                    kw = dict(COLLECTOR_ARMS[arm])
                    if kw.pop("backend", None) == "c":
                        from collectors.cebpf import CCollector as make
                    else:
                        from collectors.ebpf import BpftraceCollector as make
                    collector = make(dns_ports=[svc.dns_server[1]], ports=[svc.api_port, svc.llm_port],
                                     reduce_rtt=True, **kw)
                if arm.startswith("otel+ebpf"):
                    ports = [svc.api_port, svc.llm_port] if arm.endswith("filtered") else None
                    collector = KernelCollector(dns_ports=[svc.dns_server[1]], ports=ports,
                                                reduce_rtt=arm.endswith("filtered"))
                if collector:
                    collector.__enter__()
                try:
                    for w, (fn, iters) in plan.items():
                        bt0 = bpftrace_cpu_s(collector._proc.pid) if collector else None
                        ev0 = len(collector.events) if collector else 0
                        s0 = time.perf_counter()
                        res = run_block(fn, factory, iters)
                        secs = time.perf_counter() - s0
                        bt_ms = None
                        if collector:
                            bt1 = bpftrace_cpu_s(collector._proc.pid)
                            if bt0 is not None and bt1 is not None:
                                bt_ms = 1000 * (bt1 - bt0) / len(res)
                        blocks[(w, arm)].append({
                            "lat": [x[0] for x in res], "proc": [x[1] for x in res], "main": [x[2] for x in res],
                            "bt_ms": bt_ms, "secs": secs,
                            "events": (len(collector.events) - ev0) if collector else 0})
                finally:
                    if collector:
                        collector.__exit__(None, None, None)
            print(f"  round {rnd + 1}/{rounds} done", file=sys.stderr, flush=True)
    return blocks


def round_overhead(blocks, w, arm, key):
    """Per-round ratios of this arm's block mean to the same round's untraced block mean, in %."""
    return [100 * (statistics.fmean(a[key]) / statistics.fmean(u[key]) - 1)
            for a, u in zip(blocks[(w, arm)], blocks[(w, "untraced")])]


def fmt_rel(xs, seed):
    lo, hi = bootstrap_mean_ci(xs, seed)
    return f"{statistics.fmean(xs):+7.1f}% [{lo:+.1f}, {hi:+.1f}]"


def report(blocks, rounds):
    for w in WORKLOADS:
        unit = "run" if w == "agent" else "call"
        n = sum(len(b["lat"]) for b in blocks[(w, "untraced")])
        print(f"\nWORKLOAD {w}  (per {unit}; {rounds} rounds, {n} timed {unit}s per arm)")
        print(f"{'arm':<20}{'lat mean':>10}{'median':>9}{'p95':>9}{'main CPU':>10}{'proc CPU':>10}"
              f"{'bpftrace':>10}{'events/s':>9}")
        for arm in ARMS:
            bl = blocks[(w, arm)]
            flat = lambda k: [x for b in bl for x in b[k]]
            lat = flat("lat")
            bts = [b["bt_ms"] for b in bl if b["bt_ms"] is not None]
            events = sum(b["events"] for b in bl)
            secs = sum(b["secs"] for b in bl)
            print(f"{arm:<20}{statistics.fmean(lat):>9.3f}m{statistics.median(lat):>8.3f}m{pctile(lat, .95):>8.3f}m"
                  f"{statistics.fmean(flat('main')):>9.3f}m{statistics.fmean(flat('proc')):>9.3f}m"
                  f"{(f'{statistics.fmean(bts):.3f}m' if bts else '-'):>10}{(f'{events / secs:.0f}' if events else '-'):>9}")
        print(f"\n  overhead vs the same round's untraced block: mean of {rounds} rounds [95% bootstrap interval]")
        print(f"  {'arm':<20}{'latency':<28}{'main-thread CPU':<28}{'process CPU':<28}")
        for arm in ARMS[1:]:
            print(f"  {arm:<20}{fmt_rel(round_overhead(blocks, w, arm, 'lat'), 1):<28}"
                  f"{fmt_rel(round_overhead(blocks, w, arm, 'main'), 2):<28}"
                  f"{fmt_rel(round_overhead(blocks, w, arm, 'proc'), 3):<28}")
    print("\n'untraced-ctrl' is the A/A control (identical to 'untraced'): effects inside its interval are noise.")
    print("Latency/CPU in ms per run (agent) or per call (api_calls). Mock service CPU is not counted.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--arms", choices=["stage6", "stage12", "stage12c", "stage12d"], default="stage6",
                    help="stage12: compare the collector's parts (tracepoint vs fentry connect-end, with/without TCP-send probe)")
    ap.add_argument("--agent-iters", type=int, default=40)
    ap.add_argument("--api-iters", type=int, default=30)
    a = ap.parse_args()
    global ARMS
    if a.arms == "stage12":
        ARMS = STAGE12_ARMS
    elif a.arms == "stage12c":
        ARMS = STAGE12C_ARMS
    elif a.arms == "stage12d":
        ARMS = STAGE12D_ARMS
    if not available():
        sys.exit("needs passwordless sudo bpftrace: run scripts/setup_sudoers.sh once")
    report(collect(a.rounds, a.agent_iters, a.api_iters), a.rounds)


if __name__ == "__main__":
    main()
