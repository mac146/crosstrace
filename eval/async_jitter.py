"""Stage 19: does assignment-by-time survive uneven waits before connecting? (predictions: docs/predictions-stage19.md)

    python -m eval.async_jitter                  # record (ROOT) and evaluate
    python -m eval.async_jitter --no-record

Stage 17's async scenario (24 coroutines on one thread, kinds H/N/S, real OBI beside our collector) with a random wait, uniform on 0..J ms, between a call's span
start and its connection, J in {0, 50, 200, 500}. Calibration at each J. Arms: the Stage 17 arms plus two ablations of the matcher (start time only, duration only).
Primary measure: share of calls assigned to their TRUE connection; secondary: diagnosis top-1 and the gap between `v3 + assignment` and `v3 + declared port`.
REQUIRES passwordless sudo for bpftrace/ctrace/tc/obi (scripts/setup_sudoers.sh).
"""
import argparse
import json
import os
import sys
import time

from correlator.assign import declare_ports_by_assignment
from correlator.join import _is_http
from diagnosers import baseline, combined
from diagnosers.profile import _threshold, build_profile, kernel_profile_from_timelines
from eval.async_conc import ASSIGN_WEIGHTS, join_views, one_run, strip_declared
from eval.metrics import wilson
from eval.obi_conc import TRUTH, http_spans, match_evidence, parse_http
from eval.selective import EXT4
from eval.selective_obi import LOG, OBI, Obi, parse_stats
from faultlab.netem import available as tc_available
from collectors.ebpf import available as bpf_available

JITTERS = [0, 50, 200, 500]
C = 24
MODES = ["asis", "declared", "assign", "assign_start", "assign_dur"]
ARMS = ["base", "v3_asis", "v3_declared", "v3_assign", "v3_assign_start", "v3_assign_dur", "pool", "obi_flows", "obi_port"]
LABEL = {"base": "spans only", "v3_asis": "v3, no declared port", "v3_declared": "v3 + declared port (control)",
         "v3_assign": "v3 + assignment (Stage 17)", "v3_assign_start": "  assignment, start only", "v3_assign_dur": "  assignment, duration only",
         "pool": "host-wide", "obi_flows": "OBI, order matching", "obi_port": "OBI, exact port (control)"}
ARM_MODE = {"v3_asis": "asis", "v3_declared": "declared", "v3_assign": "assign", "v3_assign_start": "assign_start", "v3_assign_dur": "assign_dur"}


def record(path, cal, n):
    data = json.load(open(path)) if os.path.exists(path) else {}
    with Obi() as obi:
        for jit in JITTERS:
            for role, mixed, count in (("cal", False, cal), ("mixed", True, n)):
                for j in range(count):
                    rid = f"j{jit}-{role}-{j}"
                    if rid in data:
                        continue
                    data[rid] = {"c": C, "jit": jit, "role": role, **one_run(C, mixed, f"s19-{jit}-{role}-{j}", obi, jitter_ms=jit)}
                    time.sleep(0.5)
                    print(f"  {rid}", file=sys.stderr, flush=True)
    data["_http"] = [list(x) for x in parse_http(open(LOG).read())]
    data["_stats"] = [list(x) for x in parse_stats(open(LOG).read())]
    json.dump(data, open(path, "w"))
    return data


def true_connection_share(r, weights):
    """{kind: [calls assigned to their true connection, calls]} for one run (the declared port is the truth)."""
    kind_of = {}
    truth = {}
    for i, a in enumerate(r["agents"]):
        for s in a:
            if _is_http(s):
                kind_of[s["span_id"]] = r["kinds"][i]
                truth[s["span_id"]] = s["attrs"].get("network.local.port")
    spans = [s for a in r["agents"] for s in a]
    fixed, _, _ = declare_ports_by_assignment(strip_declared(spans), r["events"], [tuple(c) for c in r["sync"]], weights)
    out = {k: [0, 0] for k in ("H", "N", "S")}
    for s in fixed:
        if _is_http(s):
            k = kind_of[s["span_id"]]
            out[k][0] += s["attrs"].get("network.local.port") == truth[s["span_id"]]
            out[k][1] += 1
    return out


