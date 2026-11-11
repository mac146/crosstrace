"""Run an agent under the proxy: record healthy paths, check that replay is stable, and run fault trials (build plan steps 3 and 5).

The agent is any command. The only thing changed for it is its environment: OPENAI_BASE_URL and ANTHROPIC_BASE_URL point at the proxy.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

from crosstrace.proxy import FaultPlan, ProxyThread, make_fault
from crosstrace.proxy.recording import Recording
from crosstrace.verdict import TrialResult, attempts_at, classify, early_silent, STORM_LIMIT

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

    def environ(self, base_url, replay):
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        # "{proxy_url}" in a configured value: for agents that read their base URL from their own variable (e.g. OPENCODE_BASE_URL)
        env.update({k: v.replace("{proxy_url}", base_url) for k, v in self.env.items()})
        env.update(OPENAI_BASE_URL=base_url + "/v1", ANTHROPIC_BASE_URL=base_url)
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


def run_agent(spec, base_url, replay, watch=None, poll_s=0.05, timeout_s=None):
    """Run the agent once. watch(): returns a reason string to stop it early (verdict already decided), else None."""
    timeout_s = timeout_s or spec.timeout_s
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        t0 = time.monotonic()
        proc = subprocess.Popen(spec.command, cwd=spec.cwd, env=spec.environ(base_url, replay), stdout=out, stderr=err, text=True,
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


def record(spec, path=None):
    """One healthy run against the real providers (or spec.upstreams), recorded. Fails loudly if the agent fails."""
    with ProxyThread(mode="record", upstreams=spec.upstreams, masks=spec.masks) as p:
        o = run_agent(spec, p.base_url, replay=False)
    if o.timed_out or o.exit_code != 0:
        raise RuntimeError(f"healthy run failed (exit {o.exit_code}, timed out {o.timed_out}): {o.stderr[-500:]}")
    if not p.recording.exchanges:
        raise RuntimeError("the agent made no model calls through the proxy: is its base URL set from OPENAI_BASE_URL/ANTHROPIC_BASE_URL?")
    p.recording.meta.update(command=spec.command, stdout=o.stdout, exit_code=o.exit_code, seconds=round(o.seconds, 2))
    if path:
        p.recording.save(path)
    return p.recording


def replay(spec, recording):
    """One replay with no fault: (outcome, proxy) for inspection."""
    with ProxyThread(mode="replay", recording=recording, masks=spec.masks) as p:
        o = run_agent(spec, p.base_url, replay=True)
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


def trial(spec, recording, fault, at, heal_after=1, path=0, timeout_s=None, **params):
    """One fault at one exchange; returns a TrialResult with the verdict."""
    f = make_fault(fault, **params) if isinstance(fault, str) else fault
    plan = FaultPlan(f, at=at, heal_after=heal_after)
    if not f.applies(recording.exchanges[at]):
        return TrialResult(path=path, fault=f.name, at=at, verdict="N/A", detail=f"{f.name} does not apply to exchange {at}")
    with ProxyThread(mode="replay", recording=recording, faults=plan, masks=spec.masks) as p:
        def watch():
            if early_silent(at, p.requests):
                return "silent"
            if attempts_at(at, p.requests) > STORM_LIMIT:
                return "storm"
            return None
        o = run_agent(spec, p.base_url, replay=True, watch=watch, timeout_s=timeout_s)
    return classify(f.name, at, recording, list(p.requests), o.exit_code, o.stdout, o.timed_out, recording.meta.get("stdout", ""),
                    path=path, seconds=o.seconds)


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


def run_planned(spec_for, recordings, planned, timeout_s, jobs=1, progress=None):
    """Run planned trials, `jobs` at a time (each trial has its own proxy and agent process). Results in plan order."""
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        pi, fs, k = item
        f = make_fault(fs.name, **effective_params(fs.name, fs.params, timeout_s))
        r = trial(spec_for(pi), recordings[pi], f, k, heal_after=fs.heal, path=pi, timeout_s=timeout_s)
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
