"""Stage 15: the real OpenTelemetry eBPF Instrumentation (OBI) against our join, on the Stage 14 scenarios.

    python -m eval.selective_obi --n 20 --cal 30          # record (ROOT) and evaluate
    python -m eval.selective_obi --no-record

OBI v1.0.0 (installed by scripts/setup_sudoers.sh at /usr/local/libexec/crosstrace/obi) runs for the whole batch, instrumenting any process
listening on an ephemeral port, and prints each HTTP request it saw with its server-side duration. Our collector runs at the same time, so the
SAME runs yield both kinds of evidence. Arm `obi_real` = the rules of v3 fed only OBI's per-request server duration (matched by server port and
request path); `obi` is the Stage 14 emulation. Not built: `obi_real+net` (OBI's network metrics), see docs/results-stage15.md.
REQUIRES passwordless sudo for bpftrace/ctrace/tc/obi (scripts/setup_sudoers.sh).
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

from agent.tools import seed_db
from collectors.ebpf import available as bpf_available
from correlator.join import _is_http, breakdown
from diagnosers import baseline, combined
from diagnosers.profile import _threshold, build_profile, kernel_profile_from_timelines
from eval import selective
from eval.selective import SCENARIOS, EXT4, join_run, one_run
from faultlab.netem import available as tc_available

OBI = "/usr/local/libexec/crosstrace/obi"
LOG = "/tmp/obi_stage15.log"
# route_harvester_timeout 0s: with the default, every new Python process cost a 10 s "route harvesting" wait and attaches were serialised
# behind it. poll_interval 500 ms: find new servers quickly. The two excludes keep OBI off unrelated processes that happen to listen on a
# port in the range (another tool of this developer's, and the time daemon).
CONFIG = """log_level: info
trace_printer: text
route_harvester_timeout: 0s
prometheus_export:
  port: 9464
  features: ["application", "stats"]
stats:
  enable: true
  print_stats: true
discovery:
  poll_interval: 500ms
  instrument:
    - open_ports: "32768-60999"
  exclude_instrument:
    - exe_path: "*hermes*"
    - exe_path: "*chronyd*"
"""
HTTP_LINE = re.compile(r"\((?P<dur>[0-9.]+)(?P<unit>µs|ms|s)\[[^\]]*\]\) HTTP\(subType=\d+\) (?P<status>\d+) (?P<method>[A-Z]+) "
                       r"(?P<path>\S+?)\(.*?\) \[[^\]]*\]->\[[^\]]* as [^:]*:(?P<port>\d+)\]")
UNIT_MS = {"µs": 1e-3, "ms": 1.0, "s": 1000.0}
STATS_LINE = re.compile(r"^stats: .*? src\.port=(?P<src>\d+) dst\.port=(?P<dst>\d+) srtt=(?P<srtt>\d+)\s*$", re.M)


def parse_stats(text):
    """[(src_port, dst_port, srtt_us)] from OBI's printed connection statistics, in print order (one line per end of a connection)."""
    return [(int(m["src"]), int(m["dst"]), int(m["srtt"])) for m in STATS_LINE.finditer(text)]


def parse_log(text):
    """[(server_port, path, duration_ms)] for the HTTP requests OBI printed (server side)."""
    out = []
    for line in text.splitlines():
        m = HTTP_LINE.search(line)
        if m:
            out.append((int(m["port"]), m["path"], float(m["dur"]) * UNIT_MS[m["unit"]]))
    return out


class Obi:
    attached = False

    def __enter__(self):
        self.cfg = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
        self.cfg.write(CONFIG)
        self.cfg.close()
        open(LOG, "w").close()
        self.out = open(LOG, "a")
        self.proc = subprocess.Popen(["sudo", "-n", OBI, "-config", self.cfg.name], stdout=self.out, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        for _ in range(60):                       # the tracer itself only launches once there is a process to attach to
            time.sleep(0.5)
            if "Starting main node" in open(LOG).read():
                return self
            if self.proc.poll() is not None:
                raise RuntimeError("obi exited: " + open(LOG).read()[-600:])
        raise RuntimeError("obi did not start: " + open(LOG).read()[-600:])

    def warm_up(self, ports, timeout=30):
        """Send /health requests (from a curl subprocess, so our own process's events stay clean) to each server until OBI's log shows it
        reported one for that port. Without this OBI missed the first calls, or all calls, of runs against freshly started servers."""
        for port in ports:
            deadline = time.time() + timeout
            while time.time() < deadline:
                subprocess.run(["curl", "-s", "-o", "/dev/null", f"http://127.0.0.1:{port}/health"], timeout=10)
                time.sleep(0.25)
                if any(f"crosstrace:{port}]" in l and "/health" in l for l in open(LOG).read().splitlines()):
                    break
            else:
                raise RuntimeError(f"obi never reported a request on port {port} within {timeout}s")

    def wait_attached(self, pids, timeout=30):
        """First call: block until OBI has attached to a process of the Python executable and launched its tracer. OBI keys its probes by
        EXECUTABLE (one inode), so later Python servers are logged as "already instrumented" and get no attach line of their own: for
        them a short settle time is used, and the stage reports how many requests OBI nevertheless missed."""
        if self.attached:
            time.sleep(0.6)
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            log = open(LOG).read()
            if "instrumenting process" in log and "Launching p.Tracer" in log:
                time.sleep(1.0)
                self.attached = True
                return
            time.sleep(0.1)
        raise RuntimeError(f"obi did not attach to anything within {timeout}s: " + open(LOG).read()[-400:])

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGINT)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.kill(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=5)
        self.out.close()


