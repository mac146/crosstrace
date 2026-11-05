"""Stage 4: baseline (agent spans only) vs combined (spans + kernel evidence) on the SAME runs.

    python -m eval.run_full --n 30

REQUIRES passwordless sudo bpftrace and tc (scripts/setup_sudoers.sh): every run is recorded with the eBPF
collector on (~1 s startup each, so ~250 runs take about 15 minutes).

Seeds match the Stage 1 baseline evaluation (1000..1029 per fault), so fault parameters are identical;
the runs themselves are new (split "full"), and healthy calibration runs are split "fullcal" (seeds 900000+).
Both diagnosers use the same step thresholds, built from the same calibration runs.
"""
import argparse
import json
import os
import sys

from collectors import store
from collectors.ebpf import available as bpf_available
from correlator.join import breakdown, _is_http
from correlator.load import load_timeline
from diagnosers import baseline, combined
from diagnosers.common import COMPONENT, dur_ms, root_span
from diagnosers.profile import build_kernel_profile, build_profile
from eval.metrics import aggregate, score_run, wilson
from faultlab.faults import FAULT_TYPES, NEEDS_ROOT
from faultlab.netem import available as tc_available
from faultlab.runner import run_one

CAL_SEED0 = 900_000
EVAL_SEED0 = 1_000
NETWORK = ("dns_delay", "net_latency", "packet_loss")


def collect(con, faults, n, n_cal, force):
    todo = [("fullcal", "none", CAL_SEED0 + i) for i in range(n_cal)]
    todo += [("full", f, EVAL_SEED0 + i) for f in faults for i in range(n)]
    for k, (split, fault, seed) in enumerate(todo, 1):
        rid = f"{split}-{fault}-{seed}"
        if not force and store.run_exists(con, rid) and store.load_env(con, rid) is not None:
            continue
        run_one(fault, seed, con, split, kernel=True)
        if k % 10 == 0:
            print(f"  {k}/{len(todo)} runs", file=sys.stderr, flush=True)


def build_profiles(con):
    cal = [store.load_spans(con, r["run_id"]) for r in store.list_runs(con, "fullcal", "none")]
    profile = build_profile(cal)
    bds = []
    for r in store.list_runs(con, "fullcal", "none"):
        tl, _ = load_timeline(con, r["run_id"])
        bds += [breakdown(e) for e in tl.entries if _is_http(e.span)]
    return profile, build_kernel_profile(bds)


def evaluate(con, faults):
    profile, kprofile = build_profiles(con)
    out = {}
    for fault in faults:
        rows = []
        for r in store.list_runs(con, "full", fault):
            rid = r["run_id"]
            spans = store.load_spans(con, rid)
            truth = store.load_truth(con, rid)["fault_type"]
            try:
                tl, _ = load_timeline(con, rid)
            except ValueError:
                rows.append({"run_id": rid, "no_kernel": True})
                continue
            slow = dur_ms(root_span(spans)) > profile["run"]
            b = [c.cause for c in baseline.diagnose(spans, profile, tie_seed=r["seed"])]
            c = [x.cause for x in combined.diagnose(tl, profile, kprofile, tie_seed=r["seed"])]
            rows.append({"run_id": rid, "no_kernel": False, "status": r["status"], "slow": slow,
                         "baseline": score_run(b, truth), "combined": score_run(c, truth),
                         "b_top": b[0], "c_top": c[0]})
        out[fault] = rows
    return profile, kprofile, out


def pct(x, ci=None):
    s = f"{100 * x:5.1f}%"
    return s if ci is None else f"{s} [{100 * ci[0]:3.0f}-{100 * ci[1]:3.0f}]"


def summarize(rows, who):
    ok = [r for r in rows if not r["no_kernel"]]
    return aggregate([r[who] for r in ok]) if ok else None


def print_report(profile, kprofile, results):
    print("\nhealthy step thresholds (ms): " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(profile.items())))
    print("healthy kernel thresholds (ms): " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(kprofile.items())))
    print("\nPER FAULT, same runs for both diagnosers (95% Wilson interval in brackets)")
    print(f"{'fault':<12}{'n':>4}  {'':5}{'top-1':<18}{'top-3':<18}{'false-attr':<18}")
    all_rows = []
    for fault, rows in results.items():
        ok = [r for r in rows if not r["no_kernel"]]
        all_rows += [(fault, r) for r in ok]
        for who in ("baseline", "combined"):
            a = summarize(rows, who)
            if a is None:
                continue
            print(f"{fault if who == 'baseline' else '':<12}{a['n'] if who == 'baseline' else '':>4}  "
                  f"{who[:4]:<5}{pct(a['top1'], a['top1_ci']):<18}{pct(a['top3'], a['top3_ci']):<18}"
                  f"{pct(a['false_attr'], a['false_attr_ci']):<18}")
        skipped = len(rows) - len(ok)
        errs = sum(r["status"] != "ok" for r in ok)
        if skipped or errs:
            print(f"{'':<12}   (excluded: {skipped} without kernel data; {errs} agent runs errored)")

    def agg(label, pred):
        sel = [r for f, r in all_rows if pred(f, r)]
        if not sel:
            return
        print(f"\n{label}  (n={len(sel)})")
        for who in ("baseline", "combined"):
            a = aggregate([r[who] for r in sel])
            print(f"  {who:<9} top-1 {pct(a['top1'], a['top1_ci'])}   top-3 {pct(a['top3'], a['top3_ci'])}   "
                  f"false-attr {pct(a['false_attr'], a['false_attr_ci'])}")
        b_only = sum(r["baseline"]["top1"] and not r["combined"]["top1"] for r in sel)
        c_only = sum(r["combined"]["top1"] and not r["baseline"]["top1"] for r in sel)
        print(f"  paired top-1: combined right & baseline wrong = {c_only}, baseline right & combined wrong = {b_only}")

    agg("NETWORK FAULTS (dns_delay, net_latency, packet_loss)", lambda f, r: f in NETWORK)
    agg("NON-NETWORK FAULTS (slow_tool, slow_db, slow_model, rate_limit)",
        lambda f, r: f not in NETWORK and f != "none")
    agg("HEALTHY RUNS", lambda f, r: f == "none")
    agg("packet_loss, only runs that were visibly slow", lambda f, r: f == "packet_loss" and r["slow"])
    agg("packet_loss, only runs that were NOT visibly slow", lambda f, r: f == "packet_loss" and not r["slow"])
    agg("ALL FAULT TYPES TOGETHER", lambda f, r: True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--cal", type=int, default=30)
    ap.add_argument("--faults", nargs="+", default=FAULT_TYPES, choices=FAULT_TYPES)
    ap.add_argument("--db", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", default=None, help="also write per-run results here")
    a = ap.parse_args()
    if not bpf_available() or (NEEDS_ROOT & set(a.faults) and not tc_available()):
        sys.exit("needs passwordless sudo bpftrace and tc: run scripts/setup_sudoers.sh once")
    con = store.connect(a.db)
    collect(con, a.faults, a.n, a.cal, a.force)
    profile, kprofile, results = evaluate(con, a.faults)
    print_report(profile, kprofile, results)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"profile": profile, "kernel_profile": kprofile, "runs": results}, f, indent=1)
        print(f"\nper-run results written to {a.json}")


if __name__ == "__main__":
    main()
