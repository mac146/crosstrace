"""Stage 17: async agents on ONE thread, without a declared port (predictions: docs/predictions-stage17.md).

    python -m eval.async_conc                  # record (ROOT) and evaluate
    python -m eval.async_conc --no-record

Same scenario as Stage 16 (agent kinds H healthy / N latency on its source address / S slow server for its source address, lockstep, one API
server) but every agent is a coroutine of ONE asyncio loop in ONE thread, so all spans carry the same process id and thread id. Real OBI
runs next to our collector (as in Stages 15-16). Arms: base; v3_asis (our join, no declared port); v3_declared (the span declares its local port:
positive control); v3_assign (correlator.assign: ports found by assignment on start time and duration, nothing declared); pool (host-wide);
obi_flows (OBI evidence matched by order); obi_port (OBI evidence matched exactly by the declared port: positive control).
REQUIRES passwordless sudo for bpftrace/ctrace/tc/obi (scripts/setup_sudoers.sh).
"""
import argparse
import asyncio
import contextlib
import json
import os
import random
import sys
import tempfile
import time
from copy import deepcopy

from agent.tools import seed_db
from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available as bpf_available
from correlator.assign import declare_ports_by_assignment
from correlator.join import _is_http, correlate
from diagnosers import baseline, combined
from diagnosers.profile import _threshold, build_profile, kernel_profile_from_timelines
from eval.metrics import wilson
from eval.obi_conc import KIND_IP, TRUTH, http_spans, match_evidence, parse_http
from eval.selective import EXT4
from eval.selective_obi import LOG, OBI, Obi, parse_stats
from faultlab.netem import Netem, available as tc_available
from mockservices.launcher import services

ARMS = ["base", "v3_asis", "v3_declared", "v3_assign", "pool", "obi_flows", "obi_port"]
LABEL = {"base": "spans only", "v3_asis": "v3, no declared port", "v3_declared": "v3 + declared port", "v3_assign": "v3 + assignment (new)",
         "pool": "host-wide", "obi_flows": "OBI, order matching", "obi_port": "OBI, exact port"}