def record(path, n, n_cal):
    data = json.load(open(path)) if os.path.exists(path) else {}
    todo = [("none", f"cal{i}", "calib") for i in range(n_cal)]
    todo += [(sc, f"s{i}", "eval") for sc in SCENARIOS for i in range(n)]
    with tempfile.TemporaryDirectory() as tmp, Obi() as obi:
        db = os.path.join(tmp, "app.db")
        seed_db(db)
        for k, (sc, seed, role) in enumerate(todo, 1):
            rid = f"{role}-{sc}-{seed}"
            if rid in data:
                continue
            data[rid] = {"scenario": sc, "role": role,
                         **one_run(sc, seed, db, ready_hook=lambda pids, ports: (obi.wait_attached(pids), obi.warm_up(ports)))}
            time.sleep(0.4)                      # let OBI flush this run's last requests
            if k % 10 == 0:
                print(f"  {k}/{len(todo)} runs", file=sys.stderr, flush=True)
    data["_obi"] = [list(x) for x in parse_log(open(LOG).read())]
    data["_obi_stats"] = [list(x) for x in parse_stats(open(LOG).read())]
    json.dump(data, open(path, "w"))
    return data


def span_index(span):
    return int(span["attrs"]["url.full"].rsplit("/", 1)[1])


def all_obi_waits(runs, obi_calls):
    """rid -> {span_id: OBI server duration ms}. Runs are taken in RECORDING order and each OBI request is used once, so a server port
    that two runs happened to share (ephemeral ports are reused) cannot give the later run the earlier run's request."""
    by_key = {}
    for port, path, ms in obi_calls:
        by_key.setdefault((port, path), []).append(ms)
    out = {}
    for rid, r in runs.items():                       # dict order = recording order
        waits = {}
        for s in r["spans"]:
            if "server.port" in s["attrs"] and "url.full" in s["attrs"]:
                key = (s["attrs"]["server.port"], "/shipping/" + str(span_index(s)))
                if by_key.get(key):
                    waits[s["span_id"]] = by_key[key].pop(0)
        out[rid] = waits
    return out


def rtts_per_span(runs, stats):
    """rid -> {span_id: connection RTT in ms}, matching each call to its connection by destination port and order.
    Runs are taken newest first and the LAST n client-side lines of a port are used, so the warm-up connections that precede a run's calls
    (and an older run that shared the port) are never mistaken for its calls."""
    by_port = {}
    for src, dst, srtt in stats:
        by_port.setdefault(dst, []).append(srtt / 1000.0)         # client-side socket: destination = the server's port
    out = {}
    for rid, r in reversed(list(runs.items())):
        spans = sorted((s for s in r["spans"] if "server.port" in s["attrs"] and "url.full" in s["attrs"]), key=lambda s: s["start_ns"])
        per_port = {}
        for s in spans:
            per_port.setdefault(s["attrs"]["server.port"], []).append(s)
        res = {}
        for port, ss in per_port.items():
            lines = by_port.get(port, [])
            take = lines[len(lines) - len(ss):] if len(lines) >= len(ss) else []
            del lines[len(lines) - len(take):]
            for s, v in zip(ss, take):
                res[s["span_id"]] = v
        out[rid] = res
    return out


