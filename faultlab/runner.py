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
        self.con = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.con.execute("BEGIN EXCLUSIVE")
        self._timer = threading.Timer(self.hold_ms / 1000, self._release)
        self._timer.start()
        return self

    def _release(self):
        if not self._released.is_set():
            self._released.set()
            self.con.execute("ROLLBACK")
            self.con.close()

    def __exit__(self, *exc):
        self._timer.cancel()
        self._release()


def run_one(fault, seed, con, split="eval", kernel=False):
    """Run a trial and save it to the store. Returns the run_id.

    kernel=True also records kernel events with the eBPF collector (needs passwordless sudo bpftrace).
    """
    plan = make_plan(fault, seed)
    run_id = f"{split}-{fault}-{seed}"
    status, error = "ok", None
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        svc = stack.enter_context(services(plan.llm_args, (), plan.dns_args))
        if plan.netem:
            stack.enter_context(Netem(svc.api_port, **plan.netem))
        collector = None
        if kernel:
            # must start before DbLock: collector startup takes ~1s and would eat the lock window
            collector = stack.enter_context(KernelCollector(dns_ports=[svc.dns_server[1]]))
        if plan.db_lock_ms:
            stack.enter_context(DbLock(db, plan.db_lock_ms))
        tracing = Tracing()
        llm = LLMClient(svc.llm_url, tracing)
        tools = Tools(db, svc.api_url, svc.dns_server, tracing, plan.slow_extra_ms)
        t_start = time.time_ns()
        try:
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
