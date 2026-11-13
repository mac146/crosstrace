"""Run an agent under the proxy: record healthy paths, check that replay is stable, and run fault trials (build plan steps 3 and 5).

The agent is any command. The only thing changed for it is its environment: OPENAI_BASE_URL and ANTHROPIC_BASE_URL point at the proxy.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

from crosstrace.mcp.ledger import effective_lists, tool_calls
from crosstrace.mcp.verdict import classify_tool, write_check
from crosstrace.mcp.wrap import ENV as MCP_ENV
from crosstrace.proxy import FaultPlan, ProxyThread, make_fault
from crosstrace.proxy.recording import Recording
from crosstrace.verdict import TrialResult, attempts_at, classify, early_silent, next_move_verdict, STORM_LIMIT

# variables that would send the agent around the proxy, or to a real provider with a real key during replay
STRIP_ENV = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE", "HTTP_PROXY", "HTTPS_PROXY",
             "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@dataclass
class AgentSpec:
    command: list                         # argv, e.g. ["python", "agent.py", "--question", "where is order 42?"]
    cwd: str = None
    env: dict = field(default_factory=dict)
    timeout_s: float = 120.0              # budget per run; a trial past it is a HANG
    upstreams: dict = field(default_factory=dict)   # record mode: provider -> base URL (default: the real providers)
    masks: list = field(default_factory=list)
    mcp: dict = field(default_factory=dict)         # {"writes": [tool names], "reads": [tool names]}: overrides for the write heuristic

    def environ(self, base_url, replay, mcp_control=None):
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        # "{proxy_url}" in a configured value: for agents that read their base URL from their own variable (e.g. OPENCODE_BASE_URL)
        env.update({k: v.replace("{proxy_url}", base_url) for k, v in self.env.items()})
        env.update(OPENAI_BASE_URL=base_url + "/v1", ANTHROPIC_BASE_URL=base_url)
        if mcp_control:
            env[MCP_ENV] = mcp_control
        if replay:      # replay needs no real key (nothing leaves the machine), but SDKs refuse to start without one
            env.update(OPENAI_API_KEY="sk-crosstrace-replay", ANTHROPIC_API_KEY="sk-ant-crosstrace-replay")
        return env


@dataclass
class RunOutcome:
    exit_code: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False
    stopped_early: str = None


def _kill(proc):
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def run_agent(spec, base_url, replay, watch=None, poll_s=0.05, timeout_s=None, mcp_control=None):
    """Run the agent once. watch(): returns a reason string to stop it early (verdict already decided), else None."""
    timeout_s = timeout_s or spec.timeout_s
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        t0 = time.monotonic()
        proc = subprocess.Popen(spec.command, cwd=spec.cwd, env=spec.environ(base_url, replay, mcp_control), stdout=out, stderr=err, text=True,
                                start_new_session=(sys.platform != "win32"))
        timed_out, stopped = False, None
        while proc.poll() is None:
            if time.monotonic() - t0 > timeout_s:
                timed_out = True
                _kill(proc)
                break
            if watch is not None:
                stopped = watch()
                if stopped:
                    _kill(proc)
                    break
            time.sleep(poll_s)
        proc.wait()
        secs = time.monotonic() - t0
        out.seek(0)
        err.seek(0)
        return RunOutcome(proc.returncode, out.read(), err.read(), secs, timed_out, stopped)


class McpRun:
    """The control file and logs for the MCP wrapper of one agent run (the wrapper finds them through CROSSTRACE_MCP).

    Replay always gets one, even when nothing was recorded: a wrapper in replay mode never starts the real MCP server, so a replay
    cannot cause side effects."""

    def __init__(self, spec, mode, calls=None, fault=None):
        self.dir = tempfile.mkdtemp(prefix="crosstrace-mcp-")
        self.path = os.path.join(self.dir, "control.json")
        self.ledger_path = os.path.join(self.dir, "ledger.jsonl")
        self.record_path = os.path.join(self.dir, "record.jsonl")
        writes, reads, _ = effective_lists(spec.mcp, calls)          # the server's own hints count: a read-only tool is not a write
        control = {"mode": mode, "record_path": self.record_path, "ledger": self.ledger_path, "calls": calls or [], "fault": fault,
                   "writes": writes, "reads": reads}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(control, f)

    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def ledger(self):
        return self._read(self.ledger_path)

    def recorded(self):
        return self._read(self.record_path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def _mcp_calls(recording):
    """The recorded MCP calls of a recording, or None when its agent used no wrapped MCP server."""
    return (recording.meta.get("mcp") or {}).get("calls")


def record(spec, path=None):
    """One healthy run against the real providers (or spec.upstreams), recorded. Fails loudly if the agent fails."""
    with McpRun(spec, "record") as m, ProxyThread(mode="record", upstreams=spec.upstreams, masks=spec.masks) as p:
        o = run_agent(spec, p.base_url, replay=False, mcp_control=m.path)
        mcp = m.recorded()
    if mcp:
        p.recording.meta["mcp"] = {"calls": mcp}
    if o.timed_out or o.exit_code != 0:
        raise RuntimeError(f"healthy run failed (exit {o.exit_code}, timed out {o.timed_out}): {o.stderr[-500:]}")
    if not p.recording.exchanges:
        raise RuntimeError("the agent made no model calls through the proxy: is its base URL set from OPENAI_BASE_URL/ANTHROPIC_BASE_URL?")
    p.recording.meta.update(command=spec.command, stdout=o.stdout, exit_code=o.exit_code, seconds=round(o.seconds, 2))
    if path:
        p.recording.save(path)
    return p.recording


def replay(spec, recording):
    """One replay with no fault: (outcome, proxy) for inspection. The MCP ledger of the run is on p.mcp_ledger."""
    with ProxyThread(mode="replay", recording=recording, masks=spec.masks) as p, McpRun(spec, "replay", _mcp_calls(recording)) as m:
        o = run_agent(spec, p.base_url, replay=True, mcp_control=m.path)
        p.mcp_ledger = m.ledger()
    return o, p


def json_diff(a, b, path="", limit=3):
    """Paths where two JSON values differ, with short values: [(path, a, b)]."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            out += json_diff(a.get(k), b.get(k), f"{path}.{k}" if path else k, limit - len(out))
            if len(out) >= limit:
                break
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            out += json_diff(x, y, f"{path}[{i}]", limit - len(out))
            if len(out) >= limit:
                break
    elif a != b:
        short = lambda v: (repr(v)[:77] + "...") if len(repr(v)) > 80 else repr(v)
        out.append((path or "(root)", short(a), short(b)))
    return out[:limit]


