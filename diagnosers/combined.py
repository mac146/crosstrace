"""Combined diagnoser: agent spans + kernel evidence from the correlator's timeline.

Same critical path and healthy thresholds as the baseline (diagnosers.baseline). The difference is how
a slow step is blamed. Rules, all transparent:

1. Steps are the agent's model and tool calls; excess = duration - healthy threshold (as baseline).
2. chat step:
   - an HTTP attempt returned 429  -> rate_limit (full excess), slow_model (half), as baseline.
   - else, kernel evidence on its HTTP spans:
       server wait above its healthy threshold -> slow_model (the server held the response: confirmed)
       network signal (RTT or retransmits)    -> split like an API step below
       neither                                 -> slow_model (nothing else explains it)
3. db_query / slow_tool steps: blamed on slow_db / slow_tool. The kernel shows no network involvement;
   there is nothing it can add (these steps make no network calls).
4. http_api step, using the kernel evidence attached to its HTTP span:
   dns_delay   gets (DNS lookup time - healthy threshold) if positive;
   the rest of the excess goes to
     net_latency  if the largest smoothed RTT on the connection exceeds its healthy threshold,
     packet_loss  if the connection had any TCP retransmit,
     both: split equally (v2: all to packet_loss, see attribute_http); neither: nothing network-ish explains the rest, so fall back to the
     baseline's equal 3-way split (low confidence).
   If the span has no kernel events at all, fall back to the baseline's equal 3-way split too.
5. Latent loss: a retransmit on any HTTP span's connection adds packet_loss with a tiny score (ranked last)
   even when no step was slow, so loss that TCP absorbed is still reported. If nothing else is anomalous
   this makes packet_loss the answer instead of "none".
6. Scores are summed per cause; equal scores are ordered by a seeded draw (as baseline); no anomaly -> "none".
"""
import random

from correlator.join import _is_http, breakdown
from diagnosers.common import Cause, attr, descendants, dur_ms, operations

NETWORK = ("dns_delay", "net_latency", "packet_loss")
LATENT_LOSS_SCORE = 0.001


def attribute_http(excess, b, kp, loss_explains_rtt=False):
    """Blame a slow HTTP-backed step. b: correlator breakdown of its HTTP span (or None).
    Returns ({cause: ms}, {cause: why}).

    loss_explains_rtt (v2, added AFTER seeing v1's results on seeds 1000-1029): a lost SYN is retransmitted
    after ~1 s, and the kernel's first RTT sample then includes that wait, so "RTT high" and "retransmits"
    appear together when the only fault is packet loss. v1 split the blame equally and broke the tie at
    random; v2 lets retransmits explain the RTT."""
    if b is None or b["n_events"] == 0:
        third = excess / 3
        return ({c: third for c in NETWORK},
                {c: "no kernel evidence: spans cannot split DNS / latency / loss" for c in NETWORK})
    scores, why = {}, {}
    dns = max(0.0, (b["dns_ms"] or 0.0) - kp["dns_ms"])
    if dns > 0:
        scores["dns_delay"] = min(dns, excess)
        why["dns_delay"] = f"DNS lookup took {b['dns_ms']:.0f}ms (healthy threshold {kp['dns_ms']:.0f}ms)"
    rest = max(0.0, excess - dns)
    lat = (b["srtt_max_ms"] or 0.0) > kp["srtt_ms"]
    loss = b["retransmits"] >= 1
    if lat and loss and loss_explains_rtt:
        scores["packet_loss"] = rest
        lat = False
    elif lat and loss:
        scores["net_latency"], scores["packet_loss"] = rest / 2, rest / 2
    elif lat:
        scores["net_latency"] = rest
    elif loss:
        scores["packet_loss"] = rest
    elif dns == 0 and rest > 0:
        third = rest / 3
        return ({c: third for c in NETWORK},
                {c: "kernel saw no DNS/RTT/retransmit anomaly: cannot tell which (low confidence)" for c in NETWORK})
    if lat:
        why["net_latency"] = (f"smoothed RTT reached {b['srtt_max_ms']:.0f}ms "
                              f"(healthy threshold {kp['srtt_ms']:.0f}ms), connect took {b['connect_ms'] or 0:.0f}ms")
    if loss:
        why["packet_loss"] = f"{b['retransmits']} TCP retransmit(s) on the connection"
    return {c: v for c, v in scores.items() if v > 0}, why


def diagnose(timeline, profile, kprofile, tie_seed=0, loss_explains_rtt=False):
    spans = [e.span for e in timeline.entries]
    http_entry = {e.span["span_id"]: e for e in timeline.entries if _is_http(e.span)}
    scores, evidence = {}, {}

    def add(cause, amount, why):
        scores[cause] = scores.get(cause, 0.0) + amount
        evidence.setdefault(cause, []).append(why)

    for key, span in operations(spans):
        excess = dur_ms(span) - profile[key]
        if excess <= 0:
            continue
        step = f"{span['name']} took {dur_ms(span):.0f}ms ({excess:.0f}ms over threshold)"
        https = [http_entry[d["span_id"]] for d in descendants(spans, span) if d["span_id"] in http_entry]
        bds = [breakdown(h) for h in https]
        if key == "chat":
            if any(attr(d, "http.response.status_code") == 429 for d in descendants(spans, span)):
                add("rate_limit", excess, step + ", HTTP 429 seen")
                add("slow_model", 0.5 * excess, step + " (retries can look like a slow model)")
                continue
            waits = [b["server_wait_ms"] for b in bds if b["server_wait_ms"] is not None]
            net = any(b["retransmits"] >= 1 or (b["srtt_max_ms"] or 0) > kprofile["srtt_ms"] for b in bds)
            if waits and max(waits) > kprofile["server_wait_ms"]:
                add("slow_model", excess, step + f"; kernel: server held the response {max(waits):.0f}ms")
            elif net:
                b = max(bds, key=lambda x: x["span_ms"])
                for c, v in attribute_http(excess, b, kprofile, loss_explains_rtt)[0].items():
                    add(c, v, step + "; kernel: network signal on the model connection")
            else:
                add("slow_model", excess, step + "; kernel: network and server wait look healthy")
        elif key == "tool:db_query":
            add("slow_db", excess, step + "; kernel: no network involved")
        elif key == "tool:slow_tool":
            add("slow_tool", excess, step + "; kernel: no network involved")
        elif key == "tool:http_api":
            b = max(bds, key=lambda x: x["span_ms"]) if bds else None
            sc, why = attribute_http(excess, b, kprofile, loss_explains_rtt)
            for c, v in sc.items():
                add(c, v, step + "; " + why[c])
        else:
            raise ValueError(f"unknown step {key}")

    if "packet_loss" not in scores:
        n = sum(breakdown(e)["retransmits"] for e in http_entry.values())
        if n:
            add("packet_loss", LATENT_LOSS_SCORE, f"{n} TCP retransmit(s) seen although no step was slow")

    if not scores:
        return [Cause("none", 1.0, "no step exceeded its healthy threshold and no retransmits were seen")]
    rng = random.Random(tie_seed)
    ranked = sorted(scores, key=lambda c: (-scores[c], rng.random()))
    return [Cause(c, scores[c], "; ".join(evidence[c])) for c in ranked]
