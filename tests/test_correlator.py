"""Join correctness against hand-built events whose right answer is known by construction.

Time axis: everything is defined in "ms since run start". Kernel events are written in the monotonic
domain with a large arbitrary offset to the wall clock, so a join that forgot to align clocks fails.
"""
import itertools

import pytest

from correlator.integrity import check
from correlator.join import ClockMap, breakdown, correlate

MONO0 = 7_000_000_000_000           # arbitrary monotonic origin
OFFSET = 1_793_000_000_000_000_000 - MONO0   # wall - mono
PAIRS = [(MONO0, MONO0 + OFFSET, 100)]
_id = itertools.count(1)


def wall(ms):
    return MONO0 + OFFSET + int(ms * 1e6)


def ev(name, ms, **kw):
    return {"ev": name, "t": MONO0 + int(ms * 1e6), **kw}


def http(name, s, e, pid, tid, port, host="api.mock.local", parent=None):
    return {"span_id": f"s{next(_id)}", "parent_id": parent, "name": name, "start_ns": wall(s), "end_ns": wall(e),
            "attrs": {"process.pid": pid, "thread.id": tid, "server.port": port, "server.address": host}}


def plain(name, s, e, sid="root", parent=None):
    return {"span_id": sid, "parent_id": parent, "name": name, "start_ns": wall(s), "end_ns": wall(e),
            "attrs": {"process.pid": 10, "thread.id": 10}}


def attached(tl, span):
    entry = next(e for e in tl.entries if e.span is span)
    return [(a.event["ev"], a.match) for a in entry.kernel]


def names(tl, span):
    return sorted(x for x, _ in attached(tl, span))


def full_get(port=8000, cport=40000, pid=10, tid=10, t0=100):
    """Kernel events of one healthy GET starting at t0 ms (DNS, connect, request, response, close ack)."""
    return [
        ev("dns_query", t0 + 1, pid=pid, tid=tid, daddr="127.0.0.1", dport=5353),
        ev("dns_response", t0 + 301, pid=pid, tid=tid, lat_ns=300_000_000, bytes=48),
        ev("tcp_connect_start", t0 + 302, pid=pid, tid=tid, dport=port),
        ev("tcp_connect_end", t0 + 303, pid=pid, tid=tid, sport=cport, dport=port, lat_ns=1_000_000),
        ev("tcp_rtt", t0 + 304, sport=port, dport=cport, bytes=100, srtt_us=40),       # server got request
        ev("tcp_rtt", t0 + 354, sport=cport, dport=port, bytes=200, srtt_us=40),       # client got response
        ev("tcp_retransmit", t0 + 330, sport=port, dport=cport, state=1),              # server-side, no pid
    ]


def test_clock_map_applies_offset_and_interpolates_drift():
    m = ClockMap([(MONO0, MONO0 + OFFSET, 50), (MONO0 + 2_000_000_000, MONO0 + 2_000_000_000 + OFFSET + 4_000, 80)])
    assert m.to_wall(MONO0) == MONO0 + OFFSET
    assert m.to_wall(MONO0 + 1_000_000_000) == MONO0 + 1_000_000_000 + OFFSET + 2_000   # halfway: +2 us
    assert m.to_wall(MONO0 + 9_000_000_000) == MONO0 + 9_000_000_000 + OFFSET + 4_000   # clamped beyond the end
    assert m.err_ns == 80
    with pytest.raises(ValueError):
        ClockMap([])


def test_clock_step_is_confined_to_the_interval_it_happened_in():
    # offset is 0 until MONO0+200ms, then the wall clock steps by -3.5 ms before the next sample at +300ms
    step = -3_500_000
    pairs = [(MONO0 + i * 100_000_000, MONO0 + i * 100_000_000 + OFFSET + (0 if i <= 2 else step), 50)
             for i in range(6)]
    m = ClockMap(pairs)
    assert m.max_jump_ns == 3_500_000
    assert m.to_wall(MONO0 + 150_000_000) == MONO0 + 150_000_000 + OFFSET              # before the step: exact
    assert m.to_wall(MONO0 + 450_000_000) == MONO0 + 450_000_000 + OFFSET + step       # after the step: exact
    mid = m.to_wall(MONO0 + 250_000_000) - (MONO0 + 250_000_000 + OFFSET)
    assert step < mid < 0                                                              # only the stepped interval is smeared