def stability(spec, recording, runs=2):
    """Replay with no fault `runs` times: every model request must match in order or by concurrency, the whole recording must be used,
    and the output must equal the recorded output. Also reports connections per model call (CHURN, from Stage 21)."""
    problems, churn, ua, secs = [], [], [], []
    for i in range(runs):
        o, p = replay(spec, recording)
        secs.append(o.seconds)
        model = [r for r in p.requests if r.get("provider")]
        ua += model
        misses = [r for r in model if r.get("how") == "miss"]
        if o.timed_out:
            problems.append(f"replay {i + 1}: timed out")
        if o.exit_code != recording.meta.get("exit_code", 0):
            problems.append(f"replay {i + 1}: exit {o.exit_code}: {o.stderr[-300:]}")
        for m in misses[:1]:
            pos = model.index(m)
            ref = recording.exchanges[min(pos, len(recording.exchanges) - 1)]
            diffs = json_diff(ref.request, m.get("request", {}))
            where = "; ".join(f"{p_}: recorded {x} vs now {y}" for p_, x, y in diffs) or "body differs"
            problems.append(f"replay {i + 1}: model call {pos} matched no recording ({where}); if this value changes on every run, "
                            f"add a regex for it to [replay] masks")
        rec_calls = _mcp_calls(recording)
        if rec_calls is not None:
            ledger = p.mcp_ledger
            lost = [e for e in ledger if e.get("unrecorded")]
            if lost:
                problems.append(f"replay {i + 1}: tool call {lost[0]['tool']} {lost[0]['args_key'][:60]} matched no recording; if a value in "
                                f"its arguments changes on every run, the replay cannot be stable")
            elif len(ledger) != len(tool_calls(rec_calls)):
                problems.append(f"replay {i + 1}: {len(ledger)} tool call(s) now, {len(tool_calls(rec_calls))} recorded")
        if not p.session.complete and not misses:
            problems.append(f"replay {i + 1}: only {len(p.session.used)} of {len(recording.exchanges)} recorded exchanges were requested")
        if o.stdout.strip() != recording.meta.get("stdout", "").strip():
            problems.append(f"replay {i + 1}: output differs from the recorded run")
        if model:
            churn.append(len({r['port'] for r in model}) / len(model))
    return {"stable": not problems, "problems": problems, "replay_seconds": round(max(secs), 2) if secs else None,
            "connections_per_call": round(max(churn), 2) if churn else None,
            "churn": bool(churn) and len(recording.exchanges) >= 3 and max(churn) >= 0.9,
            "requests": [{k: r.get(k) for k in ("user-agent",)} for r in ua[:1]]}


