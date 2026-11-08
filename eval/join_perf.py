"""How does the correlator's run time scale with the number of spans and events?

    python -m eval.join_perf --sizes 200 800 3200 12800

Synthetic runs with a known right answer: each task is one HTTP span on its own thread (64 threads interleaved) with its own
client port, and the kernel events a real run produces (connect start/end, a send, RTT samples both ways). Ports are reused
after 25,000 tasks. Reports the time of one correlate() call and checks that every span got exactly its own connection.
No root needed.
"""
import argparse
import time

from correlator.join import _is_http, correlate

MS = 1_000_000
PAIRS = [(0, 0, 0)]
SERVER = 8000


def synth(tasks, threads=64):
    spans, events = [], []
    for i in range(tasks):
        tid = 1000 + (i % threads)
        t0 = (i // threads) * 20.0 + (i % threads) * 0.05            # ms; threads overlap in time
        port = 20_000 + (i % 25_000)
        spans.append({"span_id": f"s{i}", "parent_id": None, "name": "POST", "start_ns": int(t0 * MS),
                      "end_ns": int((t0 + 10) * MS),
                      "attrs": {"process.pid": 1, "thread.id": tid, "server.port": SERVER,
                                "server.address": "127.0.0.1", "truth": port}})
        events += [
            {"ev": "tcp_connect_start", "t": int((t0 + 1) * MS), "pid": 1, "tid": tid, "dport": SERVER},
            {"ev": "tcp_connect_end", "t": int((t0 + 2) * MS), "pid": 1, "tid": tid, "sport": port, "dport": SERVER,
             "lat_ns": MS},
            {"ev": "tcp_send", "t": int((t0 + 3) * MS), "pid": 1, "tid": tid, "sport": port, "dport": SERVER, "bytes": 500},
            {"ev": "tcp_rtt", "t": int((t0 + 4) * MS), "sport": SERVER, "dport": port, "bytes": 500, "srtt_us": 40},
            {"ev": "tcp_rtt", "t": int((t0 + 8) * MS), "sport": port, "dport": SERVER, "bytes": 300, "srtt_us": 40},
        ]
    return spans, events


def measure(tasks):
    spans, events = synth(tasks)
    t = time.perf_counter()
    tl = correlate(spans, events, PAIRS)
    seconds = time.perf_counter() - t
    wrong = 0
    for e in tl.entries:
        if _is_http(e.span):
            ports = [a.event["sport"] for a in e.kernel if a.event["ev"] == "tcp_connect_end"]
            wrong += ports != [e.span["attrs"]["truth"]]
    return len(spans), len(events), seconds, wrong, len(tl.orphans)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="+", type=int, default=[200, 800, 3200, 12800])
    a = ap.parse_args()
    print(f"{'tasks':>7}{'spans':>8}{'events':>9}{'seconds':>10}{'us/event':>10}{'wrong':>7}{'orphans':>9}")
    for n in a.sizes:
        spans, events, sec, wrong, orph = measure(n)
        print(f"{n:>7}{spans:>8}{events:>9}{sec:>10.2f}{1e6 * sec / events:>10.1f}{wrong:>7}{orph:>9}", flush=True)


if __name__ == "__main__":
    main()
