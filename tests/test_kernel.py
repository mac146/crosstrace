import os
import socket
import time

import pytest

from collectors import ebpf
from collectors.kernel_summary import summarize

ENV = {"api_port": 8000, "llm_port": 9000, "dns_port": 5353}
MS = 1_000_000


def rtt(t_ms, sport, dport, nbytes, srtt=20):
    return {"ev": "tcp_rtt", "t": int(t_ms * MS), "sport": sport, "dport": dport, "bytes": nbytes,
            "srtt_us": srtt}


def test_program_substitutes_dns_ports():
    p = ebpf.program([53, 5353])
    assert "$port == 53 || $port == 5353" in p and "@@" not in p


def test_summary_reads_dns_connect_and_retransmits():
    events = [
        {"ev": "dns_response", "t": 1 * MS, "lat_ns": 700 * MS},
        {"ev": "tcp_connect_end", "t": 2 * MS, "dport": 8000, "lat_ns": 300 * MS},
        {"ev": "tcp_connect_end", "t": 3 * MS, "dport": 9000, "lat_ns": 1 * MS},
        {"ev": "tcp_retransmit", "t": 4 * MS, "sport": 41000, "dport": 8000},
        {"ev": "tcp_retransmit", "t": 5 * MS, "sport": 8000, "dport": 41000},   # server side
        {"ev": "tcp_retransmit", "t": 6 * MS, "sport": 42000, "dport": 9000},
        {"ev": "tcp_connect_start", "t": 7 * MS, "dport": 9000},
    ]
    s = summarize(events, ENV)
    assert (s["dns_ms"], s["api_connect_ms"], s["llm_connect_ms"]) == (700.0, 300.0, 1.0)
    assert (s["api_retx"], s["llm_retx"], s["llm_connects"]) == (2, 1, 1)


def test_response_gap_is_last_request_byte_to_first_response_byte():
    events = [
        rtt(10, 9000, 41000, 141),    # server (port 9000) receives request headers
        rtt(11, 9000, 41000, 118),    # ... and body
        rtt(15, 9000, 41000, 0),      # pure ACK, ignored
        rtt(311, 41000, 9000, 145),   # client receives response headers
        rtt(312, 41000, 9000, 275),
    ]
    assert summarize(events, ENV)["llm_resp_ms"] == pytest.approx(300.0)


def test_response_gap_takes_longest_flow_and_ignores_incomplete_ones():
    events = [rtt(0, 9000, 41000, 100), rtt(5, 41000, 9000, 100),      # flow 41000: 5ms
              rtt(10, 9000, 42000, 100), rtt(410, 42000, 9000, 100),    # flow 42000: 400ms
              rtt(20, 9000, 43000, 100)]                                # no response seen
    assert summarize(events, ENV)["llm_resp_ms"] == pytest.approx(400.0)


def test_quiet_gap_between_tracked_events_only():
    events = [rtt(0, 9000, 41000, 10), rtt(5, 9000, 41000, 10), rtt(705, 9000, 41000, 10),
              {"ev": "tcp_rtt", "t": 400 * MS, "sport": 1, "dport": 2, "bytes": 5, "srtt_us": 1}]  # untracked flow
    assert summarize(events, ENV)["max_gap_ms"] == pytest.approx(700.0)


def test_empty_events_give_none_not_zero():
    s = summarize([], ENV)
    assert s["dns_ms"] is None and s["api_resp_ms"] is None and s["max_gap_ms"] is None
    assert s["api_retx"] == 0 and s["llm_connects"] == 0


def test_sync_clocks_pairs_clocks_closely():
    mono, wall, err = ebpf.sync_clocks()
    assert err < 1_000_000                                   # under 1 ms
    assert abs(time.time_ns() - wall) < 1_000_000_000        # sanity: wall pair is "now"
    assert abs(time.clock_gettime_ns(time.CLOCK_MONOTONIC) - mono) < 1_000_000_000


@pytest.mark.skipif(not ebpf.available(), reason="needs passwordless sudo bpftrace (scripts/setup_sudoers.sh)")
def test_live_kernel_event_is_attributed_and_clock_aligned():
    """A real UDP sendto must show up as dns_query with our pid/tid, and its kernel timestamp,
    mapped to wall time through the sync pair, must fall inside the wall-clock window around the send."""
    with ebpf.KernelCollector(dns_ports=[39999]) as k:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        w0 = time.time_ns()
        sock.sendto(b"x", ("127.0.0.1", 39999))
        w1 = time.time_ns()
        sock.close()
        time.sleep(0.3)  # let the event reach us before stopping
    q = [e for e in k.events if e["ev"] == "dns_query" and e["dport"] == 39999]
    assert len(q) == 1
    assert q[0]["pid"] == os.getpid() and q[0]["tid"] == __import__("threading").get_native_id()
    mono, wall, err = k.clock_sync[0]
    est_wall = q[0]["t"] - mono + wall
    slack = 2_000_000 + err   # 2 ms
    assert w0 - slack <= est_wall <= w1 + slack, (est_wall - w0, w1 - est_wall)
