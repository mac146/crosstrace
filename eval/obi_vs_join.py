"""Stage 20: our join vs real OBI under interleaved, reused-connection concurrency, with overhead (predictions: docs/predictions-stage20.md).

    python -m eval.obi_vs_join                      # record (ROOT) and evaluate, then the overhead runs
    python -m eval.obi_vs_join --no-record          # re-score recorded runs (overhead results are re-printed from the file)

C threads (C = 8, 32), NOT synchronised (random start offsets and think times), one API server, agent kinds H healthy / N latency on its source address / S slow server for its
source address, connections new per call (reuse off) or one keep-alive connection per agent (reuse on). Our compiled collector and real OBI run side by side on the same runs.
REQUIRES passwordless sudo for bpftrace/ctrace/tc/obi (scripts/setup_sudoers.sh).
"""
import argparse
import contextlib
import http.client
import json
import os
import random
import statistics
import sys
import threading
import time

from agent.dns import resolve
from agent.tracing import Tracing
from collectors import cebpf
from collectors.ebpf import KernelCollector, available as bpf_available
from correlator.join import _is_http, breakdown, correlate
from diagnosers import baseline, combined
from diagnosers.profile import _threshold, build_profile, kernel_profile_from_timelines
from eval.metrics import score_run, wilson
from eval.obi_conc import KIND_IP, TRUTH, parse_http
from eval.overhead import bootstrap_mean_ci
from eval.selective import EXT4
from eval.selective_conc import views
from eval.selective_obi import LOG, OBI, Obi, parse_stats
from faultlab.netem import Netem, available as tc_available
from mockservices.launcher import services

