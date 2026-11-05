"""Baseline diagnoser: agent-level spans only. Stands in for what existing tracing tools can do.

Rules (all transparent):
1. Critical path = the agent's steps (model calls and tool calls); the agent is sequential.
2. A step's excess = duration - healthy threshold. Steps with no excess are ignored.
3. Each anomalous step blames the causes that its span type can plausibly show:
   - chat step:  slow_model. If any HTTP attempt inside it returned 429 -> rate_limit gets the
                 full excess and slow_model half of it (a retry loop can also look like a slow model).
   - db_query:   slow_db.
   - slow_tool:  slow_tool.
   - http_api:   dns_delay, net_latency, packet_loss, each given an equal share. App-level spans
                 cannot tell these apart, because DNS, connect and retransmits all happen inside
                 one HTTP span.
4. Scores are summed per cause. Equal scores are ordered by a seeded random draw, so the baseline
   gets no unearned credit for ties (expected top-1 on a 3-way tie is 1/3).
5. If no step is anomalous the answer is "none".
"""
import random

from diagnosers.common import Cause, attr, descendants, dur_ms, operations

NETWORK_CAUSES = ("dns_delay", "net_latency", "packet_loss")


def diagnose(spans, profile, tie_seed=0):
    scores, evidence = {}, {}

    def add(cause, amount, why):
        scores[cause] = scores.get(cause, 0.0) + amount
        evidence.setdefault(cause, []).append(why)

    for key, span in operations(spans):
        excess = dur_ms(span) - profile[key]
        if excess <= 0:
            continue
        why = f"{span['name']} took {dur_ms(span):.0f}ms, {excess:.0f}ms over its healthy threshold"
        if key == "chat":
            got_429 = any(attr(d, "http.response.status_code") == 429 for d in descendants(spans, span))
            if got_429:
                add("rate_limit", excess, why + " and saw HTTP 429")
                add("slow_model", 0.5 * excess, why + " (retries can look like a slow model)")
            else:
                add("slow_model", excess, why)
        elif key == "tool:db_query":
            add("slow_db", excess, why)
        elif key == "tool:slow_tool":
            add("slow_tool", excess, why)
        elif key == "tool:http_api":
            for c in NETWORK_CAUSES:
                add(c, excess / len(NETWORK_CAUSES), why + " (cannot split DNS/connect/loss from spans)")
        else:
            raise ValueError(f"unknown step {key}")

    if not scores:
        return [Cause("none", 1.0, "no step exceeded its healthy threshold")]
    rng = random.Random(tie_seed)
    ranked = sorted(scores, key=lambda c: (-scores[c], rng.random()))
    return [Cause(c, scores[c], "; ".join(evidence[c])) for c in ranked]
