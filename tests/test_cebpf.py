"""Compiled collector: binary decoding (no root) and live parity with the bpftrace collector (needs the installed binary)."""
import socket
import struct
import threading
import time

import pytest

from collectors import cebpf, ebpf

IP = lambda s: struct.unpack("<I", socket.inet_aton(s))[0]


def rec(typ, t=1000, sk=0, lat=0, pid=0, tid=0, saddr=0, daddr=0, aux=0, aux2=0, sport=0, dport=0, comm=b"", host=b""):
    return cebpf.REC.pack(t, sk, lat, pid, tid, saddr, daddr, aux, aux2, sport, dport, typ, comm, host)


def test_record_layout_matches_the_c_struct():
    assert cebpf.REC.size == 120 and cebpf.HDR.size == 16


def test_every_event_type_decodes_to_the_bpftrace_schema():
    buf = b"".join([
        rec(cebpf.DNS_QUERY, pid=7, tid=8, daddr=IP("127.0.0.1"), dport=5353, comm=b"python"),
        rec(cebpf.DNS_RESPONSE, pid=7, tid=8, lat=5_000_000, aux=60, comm=b"python"),
        rec(cebpf.CONNECT_START, pid=7, tid=8, daddr=IP("10.0.0.2"), dport=443, sk=99, comm=b"python"),
        rec(cebpf.CONNECT_END, pid=7, tid=8, saddr=IP("10.0.0.1"), sport=40000, daddr=IP("10.0.0.2"), dport=443, lat=2_000, sk=99),
        rec(cebpf.RETRANSMIT, saddr=IP("10.0.0.1"), sport=40000, daddr=IP("10.0.0.2"), dport=443, aux=1),
        rec(cebpf.RTT, saddr=IP("10.0.0.2"), sport=443, daddr=IP("10.0.0.1"), dport=40000, aux=1234, aux2=50),
        rec(cebpf.SEND, pid=7, tid=8, sport=40000, dport=443, aux=120),
        rec(cebpf.LIBC_QUERY, pid=7, tid=8, comm=b"python", host=b"api.example.com"),
        rec(cebpf.LIBC_RESPONSE, pid=7, tid=8, lat=9_000, aux=0xFFFFFFFF, comm=b"python", host=b"api.example.com"),
        rec(cebpf.LIBC_QUERY, pid=7, tid=8, comm=b"python", host=b"127.0.0.1"),     # numeric host: dropped
    ])
    ev = cebpf.decode(buf + b"\x01\x02")                                             # torn tail ignored
    assert [e["ev"] for e in ev] == ["dns_query", "dns_response", "tcp_connect_start", "tcp_connect_end", "tcp_retransmit",
                                     "tcp_rtt", "tcp_send", "dns_query", "dns_response"]
    assert ev[0] == {"ev": "dns_query", "t": 1000, "pid": 7, "tid": 8, "comm": "python", "daddr": "127.0.0.1", "dport": 5353}
    assert ev[2]["daddr"] == "10.0.0.2" and ev[2]["sk"] == 99
    assert ev[3]["saddr"] == "10.0.0.1" and ev[3]["lat_ns"] == 2000
    assert ev[4]["state"] == 1 and ev[5]["srtt_us"] == 1234 and ev[5]["bytes"] == 50 and "pid" not in ev[5]
    assert ev[6]["bytes"] == 120
    assert ev[7]["via"] == "libc" and ev[7]["host"] == "api.example.com"
    assert ev[8]["ret"] == -1 and ev[8]["lat_ns"] == 9000


def test_selection_by_environment(monkeypatch):
    monkeypatch.setenv("CROSSTRACE_COLLECTOR", "c")
    assert isinstance(ebpf.KernelCollector(dns_ports=[1]), cebpf.CCollector)
    monkeypatch.setenv("CROSSTRACE_COLLECTOR", "bpftrace")
    assert isinstance(ebpf.KernelCollector(dns_ports=[1]), ebpf.BpftraceCollector)


def test_arguments_reach_the_loader():
    a = cebpf.CCollector(dns_ports=[5353], ports=[443, 8000], reduce_rtt=True, tcp_send=True, libc_dns=True)._argv()
    assert a[:3] == ["sudo", "-n", cebpf.BINARY]
    assert a.count("--port") == 2 and "--dns-port" in a and "--reduce-rtt" in a and "--tcp-send" in a and "--libc-dns" in a


@pytest.mark.skipif(not cebpf.available(), reason="needs the installed compiled collector (scripts/setup_sudoers.sh)")
def test_live_connection_and_send_events_are_reported_with_our_process():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept()[0].recv(10), daemon=True).start()
    with cebpf.CCollector(dns_ports=[39997], ports=[port], tcp_send=True) as k:
        c = socket.create_connection(("127.0.0.1", port))
        local = c.getsockname()[1]
        c.sendall(b"hello")
        time.sleep(0.4)
        c.close()
    srv.close()
    assert k.warnings == [] and k.lost == 0
    start = [e for e in k.events if e["ev"] == "tcp_connect_start" and e["dport"] == port]
    end = [e for e in k.events if e["ev"] == "tcp_connect_end" and e["dport"] == port]
    send = [e for e in k.events if e["ev"] == "tcp_send" and e["dport"] == port]
    assert len(start) == 1 and len(end) == 1 and len(send) == 1
    assert end[0]["sport"] == local and end[0]["pid"] == start[0]["pid"] > 0 and end[0]["lat_ns"] > 0
    assert end[0]["sk"] == start[0]["sk"] and send[0]["bytes"] == 5 and send[0]["tid"] == start[0]["tid"]
    assert end[0]["daddr"] == "127.0.0.1" and k.clock_sync
