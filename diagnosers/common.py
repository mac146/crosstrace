"""Shared vocabulary and helpers for the diagnosers.

CAUSES are the fault names, because the success metric is "first-ranked cause equals the
injected fault". COMPONENT maps each cause to the part of the system it lives in; it is used
only for the false-attribution metric (blaming a component that was healthy).
"""
from dataclasses import dataclass

CAUSES = ["dns_delay", "net_latency", "packet_loss", "slow_tool", "slow_db",
          "slow_model", "rate_limit"]
COMPONENT = {
    "none": "none",
    "dns_delay": "api", "net_latency": "api", "packet_loss": "api",   # network path to the API
    "slow_tool": "tool",
    "slow_db": "db",
    "slow_model": "model", "rate_limit": "model",
}


@dataclass(frozen=True)
class Cause:
    cause: str       # one of CAUSES, or "none"
    score: float     # attributed delay in ms (or 1.0 for "none")
    evidence: str    # human-readable support


def attr(span, key, default=None):
    return span["attrs"].get(key, default)


def dur_ms(span):
    return (span["end_ns"] - span["start_ns"]) / 1e6


def root_span(spans):
    roots = [s for s in spans if attr(s, "gen_ai.operation.name") == "invoke_agent"]
    if len(roots) != 1:
        raise ValueError(f"expected exactly one invoke_agent span, found {len(roots)}")
    return roots[0]


def descendants(spans, span):
    children = {}
    for s in spans:
        children.setdefault(s["parent_id"], []).append(s)
    out, stack = [], [span["span_id"]]
    while stack:
        for c in children.get(stack.pop(), []):
            out.append(c)
            stack.append(c["span_id"])
    return out


def operations(spans):
    """The agent's steps: direct children of invoke_agent (model calls and tool calls), as
    (key, span) in time order. These are the units on the critical path: the agent is
    sequential, so every step is on it."""
    root = root_span(spans)
    ops = []
    for s in sorted(spans, key=lambda s: s["start_ns"]):
        if s["parent_id"] != root["span_id"]:
            continue
        op = attr(s, "gen_ai.operation.name")
        if op == "chat":
            ops.append(("chat", s))
        elif op == "execute_tool":
            ops.append((f"tool:{attr(s, 'gen_ai.tool.name')}", s))
    return ops
