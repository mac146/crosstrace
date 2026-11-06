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


def score_multi(ranked, truths):
    """Score a run with several injected faults.

    ranked: cause names, best first. truths: the injected fault names (2 for a pair).
    top1_in_truth   first-ranked cause is one of the injected faults
    all_in_topk     every injected fault is among the first len(truths) ranked causes (both found, "k = 2")
    each_in_topk    share of injected faults found in the first len(truths) ranked causes
    false_attr      first-ranked cause's component is none of the injected faults' components (nor "none")
    """
    if not ranked:
        raise ValueError("empty ranking")
    k = len(truths)
    top = ranked[:k]
    comps = {COMPONENT[t] for t in truths}
    pred_comp = COMPONENT[ranked[0]]
    return {
        "top1_in_truth": ranked[0] in truths,
        "all_in_topk": all(t in top for t in truths),
        "each_in_topk": sum(t in top for t in truths) / k,
        "false_attr": pred_comp != "none" and pred_comp not in comps,
    }


def aggregate_multi(scores):
    n = len(scores)
    out = {"n": n}
    for key in ("top1_in_truth", "all_in_topk", "false_attr"):
        hits = sum(s[key] for s in scores)
        out[key] = hits / n if n else float("nan")
        out[key + "_ci"] = wilson(hits, n)
    out["each_in_topk"] = sum(s["each_in_topk"] for s in scores) / n if n else float("nan")
    return out
