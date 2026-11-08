"""libc DNS probe, TCP-send probe, reused connections and remote-server wait."""
import os
import socket
import threading
import time

import pytest

from collectors import ebpf
from correlator.join import breakdown, correlate

MS = 1_000_000
MONO0 = 7_000_000_000_000
OFFSET = 1_793_000_000_000_000_000 - MONO0
PAIRS = [(MONO0, MONO0 + OFFSET, 100)]


def wall(ms):
    return MONO0 + OFFSET + int(ms * MS)


def ev(name, ms, **kw):
    return {"ev": name, "t": MONO0 + int(ms * MS), **kw}


def http_span(sid, s, e, port=443, host="api.example.com", tid=10):
    return {"span_id": sid, "parent_id": None, "name": "POST", "start_ns": wall(s), "end_ns": wall(e),
            "attrs": {"process.pid": 10, "thread.id": tid, "server.port": port, "server.address": host}}


def kinds(tl, sid):
    entry = next(e for e in tl.entries if e.span["span_id"] == sid)
    return [(a.event["ev"], a.match) for a in entry.kernel]


def test_program_text_has_no_unfilled_placeholders_with_every_option():
    for kw in ({}, {"libc_dns": True}, {"tcp_send": True}, {"libc_dns": True, "tcp_send": True, "ports": [443, 8000]}):
        text = ebpf.program([53], **kw)
        assert "@@" not in text
    assert "getaddrinfo" in ebpf.program([53], libc_dns=True) and "getaddrinfo" not in ebpf.program([53])
    assert "tcp_sendmsg" in ebpf.program([53], tcp_send=True)
    assert "$sport == 443 || $dport == 443" in ebpf.program([53], ports=[443], tcp_send=True)


def test_reused_connection_gets_evidence_through_the_sends_of_each_span():
    """Span A opens the connection (connect + send). Span B reuses it: no connect, only a send from the same thread.
    Before tcp_send existed, B's RTT samples were unattachable."""
    a, b = http_span("A", 0, 100), http_span("B", 200, 300)
    events = [
        ev("tcp_connect_start", 1, pid=10, tid=10, dport=443),
        ev("tcp_connect_end", 15, pid=10, tid=10, sport=50000, dport=443, lat_ns=14 * MS),
        ev("tcp_send", 20, pid=10, tid=10, sport=50000, dport=443, bytes=900),
        ev("tcp_rtt", 80, sport=50000, dport=443, bytes=300, srtt_us=15000),
        ev("tcp_send", 210, pid=10, tid=10, sport=50000, dport=443, bytes=900),     # B: same connection
        ev("tcp_rtt", 290, sport=50000, dport=443, bytes=300, srtt_us=15000),       # B's response
    ]
    tl = correlate([a, b], events, PAIRS)
    assert ("tcp_send", "pid+time") in kinds(tl, "B") and ("tcp_rtt", "flow") in kinds(tl, "B")
    assert ("tcp_rtt", "flow") in kinds(tl, "A") and sum(k[0] == "tcp_rtt" for k in kinds(tl, "A")) == 1
    assert tl.orphans == []


def test_a_servers_own_send_on_loopback_is_a_flow_event_not_the_clients_request():
    s = http_span("A", 0, 100, port=8000, host="127.0.0.1")
    events = [
        ev("tcp_connect_end", 5, pid=10, tid=10, sport=41000, dport=8000, lat_ns=1),
        ev("tcp_send", 10, pid=10, tid=10, sport=41000, dport=8000, bytes=500),    # client request
        ev("tcp_send", 40, pid=99, tid=99, sport=8000, dport=41000, bytes=700),    # server response, other process
    ]
    tl = correlate([s], events, PAIRS)
    assert kinds(tl, "A").count(("tcp_send", "pid+time")) == 1
    assert ("tcp_send", "flow") in kinds(tl, "A") and tl.orphans == []


def test_a_send_from_another_thread_is_not_ours():
    s = http_span("A", 0, 100, tid=10)
    events = [ev("tcp_send", 10, pid=10, tid=77, sport=51000, dport=443, bytes=900)]
    tl = correlate([s], events, PAIRS)
    assert kinds(tl, "A") == [] and len(tl.orphans) == 1


def test_remote_server_wait_is_largest_send_to_first_response_bytes():
    s = http_span("A", 0, 1000)
    events = [
        ev("tcp_send", 5, pid=10, tid=10, sport=52000, dport=443, bytes=250),      # TLS ClientHello: small
        ev("tcp_rtt", 30, sport=52000, dport=443, bytes=2800, srtt_us=20000),      # ServerHello flight
        ev("tcp_send", 35, pid=10, tid=10, sport=52000, dport=443, bytes=80),      # Finished
        ev("tcp_send", 40, pid=10, tid=10, sport=52000, dport=443, bytes=4200),    # the request: the largest send
        ev("tcp_rtt", 940, sport=52000, dport=443, bytes=600, srtt_us=20000),      # first response bytes
        ev("tcp_rtt", 960, sport=52000, dport=443, bytes=900, srtt_us=20000),
    ]
    b = breakdown(next(e for e in correlate([s], events, PAIRS).entries))
    assert b["server_wait_ms"] == pytest.approx(900.0)


