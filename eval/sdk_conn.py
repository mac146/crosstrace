"""Stage 21: do the OpenAI SDKs reuse their connection when streaming? (predictions: docs/predictions-stage21.md)

    python -m eval.sdk_conn                 # ROOT (compiled collector); runs every arm at tail 5 ms and 0 ms
    python -m eval.sdk_conn --no-kernel     # server-side counts only (no root)

Each arm is one client process making N sequential calls to mockservices/sse_server.py. Connections are counted twice and
independently: by the kernel collector (tcp_connect_end to the server port, from outside the process, no SDK change) and by the
server (distinct client ports of the requests). Node clients need `npm install` in eval/sdk_conn_node (NODE env var = node binary).
"""
import argparse
import contextlib
import json
import os
import statistics
import subprocess
import sys
import time

from mockservices.sse_server import serve_in_thread

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = os.environ.get("NODE", os.path.expanduser("~/.local/node-v22.11.0-linux-x64/bin/node"))
ARMS = {
    "node 7.55.0 stream": [NODE, "client.mjs", "openai-new", "stream"],
    "node 7.4.0 stream": [NODE, "client.mjs", "openai-old", "stream"],
    "node 7.55.0 non-stream": [NODE, "client.mjs", "openai-new", "nonstream"],
    "python 3.46.0 sync stream": [sys.executable, "-m", "eval.sdk_conn_py", "sync-stream"],
    "python 3.46.0 async stream": [sys.executable, "-m", "eval.sdk_conn_py", "async-stream"],
    "python 3.46.0 sync non-stream": [sys.executable, "-m", "eval.sdk_conn_py", "sync-nonstream"],
}


def run_arm(argv, n, port, gap_ms):
    base = f"http://127.0.0.1:{port}/v1"
    cwd = os.path.join(HERE, "sdk_conn_node") if argv[0] == NODE else os.path.dirname(HERE)
    out = subprocess.run(argv + [str(n), base, str(gap_ms)], cwd=cwd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"{argv}: {out.stderr[-800:]}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]


def one(arm, n, tail_ms, gap_ms, kernel):
    srv = serve_in_thread(tail_ms=tail_ms)
    port = srv.server_address[1]
    try:
        with (kernel(port) if kernel else contextlib.nullcontext()) as k:
            calls = run_arm(ARMS[arm], n, port, gap_ms)
            time.sleep(0.3)
    finally:
        srv.shutdown()
        srv.server_close()
    res = {"arm": arm, "tail_ms": tail_ms, "calls": len(calls), "ok_calls": sum(c["chars"] > 0 for c in calls),
           "mean_ms": statistics.fmean(c["ms"] for c in calls[1:]) if len(calls) > 1 else None,
           "server_conns": len({r["port"] for r in srv.log}), "server_requests": len(srv.log)}
    if k is not None:
        conns = [e for e in k.events if e["ev"] == "tcp_connect_end" and e.get("dport") == port]
        res["kernel_conns"] = len(conns)
        kp, sp = {e["sport"] for e in conns}, {r["port"] for r in srv.log}
        res["kernel_ports_match_server"] = kp == sp
        res["kernel_only_ports"], res["server_only_ports"] = sorted(kp - sp), sorted(sp - kp)
        # connections that never carried a request: when were they opened, relative to the first connect?
        t0 = conns[0]["t"] if conns else 0
        res["kernel_only_at_ms"] = [round((e["t"] - t0) / 1e6, 1) for e in conns if e["sport"] in kp - sp]
        res["kernel_warnings"] = list(k.warnings)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--gap-ms", type=int, default=20)
    ap.add_argument("--tails", type=int, nargs="+", default=[5, 0])
    ap.add_argument("--arms", nargs="+", default=list(ARMS))
    ap.add_argument("--no-kernel", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "docs", "data-stage21.json"))
    a = ap.parse_args()
    kernel = None
    if not a.no_kernel:
        from collectors import cebpf
        if not cebpf.available():
            sys.exit("needs passwordless sudo for ctrace (scripts/setup_sudoers.sh), or pass --no-kernel")
        kernel = lambda port: cebpf.CCollector(dns_ports=[], ports=[port])
    rows = []
    for tail in a.tails:
        for arm in a.arms:
            r = one(arm, a.n, tail, a.gap_ms, kernel)
            rows.append(r)
            print(json.dumps(r), file=sys.stderr, flush=True)
    json.dump(rows, open(a.out, "w"), indent=1)
    print(f"\n{'arm':<32}{'tail':>5}{'calls':>7}{'server conns':>14}{'kernel conns':>14}{'conns/call':>12}{'ports agree':>13}{'mean ms':>9}")
    for r in rows:
        kc = r.get("kernel_conns")
        print(f"{r['arm']:<32}{r['tail_ms']:>5}{r['ok_calls']:>7}{r['server_conns']:>14}{kc if kc is not None else '-':>14}"
              f"{(kc if kc is not None else r['server_conns']) / max(r['calls'], 1):>12.2f}{str(r.get('kernel_ports_match_server', '-')):>13}"
              f"{r['mean_ms'] or 0:>9.1f}")


if __name__ == "__main__":
    main()