def evaluate(data):
    http = [tuple(x) for x in data.get("_http", [])]
    stats = [tuple(x) for x in data.get("_stats", [])]
    runs_all = {k: v for k, v in data.items() if not k.startswith("_")}
    evidence = match_evidence(runs_all, http, stats) if http else {}
    out = {}
    for jit in sorted({r["jit"] for r in runs_all.values()}):
        runs = {k: v for k, v in runs_all.items() if v["jit"] == jit}
        cal = {k: r for k, r in runs.items() if r["role"] == "cal"}
        profile = build_profile([s for r in cal.values() for s in r["agents"]], "coarse")
        kp = {m: kernel_profile_from_timelines([v for r in cal.values() for v in join_views(r, m)[2]], "coarse") for m in MODES}
        kp_obi = dict(kp["declared"])
        hw = [v for rid in cal for v in evidence.get(rid, {}).get("port", ({}, {}))[0].values()]
        hr = [v for rid in cal for v in evidence.get(rid, {}).get("port", ({}, {}))[1].values()]
        if hw:
            kp_obi["server_wait_ms"] = _threshold(hw, "coarse")
        if hr:
            kp_obi["srtt_ms"] = _threshold(hr, "coarse")
        res = {a: [0, 0] for a in ARMS}
        confusion = {}
        share = {m: {k: [0, 0] for k in ("H", "N", "S")} for m in ("assign", "assign_start", "assign_dur")}
        thr = profile.get("tool:http_api", 0)
        n_runs = 0
        for rid, r in runs.items():
            if r["role"] != "mixed":
                continue
            n_runs += 1
            for m in share:
                for k, (a, b) in true_connection_share(r, ASSIGN_WEIGHTS[m]).items():
                    share[m][k][0] += a
                    share[m][k][1] += b
            views = {m: join_views(r, m) for m in MODES}
            ev = evidence.get(rid)
            for n, kind in enumerate(r["kinds"]):
                spans = r["agents"][n]
                ranked = {"base": [x.cause for x in baseline.diagnose(spans, profile, tie_seed=n, api_causes=EXT4)]}
                for arm, m in ARM_MODE.items():
                    tl, all_http, per_agent, _ = views[m]
                    ranked[arm] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp[m], tie_seed=n, loss_explains_rtt=True, slow_api=True)]
                tl, all_http, per_agent, _ = views["declared"]
                ranked["pool"] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp["declared"], tie_seed=n, loss_explains_rtt=True,
                                                                     slow_api=True, source="pooled", pool_entries=all_http)]
                for arm, method in (("obi_flows", "order"), ("obi_port", "port")):
                    if ev:
                        w, rr = ev[method]
                        ranked[arm] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp_obi, tie_seed=n, loss_explains_rtt=True, slow_api=True,
                                                                          source="obi_real_flows", pool_entries=all_http, obi_waits=w, obi_srtts=rr)]
                    else:
                        ranked[arm] = ["none"]
                for arm in ARMS:
                    ok = ranked[arm][0] == TRUTH[kind]
                    res[arm][0] += ok
                    res[arm][1] += 1
                    if arm == "v3_assign" and not ok:
                        confusion[(kind, ranked[arm][0])] = confusion.get((kind, ranked[arm][0]), 0) + 1
        out[jit] = {"res": res, "confusion": confusion, "share": share, "runs": n_runs, "thr": thr}
    return out


def report(out):
    def pc(k, n):
        lo, hi = wilson(k, n) if n else (0, 0)
        return f"{100 * k / n:5.1f}% [{100 * lo:3.0f}-{100 * hi:3.0f}]" if n else "-"
    for jit, g in out.items():
        res, share = g["res"], g["share"]
        print(f"\n=== wait J = {jit} ms (uniform 0..J before each connect), {C} coroutines, {g['runs']} mixed runs; healthy http_api threshold {g['thr']:.0f} ms")
        print("  calls assigned to their TRUE connection:        overall              H                    N                    S")
        for m, label in (("assign", "combined cost (Stage 17)"), ("assign_start", "start time only"), ("assign_dur", "duration only")):
            tot = [sum(share[m][k][i] for k in share[m]) for i in (0, 1)]
            print(f"    {label:<30}{pc(*tot):<21}" + "".join(f"{pc(*share[m][k]):<21}" for k in ("H", "N", "S")))
        print("  per-agent diagnosis top-1:")
        for arm in ARMS:
            print(f"    {LABEL[arm]:<32}{pc(*res[arm])}")
        gap = 100 * (res["v3_declared"][0] / res["v3_declared"][1] - res["v3_assign"][0] / res["v3_assign"][1])
        errs = g["confusion"]
        total = sum(errs.values())
        ns = errs.get(("N", "slow_api"), 0) + errs.get(("S", "net_latency"), 0)
        print(f"  gap (declared port minus assignment): {gap:+.1f} points; assignment errors {total}" +
              (f", N<->S swaps {ns} ({100 * ns / total:.0f}%): " + ", ".join(f"{k[0]}->{k[1]}: {v}" for k, v in sorted(errs.items(), key=lambda x: -x[1])) if total else ""))
    print("\nAgents of one run share a server and a lockstep schedule, so agent-runs are not independent: the intervals are optimistic.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/async_jitter.json"))
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--cal", type=int, default=6)
    ap.add_argument("--mixed-runs", type=int, default=8)
    a = ap.parse_args()
    if not a.no_record:
        if not bpf_available() or not tc_available() or not os.path.exists(OBI):
            sys.exit("needs passwordless sudo for bpftrace, tc and obi: run scripts/setup_sudoers.sh once")
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        data = record(a.out, a.cal, a.mixed_runs)
    else:
        data = json.load(open(a.out))
    report(evaluate(data))


if __name__ == "__main__":
    main()