CALLS = 8
PASSES = 4
CONDITIONS = [(8, False), (8, True), (32, False), (32, True)]
ARMS = ["base", "v3_join", "host_wide", "obi_order", "obi_exact"]
LABEL = {"base": "spans only", "v3_join": "our join (no declared port)", "host_wide": "host-wide counters",
         "obi_order": "OBI, order matching", "obi_exact": "OBI, exact client port (control)"}
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# ---- the workload ---------------------------------------------------------------------------------------------------------------
def agent_thread(idx, kind, tracing, port, dns_server, reuse, seed, errors):
    rng = random.Random(f"{seed}:{idx}")
    ip = KIND_IP[kind]
    conn = None
    try:
        time.sleep(rng.uniform(0, 0.1))
        with tracing.span("invoke_agent crosstrace-agent", {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "crosstrace-agent"}):
            for n in range(1, CALLS + 1):
                if n > 1:
                    time.sleep(rng.uniform(0, 0.03))
                with tracing.span("execute_tool http_api", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "http_api"}):
                    attrs = {"http.request.method": "GET", "server.address": "api.mock.local", "server.port": port,
                             "url.full": f"http://api.mock.local:{port}/shipping/{n}"}
                    with tracing.span("GET", attrs, client=True) as span:
                        if conn is None:
                            host = resolve("api.mock.local", dns_server)
                            conn = http.client.HTTPConnection(host, port, timeout=60, source_address=(ip, 0) if ip else None)
                            conn.connect()
                        span.set_attribute("crosstrace.debug.local_port", conn.sock.getsockname()[1])    # validation only
                        conn.request("GET", f"/shipping/{n}", headers={"Host": "api.mock.local"})
                        resp = conn.getresponse()
                        resp.read()
                        span.set_attribute("http.response.status_code", resp.status)
                        if not reuse:
                            conn.close()
                            conn = None
    except Exception as e:                                       # a failed agent is reported, not hidden
        errors.append(f"{type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()


def run_threads(kinds, port, dns_server, reuse, seed):
    tracers = [Tracing() for _ in kinds]
    errors = []
    threads = [threading.Thread(target=agent_thread, args=(i, k, tracers[i], port, dns_server, reuse, seed, errors)) for i, k in enumerate(kinds)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    return tracers, errors, time.perf_counter() - t0


def one_run(n_agents, reuse, mixed, seed, obi):
    rng = random.Random(f"s20:{n_agents}:{seed}")
    slow_ms, lat_ms = rng.randint(300, 1000), rng.randint(75, 250)
    api_args = (("--keepalive",) if reuse else ()) + (("--delay-ms", str(slow_ms), "--slow-from", KIND_IP["S"]) if mixed else ())
    kinds = [("HNS"[i % 3] if mixed else "H") for i in range(n_agents)]
    with services(api_args=api_args) as svc:
        with KernelCollector(dns_ports=[svc.dns_server[1]], ports=[svc.api_port], reduce_rtt=True, tcp_send=True) as k:
            obi.wait_attached([svc.pids["api"]])
            obi.warm_up([svc.api_port])
            with contextlib.ExitStack() as stack:
                if mixed:
                    stack.enter_context(Netem(None, delay_ms=lat_ms, addr=KIND_IP["N"]))
                tracers, errors, secs = run_threads(kinds, svc.api_port, svc.dns_server, reuse, seed)
                if mixed:
                    time.sleep(2 * lat_ms / 1000 + 0.1)          # let netem's queue drain before the qdisc goes (Stage 14 finding)
            time.sleep(0.2)
        return {"c": n_agents, "reuse": reuse, "mixed": mixed, "kinds": kinds, "agents": [t.spans() for t in tracers], "events": k.events,
                "sync": [list(c) for c in k.clock_sync], "warnings": k.warnings, "errors": errors, "ports": [svc.api_port],
                "seconds": round(secs, 2), "inject": {"slow_ms": slow_ms, "lat_ms": lat_ms}}


def record(path, plan, tag="s20"):
    data = json.load(open(path)) if os.path.exists(path) else {}
    with Obi() as obi:
        for c, reuse, role, mixed, n in plan:
            for j in range(n):
                rid = f"c{c}-r{int(reuse)}-{role}-{j}"
                if rid in data:
                    continue
                data[rid] = {"role": role, **one_run(c, reuse, mixed, f"{tag}-{c}-{int(reuse)}-{role}-{j}", obi)}
                time.sleep(0.5)
                print(f"  {rid}", file=sys.stderr, flush=True)
    data["_http"] = [list(x) for x in parse_http(open(LOG).read())]
    data["_stats"] = [list(x) for x in parse_stats(open(LOG).read())]
    json.dump(data, open(path, "w"))
    return data


# ---- OBI evidence: order matching and exact matching ----------------------------------------------------------------------------
def http_spans(r):
    return [(i, s) for i, a in enumerate(r["agents"]) for s in a if "server.port" in s["attrs"] and "url.full" in s["attrs"]]


def path_of(span):
    return "/shipping/" + span["attrs"]["url.full"].rsplit("/", 1)[1]


def obi_evidence(r, http, stats):
    """For one run: {method: {"wait": {span_id: ms}, "rtt": {span_id: ms}, "wait_port": {span_id: client port}, "rtt_port": {span_id: client port}}}.
    'exact' matches by (server port, client port[, path]); 'order' is what OBI's telemetry allows without a declared key: server durations by
    (server port, path) in print order, and the LAST n connection lines of the server port in print order (none at all when there are fewer lines than calls)."""
    spans = sorted((s for _, s in http_spans(r)), key=lambda s: s["start_ns"])
    out = {"exact": {"wait": {}, "rtt": {}, "wait_port": {}, "rtt_port": {}}, "order": {"wait": {}, "rtt": {}, "wait_port": {}, "rtt_port": {}}}
    ex_w, od_w, ex_r, od_r = {}, {}, {}, {}
    for sport, path, ms, cport in http:
        ex_w.setdefault((sport, cport, path), []).append(ms)
        od_w.setdefault((sport, path), []).append((ms, cport))
    for src, dst, srtt in stats:
        ex_r.setdefault((dst, src), []).append(srtt / 1000.0)
        od_r.setdefault(dst, []).append((srtt / 1000.0, src))
    by_server = {}
    for s in spans:
        by_server.setdefault(s["attrs"]["server.port"], []).append(s)
        lp, sp, pa = s["attrs"].get("crosstrace.debug.local_port"), s["attrs"]["server.port"], path_of(s)
        if ex_w.get((sp, lp, pa)):
            out["exact"]["wait"][s["span_id"]] = ex_w[(sp, lp, pa)].pop(0)
            out["exact"]["wait_port"][s["span_id"]] = lp
        if ex_r.get((sp, lp)):
            out["exact"]["rtt"][s["span_id"]] = ex_r[(sp, lp)][-1]
            out["exact"]["rtt_port"][s["span_id"]] = lp
    for sp, ss in by_server.items():
        groups = {}
        for s in ss:
            groups.setdefault(path_of(s), []).append(s)
        for pa, ps in groups.items():
            entries = od_w.get((sp, pa), [])
            for s, (ms, cport) in zip(ps, entries):
                out["order"]["wait"][s["span_id"]] = ms
                out["order"]["wait_port"][s["span_id"]] = cport
        lines = od_r.get(sp, [])
        if len(lines) >= len(ss):
            for s, (ms, src) in zip(ss, lines[len(lines) - len(ss):]):
                out["order"]["rtt"][s["span_id"]] = ms
                out["order"]["rtt_port"][s["span_id"]] = src
    return out


def own_connection_stats(entry):
    """(has evidence, every attached event with ports belongs to the call's own connection) for one joined HTTP span."""
    lp, sp = entry.span["attrs"].get("crosstrace.debug.local_port"), entry.span["attrs"]["server.port"]
    ports = []
    for a in entry.kernel:
        e = a.event
        if e.get("sport") is not None and e.get("dport") is not None:
            ports.append(e["sport"] if e["dport"] == sp else e["dport"])
    return bool(entry.kernel), bool(ports) and all(p == lp for p in ports)


# ---- evaluation -----------------------------------------------------------------------------------------------------------------
def top2(ranked, truth):
    return truth in ranked[:2]


def evaluate(data):
    http = [tuple(x) for x in data.get("_http", [])]
    stats = [tuple(x) for x in data.get("_stats", [])]
    runs_all = {k: v for k, v in data.items() if not k.startswith("_")}
    out = {}
    for c, reuse in CONDITIONS:
        runs = {k: v for k, v in runs_all.items() if v["c"] == c and v["reuse"] == reuse}
        if not runs:
            continue
        cal = {k: r for k, r in runs.items() if r["role"] == "cal"}
        ev = {k: obi_evidence(r, http, stats) for k, r in runs.items()}
        profile = build_profile([s for r in cal.values() for s in r["agents"]], "coarse")
        kp = kernel_profile_from_timelines([v for r in cal.values() for v in views(r)[2]], "coarse")
        kp_obi = dict(kp)
        hw = [x for k in cal for x in ev[k]["exact"]["wait"].values()]
        hr = [x for k in cal for x in ev[k]["exact"]["rtt"].values()]
        if hw:
            kp_obi["server_wait_ms"] = _threshold(hw, "coarse")
        if hr:
            kp_obi["srtt_ms"] = _threshold(hr, "coarse")
        res = {a: {k: {"n": 0, "top1": 0, "top2": 0, "fa": 0} for k in ("all", "H", "N", "S")} for a in ARMS}
        rel = {"calls": 0, "ours_missing": 0, "ours_own": 0, "obi_wait_missing": 0, "obi_rtt_missing": 0, "order_wait_ok": 0, "order_rtt_ok": 0,
               "ours_warn": 0, "errors": 0, "runs": 0}
        for rid, r in runs.items():
            if r["role"] != "mixed":
                continue
            rel["runs"] += 1
            rel["errors"] += len(r["errors"])
            rel["ours_warn"] += bool(r["warnings"])
            tl, all_http, per_agent = views(r)
            for e in all_http:
                lp = e.span["attrs"].get("crosstrace.debug.local_port")
                has, own = own_connection_stats(e)
                rel["calls"] += 1
                rel["ours_missing"] += not has
                rel["ours_own"] += own
                sid = e.span["span_id"]
                rel["obi_wait_missing"] += sid not in ev[rid]["exact"]["wait"]
                rel["obi_rtt_missing"] += sid not in ev[rid]["exact"]["rtt"]
                rel["order_wait_ok"] += ev[rid]["order"]["wait_port"].get(sid) == lp
                rel["order_rtt_ok"] += ev[rid]["order"]["rtt_port"].get(sid) == lp
            for n, kind in enumerate(r["kinds"]):
                spans = r["agents"][n]
                ranked = {"base": [x.cause for x in baseline.diagnose(spans, profile, tie_seed=n, api_causes=EXT4)]}
                ranked["v3_join"] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp, tie_seed=n, loss_explains_rtt=True, slow_api=True)]
                ranked["host_wide"] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp, tie_seed=n, loss_explains_rtt=True, slow_api=True,
                                                                          source="pooled", pool_entries=all_http)]
                for arm, m in (("obi_order", "order"), ("obi_exact", "exact")):
                    ranked[arm] = [x.cause for x in combined.diagnose(per_agent[n], profile, kp_obi, tie_seed=n, loss_explains_rtt=True, slow_api=True,
                                                                      source="obi_real_flows", pool_entries=all_http,
                                                                      obi_waits=ev[rid][m]["wait"], obi_srtts=ev[rid][m]["rtt"])]
                for arm in ARMS:
                    sc = score_run(ranked[arm], TRUTH[kind])
                    for key in ("all", kind):
                        cell = res[arm][key]
                        cell["n"] += 1
                        cell["top1"] += sc["top1"]
                        cell["top2"] += top2(ranked[arm], TRUTH[kind])
                        cell["fa"] += sc["false_attr"]
        out[(c, reuse)] = {"res": res, "rel": rel, "thr": (profile.get("tool:http_api", 0), kp, kp_obi)}
    return out


