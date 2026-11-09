"""Matching calls to connections WITHOUT a declared port, by optimal assignment on start time and duration (Stage 17).

For async agents on one thread every span has the same pid and thread id, so process/thread matching cannot tell the calls apart (Stage 11).
This module tests one idea fixed in advance (docs/predictions-stage17.md): within one run and one server port, match the HTTP client spans to
the kernel's connections with the cost

    cost(span, connection) = |span.start - connection.connect_start| + |span.duration - connection.active_duration|     (ms, equal weights)

by optimal (Hungarian) assignment, then write the matched connection's local port into the span as `network.local.port` so the unchanged
correlator (correlator.join, step 0) does the rest. Nothing here reads pid or thread id. The cost is NOT to be tuned after the scored runs.

A connection is one connect_start/connect_end pair (paired by the kernel socket pointer `sk`). Its active duration runs from its connect_start
to the last event of the same 4-tuple before the next connection reused that client port.
"""
import bisect
from copy import deepcopy

from correlator.join import ClockMap, _is_http

INF = float("inf")


def hungarian(cost):
    """Minimum-cost assignment of every row to a distinct column (rows <= columns). Returns the column of each row. O(rows^2 * columns)."""
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    if n > m:
        raise ValueError("more rows than columns")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    ans = [None] * n
    for j in range(1, m + 1):
        if p[j]:
            ans[p[j] - 1] = j - 1
    return ans


def connections(events, clock):
    """[{port, dport, start_ns, end_ns}] (wall clock) for every connection the kernel saw being opened (connect_start paired with its connect_end)."""
    starts, ends = {}, []
    for e in events:
        if e["ev"] == "tcp_connect_start" and e.get("sk") is not None:
            starts.setdefault(e["sk"], []).append(e["t"])
        elif e["ev"] == "tcp_connect_end" and e.get("sk") is not None:
            ends.append(e)
    flow_times = {}                                          # (min port, max port) -> sorted monotonic times of its events
    for e in events:
        if e["ev"] in ("tcp_rtt", "tcp_retransmit", "tcp_send") and e.get("sport") is not None and e.get("dport") is not None:
            flow_times.setdefault((min(e["sport"], e["dport"]), max(e["sport"], e["dport"])), []).append(e["t"])
    for ts in flow_times.values():
        ts.sort()
    conns = []
    for e in ends:
        cands = [t for t in starts.get(e["sk"], []) if t <= e["t"]]
        t0 = max(cands) if cands else e["t"] - e.get("lat_ns", 0)
        conns.append({"port": e["sport"], "dport": e["dport"], "t0": t0, "t_end": e["t"]})
    conns.sort(key=lambda c: c["t0"])
    last_by_pair = {}
    for c in reversed(conns):                                 # the next connection on the same client port bounds this one's activity
        key = (min(c["port"], c["dport"]), max(c["port"], c["dport"]))
        nxt = last_by_pair.get(key, INF)
        ts = flow_times.get(key, [])
        lo, hi = bisect.bisect_left(ts, c["t0"]), bisect.bisect_left(ts, nxt)
        last = ts[hi - 1] if hi > lo else c["t_end"]
        c["active_end"] = max(last, c["t_end"])
        last_by_pair[key] = c["t0"]
    for c in conns:
        c["start_ns"] = clock.to_wall(c["t0"])
        c["end_ns"] = clock.to_wall(c["active_end"])
    return conns


def declare_ports_by_assignment(spans, events, clock_pairs):
    """Copy of `spans` where each HTTP client span without `network.local.port` that could be assigned to a connection carries the connection's
    local port. Returns (new_spans, number_assigned, number_of_http_spans_considered)."""
    clock = ClockMap(clock_pairs)
    conns = connections(events, clock)
    by_server = {}
    for c in conns:
        by_server.setdefault(c["dport"], []).append(c)
    out = [deepcopy(s) for s in spans]
    groups = {}
    for s in out:
        if _is_http(s) and s["attrs"].get("network.local.port") is None and s["attrs"].get("server.port") in by_server:
            groups.setdefault(s["attrs"]["server.port"], []).append(s)
    assigned = considered = 0
    for port, ss in groups.items():
        cs = by_server[port]
        considered += len(ss)
        cost = [[(abs(s["start_ns"] - c["start_ns"]) + abs((s["end_ns"] - s["start_ns"]) - (c["end_ns"] - c["start_ns"]))) / 1e6
                 for c in cs] for s in ss]
        if len(ss) <= len(cs):
            cols = hungarian(cost)
            pairs = list(enumerate(cols))
        else:                                                  # more calls than connections: assign connections to their best calls
            rows = hungarian([list(r) for r in zip(*cost)])
            pairs = [(row, i) for i, row in enumerate(rows)]
        for si, ci in pairs:
            if si is not None and ci is not None:
                ss[si]["attrs"]["network.local.port"] = cs[ci]["port"]
                assigned += 1
    return out, assigned, considered
