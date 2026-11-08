"""Stage 9: does the join stay correct when many agents run at once?

    python -m eval.concurrency --configs 1x1 1x8 1x32 4x8 8x8 8x16

Each config is PROCSxTHREADS: that many OS processes, each running that many threads, every thread running agent tasks
back to back against ONE set of mock services with ONE kernel collector on the machine. All spans of all tasks are joined in a
single correlate() call, then checked against ground truth the agent itself recorded (the real client port of each socket,
`crosstrace.debug.local_port`; the correlator never reads it). A span passes if the connection the join attached to it is the
one it really used and nothing foreign was attached.

REQUIRES passwordless sudo bpftrace (scripts/setup_sudoers.sh). Keep the machine otherwise idle.
"""
import argparse
import concurrent.futures
import multiprocessing
import os
import sys
import tempfile
import threading
import time

from agent.agent import run_agent
from agent.llm_client import LLMClient
from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available
from correlator.integrity import check
from correlator.join import ClockMap, _is_http, correlate
from mockservices.launcher import services

TASK = "What is customer 42's order total and shipping status?"


def worker(spec):
    """One process: `threads` threads, each running `tasks` agent tasks. Returns (spans, error strings)."""
    llm_url, api_url, dns_server, db, threads, tasks = spec
    spans, errors, lock = [], [], threading.Lock()
    barrier = threading.Barrier(threads)

    def run_thread():
        barrier.wait()                       # start together, so threads really overlap
        for _ in range(tasks):
            tr = Tracing()
            try:
                run_agent(TASK, LLMClient(llm_url, tr), Tools(db, api_url, dns_server, tr), tr)
            except Exception as e:           # a failed task is still data
                with lock:
                    errors.append(f"{type(e).__name__}: {str(e)[:120]}")
            got = tr.spans()
            with lock:
                spans.extend(got)

    ts = [threading.Thread(target=run_thread) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return spans, errors


def max_concurrent(spans):
    """Peak number of HTTP spans in flight at once (sweep line)."""
    ev = []
    for s in spans:
        if _is_http(s):
            ev += [(s["start_ns"], 1), (s["end_ns"], -1)]
    cur = peak = 0
    for _, d in sorted(ev):
        cur += d
        peak = max(peak, cur)
    return peak


def describe_missing(http, events, clock_pairs):
    """For spans that ended up with no connect_end: what do the RAW kernel events show about their real client port?
    Categories: was a connect_start attached; was a tcp_send of that port attached; does a raw connect_end exist for that port
    inside the span's window (=> it was lost in the join) or not at all (=> never emitted / never captured)."""
    clock = ClockMap(clock_pairs)
    ends_by_port = {}
    for e in events:
        if e["ev"] == "tcp_connect_end":
            ends_by_port.setdefault(e["sport"], []).append(clock.to_wall(e["t"]))
    cats, example = {}, None
    for e in http:
        if any(a.event["ev"] == "tcp_connect_end" for a in e.kernel):
            continue
        port = e.span["attrs"].get("crosstrace.debug.local_port")
        if port is None:   # the request failed before its socket existed (reset, timeout): nothing to compare against
            cats[("request failed, no ground-truth port",)] = cats.get(("request failed, no ground-truth port",), 0) + 1
            continue
        in_window = any(e.span["start_ns"] - 1_000_000 <= w <= e.span["end_ns"] + 1_000_000 for w in ends_by_port.get(port, []))
        key = ("start attached" if any(a.event["ev"] == "tcp_connect_start" for a in e.kernel) else "no start",
               "send attached" if any(a.event["ev"] == "tcp_send" for a in e.kernel) else "no send",
               "raw end IN window" if in_window else "no raw end for its port in window")
        cats[key] = cats.get(key, 0) + 1
        if example is None:
            a = e.span["attrs"]
            def brief(x):
                return (round((clock.to_wall(x["t"]) - e.span["start_ns"]) / 1e6, 2), x["ev"],
                        {k: v for k, v in x.items() if k in ("sport", "dport", "bytes", "lat_ns")})
            # the span's own attached events, and every raw event of the SAME thread around the span (any connection)
            same_thread = [brief(x) for x in sorted(events, key=lambda x: x["t"])
                           if x.get("pid") == a["process.pid"] and x.get("tid") == a["thread.id"]
                           and e.span["start_ns"] - 30_000_000 <= clock.to_wall(x["t"]) <= e.span["end_ns"] + 30_000_000]
            example = (port, e, {"attached": [brief(x.event) for x in e.kernel], "same thread raw": same_thread[:12]})
    return cats, example


def run_config(procs, threads, tasks):
    with tempfile.TemporaryDirectory() as tmp, services() as svc:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        spec = (svc.llm_url, svc.api_url, svc.dns_server, db, threads, tasks)
        ctx = multiprocessing.get_context("spawn")      # fork after threads exist is unsafe
        with KernelCollector(dns_ports=[svc.dns_server[1]], ports=[svc.api_port, svc.llm_port],
                             reduce_rtt=True, tcp_send=True) as k:
            t0 = time.time()
            with concurrent.futures.ProcessPoolExecutor(procs, mp_context=ctx) as ex:
                results = list(ex.map(worker, [spec] * procs))
            elapsed = time.time() - t0
            time.sleep(0.5)                              # let the last events reach the collector
        spans = [s for r, _ in results for s in r]
        errors = [e for _, es in results for e in es]
        error_kinds = {}
        for e in errors:
            error_kinds[e[:90]] = error_kinds.get(e[:90], 0) + 1
        env = {"api_port": svc.api_port, "llm_port": svc.llm_port, "dns_port": svc.dns_server[1]}
        tl = correlate(spans, k.events, k.clock_sync)
        problems = check(tl, env)
        http = [e for e in tl.entries if _is_http(e.span)]
        wrong = missing = 0
        for e in http:
            truth = e.span["attrs"].get("crosstrace.debug.local_port")
            ports = {a.event["sport"] for a in e.kernel if a.event["ev"] == "tcp_connect_end"}
            if not ports:
                missing += 1
            elif ports != {truth}:
                wrong += 1
        foreign = sum(1 for p in problems if "from a different connection" in p or "does not touch" in p)
        bad_spans = len({p.split(":")[0] for p in problems if ":" in p and p.split(":")[0] in
                         {f"{e.span['name']}@{e.span['attrs']['server.port']}" for e in http}})
        matches = {}
        for e in tl.entries:
            for a in e.kernel:
                matches[a.match] = matches.get(a.match, 0) + 1
        pinned_orphans = sum(1 for ev, _ in tl.orphans if ev["ev"] in
                             ("tcp_connect_start", "tcp_connect_end", "dns_query", "dns_response"))
        return {"procs": procs, "threads": threads, "tasks": procs * threads * tasks, "errors": len(errors),
                "http_spans": len(http), "peak_concurrent": max_concurrent(spans), "seconds": elapsed,
                "connects": sum(1 for e in k.events if e["ev"] == "tcp_connect_end"),
                "events": len(k.events), "wrong_connection": wrong, "no_connection": missing,
                "foreign_events_attached": foreign, "pinned_orphans": pinned_orphans, "matches": matches,
                "problems": len(problems), "warnings": k.warnings, "sample_problems": problems[:3],
                "clock_jump_ms": tl.clock_max_jump_ns / 1e6, "error_kinds": error_kinds,
                "missing": describe_missing(http, k.events, k.clock_sync),
                "raw_starts": sum(1 for e in k.events if e["ev"] == "tcp_connect_start")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", nargs="+", default=["1x1", "1x8", "1x32", "4x8", "8x8", "8x16"])
    ap.add_argument("--total", type=int, default=160, help="approximate tasks per config (spread over the threads)")
    a = ap.parse_args()
    if not available():
        sys.exit("needs passwordless sudo bpftrace: run scripts/setup_sudoers.sh once")
    print(f"{'config':<7}{'tasks':>6}{'http':>6}{'peak':>6}{'task err':>9}{'connects':>9}{'ok spans':>14}"
          f"{'wrong conn':>11}{'no conn':>8}{'foreign':>8}{'orphans':>8}{'s':>6}")
    for cfg in a.configs:
        p, t = (int(x) for x in cfg.split("x"))
        r = run_config(p, t, max(2, a.total // (p * t)))
        ok = r["http_spans"] - r["wrong_connection"] - r["no_connection"]
        print(f"{cfg:<7}{r['tasks']:>6}{r['http_spans']:>6}{r['peak_concurrent']:>6}{r['errors']:>9}{r['connects']:>9}"
              f"{f'{ok}/{r['http_spans']}':>14}{r['wrong_connection']:>11}{r['no_connection']:>8}"
              f"{r['foreign_events_attached']:>8}{r['pinned_orphans']:>8}{r['seconds']:>6.1f}", flush=True)
        if r["problems"] or r["warnings"]:
            print(f"        integrity problems: {r['problems']}; collector warnings: {r['warnings'][:2]}; "
                  f"sample: {r['sample_problems'][:1]}", flush=True)
        cats, example = r["missing"]
        if cats:
            print(f"        raw connect_start events: {r['raw_starts']}, connect_end events: {r['connects']}, "
                  f"spans: {r['http_spans']}", flush=True)
            print(f"        spans without a connection, by what the raw events show: {cats}", flush=True)
            if example:
                print(f"        example: real port {example[0]}, span {example[1].span['name']}; raw events of that port "
                      f"(ms from span start): {example[2]}", flush=True)
        if r["error_kinds"]:
            print(f"        task errors by kind: {r['error_kinds']}", flush=True)
        print(f"        attached events by evidence: {r['matches']}; clock step during run: {r['clock_jump_ms']:.1f} ms",
              flush=True)


if __name__ == "__main__":
    main()
