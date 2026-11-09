"""Stage 18: many concurrent REAL agents, a real model, a real network path (predictions + stop rules: docs/predictions-stage18.md).

    CROSSTRACE_ENV_FILE=/path/to/.env python -m eval.real_conc            # record (ROOT) and evaluate
    python -m eval.real_conc --no-record                                  # re-score recorded runs

C agents (LangGraph + the real model, threads of one process, started together) share one real-path fault per run: none, dns_delay or
net_latency (faultlab.real_path, the Stage 8/10 injectors). Arms: spans only, v5 joined (per-agent evidence) and v5 on host-wide
evidence (pooled over all agents of the run). STOP RULES (from the pre-registration): stop at the first sign of an account problem,
when more than 25% of agent-runs fail in two consecutive runs, or when a fault cannot be removed cleanly. Errors are scored, never dropped.
!!! REQUIRES ROOT (passwordless sudo tc, bpftrace or the compiled collector) and TEMPORARILY changes network settings: dev WSL only. !!!
"""
import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available as bpf_available
from correlator.join import _is_http, breakdown
from diagnosers import baseline, combined
from diagnosers.profile import build_profile, kernel_profile_from_timelines
from eval.metrics import wilson
from eval.selective_conc import views
from faultlab.netem import available as tc_available
from faultlab.real_path import real_path_fault
from mockservices.launcher import services

TASK = ("What is customer 42's order total and shipping status?")
ARMS = ["base", "v5", "v5_pooled"]
ACCOUNT_TROUBLE = re.compile(r"\b(402|insufficient|credit|billing|quota|payment|account)\b", re.I)
SEED0 = 18_000


def one_run(fault, seed, n_agents, db, token_cap):
    import agent.langgraph_agent as lg
    lg.TOKEN_CAP = token_cap                           # Stage 18 only: about 1,300 calls need more than the default per-process cap
    from agent.langgraph_agent import load_settings, run_langgraph
    key, base_url, _ = load_settings()
    model_host = urlparse(base_url).hostname
    with services() as svc:
        tracers = [Tracing() for _ in range(n_agents)]
        errors, start = [None] * n_agents, threading.Barrier(n_agents + 1)

        def agent(i):
            tools = Tools(db, svc.api_url, svc.dns_server, tracers[i])
            try:
                start.wait(timeout=120)
                run_langgraph(TASK, tools, tracers[i])
            except Exception as e:                      # a failed agent is data, not a reason to drop the run
                errors[i] = f"{type(e).__name__}: {e}"[:300]

        with KernelCollector(dns_ports=[svc.dns_server[1]], reduce_rtt=True, ports=[svc.api_port, 443],
                             libc_dns=True, tcp_send=True) as k:
            fault_cm, params, ips = (real_path_fault(fault, seed, model_host) if fault != "none" else (None, {}, []))
            threads = [threading.Thread(target=agent, args=(i,)) for i in range(n_agents)]
            t0 = time.time()
            if fault_cm is not None:
                fault_cm.__enter__()                    # applied only now: after the collector is up, always removed below
            try:
                for t in threads:
                    t.start()
                start.wait(timeout=120)
                for t in threads:
                    t.join(timeout=400)
            finally:
                if fault_cm is not None:
                    fault_cm.__exit__(None, None, None)
            time.sleep(0.5)
        applied = True
        if fault == "dns_delay":
            looked = [e for e in k.events if e["ev"] == "dns_response" and e.get("via") == "libc" and e.get("host") == model_host]
            applied = bool(looked) and max(e["lat_ns"] for e in looked) / 1e6 >= 0.5 * params["delay_ms"]
        elif fault == "net_latency":
            conns = [e for e in k.events if e["ev"] == "tcp_connect_end" and e.get("dport") == 443]
            applied = bool(conns) and all(e["daddr"] in ips for e in conns)
        params = {**params, "applied": applied}
        return {"fault": fault, "params": params, "agents": [t.spans() for t in tracers], "errors": errors, "events": k.events,
                "sync": [list(c) for c in k.clock_sync], "warnings": k.warnings, "seconds": round(time.time() - t0, 1)}


