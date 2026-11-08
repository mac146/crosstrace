"""Stage 14, part 2: the join vs the cheap baselines when many agents run at once (predictions: docs/predictions-stage14.md).

    python -m eval.selective_conc --agents 1 8 32            # record (ROOT) and evaluate
    python -m eval.selective_conc --no-record

C agents (threads, one trace each) run the mixed scenario of eval.selective in lockstep against ONE API server: latency window for
calls 1 and 3, slow server answers for calls 2 and 4. Every agent's truth is {net_latency, slow_api}. One collector records
everything; each agent is diagnosed on its own spans; the host-wide arms pool over ALL agents' HTTP spans.
REQUIRES passwordless sudo bpftrace/ctrace and tc (scripts/setup_sudoers.sh).
"""
import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available as bpf_available
from correlator.join import _is_http, correlate
from diagnosers import baseline, combined
from diagnosers.profile import build_profile, kernel_profile_from_timelines
from eval.metrics import aggregate_multi, score_multi
from eval.run_full import pct
from eval.selective import ARMS, EXT4, latency_window
from faultlab.netem import available as tc_available
from mockservices.launcher import services

TRUTH = ["net_latency", "slow_api"]
LAT_CALLS, SLOW_CALLS = (1, 3), (2, 4)


def one_run(n_agents, mixed, seed, db):
    rng = random.Random(f"conc:{n_agents}:{seed}")
    slow_ms, lat_ms = rng.randint(300, 1000), rng.randint(75, 250)
    api_args = ("--delay-ms", str(slow_ms), "--slow-ids", ",".join(map(str, SLOW_CALLS))) if mixed else ()
    with services(api_args=api_args) as svc:
        start, end = threading.Barrier(n_agents + 1), threading.Barrier(n_agents + 1)
        tracers = [Tracing() for _ in range(n_agents)]
        errors = []

        def agent(tracing):
            try:
                tools = Tools(db, svc.api_url, svc.dns_server, tracing)
                with tracing.span("invoke_agent crosstrace-agent", {"gen_ai.operation.name": "invoke_agent",
                                                                   "gen_ai.agent.name": "crosstrace-agent"}):
                    for i in range(1, 5):
                        start.wait(timeout=60)
                        try:
                            with tracing.span("execute_tool http_api", {"gen_ai.operation.name": "execute_tool",
                                                                       "gen_ai.tool.name": "http_api"}):
                                tools.call("http_api", {"path": f"/shipping/{i}"})
                        finally:
                            end.wait(timeout=60)
            except Exception as e:                      # a crashed agent is reported, not hidden
                errors.append(f"{type(e).__name__}: {e}")
                start.abort()
                end.abort()

        with KernelCollector(dns_ports=[svc.dns_server[1]], ports=[svc.api_port], reduce_rtt=True) as k:
            threads = [threading.Thread(target=agent, args=(t,)) for t in tracers]
            for t in threads:
                t.start()
            for i in range(1, 5):
                if mixed and i in LAT_CALLS:
                    with latency_window(svc.api_port, lat_ms):
                        start.wait(timeout=60)
                        end.wait(timeout=60)
                else:
                    start.wait(timeout=60)
                    end.wait(timeout=60)
            for t in threads:
                t.join(timeout=30)
            time.sleep(0.2)
        return {"mixed": mixed, "agents": [t.spans() for t in tracers], "events": k.events,
                "sync": [list(c) for c in k.clock_sync], "warnings": k.warnings, "errors": errors}


def record(path, plan):
    data = json.load(open(path)) if os.path.exists(path) else {}
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        for c, role, mixed, n in plan:
            for j in range(n):
                rid = f"{role}-c{c}-{j}"
                if rid in data:
                    continue
                data[rid] = {"c": c, "role": role, **one_run(c, mixed, f"{role}{j}", db)}
                print(f"  {rid}", file=sys.stderr, flush=True)
            json.dump(data, open(path, "w"))
    return data


def views(r):
    """Join the whole run once; return (all HTTP entries, [per-agent view with .entries])."""
    spans = [s for a in r["agents"] for s in a]
    tl = correlate(spans, r["events"], [tuple(c) for c in r["sync"]])
    by_agent = []
    for a in r["agents"]:
        ids = {s["span_id"] for s in a}
        by_agent.append(SimpleNamespace(entries=[e for e in tl.entries if e.span["span_id"] in ids], orphans=[]))
    return tl, [e for e in tl.entries if _is_http(e.span)], by_agent


