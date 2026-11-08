"""Which per-run evidence separates real packet loss from a network's own background retransmits?

    python -m eval.loss_features --design              # choose the feature on the already-used (design) runs
    python -m eval.loss_features --fresh rf_d          # judge the chosen feature on fresh runs

Candidate features, all from kernel events already recorded, computed over a run's model-call connections (port 443):
  F1  total retransmits                                  (the current rule in v4/v5)
  F2  most retransmits on a single connection
  F3  pairs of retransmits less than 1 s apart on one connection (RTO back-off bursts)
  F4  retransmits of an established connection (state 1), as opposed to handshake retransmits
  F5  retransmits per data send (a rate: the run's amount of traffic is the denominator)
Threshold for every feature = the largest value seen in HEALTHY design runs (so design false alarms are 0 by construction);
detection = share of loss runs above it. The feature chosen on the design set is then judged, with its frozen threshold, on a fresh set.
"""
import argparse
import sys
from collections import defaultdict

from collectors import store
from correlator.join import _is_http
from correlator.load import load_timeline

HEALTHY_DESIGN = [("realcal2", "none"), ("realcal3", "none"), ("rf_a", "none"), ("rf_b", "none")]
LOSS_DESIGN = [("rf_a", "packet_loss"), ("rf_b", "packet_loss")]       # pure loss only; combined runs are excluded
FEATURES = ["F1 total", "F2 max on one connection", "F3 bursts <1 s", "F4 established-state", "F5 per data send"]


def features(con, run_id):
    tl, _ = load_timeline(con, run_id)
    by_conn = defaultdict(list)                       # connection (sorted port pair) -> retransmit times (ns)
    established = sends = 0
    for e in tl.entries:
        if not _is_http(e.span) or e.span["attrs"]["server.port"] != 443:
            continue
        for a in e.kernel:
            ev = a.event
            if ev["ev"] == "tcp_retransmit":
                by_conn[(min(ev["sport"], ev["dport"]), max(ev["sport"], ev["dport"]))].append(ev["wall_ns"])
                established += ev.get("state") == 1
            elif ev["ev"] == "tcp_send" and ev.get("dport") == 443:
                sends += 1
    total = sum(len(v) for v in by_conn.values())
    bursts = sum(1 for v in by_conn.values() for a, b in zip(sorted(v), sorted(v)[1:]) if b - a < 1_000_000_000)
    return [total, max((len(v) for v in by_conn.values()), default=0), bursts, established,
            round(total / sends, 3) if sends else 0.0]


def load(con, groups):
    rows = []
    for split, fault in groups:
        for r in store.list_runs(con, split, fault):
            try:
                rows.append((r["run_id"], features(con, r["run_id"])))
            except ValueError:
                pass                                   # no kernel data
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", action="store_true")
    ap.add_argument("--fresh", default=None, help="split holding fresh healthy ('none') and 'packet_loss' runs")
    ap.add_argument("--feature", type=int, default=None, help="index (1-5) of the feature chosen on the design set")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    con = store.connect(a.db)
    healthy, loss = load(con, HEALTHY_DESIGN), load(con, LOSS_DESIGN)
    thresholds = [max(f[i] for _, f in healthy) for i in range(len(FEATURES))]
    if a.design:
        print(f"design set: {len(healthy)} healthy runs, {len(loss)} packet_loss runs\n")
        print(f"{'feature':<28}{'healthy max (threshold)':>24}{'loss runs above it':>22}{'loss median':>13}{'healthy median':>16}")
        for i, name in enumerate(FEATURES):
            above = sum(f[i] > thresholds[i] for _, f in loss)
            med = lambda rows: sorted(f[i] for _, f in rows)[len(rows) // 2]
            print(f"{name:<28}{thresholds[i]:>24}{f'{above}/{len(loss)} ({100 * above / len(loss):.0f}%)':>22}"
                  f"{med(loss):>13}{med(healthy):>16}")
    if a.fresh:
        i = (a.feature or 1) - 1
        fh, fl = load(con, [(a.fresh, "none")]), load(con, [(a.fresh, "packet_loss")])
        fa = sum(f[i] > thresholds[i] for _, f in fh)
        da = sum(f[i] > thresholds[i] for _, f in fl)
        print(f"FRESH set {a.fresh}: feature {FEATURES[i]} with frozen threshold {thresholds[i]}")
        print(f"  packet_loss runs detected: {da}/{len(fl)}   healthy false alarms: {fa}/{len(fh)}")
        print(f"  loss values   : {sorted(f[i] for _, f in fl)}")
        print(f"  healthy values: {sorted(f[i] for _, f in fh)}")


if __name__ == "__main__":
    main()
