"""crosstrace: find the failures your AI agent hides.

  crosstrace init      write crosstrace.toml for your agent
  crosstrace record    run your agent for real and record healthy runs (uses your API keys once)
  crosstrace test      replay with faults, no API cost; report and exit code for CI
  crosstrace report    show the last report again
  crosstrace faults    list the faults

Exit codes: 0 no problems · 1 problems found · 2 usage or config error · 3 setup problem (no recordings, unstable replay, agent failed) · 4 internal error
"""
import argparse
import glob
import hashlib
import inspect
import json
import os
import re
import shlex
import sys
import time

from crosstrace import __version__, config as C, report as R, runner
from crosstrace.proxy.faults import CATALOG, make_fault
from crosstrace.proxy.recording import Recording
from crosstrace.verdict import PROBLEM

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_SETUP, EXIT_INTERNAL = 0, 1, 2, 3, 4
NOT_APPLICABLE_WHY = {"tool_args_cut": "no streamed tool calls in the recordings",
                      "length_cut": "no non-streamed text answers in the recordings",
                      "empty_content": "no non-streamed text answers in the recordings",
                      "malformed_body": "no non-streamed model calls in the recordings"}
NOT_APPLICABLE_WHY.update({f: "no streamed model calls in the recordings" for f in
                           ("stream_cut_clean", "stream_cut_fin", "stream_cut_rst", "stream_stall", "slow_drip", "idle_kill",
                            "malformed_event", "stream_error_event")})


def err(msg):
    print(f"crosstrace: {msg}", file=sys.stderr)


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"
    return f"{s}-{hashlib.sha1(text.encode()).hexdigest()[:6]}"


# ---- init -----------------------------------------------------------------------------------------------------------------

