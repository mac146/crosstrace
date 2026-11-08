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
from faultlab.real_path import REAL_FAULTS, real_path_fault
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
    # Real-network faults (dns_delay / net_latency / packet_loss on the REAL path to the real model server) exist only
    # for llm_kind="langgraph"; the local plan is then the healthy one and the fault is applied by faultlab.real_path.
    parts = fault.split("+")      # "dns_delay+net_latency": two real faults at once (different interfaces)
    real = llm_kind == "langgraph" and all(p in REAL_FAULTS for p in parts)
    if real and (len(parts) > 2 or len(set(parts)) != len(parts) or {"net_latency", "packet_loss"} <= set(parts)):
        raise ValueError(f"unsupported combination {fault!r}: at most two different real faults, and net_latency and "
                         f"packet_loss both need the one qdisc of the default interface")
    plan = make_plan("none" if real else fault, seed, magnitude)
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
                dns_ports=[svc.dns_server[1]], reduce_rtt=True,
                ports=[svc.api_port, 443 if remote else svc.llm_port],
                libc_dns=llm_kind == "langgraph", tcp_send=llm_kind == "langgraph"))
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
        real_state, model_host = {}, None
        if real:
            from agent.langgraph_agent import load_settings
            from urllib.parse import urlparse
            model_host = urlparse(load_settings()[1]).hostname
            for part in parts:
                fault_cm, prm, ips = real_path_fault(part, seed, model_host)
                stack.enter_context(fault_cm)  # applied only now: after the collector is up, removed when the run ends
                real_state[part] = (prm, ips)
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
    truth_params = plan.params
    truth_target = plan.target
    if real:
        from diagnosers.common import COMPONENT
        truth_target = COMPONENT[parts[0]]
        # was each fault really on the path that the run used? (CDN addresses can change between resolve and connect)
        applied = True
        for part, (prm, ips) in real_state.items():
            if part == "dns_delay":
                looked = [e for e in collector.events if e["ev"] == "dns_response" and e.get("via") == "libc"
                          and e.get("host") == model_host] if collector else []
                applied &= bool(looked) and max(e["lat_ns"] for e in looked) / 1e6 >= 0.5 * prm["delay_ms"]
            else:
                connects = [e for e in collector.events if e["ev"] == "tcp_connect_end"
                            and e.get("dport") == 443] if collector else []
                applied &= bool(connects) and all(e["daddr"] in ips for e in connects)
        truth_params = dict(next(iter(real_state.values()))[0]) if len(parts) == 1 else \
            {part: dict(prm) for part, (prm, _) in real_state.items()}
        truth_params["applied"] = applied
    store.save_run(
        con,
        {"run_id": run_id, "fault_type": fault, "seed": seed, "split": split,
         "started_ns": t_start, "ended_ns": t_end, "status": status, "error": error},
        spans,
        {"fault_type": fault, "target": truth_target, "params": truth_params,
         "start_ns": t_start, "end_ns": t_end})
    if collector is not None:
        env = {"agent_pid": os.getpid(), "api_port": svc.api_port, "llm_port": svc.llm_port,
               "dns_port": svc.dns_server[1], "service_pids": svc.pids, "warnings": collector.warnings}
        store.save_kernel(con, run_id, collector.events, collector.clock_sync, env)
    return run_id