def test_remote_wait_on_a_first_call_is_not_fooled_by_a_large_client_hello():
    s = http_span("A", 0, 6000)
    events = [
        ev("tcp_send", 5, pid=10, tid=10, sport=52000, dport=443, bytes=1570),     # ClientHello: BIGGER than the request
        ev("tcp_rtt", 260, sport=52000, dport=443, bytes=2800, srtt_us=20000),     # ServerHello flight
        ev("tcp_send", 270, pid=10, tid=10, sport=52000, dport=443, bytes=80),     # Finished
        ev("tcp_send", 271, pid=10, tid=10, sport=52000, dport=443, bytes=652),    # request headers
        ev("tcp_send", 272, pid=10, tid=10, sport=52000, dport=443, bytes=900),    # request body
        ev("tcp_rtt", 5200, sport=52000, dport=443, bytes=700, srtt_us=20000),     # response, ~4.9 s later
        ev("tcp_send", 5990, pid=10, tid=10, sport=52000, dport=443, bytes=30),    # nothing answers this one
    ]
    b = breakdown(next(iter(correlate([s], events, PAIRS).entries)))
    assert b["server_wait_ms"] == pytest.approx(4928.0)


def test_a_tls13_session_ticket_after_the_request_is_not_mistaken_for_the_response():
    from correlator.join import _client_first_byte_ms
    s = http_span("A", 0, 400)
    events = [
        ev("tcp_send", 100, pid=10, tid=10, sport=52000, dport=443, bytes=900),    # the request
        ev("tcp_rtt", 100.5, sport=52000, dport=443, bytes=250, srtt_us=100),      # NewSessionTicket, right after
        ev("tcp_rtt", 368, sport=52000, dport=443, bytes=400, srtt_us=100),        # the response, after a 267 ms delay
    ]
    entry = next(iter(correlate([s], events, PAIRS).entries))
    assert breakdown(entry)["server_wait_ms"] == pytest.approx(267.5)
    assert _client_first_byte_ms([a.event for a in entry.kernel], 443) == pytest.approx(0.5)   # the superseded definition


def test_loopback_server_wait_still_uses_the_server_side_events_when_present():
    s = http_span("A", 0, 400, port=8000, host="127.0.0.1")
    events = [
        ev("tcp_connect_end", 1, pid=10, tid=10, sport=41000, dport=8000, lat_ns=1),
        ev("tcp_rtt", 10, sport=8000, dport=41000, bytes=100, srtt_us=20),         # server got the request
        ev("tcp_rtt", 310, sport=41000, dport=8000, bytes=200, srtt_us=20),        # client got the response
        ev("tcp_send", 9, pid=10, tid=10, sport=41000, dport=8000, bytes=5000),    # would give 301 if it were used
    ]
    b = breakdown(next(e for e in correlate([s], events, PAIRS).entries))
    assert b["server_wait_ms"] == pytest.approx(300.0)


def test_client_wait_is_none_without_sends_or_responses():
    s = http_span("A", 0, 100)
    only_rtt = [ev("tcp_rtt", 50, sport=52000, dport=443, bytes=300, srtt_us=1)]
    assert breakdown(next(iter(correlate([s], only_rtt, PAIRS).entries)))["server_wait_ms"] is None


@pytest.mark.skipif(not ebpf.available(), reason="needs passwordless sudo bpftrace (scripts/setup_sudoers.sh)")
def test_live_libc_lookup_and_tcp_send_are_seen_with_our_pid_and_thread():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    got = []
    threading.Thread(target=lambda: got.append(srv.accept()[0].recv(100)), daemon=True).start()
    with ebpf.KernelCollector(dns_ports=[39998], ports=[port], libc_dns=True, tcp_send=True) as k:
        t0 = time.time_ns()
        socket.getaddrinfo("localhost", None)              # a hostname, resolved through libc
        socket.getaddrinfo("127.0.0.1", None)              # numeric: must be dropped
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(b"hello-live-probe")
        client_port = c.getsockname()[1]
        time.sleep(0.4)
        c.close()
    srv.close()
    tid = threading.get_native_id()
    lookups = [e for e in k.events if e.get("via") == "libc"]
    assert any(e["ev"] == "dns_response" and e["host"] == "localhost" and e["pid"] == os.getpid() and e["tid"] == tid
               for e in lookups)
    assert not any(e["host"] == "127.0.0.1" for e in lookups)                    # numeric hosts filtered
    sends = [e for e in k.events if e["ev"] == "tcp_send" and e["pid"] == os.getpid() and e["dport"] == port]
    assert sends and sends[0]["sport"] == client_port and sends[0]["tid"] == tid and sends[0]["bytes"] == 16