async def call(tracing, kind, n, port):
    """One HTTP GET from an asyncio client bound to the agent kind's source address; its span declares the local port (as network.local.port
    AND as the validation attribute) which the arms that must not see it strip afterwards."""
    attrs = {"http.request.method": "GET", "server.address": "api.mock.local", "server.port": port,
             "url.full": f"http://api.mock.local:{port}/shipping/{n}"}
    with tracing.span("execute_tool http_api", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "http_api"}):
        with tracing.span("GET", attrs, client=True) as span:
            ip = KIND_IP[kind]
            reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(ip, 0) if ip else None)
            lp = writer.get_extra_info("sockname")[1]
            span.set_attribute("network.local.port", lp)
            span.set_attribute("crosstrace.debug.local_port", lp)
            writer.write(f"GET /shipping/{n} HTTP/1.1\r\nHost: api.mock.local\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            head = await reader.readline()
            await reader.read()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            span.set_attribute("http.response.status_code", int(head.split()[1]) if head else 0)


async def run_agents(kinds, port):
    n = len(kinds)
    start, end = asyncio.Barrier(n), asyncio.Barrier(n)
    tracers = [Tracing() for _ in kinds]
    errors = []

    async def agent(tracing, kind):
        try:
            with tracing.span("invoke_agent crosstrace-agent", {"gen_ai.operation.name": "invoke_agent",
                                                               "gen_ai.agent.name": "crosstrace-agent"}):
                for i in range(1, 5):
                    await start.wait()
                    try:
                        await call(tracing, kind, i, port)
                    finally:
                        await end.wait()
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            start.abort() if hasattr(start, "abort") else None

    await asyncio.gather(*[agent(t, k) for t, k in zip(tracers, kinds)])
    return tracers, errors


def one_run(n_agents, mixed, seed, obi):
    rng = random.Random(f"async:{n_agents}:{seed}")
    slow_ms, lat_ms = rng.randint(300, 1000), rng.randint(75, 250)
    api_args = ("--delay-ms", str(slow_ms), "--slow-from", KIND_IP["S"]) if mixed else ()
    kinds = [("HNS"[i % 3] if mixed else "H") for i in range(n_agents)]
    with services(api_args=api_args) as svc:
        with KernelCollector(dns_ports=[svc.dns_server[1]], ports=[svc.api_port], reduce_rtt=True) as k:
            obi.wait_attached([svc.pids["api"]])
            obi.warm_up([svc.api_port])
            with contextlib.ExitStack() as stack:
                if mixed:
                    stack.enter_context(Netem(None, delay_ms=lat_ms, addr=KIND_IP["N"]))
                tracers, errors = asyncio.run(run_agents(kinds, svc.api_port))
                if mixed:
                    time.sleep(2 * lat_ms / 1000 + 0.1)        # let netem's queue drain before the qdisc goes (Stage 14 finding)
            time.sleep(0.2)
        return {"mixed": mixed, "kinds": kinds, "agents": [t.spans() for t in tracers], "events": k.events,
                "sync": [list(c) for c in k.clock_sync], "warnings": k.warnings, "errors": errors, "ports": [svc.api_port],
                "inject": {"slow_ms": slow_ms, "lat_ms": lat_ms}}


def record(path, plan):
    data = json.load(open(path)) if os.path.exists(path) else {}
    with tempfile.TemporaryDirectory():
        with Obi() as obi:
            for c, role, mixed, n in plan:
                for j in range(n):
                    rid = f"{role}-c{c}-{j}"
                    if rid in data:
                        continue
                    data[rid] = {"c": c, "role": role, **one_run(c, mixed, f"{role}{j}", obi)}
                    time.sleep(0.5)
                    print(f"  {rid}", file=sys.stderr, flush=True)
    data["_http"] = [list(x) for x in parse_http(open(LOG).read())]
    data["_stats"] = [list(x) for x in parse_stats(open(LOG).read())]
    json.dump(data, open(path, "w"))
    return data


# ---- evaluation ----------------------------------------------------------------------------------------------------------------
def strip_declared(spans):
    out = deepcopy(spans)
    for s in out:
        s["attrs"].pop("network.local.port", None)
    return out


def join_views(r, mode):
    """(timeline, all HTTP entries, per-agent views) for one run; mode: asis | declared | assign."""
    all_spans = [s for a in r["agents"] for s in a]
    info = None
    if mode == "asis":
        all_spans = strip_declared(all_spans)
    elif mode == "assign":
        all_spans, assigned, considered = declare_ports_by_assignment(strip_declared(all_spans), r["events"],
                                                                     [tuple(c) for c in r["sync"]])
        info = (assigned, considered)
    tl = correlate(all_spans, r["events"], [tuple(c) for c in r["sync"]])
    per_agent = []
    for a in r["agents"]:
        ids = {s["span_id"] for s in a}
        per_agent.append(type("V", (), {"entries": [e for e in tl.entries if e.span["span_id"] in ids], "orphans": []})())
    return tl, [e for e in tl.entries if _is_http(e.span)], per_agent, info


def assignment_accuracy(r):
    """How often the assignment found each call's TRUE connection (the declared port is the truth here), over the HTTP spans."""
    spans = [s for a in r["agents"] for s in a]
    fixed, _, _ = declare_ports_by_assignment(strip_declared(spans), r["events"], [tuple(c) for c in r["sync"]])
    truth = {s["span_id"]: s["attrs"].get("network.local.port") for s in spans if _is_http(s)}
    ok = sum(1 for s in fixed if _is_http(s) and s["attrs"].get("network.local.port") == truth[s["span_id"]])
    return ok, len(truth)


def evaluate(data):
    http = [tuple(x) for x in data.get("_http", [])]
    stats = [tuple(x) for x in data.get("_stats", [])]
    runs_all = {k: v for k, v in data.items() if not k.startswith("_")}
    evidence = match_evidence(runs_all, http, stats) if http else {}
    out = {}
    for c in sorted({r["c"] for r in runs_all.values()}):
        runs = {k: v for k, v in runs_all.items() if v["c"] == c}
        cal = {k: r for k, r in runs.items() if r["role"] == "cal"}
        cal_spans = [s for r in cal.values() for s in r["agents"]]
        profile = build_profile(cal_spans, "coarse")
        kp = {}
        for mode in ("asis", "declared", "assign"):
            kp[mode] = kernel_profile_from_timelines([v for r in cal.values() for v in join_views(r, mode)[2]], "coarse")
        kp_obi = dict(kp["declared"])
        hw = [v for rid in cal for v in evidence.get(rid, {}).get("port", ({}, {}))[0].values()]
        hr = [v for rid in cal for v in evidence.get(rid, {}).get("port", ({}, {}))[1].values()]
        if hw:
            kp_obi["server_wait_ms"] = _threshold(hw, "coarse")
        if hr:
            kp_obi["srtt_ms"] = _threshold(hr, "coarse")
        res = {a: {k: [0, 0] for k in ("all", "H", "N", "S")} for a in ARMS}
        confusion = {a: {} for a in ARMS}
        info = {"agent_runs": 0, "warn": 0, "err": 0, "assign_ok": [0, 0], "obi_pairs": [0, 0], "thr": (profile.get("tool:http_api", 0), kp)}
        for rid, r in runs.items():
            if r["role"] != "mixed":
                continue
            info["warn"] += bool(r["warnings"])
            info["err"] += len(r["errors"])
            ok_, tot_ = assignment_accuracy(r)
            info["assign_ok"][0] += ok_
            info["assign_ok"][1] += tot_
            views = {m: join_views(r, m) for m in ("asis", "declared", "assign")}
            ev = evidence.get(rid)
            if ev:
                ids = {sp["span_id"] for _, sp in http_spans(r)}
                info["obi_pairs"][0] += len(ids & set(ev["port"][0])) + len(ids & set(ev["port"][1]))
                info["obi_pairs"][1] += 2 * len(ids)
            for n, kind in enumerate(r["kinds"]):
                spans = r["agents"][n]
                ranked = {"base": [x.cause for x in baseline.diagnose(spans, profile, tie_seed=n, api_causes=EXT4)]}
                for arm, mode in (("v3_asis", "asis"), ("v3_declared", "declared"), ("v3_assign", "assign")):
                    tl, all_http, per_agent, _ = views[mode]
                    ranked[arm] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp[mode], tie_seed=n, loss_explains_rtt=True,
                                                                      slow_api=True)]
                tl, all_http, per_agent, _ = views["declared"]
                ranked["pool"] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp["declared"], tie_seed=n, loss_explains_rtt=True,
                                                                     slow_api=True, source="pooled", pool_entries=all_http)]
                for arm, method in (("obi_flows", "order"), ("obi_port", "port")):
                    if ev:
                        w, rr = ev[method]
                        ranked[arm] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp_obi, tie_seed=n, loss_explains_rtt=True,
                                                                          slow_api=True, source="obi_real_flows", pool_entries=all_http,
                                                                          obi_waits=w, obi_srtts=rr)]
                    else:
                        ranked[arm] = ["none"]
                info["agent_runs"] += 1
                for arm in ARMS:
                    got = ranked[arm][0]
                    ok = got == TRUTH[kind]
                    for key in ("all", kind):
                        res[arm][key][0] += ok
                        res[arm][key][1] += 1
                    if not ok:
                        confusion[arm][(kind, got)] = confusion[arm].get((kind, got), 0) + 1
        out[c] = (res, confusion, info)
    return out