def test_timeline_reports_the_clock_jump():
    s = http("GET", 100, 460, 10, 10, 8000)
    pairs = [(MONO0, MONO0 + OFFSET, 10), (MONO0 + 10**8, MONO0 + 10**8 + OFFSET - 3_000_000, 10)]
    assert correlate([plain("root", 0, 600), s], full_get(), pairs).clock_max_jump_ns == 3_000_000


def test_every_event_of_one_request_lands_on_its_span():
    s = http("GET", 100, 460, 10, 10, 8000)
    tl = correlate([plain("root", 0, 600), s], full_get(), PAIRS)
    assert attached(tl, s).count(("dns_query", "pid+time")) == 1
    assert names(tl, s) == ["dns_query", "dns_response", "tcp_connect_end", "tcp_connect_start",
                            "tcp_retransmit", "tcp_rtt", "tcp_rtt"]
    assert ("tcp_retransmit", "flow") in attached(tl, s)    # server-side retransmit: no pid, joined by flow
    assert tl.orphans == []


def test_clock_offset_is_really_applied():
    s = http("GET", 100, 460, 10, 10, 8000)
    wrong = [(MONO0, MONO0 + OFFSET + 5_000_000_000, 100)]   # 5 s off: nothing can match
    tl = correlate([plain("root", 0, 600), s], full_get(), wrong)
    assert names(tl, s) == [] and len(tl.orphans) == 7


def test_events_from_another_process_are_not_attached():
    s = http("GET", 100, 460, 10, 10, 8000)
    decoys = [ev("dns_query", 150, pid=99, tid=99, daddr="127.0.0.1", dport=5353),
              ev("tcp_connect_start", 150, pid=99, tid=99, dport=8000),
              ev("tcp_connect_end", 151, pid=99, tid=99, sport=45000, dport=8000, lat_ns=1)]
    tl = correlate([plain("root", 0, 600), s], full_get() + decoys, PAIRS)
    assert names(tl, s).count("tcp_connect_start") == 1 and names(tl, s).count("dns_query") == 1
    assert sorted(e["ev"] for e, _ in tl.orphans) == ["dns_query", "tcp_connect_end", "tcp_connect_start"]


def test_same_process_but_other_thread_is_not_attached():
    s = http("GET", 100, 460, 10, 10, 8000)
    other_thread = [ev("tcp_connect_start", 150, pid=10, tid=77, dport=8000)]
    tl = correlate([plain("root", 0, 600), s], full_get() + other_thread, PAIRS)
    assert names(tl, s).count("tcp_connect_start") == 1
    assert [e["tid"] for e, _ in tl.orphans] == [77]


def test_connect_to_a_different_port_is_not_attached():
    s = http("GET", 100, 460, 10, 10, 8000)
    wrong_port = [ev("tcp_connect_start", 150, pid=10, tid=10, dport=9000)]
    tl = correlate([plain("root", 0, 600), s], full_get() + wrong_port, PAIRS)
    assert names(tl, s).count("tcp_connect_start") == 1
    assert [e["dport"] for e, _ in tl.orphans] == [9000]


def test_events_outside_the_window_are_not_attached():
    s = http("GET", 100, 460, 10, 10, 8000)
    early = ev("tcp_connect_start", 90, pid=10, tid=10, dport=8000)    # 10 ms before the span
    late = ev("dns_query", 480, pid=10, tid=10, daddr="127.0.0.1", dport=5353)
    tl = correlate([plain("root", 0, 600), s], full_get() + [early, late], PAIRS)
    assert sorted(e["t"] for e, _ in tl.orphans) == sorted([early["t"], late["t"]])


def test_events_just_inside_the_slack_are_attached_but_not_beyond():
    s = http("GET", 100, 460, 10, 10, 8000)
    ok = ev("tcp_connect_start", 99.5, pid=10, tid=10, dport=8000)    # 0.5 ms early < 1 ms slack
    tl = correlate([plain("root", 0, 600), s], [ok], PAIRS)
    assert names(tl, s) == ["tcp_connect_start"]


def test_two_concurrent_requests_to_the_same_port_keep_their_own_flows():
    a = http("GET", 100, 460, 10, 10, 8000)
    b = http("GET", 150, 510, 10, 11, 8000)
    events = full_get(cport=40000, tid=10, t0=100) + full_get(cport=40001, tid=11, t0=150)
    tl = correlate([plain("root", 0, 600), a, b], events, PAIRS)
    for span, cport in ((a, 40000), (b, 40001)):
        flow_ports = {x for ev_, _ in [(a_.event, 0) for a_ in next(e for e in tl.entries if e.span is span).kernel]
                      for x in (ev_.get("sport"), ev_.get("dport")) if x in (40000, 40001)}
        assert flow_ports == {cport}
        assert len(attached(tl, span)) == 7
    assert tl.orphans == []


