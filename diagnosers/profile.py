"""Healthy-run profile: how long each kind of step normally takes.

Both diagnosers use the same profile, built from calibration runs (fault_type "none", seeds
disjoint from the evaluation seeds). A step is "anomalous" if it exceeds its threshold.

Threshold rules (both fixed before any results were looked at):
  coarse (default, Stages 1-5):  threshold = max(p99, 3 * median) + 50 ms
        Robust against jitter, blind to anything under ~50 ms. The 3 * median term makes a step that normally
        takes 50 ms (slow_tool) need +100 ms before it counts.
  tight (Stage 6, subtle faults): threshold = p99 + 5 ms
        Sensitive to ~10 ms effects; pays for it in false alarms on jittery runs. Which side of that trade a user
        wants is a choice, so the evaluation reports both.
"""
import statistics

from correlator.join import _is_http, breakdown

from diagnosers.common import dur_ms, operations, root_span

MARGIN_MS = 50.0  # kept for callers/tests that import it: the coarse rule's margin


def _p99(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.99 * len(xs) + 0.999999) - 1)]  # nearest rank


RULES = {
    "coarse": lambda v: max(_p99(v), 3 * statistics.median(v)) + MARGIN_MS,
    "tight": lambda v: _p99(v) + 5.0,
}


def _threshold(values, rule="coarse"):
    return RULES[rule](values)


def build_profile(runs_spans, rule="coarse"):
    """runs_spans: list of span lists, one per healthy run -> {key: threshold_ms}."""
    samples = {}
    for spans in runs_spans:
        for key, s in operations(spans):
            samples.setdefault(key, []).append(dur_ms(s))
        samples.setdefault("run", []).append(dur_ms(root_span(spans)))
    if not samples:
        raise ValueError("empty calibration set")
    return {k: _threshold(v, rule) for k, v in samples.items()}


def build_kernel_profile(breakdowns, rule="coarse", run_retx=None):
    """Healthy thresholds for kernel-derived signals, same rule as the step thresholds.

    breakdowns: correlator.join.breakdown() dicts of HTTP spans from healthy runs.
    Keys: dns_ms (hostname lookups only), srtt_ms (largest smoothed RTT), server_wait_ms.
    run_retx: total TCP retransmits of each healthy run (v4's latent-loss baseline). Adds "retx_per_run" = the largest
    count that healthy runs show (nearest-rank p99); a healthy loopback gives 0, a healthy real network does not.
    """
    dns = [b["dns_ms"] for b in breakdowns if b["dns_ms"] is not None]
    srtt = [b["srtt_max_ms"] for b in breakdowns if b["srtt_max_ms"] is not None]
    wait = [b["server_wait_ms"] for b in breakdowns if b["server_wait_ms"] is not None]
    if not (dns and srtt and wait):
        raise ValueError("calibration runs carry no kernel evidence")
    out = {"dns_ms": _threshold(dns, rule), "srtt_ms": _threshold(srtt, rule),
           "server_wait_ms": _threshold(wait, rule)}
    if run_retx:
        out["retx_per_run"] = _p99(run_retx)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# v5 (Stage 10): thresholds per span class and run-level robust statistics. Designed after the real-path run
# (results-stage8.md): a remote model call and the local API tool have very different healthy lookup/connect/RTT, and on a
# real network single samples have heavy tails while a real fault persists across a run's calls.

MIN_CALLS = 3          # a run-level median needs at least this many calls of a class
MIN_HEALTHY_RUNS = 5   # fewer healthy runs than this: no run-level threshold is built


def http_by_class(timeline):
    """HTTP spans of a joined run, split by what asked for them: 'model' (child of a chat step) or 'tool'."""
    by_id = {e.span["span_id"]: e for e in timeline.entries}
    out = {"model": [], "tool": []}
    for e in timeline.entries:
        if _is_http(e.span):
            parent = by_id.get(e.span["parent_id"])
            op = parent.span["attrs"].get("gen_ai.operation.name") if parent else None
            out["model" if op == "chat" else "tool"].append(e)
    return out


def run_summary(entries):
    """Run-level evidence of one class: the lookup (one per fresh connection, so the max), and the MEDIAN over the run's calls
    of each call's largest smoothed RTT (a persistent delay lifts every call; sporadic spikes lift one). It is the LOWER
    median, so with an even number of calls an unaffected half keeps it low: "persistent" means MORE than half the calls."""
    bds = [breakdown(e) for e in entries]
    dns = [b["dns_ms"] for b in bds if b["dns_ms"] is not None]
    srtt = [b["srtt_max_ms"] for b in bds if b["srtt_max_ms"] is not None]
    return {"n": len(bds), "dns_ms": max(dns) if dns else None,
            "srtt_med_ms": statistics.median_low(srtt) if len(srtt) >= MIN_CALLS else None,
            "retx": sum(b["retransmits"] for b in bds)}


def kernel_profile_from_timelines(timelines, rule="coarse"):
    """The kernel thresholds of v1-v4 (pooled over every HTTP span, exactly as before) plus, under "classes", run-level
    thresholds for model calls: {"classes": {"model": {"dns_ms": .., "srtt_med_ms": ..}}}."""
    bds, run_retx, dns, srtt_med = [], [], [], []
    for tl in timelines:
        classes = http_by_class(tl)
        these = [breakdown(e) for es in classes.values() for e in es]
        bds += these
        run_retx.append(sum(b["retransmits"] for b in these))
        s = run_summary(classes["model"])
        if s["dns_ms"] is not None:
            dns.append(s["dns_ms"])
        if s["srtt_med_ms"] is not None:
            srtt_med.append(s["srtt_med_ms"])
    out = build_kernel_profile(bds, rule, run_retx)
    model = {}
    if len(dns) >= MIN_HEALTHY_RUNS:
        model["dns_ms"] = _threshold(dns, rule)
    if len(srtt_med) >= MIN_HEALTHY_RUNS:
        model["srtt_med_ms"] = _threshold(srtt_med, rule)
    out["classes"] = {"model": model}
    return out