def report(out):
    def cell(k, n):
        lo, hi = wilson(k, n) if n else (0, 0)
        return f"{100 * k / n:5.1f}% [{100 * lo:3.0f}-{100 * hi:3.0f}]" if n else "-"
    for c, (res, confusion, info) in out.items():
        thr, kp = info["thr"]
        a_ok, a_tot = info["assign_ok"]
        o_ok, o_tot = info["obi_pairs"]
        print(f"\n=== {c} coroutines on ONE thread: {info['agent_runs']} agent-runs; crashed agents {info['err']}; runs with collector warnings "
              f"{info['warn']}; assignment found the true connection for {a_ok}/{a_tot} calls ({100 * a_ok / max(a_tot, 1):.1f}%); "
              f"OBI evidence for {o_ok}/{o_tot} (span, evidence) pairs")
        print(f"{'arm':<26}{'overall':<20}{'H (none)':<20}{'N (net_latency)':<20}{'S (slow_api)':<20}")
        for arm in ARMS:
            print(f"{LABEL[arm]:<26}" + "".join(f"{cell(*res[arm][k]):<20}" for k in ("all", "H", "N", "S")))
        errs = confusion["v3_assign"]
        if errs:
            total = sum(errs.values())
            ns = errs.get(("N", "slow_api"), 0) + errs.get(("S", "net_latency"), 0)
            print(f"   v3 + assignment errors: {total}; N<->S confusions: {ns} ({100 * ns / total:.0f}%); all: " +
                  ", ".join(f"{k[0]}->{k[1]}: {v}" for k, v in sorted(errs.items(), key=lambda x: -x[1])))
    print("\nAgents of one run share a server and a lockstep schedule, so agent-runs are not independent: the intervals are optimistic.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/async_conc.json"))
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--agents", type=int, nargs="+", default=[9, 24])
    ap.add_argument("--mixed-runs", type=int, nargs="+", default=[12, 8])
    ap.add_argument("--cal", type=int, default=8)
    a = ap.parse_args()
    if not a.no_record:
        if not bpf_available() or not tc_available() or not os.path.exists(OBI):
            sys.exit("needs passwordless sudo for bpftrace, tc and obi: run scripts/setup_sudoers.sh once")
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        plan = []
        for c, n in zip(a.agents, a.mixed_runs):
            plan += [(c, "cal", False, a.cal), (c, "mixed", True, n)]
        data = record(a.out, plan)
    else:
        data = json.load(open(a.out))
    report(evaluate(data))


if __name__ == "__main__":
    main()
