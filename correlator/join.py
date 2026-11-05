"""The join: attach kernel events to the agent spans they belong to, on one clock.

Inputs
  spans          OTel span dicts (wall clock ns; attrs include process.pid, thread.id, server.address/port)
  kernel_events  collector events (CLOCK_MONOTONIC ns)
  clock_pairs    [(mono_ns, wall_ns, err_ns), ...] taken around the collection (collectors.ebpf.sync_clocks)

Clock alignment
  Every kernel event is converted to wall time: wall = mono + offset, where offset = wall - mono is
  linearly interpolated between the two sync pairs that bracket the event (pairs are sampled about every
  100 ms, so drift is absorbed and a wall-clock step is confined to one interval). Reported error bound =
  largest pair error; matching windows are padded by `slack_ns` on top of it. `clock_max_jump_ns` reports
  the largest offset change between adjacent pairs: events near such a jump can be misaligned by that much.

Which events can be attached how (strongest evidence first)
  1. dns_query / dns_response / tcp_connect_start / tcp_connect_end carry an exact PID+TID. They attach to
     the HTTP client span with the same PID+TID whose window contains them. TCP events must also have
     dport == span's server.port. If several spans qualify, the innermost (shortest) wins.
  2. A matched connect_end fixes the connection's client port. tcp_rtt and tcp_retransmit events whose
     4-tuple is that flow (either direction, so server-side retransmits too) attach to that span, within
     [start - slack, end + tail] (tail 5 s: a lost FIN is retransmitted long after the span ended). These events have no PID, so the flow is the only safe key.
  3. Fallback for spans that never completed a connect (no flow known): tcp_rtt / tcp_retransmit touching
     the span's server port, inside the padded window, and not belonging to another span's flow.
  Everything else is returned in `orphans` with a reason; nothing is silently dropped.
"""
import bisect
import ipaddress
from dataclasses import dataclass, field

PID_TCP = ("tcp_connect_start", "tcp_connect_end")
PID_DNS = ("dns_query", "dns_response")
FLOW_EVENTS = ("tcp_rtt", "tcp_retransmit")


class ClockMap:
    """monotonic -> wall. Interpolates the offset (wall - mono) between the two sync pairs that bracket
    the instant; clamps to the nearest pair outside the sampled range. Sampling densely matters: a step
    of the wall clock between two pairs is smeared over that one interval only."""

    def __init__(self, pairs):
        if not pairs:
            raise ValueError("need at least one clock sync pair")
        self.pairs = sorted(pairs)
        self._mono = [p[0] for p in self.pairs]
        self._off = [p[1] - p[0] for p in self.pairs]
        self.err_ns = max(p[2] for p in pairs)
        # largest change of the offset between adjacent pairs: a wall-clock step or fast drift
        self.max_jump_ns = max((abs(b - a) for a, b in zip(self._off, self._off[1:])), default=0)

    def to_wall(self, mono_ns):
        i = bisect.bisect_right(self._mono, mono_ns)
        if i == 0:
            off = self._off[0]
        elif i == len(self._mono):
            off = self._off[-1]
        else:
            m0, m1 = self._mono[i - 1], self._mono[i]
            o0, o1 = self._off[i - 1], self._off[i]
            # keep the ~1.8e18 ns offset an int: as a float it would only resolve ~256 ns
            off = o0 + (round((mono_ns - m0) / (m1 - m0) * (o1 - o0)) if m1 != m0 else 0)
        return mono_ns + off


@dataclass
class Attached:
    event: dict          # original event plus "wall_ns"
    match: str           # "pid+time" | "flow" | "port+time"


@dataclass
class Entry:
    span: dict
    depth: int
    kernel: list = field(default_factory=list)   # list[Attached], sorted by time


@dataclass
class Timeline:
    entries: list        # all spans, in start order
    orphans: list        # [(event, reason)]
    clock_err_ns: int
    t0_ns: int
    clock_max_jump_ns: int = 0   # biggest offset change between adjacent sync pairs (wall-clock step)


def _depths(spans):
    by_id = {s["span_id"]: s for s in spans}
    out = {}

    def depth(s):
        if s["span_id"] not in out:
            p = by_id.get(s["parent_id"])
            out[s["span_id"]] = 0 if p is None else depth(p) + 1
        return out[s["span_id"]]

    for s in spans:
        depth(s)
    return out


def _is_http(s):
    return s["attrs"].get("server.port") is not None


def _is_hostname(s):
    try:
        ipaddress.ip_address(s["attrs"].get("server.address", ""))
        return False
    except ValueError:
        return True


