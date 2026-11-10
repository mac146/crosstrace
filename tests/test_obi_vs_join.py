"""Stage 20: OBI evidence matching (order vs exact), the own-connection check, and the keep-alive server (no root needed)."""
import http.client
import threading

from eval.obi_vs_join import obi_evidence, own_connection_stats, top2
from mockservices.api_server import make_server

SP = 9000


def span(sid, start, n, lport):
    return {"span_id": sid, "parent_id": None, "name": "GET", "start_ns": start, "end_ns": start + 10,
            "attrs": {"server.port": SP, "url.full": f"http://x:{SP}/shipping/{n}", "crosstrace.debug.local_port": lport}}


def run(spans):
    return {"agents": [spans]}


def test_exact_matching_uses_the_client_port_and_order_matching_does_not():
    # two agents, A (port 40001) and B (port 40002); both call /shipping/1; B's slow call was PRINTED first but started second
    spans = [span("a1", 100, 1, 40001), span("b1", 200, 1, 40002)]
    http = [(SP, "/shipping/1", 800.0, 40002), (SP, "/shipping/1", 3.0, 40001)]            # printed: B (slow) then A (fast)
    stats = [(40002, SP, 500_000), (40001, SP, 30)]                                          # connection lines: B then A
    ev = obi_evidence(run(spans), http, stats)
    assert ev["exact"]["wait"] == {"a1": 3.0, "b1": 800.0}
    assert ev["exact"]["rtt"] == {"a1": 0.03, "b1": 500.0}
    assert ev["order"]["wait"] == {"a1": 800.0, "b1": 3.0}                                   # order pairs the first span with the first line: wrong
    assert ev["order"]["wait_port"] == {"a1": 40002, "b1": 40001}
    assert ev["order"]["rtt_port"] == {"a1": 40002, "b1": 40001}


def test_with_reused_connections_there_are_fewer_connection_lines_than_calls_so_order_matching_gives_no_rtt():
    spans = [span("a1", 100, 1, 40001), span("a2", 200, 2, 40001), span("a3", 300, 3, 40001)]      # one connection, three calls
    http = [(SP, "/shipping/1", 3.0, 40001), (SP, "/shipping/2", 3.0, 40001), (SP, "/shipping/3", 3.0, 40001)]
    stats = [(40001, SP, 30)]
    ev = obi_evidence(run(spans), http, stats)
    assert ev["order"]["rtt"] == {}                                                           # 1 line for 3 calls: nothing assigned
    assert set(ev["exact"]["rtt"]) == {"a1", "a2", "a3"}                                      # exact matching gives each call its connection's line
    assert set(ev["exact"]["wait"]) == {"a1", "a2", "a3"}


class Event:
    def __init__(self, ev):
        self.event = ev


class Entry:
    def __init__(self, port, events):
        self.span = {"attrs": {"server.port": SP, "crosstrace.debug.local_port": port}}
        self.kernel = [Event(e) for e in events]


def test_own_connection_check_flags_a_foreign_connection_and_a_missing_one():
    ok = Entry(40001, [{"ev": "tcp_send", "sport": 40001, "dport": SP}, {"ev": "tcp_rtt", "sport": SP, "dport": 40001}])
    foreign = Entry(40001, [{"ev": "tcp_send", "sport": 40001, "dport": SP}, {"ev": "tcp_rtt", "sport": SP, "dport": 40002}])
    none = Entry(40001, [])
    assert own_connection_stats(ok) == (True, True)
    assert own_connection_stats(foreign) == (True, False)
    assert own_connection_stats(none) == (False, False)


def test_top2_counts_the_truth_in_the_first_two_causes():
    assert top2(["net_latency", "packet_loss"], "net_latency") and top2(["packet_loss", "net_latency"], "net_latency")
    assert not top2(["packet_loss", "slow_api", "net_latency"], "net_latency") and top2(["none"], "none")


def test_keepalive_server_serves_two_requests_on_one_connection():
    srv = make_server(0, keepalive=True)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/shipping/1")
        c.getresponse().read()
        local = c.sock.getsockname()[1]
        c.request("GET", "/shipping/2")
        r = c.getresponse()
        r.read()
        assert r.status == 200 and c.sock.getsockname()[1] == local                    # same socket, second request
        c.close()
    finally:
        srv.shutdown()
        make_server(0, keepalive=False).server_close()                                  # leave the class default as it was
