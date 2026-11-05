"""Stage 1 exit command: run N trials per fault and print baseline (agent-only) accuracy.

    python -m eval.run_baseline --n 30

Root: net_latency / packet_loss use `tc netem` (see faultlab/netem.py). Skip them with
--faults to run everything else without root.
"""
import argparse
import sys

from collectors import store
from diagnosers import baseline
from diagnosers.common import dur_ms, root_span
from diagnosers.profile import build_profile
from eval.metrics import aggregate, score_run
from faultlab.faults import FAULT_TYPES, NEEDS_ROOT
from faultlab.netem import available as netem_available
from faultlab.runner import run_one

CAL_SEED0 = 900_000   # calibration (healthy) seeds, disjoint from evaluation seeds
EVAL_SEED0 = 1_000


def fmt_pct(x, ci):
    return f"{100 * x:5.1f}% [{100 * ci[0]:4.0f}-{100 * ci[1]:3.0f}]"


def collect(con, faults, n, n_cal, force):
    todo = [("calibration", "none", CAL_SEED0 + i) for i in range(n_cal)]
    todo += [("eval", f, EVAL_SEED0 + i) for f in faults for i in range(n)]
    for k, (split, fault, seed) in enumerate(todo, 1):
        rid = f"{split}-{fault}-{seed}"
        if not force and store.run_exists(con, rid):
            continue
        run_one(fault, seed, con, split)
        if k % 20 == 0:
            print(f"  {k}/{len(todo)} runs", file=sys.stderr, flush=True)


def evaluate(con, faults, diagnoser_name="baseline"):
    cal = [store.load_spans(con, r["run_id"]) for r in store.list_runs(con, "calibration", "none")]
    profile = build_profile(cal)
    rows = {}
    for fault in faults:
        scores, manifested, errors = [], 0, 0
        runs = store.list_runs(con, "eval", fault)
        for r in runs:
            spans = store.load_spans(con, r["run_id"])
            truth = store.load_truth(con, r["run_id"])["fault_type"]
            errors += r["status"] != "ok"
            manifested += dur_ms(root_span(spans)) > profile["run"]
            ranked = [c.cause for c in baseline.diagnose(spans, profile, tie_seed=r["seed"])]
            scores.append(score_run(ranked, truth))
        rows[fault] = {**aggregate(scores), "manifested": manifested / len(runs) if runs else float("nan"),
                       "errors": errors}
    return profile, rows


def print_table(rows, title):
    print(f"\n{title}  (95% Wilson interval in brackets)")
    print(f"{'fault':<12}{'n':>4}  {'top-1':<18}{'top-3':<18}{'false-attr':<18}{'slow run':>9}{'errors':>7}")
    for f, r in rows.items():
        print(f"{f:<12}{r['n']:>4}  {fmt_pct(r['top1'], r['top1_ci']):<18}"
              f"{fmt_pct(r['top3'], r['top3_ci']):<18}{fmt_pct(r['false_attr'], r['false_attr_ci']):<18}"
              f"{100 * r['manifested']:>8.0f}%{r['errors']:>7}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="trials per fault type")
    ap.add_argument("--cal", type=int, default=30, help="healthy calibration runs")
    ap.add_argument("--faults", nargs="+", default=FAULT_TYPES, choices=FAULT_TYPES)
    ap.add_argument("--db", default=None, help="sqlite path (default: ~/.crosstrace-data/crosstrace.db)")
    ap.add_argument("--force", action="store_true", help="re-run trials already in the database")
    a = ap.parse_args()

    if NEEDS_ROOT & set(a.faults) and not netem_available():
        sys.exit("net_latency/packet_loss need passwordless root for `tc` "
                 "(run scripts/setup_sudoers.sh once, or pass --faults without them).")
    con = store.connect(a.db)
    collect(con, a.faults, a.n, a.cal, a.force)
    profile, rows = evaluate(con, a.faults)
    print("\nhealthy thresholds (ms): " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(profile.items())))
    print_table(rows, "BASELINE (agent spans only)")
    print("\n'slow run' = share of runs whose total time exceeded the healthy threshold, i.e. the "
          "fault visibly slowed the run.")


if __name__ == "__main__":
    main()
