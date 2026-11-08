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
from correlator.join import _is_http, correlate
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
                "clock_jump_ms": tl.clock_max_jump_ns / 1e6}


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
        print(f"        attached events by evidence: {r['matches']}; clock step during run: {r['clock_jump_ms']:.1f} ms",
              flush=True)


if __name__ == "__main__":
    main()
