"""Stage 11: the join for async agents, where many requests share ONE pid and ONE thread id.

    python -m eval.async_join --concurrency 1 8 64 --tasks 320

Each task is one agent run (four model calls, as in the other tests) written with asyncio and httpx.AsyncClient against the
mock LLM server. `concurrency` coroutines run at the same time, all on a single thread, so thread id cannot tell their
requests apart. The same recorded spans and kernel events are joined twice:
  A  without `network.local.port` on the spans (the instrumentation does not know its socket)
  B  with it (the instrumentation records the client port of the socket each request used)
and both are checked against the real client port (`crosstrace.debug.local_port`, ground truth the correlator never reads).

REQUIRES passwordless sudo bpftrace (scripts/setup_sudoers.sh).
"""
import argparse
import asyncio
import copy
import os
import sys
import tempfile

import httpx

from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available
from correlator.join import _is_http, correlate
from eval.concurrency import max_concurrent
from mockservices.launcher import services


async def one_task(client, url, port, sem, all_spans):
    async with sem:
        tr = Tracing()
        with tr.span("invoke_agent async", {"gen_ai.operation.name": "invoke_agent"}):
            for k in range(4):
                with tr.span("chat mock", {"gen_ai.operation.name": "chat"}):
                    attrs = {"http.request.method": "POST", "server.address": "127.0.0.1", "server.port": port}
                    with tr.span("POST", attrs, client=True) as span:
                        body = {"model": "mock", "messages": [{"role": "user", "content": "x"}] +
                                [{"role": "tool", "tool_call_id": f"c{j}", "content": "r"} for j in range(k)]}
                        async with client.stream("POST", f"{url}/v1/chat/completions", json=body) as r:
                            sock = r.extensions["network_stream"].get_extra_info("socket")
                            local = sock.getsockname()[1]
                            span.set_attribute("crosstrace.debug.local_port", local)    # ground truth
                            span.set_attribute("network.local.port", local)             # what instrumentation could declare
                            await r.aread()
        all_spans.extend(tr.spans())


async def run_tasks(url, port, tasks, concurrency):
    sem, spans = asyncio.Semaphore(concurrency), []
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=0)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        await asyncio.gather(*(one_task(client, url, port, sem, spans) for _ in range(tasks)))
    return spans


def score(spans, events, pairs):
    tl = correlate(spans, events, pairs)
    http = [e for e in tl.entries if _is_http(e.span)]
    ok = wrong = none = 0
    for e in http:
        truth = e.span["attrs"]["crosstrace.debug.local_port"]
        ports = {a.event["sport"] for a in e.kernel if a.event["ev"] == "tcp_connect_end"}
        starts = sum(a.event["ev"] == "tcp_connect_start" for a in e.kernel)
        if not ports:
            none += 1
        elif ports == {truth} and starts == 1:
            ok += 1
        else:
            wrong += 1
    return len(http), ok, wrong, none


def run_one(tasks, concurrency):
    with tempfile.TemporaryDirectory(), services() as svc:
        with KernelCollector(dns_ports=[svc.dns_server[1]], ports=[svc.llm_port], reduce_rtt=True, tcp_send=True) as k:
            spans = asyncio.run(run_tasks(svc.llm_url, svc.llm_port, tasks, concurrency))
            import time
            time.sleep(0.5)
        threads = {s["attrs"]["thread.id"] for s in spans}
        undeclared = copy.deepcopy(spans)
        for s in undeclared:
            s["attrs"].pop("network.local.port", None)
        return {"threads": len(threads), "peak": max_concurrent(spans), "A": score(undeclared, k.events, k.clock_sync),
                "B": score(spans, k.events, k.clock_sync), "events": len(k.events), "warnings": k.warnings[:2]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", nargs="+", type=int, default=[1, 8, 64])
    ap.add_argument("--tasks", type=int, default=320)
    a = ap.parse_args()
    if not available():
        sys.exit("needs passwordless sudo bpftrace: run scripts/setup_sudoers.sh once")
    print(f"{'coroutines':>10}{'threads':>8}{'peak http':>10}{'http':>6}   {'A: own conn / wrong / none':<28}"
          f"{'B (with network.local.port)':<30}")
    for c in a.concurrency:
        r = run_one(a.tasks, c)
        (n, ao, aw, an), (_, bo, bw, bn) = r["A"], r["B"]
        print(f"{c:>10}{r['threads']:>8}{r['peak']:>10}{n:>6}   {f'{ao} / {aw} / {an}':<28}{f'{bo} / {bw} / {bn}':<30}",
              flush=True)
        if r["warnings"]:
            print("           collector warnings:", r["warnings"], flush=True)


if __name__ == "__main__":
    main()