def trial(spec, recording, fault, at, heal_after=1, path=0, timeout_s=None, continuation=True, **params):
    """One fault at one exchange; returns a TrialResult with the verdict."""
    f = make_fault(fault, **params) if isinstance(fault, str) else fault
    plan = FaultPlan(f, at=at, heal_after=heal_after)
    if not f.applies(recording.exchanges[at]):
        return TrialResult(path=path, fault=f.name, at=at, verdict="N/A", detail=f"{f.name} does not apply to exchange {at}")
    calls = _mcp_calls(recording)
    healthy = recording.meta.get("stdout", "")
    with ProxyThread(mode="replay", recording=recording, faults=plan, masks=spec.masks, continuation=continuation) as p, \
            McpRun(spec, "replay", calls) as m:
        def watch():
            # With MCP tool calls the run must finish: stopping at the first sign of SILENT could cut off the writes that come after it
            if calls is None and early_silent(at, p.requests):
                # With continuation, a change of course CrossTrace cannot read yet (DIVERGED, REPROMPTED) is settled by how the run ends
                if not (continuation and next_move_verdict(f.name, at, recording, list(p.requests), healthy) in ("DIVERGED", "REPROMPTED")):
                    return "silent"
            if any(r.get("unbounded") for r in list(p.requests)):
                return "storm"                  # the agent kept sending different requests past the cap: it will not stop on its own
            if attempts_at(at, p.requests) > STORM_LIMIT:
                return "storm"
            return None
        o = run_agent(spec, p.base_url, replay=True, watch=watch, timeout_s=timeout_s, mcp_control=m.path)
        ledger = m.ledger()
    res = classify(f.name, at, recording, list(p.requests), o.exit_code, o.stdout, o.timed_out, recording.meta.get("stdout", ""),
                   path=path, seconds=o.seconds)
    if calls is not None:                   # a write that ran again while the agent recovered is a problem even when the answer is right
        res = write_check(res, calls, ledger, *effective_lists(spec.mcp, calls))
    return res


def tool_trial(spec, recording, fault, at, path=0, timeout_s=None):
    """Lose the response to the `at`-th MCP tool call (the tool ran, the agent never hears) and see what the agent does next."""
    f = make_fault(fault) if isinstance(fault, str) else fault
    calls = _mcp_calls(recording)
    if not calls or not 0 <= at < len(tool_calls(calls)):
        return TrialResult(path=path, fault=f.name, at=at, verdict="N/A", detail=f"{f.name} does not apply: no MCP tool call {at} recorded")
    control_fault = {"name": f.name, "at": at, "how": f.p["how"], "persistent": f.p["persistent"]}
    with ProxyThread(mode="replay", recording=recording, masks=spec.masks) as p, McpRun(spec, "replay", calls, control_fault) as m:
        o = run_agent(spec, p.base_url, replay=True, timeout_s=timeout_s, mcp_control=m.path)
        ledger = m.ledger()
    writes, reads, idempotent = effective_lists(spec.mcp, calls)
    return classify_tool(f.name, at, calls, ledger, o.exit_code, o.stdout, o.timed_out, recording.meta.get("stdout", ""), path=path,
                         seconds=o.seconds, writes=writes, reads=reads, idempotent=idempotent)


