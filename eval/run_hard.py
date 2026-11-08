"""Stage 6: harder evaluations. Run one suite, score several diagnoser variants on the SAME recorded runs.

    python -m eval.run_hard --suite slowapi --split hard_a --seed0 3000 --variants base v2
    python -m eval.run_hard --suite subtle  --split sub_a  --seed0 5000 --variants base v3
    python -m eval.run_hard --suite pairs   --split pair_a --seed0 7000 --variants base v3

Suites
  slowapi  the API server is slow while the network is healthy (plus the network faults, to check that knowing about
           slow_api does not hurt them). Correct answer for slow_api: slow_api; blaming the network is a false attribution.
  subtle   20-100 ms faults close to normal noise, scored under both threshold rules (coarse and tight, see
           diagnosers/profile.py). 60 healthy runs measure the false-alarm cost of the tight rule.
  pairs    two faults injected at once (faultlab.faults.TWO_FAULT_PAIRS), scored on finding both in the top two.

Variants (base = spans only; v2 = combined as of Stage 4; v3 = v2 plus the slow_api rule, added after the
slowapi suite showed v2 cannot name a slow server). The baseline always gets the extended four-way split
(dns_delay, net_latency, packet_loss, slow_api) in this stage.

REQUIRES passwordless sudo bpftrace and tc (scripts/setup_sudoers.sh).
"""
import argparse
import contextlib
import json
import subprocess
import sys

from collectors import store
from collectors.ebpf import available as bpf_available
from correlator.join import _is_http, breakdown
from correlator.load import load_timeline
from diagnosers import baseline, combined
from diagnosers.profile import build_kernel_profile, build_profile, kernel_profile_from_timelines
from eval.metrics import aggregate, aggregate_multi, score_multi, score_run
from eval.run_full import pct
from faultlab.faults import SUBTLE_FAULTS, TWO_FAULT_PAIRS, truth_faults
from faultlab.netem import available as tc_available
from faultlab.runner import run_one

CAL_SPLIT, CAL_SEED0 = "hardcal", 910_000
EXT4 = ("dns_delay", "net_latency", "packet_loss", "slow_api")
SUITES = {
    "slowapi": {"faults": ["none", "slow_api", "dns_delay", "net_latency", "packet_loss"],
                "magnitude": "normal", "rules": ["coarse"]},
    "subtle": {"faults": ["none"] + SUBTLE_FAULTS, "magnitude": "subtle", "rules": ["coarse", "tight"]},
    "pairs": {"faults": TWO_FAULT_PAIRS, "magnitude": "normal", "rules": ["coarse"]},
}
LABEL = {"base": "base", "v2": "v2", "v3": "v3", "v4": "v4", "v5": "v5", "pool": "pool", "poolw": "poolw"}


@contextlib.contextmanager
def cpu_load(n):
    """n busy-loop processes for the duration of the block (n=0: nothing). Used to test how thresholds hold up when
    the machine is not idle; the load competes with the agent, the mock services and the collector."""
    procs = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(n)]
    try:
        yield
    finally:
        for p in procs:
            p.kill()
        for p in procs:
            p.wait()


def collect(con, suite, split, seed0, n, n_none, n_cal, force, cal_split=CAL_SPLIT, llm_kind="mock"):
    cfg = SUITES[suite]
    todo = [(cal_split, "none", CAL_SEED0 + i, "normal") for i in range(n_cal)]
    for f in cfg["faults"]:
        mag = "normal" if f == "none" else cfg["magnitude"]
        todo += [(split, f, seed0 + i, mag) for i in range(n_none if f == "none" else n)]
    for k, (sp, fault, seed, mag) in enumerate(todo, 1):
        rid = f"{sp}-{fault}-{seed}"
        if not force and store.run_exists(con, rid) and store.load_env(con, rid) is not None:
            continue
        run_one(fault, seed, con, sp, kernel=True, magnitude=mag, llm_kind=llm_kind)
        if k % 10 == 0:
            print(f"  {k}/{len(todo)} runs", file=sys.stderr, flush=True)


def calibrate(con, rule, cal_split=CAL_SPLIT):
    runs = store.list_runs(con, cal_split, "none")
    profile = build_profile([store.load_spans(con, r["run_id"]) for r in runs], rule)
    timelines = [load_timeline(con, r["run_id"])[0] for r in runs]
    return profile, kernel_profile_from_timelines(timelines, rule)


def diagnose(variant, spans, tl, profile, kprofile, seed):
    if variant == "base":
        out = baseline.diagnose(spans, profile, tie_seed=seed, api_causes=EXT4)
    elif variant == "v2":
        out = combined.diagnose(tl, profile, kprofile, tie_seed=seed, loss_explains_rtt=True)
    elif variant == "v3":
        out = combined.diagnose(tl, profile, kprofile, tie_seed=seed, loss_explains_rtt=True, slow_api=True)
    elif variant == "v5":
        out = combined.diagnose(tl, profile, kprofile, tie_seed=seed, loss_explains_rtt=True, slow_api=True,
                                latent_baseline=True, run_level=True)
    elif variant in ("pool", "poolw"):     # Stage 13: v3's rules fed run-wide host-metric evidence instead of per-span joined evidence
        out = combined.diagnose(tl, profile, kprofile, tie_seed=seed, loss_explains_rtt=True, slow_api=True,
                                source="pooled" if variant == "pool" else "pooled_wait")
    elif variant == "v4":
        out = combined.diagnose(tl, profile, kprofile, tie_seed=seed, loss_explains_rtt=True, slow_api=True,
                                latent_baseline=True)
    else:
        raise ValueError(variant)
    return [c.cause for c in out]


