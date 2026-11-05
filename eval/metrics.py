"""Scoring. Definitions (also in docs/decisions.md D10):

top1        first-ranked cause == injected fault ("none" is the right answer for healthy runs)
top3        injected fault is among the first three ranked causes
false_attr  first-ranked cause points at a component that was healthy: its component is not
            "none" and differs from the injected fault's component. Blaming the wrong cause
            inside the right component (e.g. net_latency for a dns_delay) is a miss, not a
            false attribution. For healthy runs any non-"none" answer is a false attribution.
"""
import math

from diagnosers.common import COMPONENT


def score_run(ranked, truth):
    """ranked: list of cause names, best first. truth: injected fault name."""
    if not ranked:
        raise ValueError("empty ranking")
    pred_comp = COMPONENT[ranked[0]]
    return {
        "top1": ranked[0] == truth,
        "top3": truth in ranked[:3],
        "false_attr": pred_comp != "none" and pred_comp != COMPONENT[truth],
    }


def wilson(k, n, z=1.96):
    """95% Wilson score interval for a proportion k/n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def aggregate(scores):
    """scores: list of score_run dicts for one fault type."""
    n = len(scores)
    out = {"n": n}
    for k in ("top1", "top3", "false_attr"):
        hits = sum(s[k] for s in scores)
        out[k] = hits / n if n else float("nan")
        out[k + "_ci"] = wilson(hits, n)
    return out
