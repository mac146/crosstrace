"""Stage 16: OBI plus matching against our join, with concurrent agents in DIFFERENT conditions (predictions: docs/predictions-stage16.md).

    python -m eval.obi_conc                    # record (ROOT) and evaluate
    python -m eval.obi_conc --no-record

Scenario "interleaved": one API server, C agents (threads) in lockstep, every agent requests /shipping/1../shipping/4. Agent kinds interleave:
H healthy (default source address), N source 127.0.0.2 with netem latency on that address (truth net_latency), S source 127.0.0.3 with the
server delaying answers to that address (truth slow_api). Real OBI (stats mode, printer) runs next to our collector.
Arms: base, v3 (our join), pool / pool_port (host-wide counters), obi_real+flows (OBI duration and RTT matched by server port, path and ORDER),
obi_real+port (the same OBI evidence matched EXACTLY by the client's source port, which needs the span to declare its local port).
REQUIRES passwordless sudo for bpftrace/ctrace/tc/obi (scripts/setup_sudoers.sh).
"""
import argparse
import contextlib
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from collectors.ebpf import KernelCollector, available as bpf_available
from correlator.join import _is_http
from diagnosers import baseline, combined
from diagnosers.profile import _threshold, build_profile, kernel_profile_from_timelines
from eval.metrics import wilson
from eval.selective import EXT4
from eval.selective_conc import views
from eval.selective_obi import LOG, OBI, STATS_LINE, UNIT_MS, Obi, parse_stats
from faultlab.netem import Netem, available as tc_available
from mockservices.launcher import services

KIND_IP = {"H": None, "N": "127.0.0.2", "S": "127.0.0.3"}
TRUTH = {"H": "none", "N": "net_latency", "S": "slow_api"}
ARMS = ["base", "v3", "pool", "pool_port", "obi_real+flows", "obi_real+port"]
HTTP_CLIENT_LINE = re.compile(r"\((?P<dur>[0-9.]+)(?P<unit>µs|ms|s)\[[^\]]*\]\) HTTP\(subType=\d+\) (?P<status>\d+) (?P<method>[A-Z]+) "
                              r"(?P<path>\S+?)\(.*?\) \[\S+ as \S+?:(?P<cport>\d+)\]->\[[^\]]* as [^:]*:(?P<sport>\d+)\]")


def parse_http(text):
    """[(server_port, path, duration_ms, client_port)] for the HTTP requests OBI printed, in print order."""
    return [(int(m["sport"]), m["path"], float(m["dur"]) * UNIT_MS[m["unit"]], int(m["cport"]))
            for m in map(HTTP_CLIENT_LINE.search, text.splitlines()) if m]


def one_run(n_agents, mixed, seed, db, obi):
    rng = random.Random(f"obiconc:{n_agents}:{seed}")
    slow_ms, lat_ms = rng.randint(300, 1000), rng.randint(75, 250)
    api_args = ("--delay-ms", str(slow_ms), "--slow-from", KIND_IP["S"]) if mixed else ()
    kinds = [("HNS"[i % 3] if mixed else "H") for i in range(n_agents)]
    with services(api_args=api_args) as svc:
        start, end = threading.Barrier(n_agents + 1), threading.Barrier(n_agents + 1)
        tracers = [Tracing() for _ in range(n_agents)]
        errors = []

        def agent(tracing, kind):
            try:
                tools = Tools(db, svc.api_url, svc.dns_server, tracing, source_ip=KIND_IP[kind])
                with tracing.span("invoke_agent crosstrace-agent", {"gen_ai.operation.name": "invoke_agent",
                                                                   "gen_ai.agent.name": "crosstrace-agent"}):
                    for i in range(1, 5):
                        start.wait(timeout=90)
                        try:
                            with tracing.span("execute_tool http_api", {"gen_ai.operation.name": "execute_tool",
                                                                       "gen_ai.tool.name": "http_api"}):
                                tools.call("http_api", {"path": f"/shipping/{i}"})
                        finally:
                            end.wait(timeout=90)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
                start.abort()
                end.abort()

        with KernelCollector(dns_ports=[svc.dns_server[1]], ports=[svc.api_port], reduce_rtt=True) as k:
            obi.wait_attached([svc.pids["api"]])
            obi.warm_up([svc.api_port])
            with contextlib.ExitStack() as stack:
                if mixed:
                    stack.enter_context(Netem(None, delay_ms=lat_ms, addr=KIND_IP["N"]))
                threads = [threading.Thread(target=agent, args=(t, kd)) for t, kd in zip(tracers, kinds)]
                for t in threads:
                    t.start()
                for _ in range(4):
                    start.wait(timeout=90)
                    end.wait(timeout=90)
                for t in threads:
                    t.join(timeout=30)
                if mixed:
                    time.sleep(2 * lat_ms / 1000 + 0.1)        # let netem's queue drain before the qdisc goes (Stage 14 finding)
            time.sleep(0.2)
        return {"mixed": mixed, "kinds": kinds, "agents": [t.spans() for t in tracers], "events": k.events,
                "sync": [list(c) for c in k.clock_sync], "warnings": k.warnings, "errors": errors, "ports": [svc.api_port],
                "inject": {"slow_ms": slow_ms, "lat_ms": lat_ms}}


