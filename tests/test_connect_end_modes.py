"""Connection-completion probes: two implementations (fentry default, tracepoint fallback) and a correlator that survives a
connect_end with no process id."""
import threading
import time
import socket

import pytest

from collectors import ebpf
from correlator.join import breakdown, correlate

MS = 1_000_000
PAIRS = [(0, 0, 0)]
PORT = 8000


def span(sid, s, e, tid=5):
    return {"span_id": sid, "parent_id": None, "name": "POST", "start_ns": int(s * MS), "end_ns": int(e * MS),
            "attrs": {"process.pid": 1, "thread.id": tid, "server.port": PORT, "server.address": "127.0.0.1"}}


def kinds(tl, sid):
    entry = next(e for e in tl.entries if e.span["span_id"] == sid)
    return [(a.event["ev"], a.match) for a in entry.kernel]


def test_both_programs_build_without_unfilled_placeholders():
    for mode in ("fentry", "tracepoint"):
        text = ebpf.program([53], ports=[443, 8000], connect_end=mode)
        assert "@@" not in text and text.count('\\"ev\\":\\"tcp_connect_end\\"') == 1
    assert "fentry:vmlinux:tcp_finish_connect" in ebpf.program([53], connect_end="fentry")
    assert "$dport == 443 || $dport == 8000" in ebpf.program([53], ports=[443, 8000], connect_end="fentry")
    assert "inet_sock_set_state" in ebpf.program([53], connect_end="tracepoint")
    with pytest.raises(ValueError):
        ebpf.program([53], connect_end="kprobe")


def test_an_end_without_pid_attaches_through_the_connection_a_send_established():
    events = [
        {"ev": "tcp_connect_start", "t": 1 * MS, "pid": 1, "tid": 5, "dport": PORT, "sk": 9},
        {"ev": "tcp_connect_end", "t": 2 * MS, "pid": 0, "tid": 0, "sport": 40000, "dport": PORT, "lat_ns": 0, "sk": 9},
        {"ev": "tcp_send", "t": 3 * MS, "pid": 1, "tid": 5, "sport": 40000, "dport": PORT, "bytes": 100},
    ]
    tl = correlate([span("A", 0, 50)], events, PAIRS)
    assert ("tcp_connect_end", "flow") in kinds(tl, "A") and tl.orphans == []
    assert breakdown(next(iter(tl.entries)))["connect_ms"] == 0.0           # the connect time is unknown, not invented


def test_an_end_that_names_another_thread_is_never_guessed_onto_a_span():
    events = [{"ev": "tcp_connect_end", "t": 2 * MS, "pid": 1, "tid": 77, "sport": 40001, "dport": PORT,
               "lat_ns": MS, "sk": 3}]
    tl = correlate([span("A", 0, 50, tid=5)], events, PAIRS)
    assert kinds(tl, "A") == [] and len(tl.orphans) == 1


def test_an_end_with_no_pid_and_no_known_connection_stays_an_orphan():
    events = [{"ev": "tcp_connect_end", "t": 2 * MS, "pid": 0, "tid": 0, "sport": 40002, "dport": PORT, "lat_ns": 0, "sk": 4}]
    tl = correlate([span("A", 0, 50)], events, PAIRS)
    assert kinds(tl, "A") == [] and len(tl.orphans) == 1


@pytest.mark.skipif(not ebpf.available(), reason="needs passwordless sudo bpftrace (scripts/setup_sudoers.sh)")
@pytest.mark.parametrize("mode", ["tracepoint", "fentry"])
def test_live_both_probes_report_the_connection_with_our_process_and_ports(mode):
    if mode == "fentry" and not ebpf.fentry_supported():
        pytest.skip("fentry probes not supported here")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept()[0].recv(10), daemon=True).start()
    with ebpf.KernelCollector(dns_ports=[39997], ports=[port], connect_end=mode) as k:
        c = socket.create_connection(("127.0.0.1", port))
        local = c.getsockname()[1]
        c.sendall(b"hello")
        time.sleep(0.4)
        c.close()
    srv.close()
    ends = [e for e in k.events if e["ev"] == "tcp_connect_end" and e["dport"] == port]
    assert len(ends) == 1 and ends[0]["sport"] == local
    assert ends[0]["pid"] > 0 and ends[0]["lat_ns"] > 0             # taken from the map written at connect start
    assert ends[0]["daddr"] == "127.0.0.1" and ends[0]["saddr"] == "127.0.0.1"