def pc(k, n):
    lo, hi = wilson(k, n) if n else (0, 0)
    return f"{100 * k / n:5.1f}% [{100 * lo:3.0f}-{100 * hi:3.0f}]" if n else "-"


def report(out):
    for (c, reuse), g in out.items():
        res, rel = g["res"], g["rel"]
        n = max(rel["calls"], 1)
        print(f"\n=== {c} threads, connections {'REUSED (keep-alive)' if reuse else 'new per call'}: {res['base']['all']['n']} agent-runs in {rel['runs']} runs; "
              f"crashed agents {rel['errors']}; runs with collector warnings {rel['ours_warn']}; healthy http_api threshold {g['thr'][0]:.0f} ms")
        print(f"{'arm':<34}{'top-1':<19}{'top-2':<19}{'false attr.':<19}{'H top-1':<9}{'N top-1':<9}{'S top-1':<9}")
        for arm in ARMS:
            a = res[arm]["all"]
            print(f"{LABEL[arm]:<34}{pc(a['top1'], a['n']):<19}{pc(a['top2'], a['n']):<19}{pc(a['fa'], a['n']):<19}" +
                  "".join(f"{100 * res[arm][k]['top1'] / max(res[arm][k]['n'], 1):<9.0f}" for k in ("H", "N", "S")))
        print(f"  evidence missing, per call ({rel['calls']} calls): ours {100 * rel['ours_missing'] / n:.1f}%   OBI server duration {100 * rel['obi_wait_missing'] / n:.1f}%   "
              f"OBI connection RTT {100 * rel['obi_rtt_missing'] / n:.1f}%")
        print(f"  correlation reliability (evidence belongs to the call's own connection): our join {100 * rel['ours_own'] / n:.1f}%   "
              f"OBI order matching: duration {100 * rel['order_wait_ok'] / n:.1f}%, RTT {100 * rel['order_rtt_ok'] / n:.1f}%")
    print("\nAgents of one run share a server and a schedule, so agent-runs are not independent: the intervals are optimistic.")


