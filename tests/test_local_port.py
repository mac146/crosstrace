"""Spans that declare their socket (network.local.port) are joined by it, which survives many requests on ONE thread."""
from correlator.join import correlate

MS = 1_000_000
PAIRS = [(0, 0, 0)]
PORT = 8000


def span(sid, s, e, local_port=None):
    attrs = {"process.pid": 1, "thread.id": 5, "server.port": PORT, "server.address": "127.0.0.1"}   # one shared thread
    if local_port is not None:
        attrs["network.local.port"] = local_port
    return {"span_id": sid, "parent_id": None, "name": "POST", "start_ns": int(s * MS), "end_ns": int(e * MS), "attrs": attrs}


def conn(port, t0, sk):
    """Events of one connection opened at t0 (ms) by a coroutine on thread 5."""
    return [
        {"ev": "tcp_connect_start", "t": int((t0 + 1) * MS), "pid": 1, "tid": 5, "dport": PORT, "sk": sk},
        {"ev": "tcp_connect_end", "t": int((t0 + 2) * MS), "pid": 1, "tid": 5, "sport": port, "dport": PORT,
         "lat_ns": MS, "sk": sk},
        {"ev": "tcp_send", "t": int((t0 + 3) * MS), "pid": 1, "tid": 5, "sport": port, "dport": PORT, "bytes": 400},
        {"ev": "tcp_rtt", "t": int((t0 + 4) * MS), "sport": PORT, "dport": port, "bytes": 400, "srtt_us": 20},
    ]


def ends(tl, sid):
    entry = next(e for e in tl.entries if e.span["span_id"] == sid)
    return sorted(a.event["sport"] for a in entry.kernel if a.event["ev"] == "tcp_connect_end"), \
        sum(a.event["ev"] == "tcp_connect_start" for a in entry.kernel), {a.match for a in entry.kernel}


def overlapping_pair(declare):
    # two coroutines on one thread: A and B overlap in time; their connections are interleaved
    a, b = span("A", 0, 100, 40001 if declare else None), span("B", 5, 60, 40002 if declare else None)
    events = conn(40001, 10, sk=111) + conn(40002, 20, sk=222)
    return correlate([a, b], events, PAIRS)


def test_with_declared_ports_each_overlapping_span_gets_exactly_its_own_connection():
    tl = overlapping_pair(declare=True)
    assert ends(tl, "A")[0] == [40001] and ends(tl, "B")[0] == [40002]
    assert ends(tl, "A")[1] == 1 and ends(tl, "B")[1] == 1            # connect_start paired via the socket pointer
    assert ends(tl, "A")[2] == {"local-port", "flow"} and tl.orphans == []


def test_without_declared_ports_one_shared_thread_is_ambiguous():
    """The documented limit: A's connect (t=11..12) lies inside both spans; the innermost (B) wins, which is wrong."""
    tl = overlapping_pair(declare=False)
    assert ends(tl, "A")[0] != [40001]                                  # misattributed: not recoverable from time alone


def test_a_span_with_a_port_and_one_without_are_both_handled():
    a, b = span("A", 0, 100, 40001), span("B", 200, 300)                # B does not declare (e.g. another client library)
    tl = correlate([a, b], conn(40001, 10, 111) + conn(40009, 210, 222), PAIRS)
    assert ends(tl, "A")[0] == [40001] and ends(tl, "B")[0] == [40009]
    assert ends(tl, "B")[2] == {"pid+time", "flow"}


def test_a_declared_port_never_matches_another_servers_connection():
    a = span("A", 0, 100, 40001)
    wrong_server = [{"ev": "tcp_connect_end", "t": 10 * MS, "pid": 1, "tid": 5, "sport": 40001, "dport": 9999,
                     "lat_ns": MS, "sk": 1}]
    tl = correlate([a], wrong_server, PAIRS)
    assert ends(tl, "A")[0] == [] and len(tl.orphans) == 1


def test_reused_port_is_resolved_by_time():
    a, b = span("A", 0, 50, 40001), span("B", 1000, 1050, 40001)        # the same client port, a second later
    tl = correlate([a, b], conn(40001, 10, 111) + conn(40001, 1010, 333), PAIRS)
    assert ends(tl, "A")[0] == [40001] and ends(tl, "B")[0] == [40001]
    assert ends(tl, "A")[1] == 1 and ends(tl, "B")[1] == 1


def test_socket_pointer_reuse_does_not_pair_a_start_with_an_old_end():
    a, b = span("A", 0, 50, 40001), span("B", 60_000, 60_050, 40002)    # same kernel socket address a minute later
    events = conn(40001, 10, sk=777) + conn(40002, 60_010, sk=777)
    tl = correlate([a, b], events, PAIRS)
    assert ends(tl, "A")[1] == 1 and ends(tl, "B")[1] == 1
