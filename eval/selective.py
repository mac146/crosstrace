"""Stage 14: what does the per-span join add beyond the strongest cheap baselines when faults are SELECTIVE?

    python -m eval.selective --n 20 --cal 30 --out ~/.crosstrace-data/selective.json     # record (ROOT) and evaluate
    python -m eval.selective --no-record --out ~/.crosstrace-data/selective.json         # re-evaluate recorded runs

Scenarios (4 sequential API calls per run; predictions: docs/predictions-stage14.md):
  none, sel_slow_api, sel_latency, mixed_one_server, two_servers (see SCENARIOS).
Arms: base (spans), v3 (joined), pool (host-wide counters), pool_port (per-destination metrics incl. server wait),
obi (EMULATED per-request server timing only, no DNS/RTT/loss; not the real OpenTelemetry eBPF agent).

REQUIRES passwordless sudo bpftrace/ctrace and tc (scripts/setup_sudoers.sh).
"""
import argparse
import contextlib
import json
import os
import random
import sys
import tempfile
import time

from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available as bpf_available
from correlator.join import _is_http, correlate
from diagnosers import baseline, combined
from diagnosers.profile import build_profile, kernel_profile_from_timelines
from eval.metrics import aggregate, aggregate_multi, score_multi, score_run
from eval.run_full import pct
from faultlab.netem import Netem, available as tc_available
from mockservices import launcher
from mockservices.launcher import free_port, services

CALLS = 4
EXT4 = ("dns_delay", "net_latency", "packet_loss", "slow_api")
ARMS = ["base", "v3", "pool", "pool_port", "obi"]
# truth: the injected faults (a string for one fault). slow: call numbers (1-based) the server answers slowly; lat: call numbers
# wrapped in a latency window; two_servers: calls alternate between server A (slow) and B (latency on its port, whole run).
SCENARIOS = {
    "none": {"truth": "none"},
    "sel_slow_api": {"truth": "slow_api", "slow": (2, 4)},
    "sel_latency": {"truth": "net_latency", "lat": (2, 4)},
    "mixed_one_server": {"truth": ["net_latency", "slow_api"], "lat": (1, 3), "slow": (2, 4)},
    "two_servers": {"truth": ["slow_api", "net_latency"], "two_servers": True},
}


@contextlib.contextmanager
def latency_window(port, delay_ms):
    """A latency fault on one port. On exit it waits for packets still queued in netem (delayed FIN/ACKs) to be delivered BEFORE the
    qdisc is removed: deleting it drops them, TCP retransmits them, and those retransmits looked like packet loss on the call (found in the
    smoke test of this stage)."""
    with Netem(port, delay_ms=delay_ms):
        yield
        time.sleep(2 * delay_ms / 1000 + 0.1)


def one_run(scenario, seed, db):
    cfg = SCENARIOS[scenario]
    rng = random.Random(f"{scenario}:{seed}")
    slow_ms, lat_ms = rng.randint(300, 1000), rng.randint(75, 250)
    api_args = ()
    if cfg.get("two_servers"):
        api_args = ("--delay-ms", str(slow_ms))
    elif cfg.get("slow"):
        api_args = ("--delay-ms", str(slow_ms), "--slow-ids", ",".join(map(str, cfg["slow"])))
    with services(api_args=api_args) as svc:
        procs = []
        try:
            ports = [svc.api_port]
            port_b = None
            if cfg.get("two_servers"):
                port_b = free_port()
                procs.append(launcher._spawn("mockservices.api_server", port_b, ()))
                ports.append(port_b)
            tracing = Tracing()
            tools_a = Tools(db, svc.api_url, svc.dns_server, tracing)
            tools_b = Tools(db, f"http://{launcher.API_HOST}:{port_b}", svc.dns_server, tracing) if port_b else tools_a
            with KernelCollector(dns_ports=[svc.dns_server[1]], ports=ports, reduce_rtt=True) as k:
                with contextlib.ExitStack() as stack:
                    if port_b:
                        stack.enter_context(latency_window(port_b, lat_ms))        # B's whole path is slow
                    with tracing.span("invoke_agent crosstrace-agent", {"gen_ai.operation.name": "invoke_agent",
                                                                       "gen_ai.agent.name": "crosstrace-agent"}):
                        for i in range(1, CALLS + 1):
                            tools = tools_b if (port_b and i % 2 == 0) else tools_a
                            win = (latency_window(svc.api_port, lat_ms) if i in cfg.get("lat", ())
                                   else contextlib.nullcontext())
                            with win:                                                 # window opens and closes outside the spans
                                with tracing.span("execute_tool http_api", {"gen_ai.operation.name": "execute_tool",
                                                                           "gen_ai.tool.name": "http_api"}):
                                    tools.call("http_api", {"path": f"/shipping/{i}"})
                time.sleep(0.2)
            return {"spans": tracing.spans(), "events": k.events, "sync": [list(c) for c in k.clock_sync],
                    "warnings": k.warnings}
        finally:
            for p in procs:
                p.terminate()
                p.wait(timeout=5)
                p.stdout.close()


