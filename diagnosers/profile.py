"""Healthy-run profile: how long each kind of step normally takes.

Both diagnosers use the same profile, built from calibration runs (fault_type "none", seeds
disjoint from the evaluation seeds). A step is "anomalous" if it exceeds its threshold.

Threshold rule, fixed before any results were looked at:
    threshold = max(p99, 3 * median) + MARGIN_MS
"""
import statistics

from diagnosers.common import dur_ms, operations, root_span

MARGIN_MS = 50.0


def _p99(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.99 * len(xs) + 0.999999) - 1)]  # nearest rank


def build_profile(runs_spans):
    """runs_spans: list of span lists, one per healthy run -> {key: threshold_ms}."""
    samples = {}
    for spans in runs_spans:
        for key, s in operations(spans):
            samples.setdefault(key, []).append(dur_ms(s))
        samples.setdefault("run", []).append(dur_ms(root_span(spans)))
    if not samples:
        raise ValueError("empty calibration set")
    return {k: max(_p99(v), 3 * statistics.median(v)) + MARGIN_MS for k, v in samples.items()}


def _threshold(values):
    return max(_p99(values), 3 * statistics.median(values)) + MARGIN_MS


def build_kernel_profile(breakdowns):
    """Healthy thresholds for kernel-derived signals, same rule as the step thresholds.

    breakdowns: correlator.join.breakdown() dicts of HTTP spans from healthy runs.
    Keys: dns_ms (hostname lookups only), srtt_ms (largest smoothed RTT), server_wait_ms.
    """
    dns = [b["dns_ms"] for b in breakdowns if b["dns_ms"] is not None]
    srtt = [b["srtt_max_ms"] for b in breakdowns if b["srtt_max_ms"] is not None]
    wait = [b["server_wait_ms"] for b in breakdowns if b["server_wait_ms"] is not None]
    if not (dns and srtt and wait):
        raise ValueError("calibration runs carry no kernel evidence")
    return {"dns_ms": _threshold(dns), "srtt_ms": _threshold(srtt), "server_wait_ms": _threshold(wait)}