# ---- overhead -------------------------------------------------------------------------------------------------------------------
def cpu_seconds(names):
    """{comm: CPU seconds so far} summed over the processes with these names."""
    out = {n: 0.0 for n in names}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            raw = open(f"/proc/{pid}/stat").read()
        except OSError:
            continue
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        if comm in out:
            f = raw[raw.rindex(")") + 2:].split()
            out[comm] += (int(f[11]) + int(f[12])) / TICK
    return out


def overhead_run(arm, c=32):
    """One healthy keep-alive workload under one arm; returns call latency (ms), process CPU and collector CPU per call (ms)."""
    with services(api_args=("--keepalive",)) as svc, contextlib.ExitStack() as stack:
        obi = ours = None
        if arm in ("ours", "both"):
            ours = stack.enter_context(cebpf.CCollector(dns_ports=[svc.dns_server[1]], ports=[svc.api_port], reduce_rtt=True, tcp_send=True))
        if arm in ("obi", "both"):
            obi = stack.enter_context(Obi())
            obi.wait_attached([svc.pids["api"]])
            obi.warm_up([svc.api_port])
        time.sleep(1.0)
        c0, p0 = cpu_seconds(("ctrace", "obi")), time.process_time()
        tracers, errors, secs = [], [], 0.0
        for k in range(PASSES):                          # four passes (about 1,000 calls): CPU is read in 10 ms ticks, one pass would be too coarse
            tr, er, sc = run_threads(["H"] * c, svc.api_port, svc.dns_server, True, f"ovh-{arm}-{k}")
            tracers += tr
            errors += er
            secs += sc
        time.sleep(0.5)
        c1, p1 = cpu_seconds(("ctrace", "obi")), time.process_time()
        lat = [(s["end_ns"] - s["start_ns"]) / 1e6 for t in tracers for s in t.spans() if "server.port" in s["attrs"]]
        calls = max(len(lat), 1)
        return {"lat_ms": statistics.fmean(lat) if lat else None, "wall_s": secs, "errors": len(errors), "calls": len(lat),
                "app_cpu_ms_per_call": 1000 * (p1 - p0) / calls, "ctrace_cpu_ms_per_call": 1000 * (c1["ctrace"] - c0["ctrace"]) / calls,
                "obi_cpu_ms_per_call": 1000 * (c1["obi"] - c0["obi"]) / calls}


