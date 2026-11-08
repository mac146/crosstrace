"""Stage 11: is the client-side "server wait" right for a KNOWN slow server over TLS?

    python -m eval.remote_wait --n 12

The real model belongs to someone else, so a slow remote server cannot be injected there. This stands in for one: the mock LLM
server behind TLS (self-signed certificate, the LangGraph agent as client) answers each request after a known delay. The kernel
sees both ends of a loopback connection, so the server's side is REMOVED before the join: only what a client could see of a
remote server remains (its own sends and what it receives). The wait the correlator derives from that alone is compared with the
injected delay. Every call opens a new TLS connection (the mock closes after each reply), so every call starts with handshake sends.

REQUIRES passwordless sudo bpftrace and `openssl` (scripts/setup_sudoers.sh).
"""
import argparse
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time

from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available
from correlator.join import _client_first_byte_ms, _is_http, breakdown, correlate
from mockservices.launcher import services

TASK = "What is customer 42's order total and shipping status?"


def make_cert(d):
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    return cert, key


def one_run(delay_ms, cert, key, db):
    from agent.langgraph_agent import run_langgraph
    os.environ["SSL_CERT_FILE"] = cert                       # httpx trusts the self-signed certificate
    args = ["--tls-cert", cert, "--tls-key", key] + (["--delay-ms", str(delay_ms)] if delay_ms else [])
    with services(llm_args=args) as svc:
        url = svc.llm_url.replace("http://", "https://")
        tracing = Tracing()
        tools = Tools(db, svc.api_url, svc.dns_server, tracing)
        with KernelCollector(dns_ports=[svc.dns_server[1]], ports=[svc.llm_port], reduce_rtt=True, tcp_send=True) as k:
            run_langgraph(TASK, tools, tracing, endpoint=(url + "/v1", "mock", "mock", 0))
            time.sleep(0.3)
        # what a client of a REMOTE server could see: drop the server's own events (it sends from, and receives on, its port)
        client_only = [e for e in k.events
                       if not ((e["ev"] in ("tcp_send", "tcp_rtt", "tcp_retransmit") and e.get("sport") == svc.llm_port))]
        tl = correlate(tracing.spans(), client_only, k.clock_sync)
        model = [e for e in tl.entries if _is_http(e.span) and e.span["attrs"]["server.port"] == svc.llm_port]
        out = []
        for e in model:
            b = breakdown(e)
            b["first_byte_ms"] = _client_first_byte_ms([a.event for a in e.kernel], svc.llm_port)   # the superseded definition
            out.append(b)
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12, help="runs per condition (4 model calls each)")
    ap.add_argument("--seed", type=int, default=1100, help="seed of the injected delays")
    a = ap.parse_args()
    if not available():
        sys.exit("needs passwordless sudo bpftrace: run scripts/setup_sudoers.sh once")
    rng = random.Random(a.seed)
    with tempfile.TemporaryDirectory() as d:
        cert, key = make_cert(d)
        db = os.path.join(d, "app.db")
        seed_db(db)
        healthy, slow = [], []
        for _ in range(a.n):
            healthy += [(0, b) for b in one_run(0, cert, key, db)]
        for _ in range(a.n):
            delay = rng.randint(200, 600)
            slow += [(delay, b) for b in one_run(delay, cert, key, db)]
    h = [b["server_wait_ms"] for _, b in healthy if b["server_wait_ms"] is not None]
    print(f"healthy calls: {len(healthy)}, wait defined on {len(h)}; wait ms median {statistics.median(h):.1f}, "
          f"max {max(h):.1f}   (prediction: under 30 ms)")
    old_ok = sum(1 for delay, b in slow if b["first_byte_ms"] is not None
                 and abs(b["first_byte_ms"] - delay) <= max(0.10 * delay, 30.0))
    errs, undefined = [], 0
    for delay, b in slow:
        w = b["server_wait_ms"]
        if w is None:
            undefined += 1
            continue
        errs.append((w - delay, abs(w - delay) <= max(0.10 * delay, 30.0), delay, w))
    ok = sum(1 for _, good, _, _ in errs if good)
    print(f"slow calls: {len(slow)}, wait defined on {len(errs)} (undefined {undefined}); "
          f"within max(10%, 30 ms) of the injected delay: {ok}/{len(slow)} = {100 * ok / len(slow):.0f}%   "
          f"[superseded 'first payload byte' definition on the same calls: {old_ok}/{len(slow)}]")
    diffs = [e for e, _, _, _ in errs]
    print(f"error (measured - injected) ms: median {statistics.median(diffs):+.1f}, min {min(diffs):+.1f}, max {max(diffs):+.1f}")
    for e, good, delay, w in sorted(errs, key=lambda x: -abs(x[0]))[:4]:
        print(f"   worst: injected {delay} ms, measured {w:.0f} ms ({e:+.0f}){'' if good else '  <- outside tolerance'}")


if __name__ == "__main__":
    main()