def record(path, plan, n_agents, token_cap):
    from agent.langgraph_agent import TOTAL
    data = json.load(open(path)) if os.path.exists(path) else {}
    bad_streak = 0
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        for role, fault, n in plan:
            for j in range(n):
                rid = f"{role}-{fault}-{j}"
                if rid in data:
                    continue
                r = one_run(fault, SEED0 + {'none': 0, 'dns_delay': 1000, 'net_latency': 2000}[fault] + (500 if role == 'cal' else 0) + j, n_agents, db, token_cap)
                r["role"] = role
                data[rid] = r
                failed = sum(1 for e in r["errors"] if e) / n_agents
                print(f"  {rid}: {r['seconds']} s, agents failed {sum(1 for e in r['errors'] if e)}/{n_agents}, applied={r['params'].get('applied')}",
                      file=sys.stderr, flush=True)
                json.dump({**data, "_usage": dict(TOTAL)}, open(path, "w"))
                trouble = [e for e in r["errors"] if e and ACCOUNT_TROUBLE.search(e)]
                if trouble:
                    print(f"STOP RULE: possible account problem: {trouble[0][:160]}", file=sys.stderr)
                    return data, "account problem"
                bad_streak = bad_streak + 1 if failed > 0.25 else 0
                if bad_streak >= 2:
                    print("STOP RULE: more than 25% of agent-runs failed in two consecutive runs", file=sys.stderr)
                    return data, "failure rate"
    return data, None


def evaluate(data):
    runs = {k: v for k, v in data.items() if not k.startswith("_")}
    cal = [r for r in runs.values() if r["role"] == "cal"]
    cal_spans, cal_views, retx = [], [], []
    for r in cal:
        tl, all_http, per_agent = views(r)
        for spans, v, err in zip(r["agents"], per_agent, r["errors"]):
            if err or not spans:
                continue                              # calibration uses completed healthy agent-runs only
            cal_spans.append(spans)
            cal_views.append(v)
    profile = build_profile(cal_spans, "coarse")
    kprofile = kernel_profile_from_timelines(cal_views, "coarse")
    out = {}
    for fault in ("none", "dns_delay", "net_latency"):
        rows = {a: [0, 0] for a in ARMS}
        info = {"runs": 0, "agent_runs": 0, "errored": 0, "excluded": 0, "model_spans": 0, "with_evidence": 0, "warn": 0,
                "http_errors": 0, "seconds": []}
        for rid, r in runs.items():
            if r["role"] != "eval" or r["fault"] != fault:
                continue
            if r["params"].get("applied") is False:
                info["excluded"] += 1
                continue
            tl, all_http, per_agent = views(r)
            run_view = type("V", (), {"entries": [e for e in tl.entries]})()
            info["runs"] += 1
            info["warn"] += bool(r["warnings"])
            info["seconds"].append(r["seconds"])
            model = [e for e in all_http if e.span["attrs"]["server.port"] == 443]
            info["model_spans"] += len(model)
            info["with_evidence"] += sum(breakdown(e)["n_events"] > 0 for e in model)
            info["http_errors"] += sum(1 for e in model if e.span["attrs"].get("http.response.status_code") not in (200, None))
            for n, (spans, view, err) in enumerate(zip(r["agents"], per_agent, r["errors"])):
                info["agent_runs"] += 1
                info["errored"] += bool(err)
                if not spans or not any(s["attrs"].get("gen_ai.operation.name") == "invoke_agent" for s in spans):
                    ranked = {a: ["none"] for a in ARMS}   # an agent that produced no run span has nothing to diagnose
                else:
                    ranked = {"base": [c.cause for c in baseline.diagnose(spans, profile, tie_seed=n,
                                                                          api_causes=("dns_delay", "net_latency", "packet_loss", "slow_api"))],
                              "v5": [c.cause for c in combined.diagnose(view, profile, kprofile, tie_seed=n, loss_explains_rtt=True, slow_api=True,
                                                                        latent_baseline=True, run_level=True)],
                              "v5_pooled": [c.cause for c in combined.diagnose(view, profile, kprofile, tie_seed=n, loss_explains_rtt=True,
                                                                               slow_api=True, latent_baseline=True, run_level=True,
                                                                               source="pooled", pool_entries=all_http, run_view=run_view)]}
                for a in ARMS:
                    ok = ranked[a][0] == fault
                    rows[a][0] += ok
                    rows[a][1] += 1
        out[fault] = (rows, info)
    return out, profile, kprofile, len(cal_spans)