def record(path, plan):
    data = json.load(open(path)) if os.path.exists(path) else {}
    with tempfile.TemporaryDirectory() as tmp, Obi() as obi:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        for c, role, mixed, n in plan:
            for j in range(n):
                rid = f"{role}-c{c}-{j}"
                if rid in data:
                    continue
                data[rid] = {"c": c, "role": role, **one_run(c, mixed, f"{role}{j}", db, obi)}
                time.sleep(0.5)
                print(f"  {rid}", file=sys.stderr, flush=True)
    data["_http"] = [list(x) for x in parse_http(open(LOG).read())]
    data["_stats"] = [list(x) for x in parse_stats(open(LOG).read())]
    json.dump(data, open(path, "w"))
    return data


def http_spans(r):
    """[(agent index, span)] of every HTTP client span of the run."""
    return [(i, s) for i, a in enumerate(r["agents"]) for s in a if "server.port" in s["attrs"] and "url.full" in s["attrs"]]


def call_no(span):
    return int(span["attrs"]["url.full"].rsplit("/", 1)[1])


def match_evidence(runs, http, stats):
    """Per run and per matching method, span_id -> OBI server duration (ms) and span_id -> OBI connection RTT (ms).
    'order': by server port, path and order of appearance (spans sorted by start time; RTT lines taken from the end, in order).
    'port' : exactly, by the client's source port (the span's declared local port)."""
    out = {}
    pool_http_order, pool_stats_order, pool_http_port, pool_stats_port = {}, {}, {}, {}
    for sport, path, ms, cport in http:
        pool_http_order.setdefault((sport, path), []).append(ms)
        pool_http_port.setdefault((sport, cport), []).append(ms)
    for src, dst, srtt in stats:
        pool_stats_order.setdefault(dst, []).append(srtt / 1000.0)
        pool_stats_port.setdefault((dst, src), []).append(srtt / 1000.0)
    for rid, r in reversed(list(runs.items())):                 # newest first: take the LAST matching lines
        spans = sorted(http_spans(r), key=lambda x: x[1]["start_ns"])
        order_w, order_r, port_w, port_r = {}, {}, {}, {}
        by_port = {}
        for _, s in spans:
            by_port.setdefault(s["attrs"]["server.port"], []).append(s)
        for port, ss in by_port.items():
            lines = pool_stats_order.get(port, [])
            take = lines[len(lines) - len(ss):] if len(lines) >= len(ss) else []
            del lines[len(lines) - len(take):]
            for s, v in zip(ss, take):
                order_r[s["span_id"]] = v
            for path in {"/shipping/" + str(call_no(s)) for s in ss}:
                ps = [s for s in ss if "/shipping/" + str(call_no(s)) == path]
                entries = pool_http_order.get((port, path), [])
                take_w = entries[len(entries) - len(ps):] if len(entries) >= len(ps) else []
                del entries[len(entries) - len(take_w):]
                for s, v in zip(ps, take_w):
                    order_w[s["span_id"]] = v
        for _, s in spans:
            lp = s["attrs"].get("crosstrace.debug.local_port")       # stands for the declared network.local.port
            key = (s["attrs"]["server.port"], lp)
            if pool_http_port.get(key):
                port_w[s["span_id"]] = pool_http_port[key].pop()
            if pool_stats_port.get(key):
                port_r[s["span_id"]] = pool_stats_port[key].pop()
        out[rid] = {"order": (order_w, order_r), "port": (port_w, port_r)}
    return out