def overhead(rounds, path):
    data = json.load(open(path)) if os.path.exists(path) else {}
    done = data.get("_overhead", [])
    for rnd in range(len(done), rounds):
        order = ["none", "ours", "obi", "both"]
        random.Random(rnd).shuffle(order)
        row = {}
        for arm in order:
            row[arm] = overhead_run(arm)
            print(f"  overhead round {rnd + 1}/{rounds} {arm}", file=sys.stderr, flush=True)
        done.append(row)
        data["_overhead"] = done
        json.dump(data, open(path, "w"))
    return done


def report_overhead(done):
    if not done:
        return
    print(f"\n=== overhead: 32 threads, keep-alive, healthy workload, {len(done)} paired rounds (arms in random order; the OpenTelemetry spans are in every arm)")
    print(f"{'arm':<8}{'mean call latency ms':>22}{'vs none (paired)':>26}{'app CPU ms/call':>18}{'ctrace CPU ms/call':>20}{'obi CPU ms/call':>18}{'added CPU ms/call':>19}")
    for arm in ("none", "ours", "obi", "both"):
        lat = [r[arm]["lat_ms"] for r in done]
        ratio = [100 * (r[arm]["lat_ms"] / r["none"]["lat_ms"] - 1) for r in done]
        lo, hi = bootstrap_mean_ci(ratio, 1) if len(ratio) > 1 else (0, 0)
        print(f"{arm:<8}{statistics.fmean(lat):>22.2f}{(f'{statistics.fmean(ratio):+.1f}% [{lo:+.1f}, {hi:+.1f}]' if arm != 'none' else '-'):>26}"
              f"{statistics.fmean(r[arm]['app_cpu_ms_per_call'] for r in done):>18.3f}{statistics.fmean(r[arm]['ctrace_cpu_ms_per_call'] for r in done):>20.3f}"
              f"{statistics.fmean(r[arm]['obi_cpu_ms_per_call'] for r in done):>18.3f}"
              f"{(statistics.fmean(r[arm]['app_cpu_ms_per_call'] - r['none']['app_cpu_ms_per_call'] + r[arm]['ctrace_cpu_ms_per_call'] + r[arm]['obi_cpu_ms_per_call'] for r in done) if arm != 'none' else 0):>19.3f}")
    errs = sum(r[a]["errors"] for r in done for a in r)
    print(f"  agent errors across all overhead runs: {errs}. 'added CPU' = the agent process's extra CPU against the same round's 'none' + the collector process CPU (our collector's reader thread is inside the agent process).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/obi_vs_join.json"))
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--cal", type=int, default=6)
    ap.add_argument("--mixed8", type=int, default=12)
    ap.add_argument("--mixed32", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--skip-overhead", action="store_true")
    ap.add_argument("--seed-tag", default="s20", help="seed namespace; smoke tests must use a different one from the scored runs")
    a = ap.parse_args()
    if not a.no_record:
        if not bpf_available() or not tc_available() or not os.path.exists(OBI) or not cebpf.available():
            sys.exit("needs passwordless sudo for bpftrace/ctrace, tc and obi: run scripts/setup_sudoers.sh once")
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        plan = []
        for c, reuse in CONDITIONS:
            plan += [(c, reuse, "cal", False, a.cal), (c, reuse, "mixed", True, a.mixed8 if c == 8 else a.mixed32)]
        data = record(a.out, plan, a.seed_tag)
    else:
        data = json.load(open(a.out))
    report(evaluate(data))
    if not a.no_record and not a.skip_overhead:
        report_overhead(overhead(a.rounds, a.out))
    elif "_overhead" in data:
        report_overhead(data["_overhead"])


if __name__ == "__main__":
    main()
