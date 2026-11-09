"""Stage 17: assignment of calls to connections by time and duration (no root needed)."""
import itertools
import random

from correlator.assign import connections, declare_ports_by_assignment, hungarian
from correlator.join import ClockMap, correlate

MS = 1_000_000
PAIRS = [(0, 0, 0)]
PORT = 8000


def total(cost, cols):
    return sum(cost[i][j] for i, j in enumerate(cols))


def test_hungarian_is_optimal_on_random_square_and_rectangular_matrices():
    rng = random.Random(7)
    for n, m in [(1, 1), (3, 3), (4, 4), (3, 5), (5, 6), (2, 6)]:
        for _ in range(20):
            cost = [[rng.randint(0, 50) for _ in range(m)] for _ in range(n)]
            cols = hungarian(cost)
            assert len(set(cols)) == n
            best = min(total(cost, p) for p in itertools.permutations(range(m), n))
            assert total(cost, cols) == best


def span(sid, start, dur, **extra):
    return {"span_id": sid, "parent_id": None, "name": "GET", "start_ns": int(start * MS), "end_ns": int((start + dur) * MS),
            "attrs": {"process.pid": 1, "thread.id": 5, "server.port": PORT, "server.address": "127.0.0.1",
                      "url.full": "http://x/" + sid, **extra}}


def conn_events(sk, sport, t0, dur):
    """connect_start .. response data, kernel monotonic times in ms (the clock map below is the identity)."""
    return [{"ev": "tcp_connect_start", "t": int(t0 * MS), "pid": 1, "tid": 5, "dport": PORT, "sk": sk},
            {"ev": "tcp_connect_end", "t": int((t0 + 0.1) * MS), "pid": 1, "tid": 5, "sport": sport, "dport": PORT, "lat_ns": 0, "sk": sk},
            {"ev": "tcp_rtt", "t": int((t0 + dur) * MS), "sport": PORT, "dport": sport, "srtt_us": 50, "bytes": 200}]


def test_calls_with_different_durations_get_their_own_connection_even_when_they_start_together():
    spans = [span("fast", 100.0, 3), span("mid", 100.2, 300), span("slow", 100.4, 800)]
    events = conn_events(1, 40001, 100.1, 3) + conn_events(2, 40002, 100.3, 300) + conn_events(3, 40003, 100.5, 800)
    out, n, considered = declare_ports_by_assignment(spans, events, PAIRS)
    got = {s["span_id"]: s["attrs"]["network.local.port"] for s in out}
    assert (n, considered) == (3, 3)
    assert got == {"fast": 40001, "mid": 40002, "slow": 40003}
    assert "network.local.port" not in spans[0]["attrs"]                      # the input is not modified


def test_pid_and_thread_are_not_used_and_the_corrected_spans_join_their_own_events():
    spans = [span("a", 100.0, 3), span("b", 100.2, 500)]                       # same pid and thread id
    events = conn_events(1, 40001, 100.1, 3) + conn_events(2, 40002, 100.3, 500)
    for e in events:
        e["pid"], e["tid"] = 99, 99                                              # wrong on purpose: process matching would fail
    fixed, _, _ = declare_ports_by_assignment(spans, events, PAIRS)
    tl = correlate(fixed, events, PAIRS)
    ports = {en.span["span_id"]: {a.event.get("sport") or a.event.get("dport") for a in en.kernel if a.event["ev"] == "tcp_connect_end"}
             for en in tl.entries}
    assert ports["a"] == {40001} and ports["b"] == {40002}


def test_a_span_that_already_declares_its_port_is_left_alone_and_extra_connections_are_ignored():
    spans = [span("a", 100.0, 3, **{"network.local.port": 40009}), span("b", 100.2, 500)]
    events = conn_events(1, 40001, 90.0, 3) + conn_events(2, 40002, 100.3, 500)   # an extra earlier connection (a warm-up request)
    out, n, considered = declare_ports_by_assignment(spans, events, PAIRS)
    assert out[0]["attrs"]["network.local.port"] == 40009 and out[1]["attrs"]["network.local.port"] == 40002
    assert (n, considered) == (1, 1)


def test_connection_activity_stops_at_the_next_use_of_the_same_client_port():
    events = conn_events(1, 40001, 100.0, 10) + conn_events(2, 40001, 500.0, 10)       # port 40001 reused 400 ms later
    cs = connections(events, ClockMap(PAIRS))
    assert len(cs) == 2
    assert abs(cs[0]["end_ns"] - cs[0]["start_ns"]) < 20 * MS
    assert abs(cs[1]["end_ns"] - cs[1]["start_ns"]) < 20 * MS


def test_more_calls_than_connections_assigns_each_connection_once():
    spans = [span("a", 100.0, 3), span("b", 100.1, 3), span("c", 100.2, 3)]
    events = conn_events(1, 40001, 100.0, 3) + conn_events(2, 40002, 100.1, 3)
    out, n, considered = declare_ports_by_assignment(spans, events, PAIRS)
    ports = [s["attrs"].get("network.local.port") for s in out]
    assert n == 2 and considered == 3 and len({p for p in ports if p}) == 2


def test_weights_select_the_start_only_and_duration_only_costs():
    # two calls starting at the same instant with different durations: duration separates them, start cannot
    spans = [span("long", 100.0, 500), span("short", 100.0, 3)]
    events = conn_events(1, 40001, 100.1, 3) + conn_events(2, 40002, 100.1, 500)
    by_dur, _, _ = declare_ports_by_assignment(spans, events, PAIRS, weights=(0.0, 1.0))
    got = {s["span_id"]: s["attrs"]["network.local.port"] for s in by_dur}
    assert got == {"long": 40002, "short": 40001}
    by_start, _, _ = declare_ports_by_assignment(spans, events, PAIRS, weights=(1.0, 0.0))     # a tie on start: any assignment is allowed
    assert {s["attrs"]["network.local.port"] for s in by_start} == {40001, 40002}
    # a call that waits before connecting: the combined cost still prefers the right connection when durations differ a lot
    waiting = [span("w", 100.0, 520), span("n", 100.05, 3)]            # w started at 100.0 but connected at 100.4 after a wait
    ev2 = conn_events(1, 40011, 100.4, 120) + conn_events(2, 40012, 100.1, 3)
    out, _, _ = declare_ports_by_assignment(waiting, ev2, PAIRS)
    assert {s["span_id"]: s["attrs"]["network.local.port"] for s in out} == {"w": 40011, "n": 40012}
