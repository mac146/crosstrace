"""Stage 8: faults injected on the REAL network path to a REAL model, scored by every diagnoser variant.

    CROSSTRACE_ENV_FILE=/path/to/.env python -m eval.real_faults --n 8 --cal 16 --variants base v3 v4

!!! REQUIRES ROOT (passwordless sudo tc and bpftrace, scripts/setup_sudoers.sh) and TEMPORARILY changes network settings of
the real interface (see faultlab/real_path.py: what exactly is matched, and the safety nets). Dev WSL / VM only. !!!

Faults (faultlab/real_path.py): dns_delay (UDP to the resolver delayed 4-8 s), net_latency (egress to the model server
delayed 0.8-1.5 s per packet), packet_loss (egress to the model server loses 15-30% of packets). Healthy and local-fault groups
come from the same agent. Each run uses a fresh HTTP client, so each pays a DNS lookup and a TLS connect.
A run in which the fault was not actually on the path used (CDN addresses can change between resolving and connecting) is
reported separately and not scored.

Cost: about 4 small model calls per run (see the totals printed at the end).
"""
import argparse
import statistics
import sys

from collectors import store
from collectors.ebpf import available as bpf_available
from correlator.join import _is_http, breakdown
from correlator.load import load_timeline
from diagnosers.common import dur_ms, root_span
from eval.metrics import aggregate, score_run
from eval.real_model import calibrate
from eval.run_full import pct
from eval.run_hard import diagnose
from faultlab.netem import available as tc_available
from faultlab.real_path import REAL_FAULTS
from faultlab.runner import run_one

CAL_SEED0 = 930_000
GROUPS = ["none", *REAL_FAULTS]


def collect(con, a):
    todo = [(a.cal_split, "none", CAL_SEED0 + i) for i in range(a.cal)]
    todo += [(a.split, f, a.seed0 + i) for f in a.faults for i in range(a.n)]
    for k, (split, fault, seed) in enumerate(todo, 1):
        rid = f"{split}-{fault}-{seed}"
        if not a.force and store.run_exists(con, rid) and store.load_env(con, rid) is not None:
            continue
        run_one(fault, seed, con, split, kernel=True, llm_kind="langgraph")
        print(f"  {k}/{len(todo)} runs", file=sys.stderr, flush=True)


def evaluate(con, a, profile, kprofile):
    out = {}
    for fault in a.faults:
        rows, excluded = [], 0
        for r in store.list_runs(con, a.split, fault):
            params = store.load_truth(con, r["run_id"])["params"]
            if params.get("applied") is False:
                excluded += 1
                continue
            try:
                tl, _ = load_timeline(con, r["run_id"])
            except ValueError:
                excluded += 1
                continue
            spans = store.load_spans(con, r["run_id"])
            model = [e for e in tl.entries if _is_http(e.span) and e.span["attrs"]["server.port"] == 443]
            bds = [breakdown(e) for e in model]
            rows.append({"run_id": r["run_id"], "status": r["status"], "params": params,
                         "ranked": {v: diagnose(v, spans, tl, profile, kprofile, r["seed"]) for v in a.variants},
                         "run_ms": dur_ms(root_span(spans)) if spans else None,
                         "dns_ms": max([b["dns_ms"] for b in bds if b["dns_ms"] is not None], default=None),
                         "connect_ms": max([b["connect_ms"] for b in bds if b["connect_ms"] is not None], default=None),
                         "srtt_ms": max([b["srtt_max_ms"] for b in bds if b["srtt_max_ms"] is not None], default=None),
                         "retx": sum(b["retransmits"] for b in bds),
                         "model_spans": len(bds),
                         "with_evidence": sum(b["n_events"] > 0 for b in bds)})
        out[fault] = (rows, excluded)
    return out


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def fmt(x, nd=0):
    return "-" if x is None else f"{x:.{nd}f}"