def record(path, n, n_cal):
    data = json.load(open(path)) if os.path.exists(path) else {}
    todo = [("none", f"cal{i}", "calib") for i in range(n_cal)]
    todo += [(sc, f"s{i}", "eval") for sc in SCENARIOS for i in range(n)]
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        for k, (sc, seed, role) in enumerate(todo, 1):
            rid = f"{role}-{sc}-{seed}"
            if rid in data:
                continue
            data[rid] = {"scenario": sc, "role": role, **one_run(sc, seed, db)}
            if k % 10 == 0:
                print(f"  {k}/{len(todo)} runs", file=sys.stderr, flush=True)
                json.dump(data, open(path, "w"))
    json.dump(data, open(path, "w"))
    return data


def join_run(r):
    return correlate(r["spans"], r["events"], [tuple(c) for c in r["sync"]])


def evaluate(data):
    cal = [r for r in data.values() if r["role"] == "calib"]
    cal_tl = [join_run(r) for r in cal]
    profile = build_profile([r["spans"] for r in cal], "coarse")
    kprofile = kernel_profile_from_timelines(cal_tl, "coarse")
    print(f"calibration: {len(cal)} healthy runs; step threshold http_api {profile['tool:http_api']:.0f} ms; kernel thresholds "
          + ", ".join(f"{k}={v:.0f}" for k, v in sorted(kprofile.items()) if isinstance(v, (int, float))))
    results = {}
    for n, (rid, r) in enumerate(sorted(data.items())):
        if r["role"] != "eval":
            continue
        tl = join_run(r)
        seed = n                                  # fixed tie-break draw per run, independent of the arm
        https = [e for e in tl.entries if _is_http(e.span)]
        ranked = {"base": [c.cause for c in baseline.diagnose(r["spans"], profile, tie_seed=seed, api_causes=EXT4)]}
        for arm, src in (("v3", "joined"), ("pool", "pooled"), ("pool_port", "pooled_port"), ("obi", "obi")):
            ranked[arm] = [c.cause for c in combined.diagnose(tl, profile, kprofile, tie_seed=seed, loss_explains_rtt=True,
                                                              slow_api=True, source=src)]
        results.setdefault(r["scenario"], []).append({"ranked": ranked, "http": len(https),
                                                      "with_events": sum(1 for e in https if e.kernel),
                                                      "orphans": len(tl.orphans), "warnings": r["warnings"]})
    return results


def report(results):
    for sc, rows in results.items():
        truth = SCENARIOS[sc]["truth"]
        multi = isinstance(truth, list)
        http, ev = sum(r["http"] for r in rows), sum(r["with_events"] for r in rows)
        print(f"\n--- {sc}  (n={len(rows)}; {ev}/{http} HTTP spans had kernel events; "
              f"orphans {sum(r['orphans'] for r in rows)}; collector warnings {sum(bool(r['warnings']) for r in rows)})")
        print(f"{'arm':<10}{'both in top-2' if multi else 'top-1':<22}{'false alarm / false attr':<26}")
        for arm in ARMS:
            if multi:
                sc_ = [score_multi(r["ranked"][arm], truth) for r in rows]
                a = aggregate_multi(sc_)
                print(f"{arm:<10}{pct(a['all_in_topk'], a['all_in_topk_ci']):<22}{pct(a['false_attr'], a['false_attr_ci']):<26}")
            elif truth == "none":
                bad = sum(r["ranked"][arm][0] != "none" for r in rows)
                print(f"{arm:<10}{f'{len(rows) - bad}/{len(rows)} said none':<22}{f'{bad}/{len(rows)} false alarms':<26}")
            else:
                sc_ = [score_run(r["ranked"][arm], truth) for r in rows]
                a = aggregate(sc_)
                print(f"{arm:<10}{pct(a['top1'], a['top1_ci']):<22}{pct(a['false_attr'], a['false_attr_ci']):<26}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--cal", type=int, default=30)
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/selective.json"))
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args()
    if not a.no_record:
        if not bpf_available() or not tc_available():
            sys.exit("needs passwordless sudo bpftrace and tc: run scripts/setup_sudoers.sh once")
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        data = record(a.out, a.n, a.cal)
    else:
        data = json.load(open(a.out))
    report(evaluate(data))


if __name__ == "__main__":
    main()
