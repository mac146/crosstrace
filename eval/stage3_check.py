"""Stage 3 exit check: join agent spans with kernel events and show the unified timeline.

    python -m eval.stage3_check --k 5            # K runs per fault, prints one timeline per fault
    python -m eval.stage3_check --show kernel-slow_db-5000 --verbose

REQUIRES passwordless sudo bpftrace and tc (scripts/setup_sudoers.sh) when it has to record runs.
Prints (1) the unified timeline of the first run of each fault, (2) join integrity over all runs.
"""
import argparse
import sys

from collectors import store
from collectors.ebpf import available as bpf_available
from correlator.integrity import check
from correlator.load import load_timeline
from correlator.render import render
from faultlab.faults import FAULT_TYPES
from faultlab.netem import available as tc_available
from faultlab.runner import run_one

SEED0 = 5_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--db", default=None)
    ap.add_argument("--faults", nargs="+", default=FAULT_TYPES, choices=FAULT_TYPES)
    ap.add_argument("--show", help="print the timeline of one stored run id and exit")
    ap.add_argument("--verbose", action="store_true", help="include every rtt sample")
    a = ap.parse_args()
    con = store.connect(a.db)

    if a.show:
        tl, env = load_timeline(con, a.show)
        print(render(tl, a.verbose))
        return

    if not bpf_available() or not tc_available():
        sys.exit("needs passwordless sudo bpftrace and tc: run scripts/setup_sudoers.sh once")
    total = bad = 0
    by_match, by_reason = {}, {}
    stepped = stepped_bad = 0
    shown = []
    for fault in a.faults:
        for i in range(a.k):
            rid = f"kernel-{fault}-{SEED0 + i}"
            if not store.run_exists(con, rid) or store.load_env(con, rid) is None:
                run_one(fault, SEED0 + i, con, split="kernel", kernel=True)
            tl, env = load_timeline(con, rid)
            problems = check(tl, env)
            run = next(r for r in store.list_runs(con, "kernel", fault) if r["run_id"] == rid)
            if run["status"] != "ok":      # a crashed run has few spans and would pass the join checks vacuously
                problems.append(f"agent run did not complete: {run['error']}")
            n_http = sum(1 for en in tl.entries if en.span["attrs"].get("server.port") is not None)
            if n_http < 5:
                problems.append(f"only {n_http} HTTP spans (a complete run has at least 5)")
            total += 1
            for en in tl.entries:
                for at in en.kernel:
                    by_match[at.match] = by_match.get(at.match, 0) + 1
            for ev_, why in tl.orphans:
                by_reason[why] = by_reason.get(why, 0) + 1
            bad += bool(problems)
            if tl.clock_max_jump_ns > 1_000_000:
                stepped += 1
                stepped_bad += bool(problems)
            if problems:
                print(f"JOIN PROBLEMS in {rid}: {problems}", file=sys.stderr)
            if i == 0:
                shown.append((fault, rid, tl))
    for fault, rid, tl in shown:
        print(f"\n===== {rid}  (injected: {fault}) =====")
        print(render(tl, verbose=False))
    print(f"\njoin integrity: {total - bad}/{total} runs passed all checks")
    print(f"runs where the wall clock stepped >1 ms during the run: {stepped} "
          f"(of which failed the checks: {stepped_bad})")
    print(f"attached kernel events by evidence: {by_match}")
    print(f"unattached kernel events: {by_reason}")


if __name__ == "__main__":
    main()