def report(out, profile, kprofile, n_cal, data, stop):
    def cell(k, n):
        lo, hi = wilson(k, n) if n else (0, 0)
        return f"{k}/{n} = {100 * k / n:4.0f}% [{100 * lo:.0f}-{100 * hi:.0f}]" if n else "-"
    print(f"calibration: {n_cal} completed healthy agent-runs; http_api step threshold {profile.get('tool:http_api', 0):.0f} ms, chat {profile.get('chat', 0):.0f} ms; "
          f"retx_per_run {kprofile.get('retx_per_run')}; model-class thresholds {kprofile.get('classes')}")
    print(f"{'fault':<14}{'runs':>5}{'agent-runs':>11}{'errored':>9}{'excluded runs':>14}   " + "".join(f"{a:<24}" for a in ARMS))
    for fault, (rows, info) in out.items():
        label = "healthy: false alarms" if fault == "none" else "top-1"
        cells = []
        for a in ARMS:
            k, n = rows[a]
            cells.append(f"{(n - k) if fault == 'none' else k}/{n}" + ("" if fault == "none" else f" = {100 * k / max(n, 1):.0f}% [{100 * wilson(k, n)[0]:.0f}-{100 * wilson(k, n)[1]:.0f}]"))
        print(f"{fault:<14}{info['runs']:>5}{info['agent_runs']:>11}{info['errored']:>9}{info['excluded']:>14}   " + "".join(f"{c:<24}" for c in cells)
              + f"  ({label})")
    tot_spans = sum(i["model_spans"] for _, i in out.values())
    tot_ev = sum(i["with_evidence"] for _, i in out.values())
    http_err = sum(i["http_errors"] for _, i in out.values())
    warn = sum(i["warn"] for _, i in out.values())
    print(f"\nmodel-call spans with kernel evidence: {tot_ev}/{tot_spans} ({100 * tot_ev / max(tot_spans, 1):.0f}%); model-call HTTP errors: {http_err}; "
          f"runs with collector warnings: {warn}")
    usage = data.get("_usage", {})
    cost = usage.get("input_tokens", 0) * 0.034 / 1e6 + usage.get("output_tokens", 0) * 0.083 / 1e6
    print(f"API use: {usage.get('calls', 0)} calls, {usage.get('input_tokens', 0)} input + {usage.get('output_tokens', 0)} output tokens, about ${cost:.4f}")
    if stop:
        print(f"STOPPED EARLY by a stop rule: {stop}")
    print("Agents of one run share a path fault and a schedule, so agent-runs are not independent: the intervals are optimistic.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/real_conc.json"))
    ap.add_argument("--agents", type=int, default=8)
    ap.add_argument("--cal", type=int, default=8)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--token-cap", type=int, default=1_000_000)
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args()
    stop = None
    if not a.no_record:
        if not bpf_available() or not tc_available():
            sys.exit("needs passwordless sudo bpftrace/ctrace and tc: run scripts/setup_sudoers.sh once")
        from agent.langgraph_agent import load_settings
        key, base_url, model = load_settings()
        if not key:
            sys.exit("no API key: set CROSSTRACE_ENV_FILE (read in place, never copied) or put OPENROUTER_API_KEY in .env")
        print(f"model endpoint: {base_url}  model: {model}", file=sys.stderr)
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        plan = [("cal", "none", a.cal), ("eval", "none", a.n), ("eval", "dns_delay", a.n), ("eval", "net_latency", a.n)]
        data, stop = record(a.out, plan, a.agents, a.token_cap)
        from agent.langgraph_agent import TOTAL
        data["_usage"] = dict(TOTAL)
    else:
        data = json.load(open(a.out))
    out, profile, kprofile, n_cal = evaluate(data)
    report(out, profile, kprofile, n_cal, data, stop)


if __name__ == "__main__":
    main()