def evaluate(con, suite, split, rule, variants, cal_split=CAL_SPLIT):
    profile, kprofile = calibrate(con, rule, cal_split)
    out = {}
    for fault in SUITES[suite]["faults"]:
        rows = []
        for r in store.list_runs(con, split, fault):
            try:
                tl, _ = load_timeline(con, r["run_id"])
            except ValueError:
                rows.append({"run_id": r["run_id"], "no_kernel": True})
                continue
            spans = store.load_spans(con, r["run_id"])
            rows.append({"run_id": r["run_id"], "no_kernel": False, "status": r["status"],
                         "ranked": {v: diagnose(v, spans, tl, profile, kprofile, r["seed"]) for v in variants}})
        out[fault] = rows
    return profile, kprofile, out


def report(suite, rule, variants, profile, kprofile, results):
    multi = suite == "pairs"
    print(f"\n=== suite {suite}, threshold rule '{rule}' ===")
    print("step thresholds (ms): " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(profile.items())))
    print("kernel thresholds (ms): " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(kprofile.items())
                                                  if isinstance(v, (int, float))))
    head = "top-1 in truth      both in top-2      false-attr         said none" if multi else \
           "top-1               top-3              false-attr         said none"
    print(f"\n{'fault':<24}{'n':>3}  {'':5}{head}")
    pooled = {v: [] for v in variants}
    for fault, rows in results.items():
        ok = [r for r in rows if not r["no_kernel"]]
        truths = truth_faults(fault)
        for v in variants:
            if multi:
                sc = [score_multi(r["ranked"][v], truths) for r in ok]
                a = aggregate_multi(sc) if sc else None
                cols = (f"{pct(a['top1_in_truth'], a['top1_in_truth_ci']):<19}"
                        f"{pct(a['all_in_topk'], a['all_in_topk_ci']):<19}") if a else ""
            else:
                sc = [score_run(r["ranked"][v], fault) for r in ok]
                a = aggregate(sc) if sc else None
                cols = (f"{pct(a['top1'], a['top1_ci']):<19}{pct(a['top3'], a['top3_ci']):<19}") if a else ""
            if not a:
                continue
            pooled[v] += sc
            none_rate = sum(r["ranked"][v][0] == "none" for r in ok) / len(ok)
            print(f"{(fault if v == variants[0] else ''):<24}{(a['n'] if v == variants[0] else ''):>3}  "
                  f"{LABEL[v]:<5}{cols}{pct(a['false_attr'], a['false_attr_ci']):<19}{100 * none_rate:5.1f}%")
        errs = sum(r["status"] != "ok" for r in ok)
        skipped = len(rows) - len(ok)
        if errs or skipped:
            print(f"{'':<24}   (excluded {skipped} without kernel data; {errs} agent runs errored, scored as they are)")
    if not multi:
        healthy = results.get("none", [])
        print(f"\nhealthy runs: false alarm (answer other than 'none'):")
        for v in variants:
            ok = [r for r in healthy if not r["no_kernel"]]
            bad = sum(r["ranked"][v][0] != "none" for r in ok)
            print(f"  {LABEL[v]:<5} {bad}/{len(ok)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True, choices=list(SUITES))
    ap.add_argument("--split", required=True, help="run split name; use a new one for fresh seeds")
    ap.add_argument("--seed0", type=int, required=True)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--n-none", type=int, default=None, help="healthy runs (default: 60 for subtle, else n)")
    ap.add_argument("--cal", type=int, default=60)
    ap.add_argument("--load", type=int, default=0, help="busy-loop processes running while RECORDING (CPU load)")
    ap.add_argument("--cal-split", default=CAL_SPLIT,
                    help="healthy calibration split to record into / calibrate from (e.g. hardcal_load)")
    ap.add_argument("--llm-kind", default="mock", choices=["mock", "langgraph-mock"],
                    help="which agent to run: our own (mock) or the LangGraph agent against the mock model server")
    ap.add_argument("--only", nargs="+", default=None, help="restrict to these faults of the suite")
    ap.add_argument("--variants", nargs="+", default=["base", "v2"], choices=list(LABEL))
    ap.add_argument("--db", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-record", action="store_true", help="only evaluate runs already in the database")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    n_none = a.n_none or (60 if a.suite == "subtle" else a.n)
    if a.only:
        bad = set(a.only) - set(SUITES[a.suite]["faults"])
        if bad:
            sys.exit(f"not in suite {a.suite}: {sorted(bad)}")
        SUITES[a.suite]["faults"] = a.only
    con = store.connect(a.db)
    if not a.no_record:
        if not bpf_available() or not tc_available():
            sys.exit("needs passwordless sudo bpftrace and tc: run scripts/setup_sudoers.sh once")
        with cpu_load(a.load):
            collect(con, a.suite, a.split, a.seed0, a.n, n_none, a.cal, a.force, a.cal_split, a.llm_kind)
    dump = {}
    for rule in SUITES[a.suite]["rules"]:
        profile, kprofile, results = evaluate(con, a.suite, a.split, rule, a.variants, a.cal_split)
        report(a.suite, rule, a.variants, profile, kprofile, results)
        dump[rule] = {"profile": profile, "kernel_profile": kprofile, "runs": results}
    if a.json:
        with open(a.json, "w") as f:
            json.dump(dump, f, indent=1)
        print(f"\nper-run results written to {a.json}")


if __name__ == "__main__":
    main()
