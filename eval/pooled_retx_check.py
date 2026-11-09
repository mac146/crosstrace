"""Stage 16 follow-up (post hoc): does the pooled arm's kind-N accuracy recover if pooled retransmits are removed?

    python -m eval.pooled_retx_check          # needs the recorded runs of eval.obi_conc (~/.crosstrace-data/obi_conc.json)
"""
import collections, json
from diagnosers import combined
from diagnosers.profile import build_profile, kernel_profile_from_timelines
from eval.selective_conc import views

import os
d = json.load(open(os.path.expanduser("~/.crosstrace-data/obi_conc.json")))
runs = {k: v for k, v in d.items() if not k.startswith("_")}
orig = combined.pooled


def no_retx(bds):
    p = orig(bds)
    p["retransmits"] = 0
    return p


for c in (9, 24):
    cal = [r for r in runs.values() if r["c"] == c and r["role"] == "cal"]
    cs, cv = [], []
    for r in cal:
        cs += r["agents"]
        cv += views(r)[2]
    profile, kp = build_profile(cs, "coarse"), kernel_profile_from_timelines(cv, "coarse")
    for label, fn in (("as run", orig), ("pooled retransmits removed", no_retx)):
        combined.pooled = fn
        ok = tot = 0
        first = collections.Counter()
        for rid, r in runs.items():
            if r["c"] != c or r["role"] != "mixed":
                continue
            tl, all_http, per_agent = views(r)
            for n, (view, kind) in enumerate(zip(per_agent, r["kinds"])):
                if kind != "N":
                    continue
                ranked = [x.cause for x in combined.diagnose(view, profile, kp, tie_seed=n, loss_explains_rtt=True, slow_api=True,
                                                            source="pooled", pool_entries=all_http)]
                ok += ranked[0] == "net_latency"
                tot += 1
                first[ranked[0]] += 1
        print(f"C={c} kind N, pooled arm, {label}: {ok}/{tot} correct {dict(first)}")
    combined.pooled = orig