def evaluate(data):
    out = {}
    for c in sorted({r["c"] for r in data.values()}):
        cal = [r for r in data.values() if r["c"] == c and r["role"] == "cal"]
        cal_spans, cal_views = [], []
        for r in cal:
            cal_spans += r["agents"]
            cal_views += views(r)[2]
        profile = build_profile(cal_spans, "coarse")
        kprofile = kernel_profile_from_timelines(cal_views, "coarse")
        res = {"healthy": {a: [0, 0] for a in ARMS}, "mixed": {a: [] for a in ARMS}, "runs_all_right": {a: 0 for a in ARMS},
               "runs": 0, "events_with": 0, "events_http": 0, "warn": 0, "err": 0, "orph": 0,
               "thr": (profile["tool:http_api"], kprofile)}
        for rid, r in sorted(data.items()):
            if r["c"] != c or r["role"] == "cal":
                continue
            tl, all_http, per_agent = views(r)
            res["events_http"] += len(all_http)
            res["events_with"] += sum(1 for e in all_http if e.kernel)
            res["warn"] += bool(r["warnings"])
            res["err"] += len(r["errors"])
            res["orph"] += len(tl.orphans)
            right = {a: True for a in ARMS}
            for n, (spans, view) in enumerate(zip(r["agents"], per_agent)):
                seed = n
                ranked = {"base": [x.cause for x in baseline.diagnose(spans, profile, tie_seed=seed, api_causes=EXT4)]}
                for arm, src in (("v3", "joined"), ("pool", "pooled"), ("pool_port", "pooled_port"), ("obi", "obi")):
                    ranked[arm] = [x.cause for x in combined.diagnose(view, profile, kprofile, tie_seed=seed,
                                                                      loss_explains_rtt=True, slow_api=True, source=src,
                                                                      pool_entries=all_http)]
                for arm in ARMS:
                    if r["role"] == "healthy":
                        bad = ranked[arm][0] != "none"
                        res["healthy"][arm][0] += bad
                        res["healthy"][arm][1] += 1
                    else:
                        sc = score_multi(ranked[arm], TRUTH)
                        res["mixed"][arm].append(sc)
                        right[arm] &= bool(sc["all_in_topk"])
            if r["role"] == "mixed":
                res["runs"] += 1
                for arm in ARMS:
                    res["runs_all_right"][arm] += right[arm]
        out[c] = res
    return out


def report(out):
    for c, res in out.items():
        thr, kp = res["thr"]
        print(f"\n=== {c} concurrent agent(s): {res['runs']} mixed runs = {len(res['mixed']['v3'])} agent-runs; "
              f"{res['events_with']}/{res['events_http']} HTTP spans had kernel events; orphans {res['orph']}; "
              f"runs with collector warnings {res['warn']}; crashed agents {res['err']}")
        print(f"    healthy thresholds at this load: http_api {thr:.0f} ms, " +
              ", ".join(f"{k}={v:.0f}" for k, v in sorted(kp.items()) if isinstance(v, (int, float))))
        print(f"{'arm':<10}{'both in top-2 (agent-runs)':<30}{'runs where every agent right':<32}{'healthy false alarms':<22}")
        for arm in ARMS:
            a = aggregate_multi(res["mixed"][arm])
            fa, n = res["healthy"][arm]
            print(f"{arm:<10}{pct(a['all_in_topk'], a['all_in_topk_ci']):<30}{f'{res['runs_all_right'][arm]}/{res['runs']}':<32}"
                  f"{f'{fa}/{n}':<22}")
    print("\nAgents of one run share a server and a window, so agent-runs are not independent: the intervals are optimistic,"
          " the per-run column is the safer count.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, nargs="+", default=[1, 8, 32])
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/selective_conc.json"))
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args()
    if not a.no_record:
        if not bpf_available() or not tc_available():
            sys.exit("needs passwordless sudo bpftrace and tc: run scripts/setup_sudoers.sh once")
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        plan = []
        for c in a.agents:
            plan += [(c, "cal", False, 12), (c, "healthy", False, 6), (c, "mixed", True, 10 if c >= 32 else 20)]
        data = record(a.out, plan)
    else:
        data = json.load(open(a.out))
    report(evaluate(data))


if __name__ == "__main__":
    main()