def evaluate(data):
    http = [tuple(x) for x in data.get("_http", [])]
    stats = [tuple(x) for x in data.get("_stats", [])]
    runs_all = {k: v for k, v in data.items() if not k.startswith("_")}
    evidence = match_evidence(runs_all, http, stats)
    out = {}
    for c in sorted({r["c"] for r in runs_all.values()}):
        runs = {k: v for k, v in runs_all.items() if v["c"] == c}
        cal_spans, cal_views, healthy_w, healthy_r = [], [], [], []
        for rid, r in runs.items():
            if r["role"] != "cal":
                continue
            cal_spans += r["agents"]
            cal_views += views(r)[2]
            healthy_w += list(evidence[rid]["port"][0].values())
            healthy_r += list(evidence[rid]["port"][1].values())
        profile = build_profile(cal_spans, "coarse")
        kprofile = kernel_profile_from_timelines(cal_views, "coarse")
        kp_obi = dict(kprofile)
        if healthy_w:
            kp_obi["server_wait_ms"] = _threshold(healthy_w, "coarse")
        if healthy_r:
            kp_obi["srtt_ms"] = _threshold(healthy_r, "coarse")
        res = {a: {k: [0, 0] for k in ("all", "H", "N", "S")} for a in ARMS}
        info = {"agent_runs": 0, "matched": [0, 0], "warn": 0, "err": 0, "thr": (profile["tool:http_api"], kprofile, kp_obi)}
        for rid, r in runs.items():
            if r["role"] != "mixed":
                continue
            tl, all_http, per_agent = views(r)
            info["warn"] += bool(r["warnings"])
            info["err"] += len(r["errors"])
            ev = evidence[rid]
            ids = {sp["span_id"] for _, sp in http_spans(r)}
            info["matched"][0] += len(ids & set(ev["port"][0])) + len(ids & set(ev["port"][1]))
            info["matched"][1] += 2 * len(ids)
            for n, (spans, view, kind) in enumerate(zip(r["agents"], per_agent, r["kinds"])):
                ranked = {"base": [x.cause for x in baseline.diagnose(spans, profile, tie_seed=n, api_causes=EXT4)]}
                for arm, src in (("v3", "joined"), ("pool", "pooled"), ("pool_port", "pooled_port")):
                    ranked[arm] = [x.cause for x in combined.diagnose(view, profile, kprofile, tie_seed=n, loss_explains_rtt=True,
                                                                      slow_api=True, source=src, pool_entries=all_http)]
                for arm, method in (("obi_real+flows", "order"), ("obi_real+port", "port")):
                    w, rr = ev[method]
                    ranked[arm] = [x.cause for x in combined.diagnose(view, profile, kp_obi, tie_seed=n, loss_explains_rtt=True,
                                                                      slow_api=True, source="obi_real_flows", pool_entries=all_http,
                                                                      obi_waits=w, obi_srtts=rr)]
                info["agent_runs"] += 1
                for arm in ARMS:
                    ok = ranked[arm][0] == TRUTH[kind]
                    for key in ("all", kind):
                        res[arm][key][0] += ok
                        res[arm][key][1] += 1
        out[c] = (res, info)
    return out


def report(out):
    def cell(k, n):
        lo, hi = wilson(k, n) if n else (0, 0)
        return f"{100 * k / n:5.1f}% [{100 * lo:3.0f}-{100 * hi:3.0f}]" if n else "-"
    for c, (res, info) in out.items():
        thr, kp, kpo = info["thr"]
        print(f"\n=== {c} concurrent agents: {info['agent_runs']} agent-runs; crashed agents {info['err']}; runs with collector warnings "
              f"{info['warn']}; OBI evidence available for {info['matched'][0]}/{info['matched'][1]} (span, kind of evidence) pairs by exact port")
        print(f"    healthy thresholds: http_api {thr:.0f} ms; server wait ours {kp['server_wait_ms']:.0f} / OBI {kpo['server_wait_ms']:.0f} ms; "
              f"RTT ours {kp['srtt_ms']:.0f} / OBI {kpo['srtt_ms']:.0f} ms")
        print(f"{'arm':<16}{'overall':<20}{'H (none)':<20}{'N (net_latency)':<20}{'S (slow_api)':<20}")
        for arm in ARMS:
            print(f"{arm:<16}" + "".join(f"{cell(*res[arm][k]):<20}" for k in ("all", "H", "N", "S")))
    print("\nAgents of one run share a server and a lockstep schedule, so agent-runs are not independent: the intervals are optimistic.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/obi_conc.json"))
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
