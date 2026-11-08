"""Stage 6 realism check: the same agent and diagnosers with a REAL model instead of the mock.

    python -m eval.real_model --cal 12 --n 8 --variants base v2

What it asks (a small sample, not a benchmark):
  1. Does the kernel see real TLS calls to a remote host (connect, RTT, data timing), and its DNS lookups?
  2. Healthy real latency is seconds and noisy, not milliseconds: with thresholds calibrated on a few real healthy
     runs, how often do the diagnosers raise a false alarm, and do they still find local faults (slow_tool, slow_db)?
  3. What happens if the mock-calibrated thresholds are used unchanged (they should fail; this shows why
     calibration against the real service is part of the method)?
Network faults are NOT injected here: that would mean running `tc` on the real network interface.

Cost: about 4 calls per run, ~1-2k tokens each, on claude-haiku-5-5 ($0.10 / $0.50 per million tokens).
The total is printed at the end. Needs an API key (env ANTHROPIC_API_KEY or ~/.config/crosstrace/anthropic_key),
`pip install -r requirements-real.txt`, and passwordless sudo bpftrace.
"""
import argparse
import sys

from collectors import store
from collectors.ebpf import available as bpf_available
from correlator.join import _is_http, breakdown
from correlator.load import load_timeline
from diagnosers.profile import build_kernel_profile, build_profile
from eval.metrics import aggregate, score_run
from eval.run_full import pct
from eval.run_hard import diagnose
from faultlab.runner import run_one

FAULTS = ["none", "slow_tool", "slow_db"]
CAL_SPLIT, CAL_SEED0 = "realcal", 920_000
EVAL_SPLIT, EVAL_SEED0 = "real", 8_000
MOCK_CAL_SPLITS = ("hardcal", "fullcal")


def collect(con, n, n_cal, force, llm_kind="anthropic"):
    todo = [(CAL_SPLIT, "none", CAL_SEED0 + i) for i in range(n_cal)]
    todo += [(EVAL_SPLIT, f, EVAL_SEED0 + i) for f in FAULTS for i in range(n)]
    for k, (split, fault, seed) in enumerate(todo, 1):
        rid = f"{split}-{fault}-{seed}"
        if not force and store.run_exists(con, rid) and store.load_env(con, rid) is not None:
            continue
        run_one(fault, seed, con, split, kernel=True, llm_kind=llm_kind)
        print(f"  {k}/{len(todo)} runs", file=sys.stderr, flush=True)


def calibrate(con, split, rule="coarse"):
    runs = store.list_runs(con, split, "none")
    if not runs:
        return None
    profile = build_profile([store.load_spans(con, r["run_id"]) for r in runs], rule)
    bds, run_retx = [], []
    for r in runs:
        tl, _ = load_timeline(con, r["run_id"])
        these = [breakdown(e) for e in tl.entries if _is_http(e.span)]
        bds += these
        run_retx.append(sum(b["retransmits"] for b in these))
    return profile, build_kernel_profile(bds, rule, run_retx)


def kernel_visibility(con):
    """Share of HTTP spans that got kernel evidence, real-model spans (port 443) vs the local API tool."""
    counts = {"real model (TLS, remote)": [0, 0, 0, 0, 0], "API tool (local, stub resolver)": [0, 0, 0, 0, 0]}
    for split in (CAL_SPLIT, EVAL_SPLIT):
        for r in con.execute("SELECT run_id FROM runs WHERE split = ?", (split,)).fetchall():
            try:
                tl, _ = load_timeline(con, r[0])
            except ValueError:
                continue
            for e in tl.entries:
                if not _is_http(e.span):
                    continue
                key = "real model (TLS, remote)" if e.span["attrs"]["server.port"] == 443 else \
                      "API tool (local, stub resolver)"
                kinds = [a.event["ev"] for a in e.kernel]
                c = counts[key]
                c[0] += 1
                c[1] += "tcp_connect_end" in kinds
                c[2] += "dns_response" in kinds
                c[3] += "tcp_rtt" in kinds
                c[4] += breakdown(e)["server_wait_ms"] is not None
    return counts


def evaluate(con, profile, kprofile, variants):
    out = {}
    for fault in FAULTS:
        rows = []
        for r in store.list_runs(con, EVAL_SPLIT, fault):
            try:
                tl, _ = load_timeline(con, r["run_id"])
            except ValueError:
                continue
            spans = store.load_spans(con, r["run_id"])
            rows.append({v: diagnose(v, spans, tl, profile, kprofile, r["seed"]) for v in variants}
                        | {"status": r["status"]})
        out[fault] = rows
    return out