def evaluate(data):
    obi_calls = [tuple(x) for x in data.get("_obi", [])]
    runs = {k: v for k, v in data.items() if not k.startswith("_")}
    cal = [r for r in runs.values() if r["role"] == "calib"]
    cal_tl = [join_run(r) for r in cal]
    profile = build_profile([r["spans"] for r in cal], "coarse")
    kprofile = kernel_profile_from_timelines(cal_tl, "coarse")
    waits_by_run = all_obi_waits(runs, obi_calls)
    stats = [tuple(x) for x in data.get("_obi_stats", [])]
    rtt_by_run = rtts_per_span(runs, stats) if stats else {}
    healthy_rtt = [v for rid, r in runs.items() if r["role"] == "calib" for v in rtt_by_run.get(rid, {}).values()]
    healthy_obi = [v for rid, r in runs.items() if r["role"] == "calib" for v in waits_by_run[rid].values()]
    kp_obi = dict(kprofile)
    kp_obi["server_wait_ms"] = _threshold(healthy_obi, "coarse")           # OBI's own healthy calibration
    kp_net = dict(kp_obi)
    if healthy_rtt:
        kp_net["srtt_ms"] = _threshold(healthy_rtt, "coarse")           # OBI's own healthy RTT calibration
    print(f"calibration: {len(cal)} healthy runs, OBI saw {len(healthy_obi)}/{4 * len(cal)} calls; server-wait threshold: our collector "
          f"{kprofile['server_wait_ms']:.0f} ms, OBI {kp_obi['server_wait_ms']:.0f} ms")
    results, agree, missing, ports_seen, seen_rtt = {}, [], 0, {}, 0
    for n, (rid, r) in enumerate(sorted(runs.items())):
        if r["role"] != "eval":
            continue
        for p in r["ports"]:
            ports_seen.setdefault(p, []).append(rid)
        tl = join_run(r)
        seed = n
        waits = waits_by_run[rid]
        https = [e for e in tl.entries if _is_http(e.span)]
        missing += len(https) - sum(1 for e in https if e.span["span_id"] in waits)
        for e in https:                                                       # our server wait vs OBI duration, slow calls only
            inj = r["inject"]["slow_ms"]
            ours, theirs = breakdown(e)["server_wait_ms"], waits.get(e.span["span_id"])
            if theirs is not None and ours is not None and theirs > 0.5 * inj:
                agree.append((inj, ours, theirs))
        flow_rtt = rtt_by_run.get(rid, {})
        run_rtt = max(flow_rtt.values()) if flow_rtt else None
        net_rtt = {sid: run_rtt for sid in flow_rtt} if run_rtt is not None else {}
        seen_rtt += len(flow_rtt)
        ranked = {"base": [c.cause for c in baseline.diagnose(r["spans"], profile, tie_seed=seed, api_causes=EXT4)]}
        for arm, src, kp in (("v3", "joined", kprofile), ("pool", "pooled", kprofile), ("pool_port", "pooled_port", kprofile),
                             ("obi", "obi", kprofile), ("obi_real", "obi_real", kp_obi),
                             ("obi_real+net", "obi_real_net", kp_net), ("obi_real+flows", "obi_real_flows", kp_net)):
            ranked[arm] = [c.cause for c in combined.diagnose(tl, profile, kp, tie_seed=seed, loss_explains_rtt=True,
                                                              slow_api=True, source=src, obi_waits=waits,
                                                              obi_srtts=net_rtt if src == "obi_real_net" else flow_rtt)]
        results.setdefault(r["scenario"], []).append({"ranked": ranked, "http": len(https),
                                                      "with_events": sum(1 for e in https if e.kernel),
                                                      "orphans": len(tl.orphans), "warnings": r["warnings"]})
    dup = sum(1 for v in ports_seen.values() if len(v) > 1)
    print(f"OBI connection RTT lines matched to {seen_rtt} of {sum(len([s for s in r['spans'] if 'server.port' in s['attrs']]) for r in runs.values() if r['role'] == 'eval')} "
          f"scenario calls; healthy RTT threshold from OBI: {kp_net['srtt_ms']:.0f} ms (ours {kprofile['srtt_ms']:.0f} ms)")
    return results, agree, missing, dup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--cal", type=int, default=30)
    ap.add_argument("--out", default=os.path.expanduser("~/.crosstrace-data/selective_obi2.json"))
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args()
    if not a.no_record:
        if not bpf_available() or not tc_available() or not os.path.exists(OBI):
            sys.exit("needs passwordless sudo for bpftrace, tc and obi: run scripts/setup_sudoers.sh once")
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        data = record(a.out, a.n, a.cal)
    else:
        data = json.load(open(a.out))
    results, agree, missing, dup = evaluate(data)
    selective.ARMS = ["base", "v3", "pool", "pool_port", "obi", "obi_real", "obi_real+net", "obi_real+flows"]
    selective.report(results)
    if agree:
        ok = sum(1 for inj, ours, theirs in agree if abs(theirs - ours) <= max(0.10 * ours, 30.0))
        ok_inj = sum(1 for inj, ours, theirs in agree if abs(theirs - inj) <= max(0.10 * inj, 30.0))
        print(f"\nOBI duration vs our server wait on {len(agree)} slow calls: within max(10%, 30 ms) of ours {ok}/{len(agree)}; "
              f"of the injected delay {ok_inj}/{len(agree)}")
    print(f"HTTP spans OBI reported nothing for: {missing}; server ports shared by more than one run: {dup}")


if __name__ == "__main__":
    main()
