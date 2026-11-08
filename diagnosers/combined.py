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
     (v3 only) server wait above its healthy threshold while all of the above are healthy: slow_api;
     both latency and loss: split equally (v2: all to packet_loss, see attribute_http);
     none of these: nothing explains the rest, so fall back to the baseline's equal split (3-way, or 4-way
     with slow_api in v3) with low confidence.
   If the span has no kernel events at all, fall back to the baseline's equal split too.
5. Latent loss: a retransmit on any HTTP span's connection adds packet_loss with a tiny score (ranked last)
   even when no step was slow, so loss that TCP absorbed is still reported. If nothing else is anomalous
   this makes packet_loss the answer instead of "none".
6. Scores are summed per cause; equal scores are ordered by a seeded draw (as baseline); no anomaly -> "none".
"""
import random

from correlator.join import _is_http, breakdown
from diagnosers.common import Cause, attr, descendants, dur_ms, operations
from diagnosers.profile import http_by_class, run_summary

NETWORK = ("dns_delay", "net_latency", "packet_loss")
LATENT_LOSS_SCORE = 0.001
API_CAUSES = NETWORK + ("slow_api",)


def attribute_http(excess, b, kp, loss_explains_rtt=False, slow_api=False):
    """Blame a slow HTTP-backed step. b: correlator breakdown of its HTTP span (or None).
    Returns ({cause: ms}, {cause: why}).

    loss_explains_rtt (v2, added AFTER seeing v1's results on seeds 1000-1029): a lost SYN is retransmitted
    after ~1 s, and the kernel's first RTT sample then includes that wait, so "RTT high" and "retransmits"
    appear together when the only fault is packet loss. v1 split the blame equally and broke the tie at
    random; v2 lets retransmits explain the RTT.

    slow_api (v3, added AFTER the slowapi suite on seeds 3000-3029 showed v2 cannot name a slow server and blames
    the healthy network instead): when DNS, RTT and retransmits are all healthy but the server held the response
    longer than its healthy wait, the remaining excess goes to slow_api. Not used for model calls."""
    causes = API_CAUSES if slow_api else NETWORK
    if b is None or b["n_events"] == 0:
        share = excess / len(causes)
        return ({c: share for c in causes},
                {c: "no kernel evidence: spans cannot split DNS / latency / loss / server" for c in causes})
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
    elif slow_api and rest > 0 and (b["server_wait_ms"] or 0.0) > kp["server_wait_ms"]:
        scores["slow_api"] = rest
        why["slow_api"] = (f"server held the response {b['server_wait_ms']:.0f}ms (healthy threshold "
                           f"{kp['server_wait_ms']:.0f}ms) while RTT and retransmits were healthy")
    elif dns == 0 and rest > 0:
        share = rest / len(causes)
        return ({c: share for c in causes},
                {c: "kernel saw no DNS/RTT/retransmit anomaly: cannot tell which (low confidence)" for c in causes})
    if lat:
        why["net_latency"] = (f"smoothed RTT reached {b['srtt_max_ms']:.0f}ms "
                              f"(healthy threshold {kp['srtt_ms']:.0f}ms), connect took {b['connect_ms'] or 0:.0f}ms")
    if loss:
        why["packet_loss"] = f"{b['retransmits']} TCP retransmit(s) on the connection"
    return {c: v for c, v in scores.items() if v > 0}, why


def pooled(bds_by_span):
    """Host-metrics evidence (Stage 13 comparison arm): the run-wide numbers a metrics stack would have, given to EVERY HTTP step.
    Maximum DNS lookup, maximum smoothed RTT, total retransmits, maximum connect time; the server wait is dropped (a host counter
    has no per-request wait), so slow_api cannot be named. Built only from events the correlator attached to the run's spans, so
    it never sees foreign traffic (favours this arm)."""
    allb = list(bds_by_span.values())

    def mx(k):
        xs = [b[k] for b in allb if b[k] is not None]
        return max(xs) if xs else None
    return {"n_events": sum(b["n_events"] for b in allb), "dns_ms": mx("dns_ms"), "connect_ms": mx("connect_ms"),
            "retransmits": sum(b["retransmits"] for b in allb), "srtt_max_ms": mx("srtt_max_ms"),
            "server_wait_ms": None, "_wait_if_generous": mx("server_wait_ms")}


def diagnose(timeline, profile, kprofile, tie_seed=0, loss_explains_rtt=False, slow_api=False,
             latent_baseline=False, run_level=False, source="joined", pool_entries=None, obi_waits=None, obi_srtts=None):
    """source (Stage 14 adds "pooled_port" and "obi"): "joined" (default, each step's own kernel events), "pooled" (run-wide host-metric numbers for every step, no
    server wait) or "pooled_wait" (as pooled plus the run's maximum server wait: an upper bound on a metrics stack).

    run_level (v5, added AFTER the real-network faults run, results-stage8.md): judge the model connection by run-level
    evidence against per-class healthy thresholds, even when no step crossed its (noisy) duration threshold:
      * the run's DNS lookup above its healthy threshold            -> dns_delay (score = lookup ms)
      * the MEDIAN over the run's model calls of the smoothed RTT above its healthy threshold -> net_latency
        (score = median x calls); a persistent delay lifts every call, a sporadic spike lifts one and is ignored.
    A slow model call these findings explain is not also blamed on slow_model. Packet loss keeps the v4 rule.
    latent_baseline (v4, added AFTER the real-model run showed 5 of 8 healthy runs with 1-4 retransmits): the latent
    loss rule fires only when the run's retransmits exceed kprofile["retx_per_run"], the most that healthy runs on the
    same network show. On a healthy loopback that is 0, so behaviour there is unchanged."""
    spans = [e.span for e in timeline.entries]
    http_entry = {e.span["span_id"]: e for e in timeline.entries if _is_http(e.span)}
    scores, evidence = {}, {}

    def add(cause, amount, why):
        scores[cause] = scores.get(cause, 0.0) + amount
        evidence.setdefault(cause, []).append(why)

    if source not in ("joined", "pooled", "pooled_wait", "pooled_port", "obi", "obi_real", "obi_real_net", "obi_real_flows"):
        raise ValueError(source)
    # pool_entries (Stage 14, concurrency): the HTTP entries of EVERY agent on the host. The pooled sources then pool over all of them,
    # as shared host counters would; the diagnosed agent's own steps still come from `timeline`. Default: this timeline's own HTTP spans.
    pe = {e.span["span_id"]: e for e in pool_entries} if pool_entries is not None else dict(http_entry)
    override = None          # span_id -> the evidence this arm is allowed to see for that HTTP span
    if source != "joined":
        own = {sid: breakdown(e) for sid, e in pe.items()}
        if source in ("pooled", "pooled_wait"):
            pool = pooled(own)
            if source == "pooled_wait":
                pool["server_wait_ms"] = pool["_wait_if_generous"]
            override = {sid: pool for sid in own}
        elif source == "pooled_port":
            # per-destination metrics: the same counters and the server wait, pooled over the spans that went to one server port
            groups = {}
            for sid, e in pe.items():
                groups.setdefault(e.span["attrs"].get("server.port"), {})[sid] = own[sid]
            override = {}
            for g in groups.values():
                p = pooled(g)
                p["server_wait_ms"] = p["_wait_if_generous"]
                override.update({sid: p for sid in g})
        elif source in ("obi_real", "obi_real_net", "obi_real_flows"):
            # real OBI output: its per-request server duration (None when OBI reported nothing for that request) and, for the +net / +flows
            # arms, an RTT per span supplied by the caller (run-wide for +net, per connection for +flows); nothing else
            rtts = obi_srtts if source != "obi_real" else None
            override = {sid: {**b, "dns_ms": None, "connect_ms": None, "retransmits": 0,
                              "srtt_max_ms": (rtts or {}).get(sid), "server_wait_ms": (obi_waits or {}).get(sid)}
                        for sid, b in own.items()}
        else:
            # obi: an auto-instrumentation agent's information content: a per-request server duration, nothing about DNS/RTT/loss
            override = {sid: {**b, "dns_ms": None, "connect_ms": None, "retransmits": 0, "srtt_max_ms": None}
                        for sid, b in own.items()}
    run_findings = False
    if run_level:
        thr = kprofile.get("classes", {}).get("model", {})
        s = run_summary(http_by_class(timeline)["model"])
        if s["dns_ms"] is not None and "dns_ms" in thr and s["dns_ms"] > thr["dns_ms"]:
            add("dns_delay", s["dns_ms"], f"DNS lookup on the model connection took {s['dns_ms']:.0f}ms "
                f"(healthy threshold {thr['dns_ms']:.0f}ms)")
            run_findings = True
        if s["srtt_med_ms"] is not None and "srtt_med_ms" in thr and s["srtt_med_ms"] > thr["srtt_med_ms"]:
            add("net_latency", s["srtt_med_ms"] * s["n"], f"median smoothed RTT over {s['n']} model calls was "
                f"{s['srtt_med_ms']:.0f}ms (healthy threshold {thr['srtt_med_ms']:.0f}ms): the delay is persistent")
            run_findings = True

    for key, span in operations(spans):
        excess = dur_ms(span) - profile[key]
        if excess <= 0:
            continue
        step = f"{span['name']} took {dur_ms(span):.0f}ms ({excess:.0f}ms over threshold)"
        https = [http_entry[d["span_id"]] for d in descendants(spans, span) if d["span_id"] in http_entry]
        bds = [({**override[h.span["span_id"]], "span_ms": breakdown(h)["span_ms"]} if override else breakdown(h))
               for h in https]
        if key == "chat":
            if any(attr(d, "http.response.status_code") == 429 for d in descendants(spans, span)):
                add("rate_limit", excess, step + ", HTTP 429 seen")
                add("slow_model", 0.5 * excess, step + " (retries can look like a slow model)")
                continue
            if run_findings:
                continue   # v5: the run-level network evidence already explains this slow model call
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
            sc, why = attribute_http(excess, b, kprofile, loss_explains_rtt, slow_api)
            for c, v in sc.items():
                add(c, v, step + "; " + why[c])
        else:
            raise ValueError(f"unknown step {key}")

    if "packet_loss" not in scores:
        n = sum(breakdown(e)["retransmits"] for e in http_entry.values())
        if n > (kprofile.get("retx_per_run", 0) if latent_baseline else 0):
            add("packet_loss", LATENT_LOSS_SCORE, f"{n} TCP retransmit(s) seen although no step was slow")

    if not scores:
        return [Cause("none", 1.0, "no step exceeded its healthy threshold and no retransmits were seen")]
    rng = random.Random(tie_seed)
    ranked = sorted(scores, key=lambda c: (-scores[c], rng.random()))
    return [Cause(c, scores[c], "; ".join(evidence[c])) for c in ranked]
