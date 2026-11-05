"""Stage 2 exit check: for every fault type, do the raw kernel events show the injected fault?

    python -m eval.stage2_check --k 5

REQUIRES passwordless sudo for bpftrace and tc (scripts/setup_sudoers.sh). Runs K trials per fault
with the eBPF collector on (split "kernel", separate from the evaluation runs), then prints
(1) median kernel signals per fault, and (2) injected parameter vs what the kernel measured.
"""
import argparse
import statistics
import sys

from collectors import store
from collectors.ebpf import available as bpf_available
from collectors.kernel_summary import summarize
from faultlab.faults import FAULT_TYPES
from faultlab.netem import available as tc_available
from faultlab.runner import run_one

SEED0 = 5_000
COLS = ["dns_ms", "api_connect_ms", "api_retx", "llm_retx", "api_srtt_ms", "api_resp_ms",
        "llm_resp_ms", "llm_connects", "max_gap_ms"]


def fmt(x):
    return "   -" if x is None else f"{x:8.1f}"


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def injected_vs_measured(fault, params, s):
    """(label, injected, measured) or None when the kernel is not expected to measure it directly."""
    if fault == "dns_delay":
        return "dns_ms", params["delay_ms"], s["dns_ms"]
    if fault == "net_latency":      # SYN out + SYN-ACK back, each delayed by the one-way delay
        return "api_connect_ms (2x one-way)", 2 * params["one_way_delay_ms"], s["api_connect_ms"]
    if fault == "slow_model":
        return "llm_resp_ms", params["delay_ms_per_call"], s["llm_resp_ms"]
    if fault == "rate_limit":
        return "extra LLM connects", params["first_n_429"], s["llm_connects"] - 4
    if fault == "packet_loss":
        return "api_retx (>0 expected)", None, s["api_retx"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5, help="trials per fault")
    ap.add_argument("--db", default=None)
    ap.add_argument("--faults", nargs="+", default=FAULT_TYPES, choices=FAULT_TYPES)
    a = ap.parse_args()
    if not bpf_available() or not tc_available():
        sys.exit("needs passwordless sudo bpftrace and tc: run scripts/setup_sudoers.sh once")
    con = store.connect(a.db)

    rows, pairs = {}, {}
    for fault in a.faults:
        sums = []
        for i in range(a.k):
            rid = run_one(fault, SEED0 + i, con, split="kernel", kernel=True)
            env = store.load_env(con, rid)
            s = summarize(store.load_kernel_events(con, rid), env)
            truth = store.load_truth(con, rid)
            sums.append(s)
            m = injected_vs_measured(fault, truth["params"], s)
            if m:
                pairs.setdefault(fault, []).append(m)
            if env["warnings"]:
                print(f"  warning in {rid}: {env['warnings']}", file=sys.stderr)
        rows[fault] = sums
        print(f"  {fault}: {a.k} runs done", file=sys.stderr, flush=True)

    print(f"\nMEDIAN KERNEL SIGNALS over {a.k} runs per fault (ms, or counts for *_retx / llm_connects)")
    print(f"{'fault':<12}" + "".join(f"{c:>16}" for c in COLS))
    for fault, sums in rows.items():
        print(f"{fault:<12}" + "".join(f"{fmt(med([s[c] for s in sums])):>16}" for c in COLS))

    print("\nINJECTED vs MEASURED BY THE KERNEL (per run)")
    for fault, ps in pairs.items():
        print(f"  {fault:<12} {ps[0][0]}")
        for _, inj, meas in ps:
            print(f"      injected {'-' if inj is None else inj:>5}   measured {fmt(meas)}")


if __name__ == "__main__":
    main()