def report(a, results, profile, kprofile):
    print("\nstep thresholds (ms): " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(profile.items())))
    print("kernel thresholds: " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(kprofile.items())))
    print(f"\n{'fault':<12}{'n':>3}  {'':5}{'top-1':<19}{'false-attr':<19}{'said none':>9}")
    for fault, (rows, excluded) in results.items():
        for v in a.variants:
            sc = [score_run(r["ranked"][v], fault) for r in rows]
            if not sc:
                continue
            ag = aggregate(sc)
            none = sum(r["ranked"][v][0] == "none" for r in rows) / len(rows)
            print(f"{(fault if v == a.variants[0] else ''):<12}{(ag['n'] if v == a.variants[0] else ''):>3}  {v:<5}"
                  f"{pct(ag['top1'], ag['top1_ci']):<19}{pct(ag['false_attr'], ag['false_attr_ci']):<19}{100 * none:8.0f}%")
        errs = sum(r["status"] != "ok" for r in rows)
        if errs or excluded:
            print(f"{'':<12}   ({errs} agent runs errored, scored as they are; {excluded} runs excluded: fault not on the "
                  f"path used, or no kernel data)")
    print("\nWHAT THE KERNEL MEASURED (medians over scored runs; injected value in the second column)")
    print(f"{'fault':<12}{'injected':>14}{'run s':>8}{'dns ms':>8}{'connect':>8}{'srtt max':>9}{'retx/run':>9}"
          f"{'spans with evidence':>21}")
    for fault, (rows, _) in results.items():
        if not rows:
            continue
        inj = med([next(iter({k: v for k, v in r["params"].items() if k != "applied"}.values()), None) for r in rows])
        unit = {"dns_delay": " ms", "net_latency": " ms", "packet_loss": " %"}.get(fault, "")
        spans = sum(r["model_spans"] for r in rows)
        ev = sum(r["with_evidence"] for r in rows)
        print(f"{fault:<12}{(fmt(inj) + unit) if inj is not None else '-':>14}{fmt(med([r['run_ms'] for r in rows]) / 1000 if rows else None, 1):>8}"
              f"{fmt(med([r['dns_ms'] for r in rows])):>8}{fmt(med([r['connect_ms'] for r in rows]), 1):>8}"
              f"{fmt(med([r['srtt_ms'] for r in rows]), 1):>9}{fmt(med([r['retx'] for r in rows])):>9}"
              f"{f'{ev}/{spans} ({100 * ev / max(spans, 1):.0f}%)':>21}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--cal", type=int, default=16, help="healthy runs for calibration (new probes, so a new split)")
    ap.add_argument("--cal-split", default="realcal2")
    ap.add_argument("--split", default="rf_a")
    ap.add_argument("--seed0", type=int, default=11_000)
    ap.add_argument("--faults", nargs="+", default=GROUPS, choices=GROUPS)
    ap.add_argument("--variants", nargs="+", default=["base", "v3", "v4"], choices=["base", "v2", "v3", "v4"])
    ap.add_argument("--rule", default="coarse", choices=["coarse", "tight"])
    ap.add_argument("--price-in", type=float, default=0.034)
    ap.add_argument("--price-out", type=float, default=0.083)
    ap.add_argument("--db", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args()
    con = store.connect(a.db)
    if not a.no_record:
        if not bpf_available() or not tc_available():
            sys.exit("needs passwordless sudo bpftrace and tc: run scripts/setup_sudoers.sh once")
        from agent.langgraph_agent import TOTAL, load_settings
        key, base_url, model = load_settings()
        if not key:
            sys.exit("no API key: set CROSSTRACE_ENV_FILE or put OPENROUTER_API_KEY / OPENCODE_API_KEY in .env")
        print(f"model endpoint: {base_url}  model: {model}", file=sys.stderr)
        collect(con, a)
        cost = TOTAL["input_tokens"] * a.price_in / 1e6 + TOTAL["output_tokens"] * a.price_out / 1e6
        print(f"\nAPI use: {TOTAL['calls']} calls, {TOTAL['input_tokens']} input + {TOTAL['output_tokens']} output tokens, "
              f"about ${cost:.4f}")
    cal = calibrate(con, a.cal_split, a.rule)
    if cal is None:
        sys.exit(f"no calibration runs in split {a.cal_split}")
    profile, kprofile = cal
    report(a, evaluate(con, a, profile, kprofile), profile, kprofile)


if __name__ == "__main__":
    main()