def correlate(spans, kernel_events, clock_pairs, slack_ns=1_000_000, tail_ns=5_000_000_000):
    clock = ClockMap(clock_pairs)
    slack = slack_ns + clock.err_ns
    spans = sorted(spans, key=lambda s: (s["start_ns"], -s["end_ns"]))
    depths = _depths(spans)
    entries = [Entry(s, depths[s["span_id"]]) for s in spans]
    http = [e for e in entries if _is_http(e.span)]
    events = sorted(({**e, "wall_ns": clock.to_wall(e["t"])} for e in kernel_events), key=lambda e: e["wall_ns"])
    claimed = set()   # index into `events`
    flows = {}        # (client_port, server_port) -> list of Entry that own the flow

    def attach(entry, i, how):
        entry.kernel.append(Attached(events[i], how))
        claimed.add(i)

    def width(e):
        return e.span["end_ns"] - e.span["start_ns"]

    def choose(candidates, t):
        """Strict containment first (innermost wins). Only if no span truly contains t, accept the
        nearest span within the clock slack: otherwise the slack would let a neighbouring span steal
        events from the one that really contains them."""
        strict = [e for e in candidates if e.span["start_ns"] <= t <= e.span["end_ns"]]
        if strict:
            return min(strict, key=width)
        near = [(max(e.span["start_ns"] - t, t - e.span["end_ns"]), width(e), i, e)
                for i, e in enumerate(candidates) if e.span["start_ns"] - slack <= t <= e.span["end_ns"] + slack]
        return min(near)[3] if near else None

    def same_thread(entry, ev):
        a = entry.span["attrs"]
        return a.get("process.pid") == ev.get("pid") and a.get("thread.id") == ev.get("tid")

    # 1. exact PID/TID evidence
    for i, ev in enumerate(events):
        if ev["ev"] in PID_DNS:   # a lookup belongs to a span that resolves a hostname
            cands = [e for e in http if same_thread(e, ev) and _is_hostname(e.span)]
        elif ev["ev"] in PID_TCP:
            cands = [e for e in http if same_thread(e, ev) and e.span["attrs"]["server.port"] == ev.get("dport")]
        else:
            continue
        owner = choose(cands, ev["wall_ns"])
        if owner:
            attach(owner, i, "pid+time")
            if ev["ev"] == "tcp_connect_end":
                flows.setdefault((ev["sport"], ev["dport"]), []).append(owner)

    # 2. flow evidence for events without a PID: same 4-tuple as a connection a span opened
    for i, ev in enumerate(events):
        if ev["ev"] not in FLOW_EVENTS:
            continue
        for (cport, sport), owners in flows.items():
            if {ev.get("sport"), ev.get("dport")} == {cport, sport}:
                t = ev["wall_ns"]
                # the connection may outlive its span (teardown, late retransmits): allow a tail
                live = [o for o in owners if o.span["start_ns"] - slack <= t <= o.span["end_ns"] + tail_ns]
                if live:   # the most recently started owner wins (tuple reuse)
                    attach(max(live, key=lambda o: o.span["start_ns"]), i, "flow")
                break

    # 3. fallback for spans with no known flow (connect never completed): port + time
    flow_owner_ids = {id(o) for owners in flows.values() for o in owners}
    flowless = [e for e in http if id(e) not in flow_owner_ids]
    flow_pairs = {frozenset(k) for k in flows}
    for i, ev in enumerate(events):
        if i in claimed or ev["ev"] not in FLOW_EVENTS:
            continue
        if frozenset((ev.get("sport"), ev.get("dport"))) in flow_pairs:
            continue   # belongs to a known flow of some span, just outside its window: not ours to guess
        cands = [e for e in flowless if e.span["attrs"]["server.port"] in (ev.get("sport"), ev.get("dport"))]
        owner = choose(cands, ev["wall_ns"])
        if owner:
            attach(owner, i, "port+time")

    targeted = {e.span["attrs"]["server.port"] for e in http}
    orphans = []
    for i, ev in enumerate(events):
        if i in claimed:
            continue
        if ev["ev"] in FLOW_EVENTS and not targeted & {ev.get("sport"), ev.get("dport")}:
            orphans.append((ev, "unrelated traffic: no span targets either port"))
        else:
            orphans.append((ev, "no span matched pid/tid/port within the time window"))
    for e in entries:
        e.kernel.sort(key=lambda a: a.event["wall_ns"])
    t0 = entries[0].span["start_ns"] if entries else 0
    return Timeline(entries, orphans, clock.err_ns, t0, clock.max_jump_ns)


def breakdown(entry):
    """Named evidence for one HTTP span, from its attached kernel events (ms). None = not observed."""
    ev = [a.event for a in entry.kernel]
    span_ms = (entry.span["end_ns"] - entry.span["start_ns"]) / 1e6

    def total(name):
        xs = [e["lat_ns"] for e in ev if e["ev"] == name]
        return sum(xs) / 1e6 if xs else None

    dns, conn = total("dns_response"), total("tcp_connect_end")
    srtt = [e["srtt_us"] for e in ev if e["ev"] == "tcp_rtt"]
    resp = _resp_gap_ms(ev, entry.span["attrs"]["server.port"])
    explained = sum(x for x in (dns, conn, resp) if x is not None)
    return {"span_ms": span_ms, "n_events": len(ev), "dns_ms": dns, "connect_ms": conn,
            "retransmits": sum(e["ev"] == "tcp_retransmit" for e in ev),
            "srtt_max_ms": max(srtt) / 1000 if srtt else None,
            "server_wait_ms": resp, "unexplained_ms": span_ms - explained}


def _resp_gap_ms(events, server_port):
    """Last request byte received by the server -> first response byte received by the client."""
    first_resp, reqs = None, []
    for e in events:
        if e["ev"] != "tcp_rtt" or not e.get("bytes"):
            continue
        if e["sport"] == server_port:
            reqs.append(e["wall_ns"])
        elif e["dport"] == server_port and first_resp is None:
            first_resp = e["wall_ns"]
    if first_resp is None:
        return None
    before = [t for t in reqs if t < first_resp]
    return (first_resp - max(before)) / 1e6 if before else None
