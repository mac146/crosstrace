"""Run one agent trial with exactly one injected fault (or none) and record it with ground truth."""
import contextlib
import os
import sqlite3
import tempfile
import threading
import time

from agent.agent import run_agent
from agent.llm_client import LLMClient
from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from collectors import store
from collectors.ebpf import KernelCollector
from faultlab.faults import make_plan
from faultlab.netem import Netem
from mockservices.launcher import services

TASK = "What is customer 42's order total and shipping status?"


class DbLock:
    """Hold an exclusive SQLite lock for hold_ms: the agent's query blocks until it is released."""

    def __init__(self, path, hold_ms):
        self.path, self.hold_ms = path, hold_ms
        self._released = threading.Event()

    def __enter__(self):
        # The lock is taken lazily by arm(), which the agent's db_query calls right before it connects. Taking it at
        # run start (the Stage 1-5 behaviour) let it expire during earlier slow steps (e.g. 429 backoff) so slow_db
        # never happened in two-fault runs. For a single slow_db fault the two are equivalent to within a few ms.
        self._timer = None
        self.con = None
        return self

    def arm(self):
        if self.con is not None:
            return                      # already armed: later queries are not delayed again
        self.con = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.con.execute("BEGIN EXCLUSIVE")
        self._timer = threading.Timer(self.hold_ms / 1000, self._release)
        self._timer.start()

    def _release(self):
        if self.con is not None and not self._released.is_set():
            self._released.set()
            self.con.execute("ROLLBACK")
            self.con.close()

    def __exit__(self, *exc):
        if self._timer is not None:
            self._timer.cancel()
        self._release()


def run_one(fault, seed, con, split="eval", kernel=False, magnitude="normal", llm_kind="mock"):
    """Run a trial and save it to the store. Returns the run_id.

    kernel=True also records kernel events with the eBPF collector (needs passwordless sudo bpftrace).
    llm_kind="anthropic" uses the real model (agent/anthropic_llm.py, needs an API key) instead of the mock server;
    the kernel collector then also watches TCP port 443.
    llm_kind="langgraph" runs the LangGraph agent (agent/langgraph_agent.py) against a real OpenAI-compatible
    endpoint, key from .env; the collector watches 443 and also port 53 (libc resolver) for DNS.
    """
    remote = llm_kind in ("anthropic", "langgraph")
    # "langgraph-mock": the same LangGraph agent, pointed at the local mock model server (no key, no internet)
    plan = make_plan(fault, seed, magnitude)
    run_id = f"{split}-{fault}-{seed}"
    status, error = "ok", None
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        svc = stack.enter_context(services(plan.llm_args, plan.api_args, plan.dns_args))
        if plan.netem:
            stack.enter_context(Netem(svc.api_port, **plan.netem))
        collector = None
        if kernel:
            # must start before DbLock: collector startup takes ~1s and would eat the lock window
            collector = stack.enter_context(KernelCollector(
                dns_ports=[svc.dns_server[1]] + ([53] if llm_kind == "langgraph" else []), reduce_rtt=True,
                ports=[svc.api_port, 443 if remote else svc.llm_port]))
        db_hook = None
        if plan.db_lock_ms:
            db_hook = stack.enter_context(DbLock(db, plan.db_lock_ms)).arm
        tracing = Tracing()
        if llm_kind == "anthropic":
            from agent.anthropic_llm import AnthropicLLM   # optional dependency, imported only when asked for
            llm = AnthropicLLM(tracing)
        elif llm_kind in ("langgraph", "langgraph-mock"):
            llm = None
        else:
            llm = LLMClient(svc.llm_url, tracing)
        tools = Tools(db, svc.api_url, svc.dns_server, tracing, plan.slow_extra_ms, db_hook=db_hook)
        t_start = time.time_ns()
        try:
            if llm_kind in ("langgraph", "langgraph-mock"):
                from agent.langgraph_agent import run_langgraph   # optional dependency, imported only when asked for
                endpoint = (svc.llm_url + "/v1", "mock", "mock", 2) if llm_kind == "langgraph-mock" else None
                run_langgraph(TASK, tools, tracing, endpoint=endpoint)
            else:
                run_agent(TASK, llm, tools, tracing)
        except Exception as e:  # a failed run is still data
            status, error = "error", f"{type(e).__name__}: {e}"
        t_end = time.time_ns()
        spans = tracing.spans()
    store.save_run(
        con,
        {"run_id": run_id, "fault_type": fault, "seed": seed, "split": split,
         "started_ns": t_start, "ended_ns": t_end, "status": status, "error": error},
        spans,
        {"fault_type": fault, "target": plan.target, "params": plan.params,
         "start_ns": t_start, "end_ns": t_end})
    if collector is not None:
        env = {"agent_pid": os.getpid(), "api_port": svc.api_port, "llm_port": svc.llm_port,
               "dns_port": svc.dns_server[1], "service_pids": svc.pids, "warnings": collector.warnings}
        store.save_kernel(con, run_id, collector.events, collector.clock_sync, env)
    return run_id