def cmd_init(a):
    path = os.path.join(os.getcwd(), C.CONFIG_NAME)
    if os.path.exists(path) and not a.force:
        err(f"{C.CONFIG_NAME} already exists (use --force to overwrite)")
        return EXIT_USAGE
    command = shlex.split(a.command) if a.command else ["python", "agent.py", "{input}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write(C.template(command))
    os.makedirs(os.path.join(C.STATE_DIR, "recordings"), exist_ok=True)
    with open(os.path.join(C.STATE_DIR, ".gitignore"), "w", encoding="utf-8") as f:
        f.write("# recordings/ is meant to be committed (CI replays it); reports are local\nreports/\n")
    print(f"wrote {C.CONFIG_NAME}. Next:\n  1. check [agent] command and inputs\n  2. crosstrace record   (runs your agent for real once per input)\n"
          f"  3. crosstrace test")
    return EXIT_OK


# ---- record ---------------------------------------------------------------------------------------------------------------

def spec_for(cfg, command, timeout_s=None):
    return runner.AgentSpec(command=command, cwd=cfg.cwd, env=cfg.env, timeout_s=timeout_s or cfg.timeout_s, upstreams=cfg.upstreams,
                            masks=cfg.masks)


def cmd_record(a):
    cfg = C.load(a.config)
    os.makedirs(cfg.recordings_dir, exist_ok=True)
    if a.clean:
        for p in glob.glob(os.path.join(cfg.recordings_dir, "*.json")):
            os.remove(p)
    runs = a.runs or cfg.runs
    print(f"recording {len(cfg.inputs)} input(s) x {runs} run(s) against the real providers"
          + (f" ({', '.join(f'{k}={v}' for k, v in cfg.upstreams.items())})" if cfg.upstreams else "") + "; this uses your API keys")
    status, saved = EXIT_OK, 0
    for inp in cfg.inputs:
        for n in range(runs):
            cmd = cfg.command_for(inp)
            name = f"{slug(inp)}-{n + 1}.json"
            try:
                rec = runner.record(spec_for(cfg, cmd))
            except RuntimeError as e:
                err(f"{name}: {e}")
                status = EXIT_SETUP
                continue
            rec.meta["input"] = inp
            path = os.path.join(cfg.recordings_dir, name)
            rec.save(path)
            saved += 1
            st = runner.stability(spec_for(cfg, cmd), rec, runs=2)
            streamed = sum(e.stream for e in rec.exchanges)
            print(f"  {name}: {len(rec.exchanges)} model calls ({streamed} streamed), {rec.meta['seconds']:.1f} s, replay "
                  + ("stable" if st["stable"] else "NOT stable"))
            fails = rec.meta.get("upstream_failures", [])
            if fails:
                print(f"    note: the real provider failed {len(fails)} time(s) during this run "
                      f"({', '.join(sorted({f['error'] for f in fails}))}) and your agent retried; only the successful attempts were recorded")
            for p in st["problems"]:
                print(f"    {p}")
            if not st["stable"]:
                status = EXIT_SETUP
    if saved:
        print(f"{saved} recording(s) in {os.path.relpath(cfg.recordings_dir)}: they contain your prompts and the model's answers; review "
              f"them, then commit them so CI can replay without API keys")
    return status


# ---- test -----------------------------------------------------------------------------------------------------------------

def load_recordings(cfg):
    paths = sorted(glob.glob(os.path.join(cfg.recordings_dir, "*.json")))
    return paths, [Recording.load(p) for p in paths]


def replay_command(cfg, rec):
    """Replay with the command from the current config (so a changed flag or a fixed agent is what gets tested); the stability check
    tells if the recording no longer matches. Recordings made outside the CLI have no input: use their own command."""
    return cfg.command_for(rec.meta["input"]) if "input" in rec.meta else rec.meta["command"]


MIN_TRIAL_S = 60.0


def trial_budget(stability):
    """Seconds a trial may run before it is a HANG: 4x the slowest fault-free replay (the same agent under the same conditions; the
    recorded wall time would include the real provider's latency and failed attempts), at least 60 s, so an agent with a sensible
    30 s read timeout that retries once is never called a HANG."""
    replays = [s["replay_seconds"] for s in stability if s.get("replay_seconds")]
    return max(MIN_TRIAL_S, 4 * max(replays, default=0))


def cmd_test(a):
    cfg = C.load(a.config)
    if a.mode not in cfg.modes:
        err(f"unknown mode {a.mode!r}; modes: {', '.join(sorted(cfg.modes))}")
        return EXIT_USAGE
    mode = cfg.modes[a.mode]
    faults = mode.faults
    if a.fault:
        faults = [C._fault_spec(f, "--fault") for f in a.fault]
        for fs in faults:
            fs.params = {**cfg.fault_params.get(fs.name, {}), **fs.params}
    jobs = a.jobs or mode.jobs
    files, recs = load_recordings(cfg)
    if not recs:
        err(f"no recordings in {os.path.relpath(cfg.recordings_dir)}; run `crosstrace record` first")
        return EXIT_SETUP
    t0 = time.monotonic()
    log = (lambda *x: None) if a.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    log(f"checking replay of {len(recs)} recording(s)...")
    stab = [runner.stability(spec_for(cfg, replay_command(cfg, r)), r, runs=mode.stability_runs) for r in recs]
    timeout_s = float(cfg.trial_timeout_s or trial_budget(stab))
    stable_paths = {i for i, s in enumerate(stab) if s["stable"]}
    planned = [t for t in runner.plan_trials(recs, faults, mode.max_exchanges, a.seed, a.path, a.at) if t[0] in stable_paths]
    planned_labels = {fs.label for _, fs, _ in planned}
    not_applicable = {fs.label: NOT_APPLICABLE_WHY.get(fs.name, "no recorded model call it applies to")
                      for fs in faults if fs.label not in planned_labels and stable_paths}
    log(f"running {len(planned)} trial(s), {jobs} at a time, budget {timeout_s:.0f} s each...")
    done = [0]

    def progress(r):
        done[0] += 1
        if r.verdict in PROBLEM:
            log(f"  [{done[0]}/{len(planned)}] {r.fault} at path {r.path} exchange {r.at}: {r.verdict}")

    results = runner.run_planned(lambda pi: spec_for(cfg, replay_command(cfg, recs[pi]), timeout_s), recs, planned, timeout_s, jobs, progress)
    rep = R.build(cfg, mode, recs, stab, results, time.monotonic() - t0, a.seed, not_applicable)
    print(R.for_terminal(R.render_text(rep)))
    if not a.no_save:
        R.save(rep, cfg.reports_dir)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, default=str)
    if a.md:
        with open(a.md, "a", encoding="utf-8") as f:            # append: works with $GITHUB_STEP_SUMMARY
            f.write(R.render_markdown(rep))
    return {"ok": EXIT_OK, "problems": EXIT_PROBLEMS, "setup_error": EXIT_SETUP}[rep["status"]]


