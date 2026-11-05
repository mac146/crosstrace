"""Reduce one run's raw kernel events to a handful of named signals.

Pure function over the collector's event dicts, so it is unit-testable with synthetic events.
This is a Stage 2 inspection aid (does the kernel see the injected fault?); the Stage 4 combined
diagnoser will build its own evidence from the correlated timeline.

Signals (ms unless noted; None = not observed):
  dns_ms           longest DNS query -> response time
  api_connect_ms   longest TCP connect (SYN sent -> handshake done) to the API port
  llm_connect_ms   same, to the LLM port
  api_retx/llm_retx   TCP retransmits on flows to/from that port (either side)
  api_srtt_ms      largest smoothed RTT sample on API flows
  api_resp_ms      per API flow: last request byte received by the server -> first response byte received
                   by the client; the longest over flows. Includes one network delay for the response.
  llm_resp_ms      same for LLM flows (longest over the 4+ calls)
  llm_connects     number of TCP connects to the LLM port (one per HTTP attempt: 4 normally, +1 per retry)
  max_gap_ms       longest stretch inside the run with no kernel event at all on the tracked flows
"""


def _flows_resp_ms(events, port):
    """Longest request-received -> response-received gap over flows to `port`."""
    by_client = {}
    for e in events:
        if e["ev"] != "tcp_rtt" or not e.get("bytes"):
            continue
        if e["sport"] == port:      # socket is the server's: it received request bytes
            by_client.setdefault(e["dport"], []).append((e["t"], "req"))
        elif e["dport"] == port:    # socket is the client's: it received response bytes
            by_client.setdefault(e["sport"], []).append((e["t"], "resp"))
    gaps = []
    for evs in by_client.values():
        evs.sort()
        first_resp = next((t for t, k in evs if k == "resp"), None)
        if first_resp is None:
            continue
        last_req = max((t for t, k in evs if k == "req" and t < first_resp), default=None)
        if last_req is not None:
            gaps.append((first_resp - last_req) / 1e6)
    return max(gaps) if gaps else None


def _max(xs):
    xs = list(xs)
    return max(xs) if xs else None


def summarize(events, env):
    api, llm = env["api_port"], env["llm_port"]
    ports = {api, llm, env["dns_port"]}

    def touches(e, port):
        return port in (e.get("sport"), e.get("dport"))

    def ms(ns):
        return None if ns is None else ns / 1e6

    ends = [e for e in events if e["ev"] == "tcp_connect_end"]
    rtt = [e for e in events if e["ev"] == "tcp_rtt"]
    tracked = [e for e in events if any(touches(e, p) for p in ports)]
    ts = sorted(e["t"] for e in tracked)
    return {
        "dns_ms": ms(_max(e["lat_ns"] for e in events if e["ev"] == "dns_response")),
        "api_connect_ms": ms(_max(e["lat_ns"] for e in ends if e["dport"] == api)),
        "llm_connect_ms": ms(_max(e["lat_ns"] for e in ends if e["dport"] == llm)),
        "api_retx": sum(e["ev"] == "tcp_retransmit" and touches(e, api) for e in events),
        "llm_retx": sum(e["ev"] == "tcp_retransmit" and touches(e, llm) for e in events),
        "api_srtt_ms": (lambda v: None if v is None else v / 1000)(
            _max(e["srtt_us"] for e in rtt if touches(e, api))),
        "api_resp_ms": _flows_resp_ms(events, api),
        "llm_resp_ms": _flows_resp_ms(events, llm),
        "llm_connects": sum(e["ev"] == "tcp_connect_start" and e.get("dport") == llm for e in events),
        "max_gap_ms": max(((b - a) / 1e6 for a, b in zip(ts, ts[1:])), default=None),
    }