def test_failed_connect_falls_back_to_port_and_time():
    s = http("GET", 100, 5000, 10, 10, 8000)
    events = [ev("tcp_connect_start", 101, pid=10, tid=10, dport=8000),
              ev("tcp_retransmit", 1100, sport=41000, dport=8000, state=2),   # SYN retransmit, tuple unknown to us
              ev("tcp_retransmit", 3100, sport=41000, dport=8000, state=2)]
    tl = correlate([plain("root", 0, 6000), s], events, PAIRS)
    assert attached(tl, s) == [("tcp_connect_start", "pid+time"), ("tcp_retransmit", "port+time"),
                               ("tcp_retransmit", "port+time")]


def test_fallback_does_not_steal_events_of_another_spans_known_flow():
    ok = http("GET", 100, 460, 10, 10, 8000)
    broken = http("GET", 500, 900, 10, 10, 8000)
    events = full_get(cport=40000, t0=100) + [
        ev("tcp_connect_start", 501, pid=10, tid=10, dport=8000),
        ev("tcp_retransmit", 405, sport=8000, dport=40000, state=1),     # late packet of ok's flow, inside broken? no: 405<500
        ev("tcp_retransmit", 600, sport=8000, dport=40000, state=1),     # stray packet of ok's flow during broken
    ]
    tl = correlate([plain("root", 0, 1000), ok, broken], events, PAIRS)
    assert "tcp_retransmit" not in names(tl, broken)    # must not be guessed onto the flowless span


def test_tail_events_of_a_flow_after_span_end_attach_within_tail_only():
    s = http("GET", 100, 460, 10, 10, 8000)
    base = full_get()
    near = ev("tcp_rtt", 1000, sport=40000, dport=8000, bytes=0, srtt_us=10)    # 540 ms after the span end: a retransmitted FIN
    far = ev("tcp_rtt", 7000, sport=40000, dport=8000, bytes=0, srtt_us=10)     # 6.5 s after: beyond the 5 s tail
    tl = correlate([plain("root", 0, 8000), s], base + [near, far], PAIRS)
    assert [e["t"] for e, _ in tl.orphans] == [far["t"]]


def test_non_http_spans_never_get_kernel_events_and_nesting_is_kept():
    root = plain("invoke_agent", 0, 600, "r")
    tool = plain("execute_tool http_api", 90, 470, "t", parent="r")
    s = http("GET", 100, 460, 10, 10, 8000, parent="t")
    tl = correlate([root, tool, s], full_get(), PAIRS)
    assert [e.depth for e in tl.entries] == [0, 1, 2]
    assert tl.entries[0].kernel == [] and tl.entries[1].kernel == [] and len(tl.entries[2].kernel) == 7


def test_breakdown_reports_each_component():
    s = http("GET", 100, 460, 10, 10, 8000)
    tl = correlate([plain("root", 0, 600), s], full_get(), PAIRS)
    b = breakdown(next(e for e in tl.entries if e.span is s))
    assert b["dns_ms"] == pytest.approx(300.0) and b["connect_ms"] == pytest.approx(1.0)
    assert b["retransmits"] == 1 and b["srtt_max_ms"] == pytest.approx(0.04)
    assert b["server_wait_ms"] == pytest.approx(50.0)           # 354 - 304
    assert b["span_ms"] == pytest.approx(360.0)
    assert b["unexplained_ms"] == pytest.approx(360 - 300 - 1 - 50)


ENV = {"api_port": 8000, "llm_port": 9000, "dns_port": 5353}


def test_integrity_check_passes_on_a_good_join_and_flags_a_bad_one():
    s = http("GET", 100, 460, 10, 10, 8000)
    good = correlate([plain("root", 0, 600), s], full_get(), PAIRS)
    assert check(good, ENV) == []
    no_connect = [e for e in full_get() if e["ev"] not in ("tcp_connect_start", "tcp_connect_end")]
    bad = correlate([plain("root", 0, 600), s], no_connect, PAIRS)
    assert any("tcp_connect_start" in p for p in check(bad, ENV))