def print_scores(title, results, variants):
    print(f"\n{title}")
    print(f"{'fault':<11}{'n':>3}  {'':5}{'top-1':<19}{'false-attr':<19}{'said none':>9}")
    for fault, rows in results.items():
        for v in variants:
            sc = [score_run(r[v], fault) for r in rows]
            if not sc:
                continue
            a = aggregate(sc)
            none = sum(r[v][0] == "none" for r in rows) / len(rows)
            print(f"{(fault if v == variants[0] else ''):<11}{(a['n'] if v == variants[0] else ''):>3}  {v:<5}"
                  f"{pct(a['top1'], a['top1_ci']):<19}{pct(a['false_attr'], a['false_attr_ci']):<19}"
                  f"{100 * none:8.0f}%")
        errs = sum(r["status"] != "ok" for r in rows)
        if errs:
            print(f"{'':<11}   ({errs} agent runs errored, scored as they are)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cal", type=int, default=12, help="healthy real-model runs for calibration")
    ap.add_argument("--n", type=int, default=8, help="runs per fault")
    ap.add_argument("--llm-kind", default="anthropic", choices=["anthropic", "langgraph"],
                    help="anthropic: our adapter for the Anthropic API; langgraph: the LangGraph agent against the "
                         "OpenAI-compatible endpoint in the env file (OpenRouter / OpenCode)")
    ap.add_argument("--price-in", type=float, default=0.10, help="$ per million input tokens, for the cost line")
    ap.add_argument("--price-out", type=float, default=0.50, help="$ per million output tokens, for the cost line")
    ap.add_argument("--split", default=None, help="evaluation split name (default real); use a new one for fresh runs")
    ap.add_argument("--seed0", type=int, default=None, help="first evaluation seed (default 8000)")
    ap.add_argument("--faults", nargs="+", default=None, choices=FAULTS,
                    help="restrict to these groups (default: none slow_tool slow_db)")
    ap.add_argument("--variants", nargs="+", default=["base", "v2"], choices=["base", "v2", "v3", "v4"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args()
    global EVAL_SPLIT, EVAL_SEED0, FAULTS
    EVAL_SPLIT = a.split or EVAL_SPLIT
    EVAL_SEED0 = a.seed0 if a.seed0 is not None else EVAL_SEED0
    FAULTS = a.faults or FAULTS
    con = store.connect(a.db)
    if not a.no_record:
        if not bpf_available():
            sys.exit("needs passwordless sudo bpftrace: run scripts/setup_sudoers.sh once")
        if a.llm_kind == "anthropic":
            from agent.anthropic_llm import KEY_FILE, TOTAL, load_api_key
            if not load_api_key():
                sys.exit(f"no API key: set ANTHROPIC_API_KEY or put the key in {KEY_FILE} (chmod 600)")
        else:
            from agent.langgraph_agent import TOTAL, load_settings
            key, base_url, model = load_settings()
            if not key:
                sys.exit("no API key: set CROSSTRACE_ENV_FILE or put OPENROUTER_API_KEY / OPENCODE_API_KEY in .env")
            print(f"model endpoint: {base_url}  model: {model}", file=sys.stderr)
        collect(con, a.n, a.cal, a.force, a.llm_kind)
        cost = TOTAL["input_tokens"] * a.price_in / 1e6 + TOTAL["output_tokens"] * a.price_out / 1e6
        print(f"\nAPI use this session: {TOTAL['calls']} calls, {TOTAL['input_tokens']} input + "
              f"{TOTAL['output_tokens']} output tokens, about ${cost:.4f} at ${a.price_in}/${a.price_out} per million")

    vis = kernel_visibility(con)
    print("\nKERNEL VISIBILITY of HTTP spans (share of spans with the evidence attached)")
    print(f"{'':<34}{'spans':>6}{'connect':>9}{'DNS':>7}{'RTT':>7}{'resp. gap':>11}")
    for name, (n, c, d, r, g) in vis.items():
        if n:
            print(f"{name:<34}{n:>6}{100 * c / n:>8.0f}%{100 * d / n:>6.0f}%{100 * r / n:>6.0f}%{100 * g / n:>10.0f}%")

    real = calibrate(con, CAL_SPLIT)
    if real is None:
        sys.exit("no real-model calibration runs recorded yet")
    print("\nstep thresholds from REAL healthy runs (ms): " + ", ".join(f"{k}={v:.0f}" for k, v in sorted(real[0].items())))
    print_scores("THRESHOLDS CALIBRATED ON THE REAL MODEL (the intended use)",
                 evaluate(con, *real, a.variants), a.variants)
    for split in MOCK_CAL_SPLITS:
        mock = calibrate(con, split)
        if mock:
            print("\nstep thresholds from MOCK healthy runs (ms): " +
                  ", ".join(f"{k}={v:.0f}" for k, v in sorted(mock[0].items())))
            print_scores("THRESHOLDS CALIBRATED ON THE MOCK, USED UNCHANGED ON THE REAL MODEL",
                         evaluate(con, *mock, a.variants), a.variants)
            break


if __name__ == "__main__":
    main()