def effective_params(name, params, timeout_s):
    """Fault timing that keeps verdicts honest. A stall must outlast the trial budget, or an agent with no timeout would simply wait it out
    and pass. An idle kill must happen inside the budget, so the trial shows how the agent reacts to it."""
    p = dict(params)
    if name == "stream_stall" and "stall_s" not in p:
        p["stall_s"] = timeout_s + 30.0
    if name == "idle_kill" and "idle_s" not in p:
        p["idle_s"] = min(60.0, timeout_s / 2)
    if name == "slow_first_byte" and "delay_s" not in p:
        p["delay_s"] = min(10.0, timeout_s / 4)
    return p


def plan_trials(recordings, faults, max_exchanges=0, seed=0, only_path=None, only_at=None):
    """[(path, FaultSpec, exchange)] for every applicable exchange; with max_exchanges, a seeded sample that always keeps the first and
    last applicable exchange (the same commit always gets the same trials)."""
    import random
    out = []
    for pi, rec in enumerate(recordings):
        if only_path is not None and pi != only_path:
            continue
        for fs in faults:
            proto = make_fault(fs.name, **fs.params)
            if proto.tool_fault:
                ks = list(range(len(tool_calls(_mcp_calls(rec) or []))))
            else:
                ks = [k for k, ex in enumerate(rec.exchanges) if proto.applies(ex)]
            if only_at is not None:
                ks = [k for k in ks if k == only_at]
            if max_exchanges and len(ks) > max_exchanges:
                rng = random.Random(f"{seed}:{pi}:{fs.label}")
                keep = {ks[0], ks[-1]} if max_exchanges >= 2 else {ks[0]}
                keep |= set(rng.sample(ks[1:-1], max(0, max_exchanges - len(keep))))
                ks = sorted(keep)
            out += [(pi, fs, k) for k in ks]
    return out


def run_planned(spec_for, recordings, planned, timeout_s, jobs=1, progress=None, continuation=True):
    """Run planned trials, `jobs` at a time (each trial has its own proxy and agent process). Results in plan order."""
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        pi, fs, k = item
        f = make_fault(fs.name, **effective_params(fs.name, fs.params, timeout_s))
        if f.tool_fault:
            r = tool_trial(spec_for(pi), recordings[pi], f, k, path=pi, timeout_s=timeout_s)
        else:
            r = trial(spec_for(pi), recordings[pi], f, k, heal_after=fs.heal, path=pi, timeout_s=timeout_s, continuation=continuation)
        r.fault = fs.label
        if progress:
            progress(r)
        return r

    if jobs <= 1:
        return [one(x) for x in planned]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(one, planned))


def matrix(spec, recordings, faults, heal_after=1, timeout_s=None, exchanges=None, progress=None):
    """Every fault at every applicable exchange of every recording. faults: names or (name, params) pairs."""
    results = []
    for pi, rec in enumerate(recordings):
        for fspec in faults:
            name, params = (fspec, {}) if isinstance(fspec, str) else fspec
            proto = make_fault(name, **params)
            for at, ex in enumerate(rec.exchanges):
                if exchanges is not None and at not in exchanges:
                    continue
                if not proto.applies(ex):
                    continue
                r = trial(spec, rec, make_fault(name, **params), at, heal_after=heal_after, path=pi, timeout_s=timeout_s)
                results.append(r)
                if progress:
                    progress(r)
    return results


def load_recordings(paths):
    return [Recording.load(p) for p in paths]