# ---- report / faults ------------------------------------------------------------------------------------------------------

def cmd_report(a):
    path = a.path
    if not path:
        cfg = C.load(a.config)
        path = os.path.join(cfg.reports_dir, "latest.json")
    if not os.path.exists(path):
        err(f"{path} not found; run `crosstrace test` first")
        return EXIT_SETUP
    with open(path, encoding="utf-8") as f:
        rep = json.load(f)
    if rep.get("schema") != R.SCHEMA:
        err(f"{path}: unsupported report schema {rep.get('schema')!r}")
        return EXIT_USAGE
    print(R.for_terminal(R.render_markdown(rep) if a.md else R.render_text(rep)))
    return EXIT_OK


def cmd_faults(a):
    for name in CATALOG:
        f = make_fault(name)
        doc = (inspect.getdoc(type(f)) or "").split("\n")[0]
        params = ", ".join(f"{k}={v}" for k, v in f.p.items())
        print(f"{name:24s} {'streams only  ' if f.stream_only else ''}{doc}" + (f"  [{params}]" if params else ""))
    print("\nIn crosstrace.toml or --fault: \"name\" heals after one failed attempt; \"name!persistent\" never heals.")
    return EXIT_OK


# ---- main -----------------------------------------------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="crosstrace", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"crosstrace {__version__}")
    ap.add_argument("--config", default=None, help="path to crosstrace.toml (default: ./crosstrace.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write crosstrace.toml")
    p.add_argument("--command", help='how to run your agent once, e.g. "python agent.py {input}"')
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("record", help="record healthy runs (real API calls)")
    p.add_argument("--runs", type=int, help="runs per input (default: [record] runs)")
    p.add_argument("--clean", action="store_true", help="delete existing recordings first")
    p.set_defaults(fn=cmd_record)
    p = sub.add_parser("test", help="replay with faults and report")
    p.add_argument("--mode", default="pr", help="pr (default, fast) or nightly (all faults, all exchanges)")
    p.add_argument("--fault", action="append", help="only this fault (repeatable); name or name!persistent")
    p.add_argument("--at", type=int, help="only this exchange")
    p.add_argument("--path", type=int, help="only this recorded path")
    p.add_argument("--jobs", type=int, help="trials in parallel")
    p.add_argument("--seed", type=int, default=0, help="sampling seed for pr mode (default 0: same trials every run)")
    p.add_argument("--json", help="also write the JSON report here")
    p.add_argument("--md", help="append the Markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    p.add_argument("--no-save", action="store_true", help="do not write .crosstrace/reports/")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_test)
    p = sub.add_parser("report", help="show the last report")
    p.add_argument("path", nargs="?")
    p.add_argument("--md", action="store_true")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("faults", help="list faults")
    p.set_defaults(fn=cmd_faults)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except C.ConfigError as e:
        err(str(e))
        return EXIT_USAGE
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    except Exception:                       # never exit 1 on a crash: CI would read it as "the agent has problems"
        import traceback
        traceback.print_exc()
        err(f"internal error (exit {EXIT_INTERNAL}); please report it with the traceback above")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
