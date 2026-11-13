"""The report: one versioned JSON document (the contract CI and tools read), rendered for the terminal and as Markdown (PR summary)."""
import json
import os
import sys
import time

from crosstrace import __version__
from crosstrace.fixes import advice, churn_advice
from crosstrace.proxy.faults import TOOL_FAULTS
from crosstrace.verdict import ORDER, PROBLEM, REVIEW, SETUP

SCHEMA = "crosstrace.report/1"


def _where(t):
    """"path 0 exchange 2" for a model call, "path 0 tool call 1" for an MCP tool call."""
    unit = "tool call" if t["fault"].split("!")[0] in TOOL_FAULTS else "exchange"
    return f"path {t['path']} {unit} {t['at']}"


def build(cfg, mode, recordings, stability, results, seconds, seed, not_applicable=None):
    allow = set(cfg.allow)
    faults = {}
    for r in results:
        f = faults.setdefault(r.fault, {"counts": {}, "applicable": 0, "trials": []})
        f["counts"][r.verdict] = f["counts"].get(r.verdict, 0) + 1
        if r.verdict not in SETUP:
            f["applicable"] += 1
        f["trials"].append(r)
    failing = [r for r in results if r.verdict in PROBLEM and r.verdict not in allow]
    review = [r for r in results if r.verdict in REVIEW and r.verdict not in allow]
    unstable = [s for s in stability if not s["stable"]]
    churn = [s for s in stability if s.get("churn")]
    status = "setup_error" if unstable else ("problems" if failing else "ok")
    by_line = {}                                   # one advice line, all the verdicts it answers (no line printed twice)
    for r in failing + review:
        for line in advice(r.verdict, r.fault.split("!")[0], r.requests):
            vs = by_line.setdefault(line, [])
            if r.verdict not in vs:
                vs.append(r.verdict)
    fixes = {}
    for line, vs in by_line.items():
        fixes.setdefault(", ".join(vs), []).append(line)
    if churn:
        fixes.setdefault("CHURN", [churn_advice(churn[0].get("requests"))])

    def trial_json(r, full):
        d = {k: getattr(r, k) for k in ("path", "fault", "at", "verdict", "detail", "attempts", "retry_gaps_s", "exit_code", "seconds")}
        d["observed"] = r.observed                          # the facts behind the verdict: attempts, fault status, the agent's next move
        d["rerun"] = f"crosstrace test {r.trial_id.replace('--fault ' + r.fault.split('!')[0], '--fault ' + r.fault)}"
        if full:
            d["stdout_tail"] = r.stdout[-500:]
            d["requests"] = [{k: q.get(k) for k in ("idx", "how", "status", "fault", "attempt", "healed", "t", "port")} for q in r.requests
                             if q.get("provider")]
        return d

    return {
        "schema": SCHEMA, "version": __version__, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": status,
        "mode": mode.name, "seed": seed, "seconds": round(seconds, 1),
        "agent": {"command": cfg.command, "inputs": cfg.inputs},
        "paths": [{"path": i, "command": r.meta.get("command"), "exchanges": len(r.exchanges),
                   "streamed": sum(e.stream for e in r.exchanges), "stable": s["stable"], "problems": s["problems"],
                   "connections_per_call": s.get("connections_per_call"), "churn": s.get("churn")}
                  for i, (r, s) in enumerate(zip(recordings, stability))],
        "allow": sorted(allow),
        "faults": {name: {"counts": f["counts"], "applicable": f["applicable"],
                          "worst": min(f["counts"], key=ORDER.index)} for name, f in faults.items()},
        "not_applicable": dict(not_applicable or {}),
        "trials": [trial_json(r, r.verdict in PROBLEM | REVIEW) for r in results],
        "failing": len(failing), "review": len(review),
        "fixes": fixes,
    }


# ---- rendering ------------------------------------------------------------------------------------------------------------

def _color():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _mark(worst, allowed, md):
    bad = worst in PROBLEM and worst not in allowed
    rev = worst in REVIEW and worst not in allowed
    if md:
        return "❌" if bad else ("⚠️" if rev else ("➖" if worst in SETUP else "✅"))
    return "✗" if bad else ("!" if rev else ("-" if worst in SETUP else "✓"))


def _counts(f):
    order = sorted(f["counts"].items(), key=lambda kv: ORDER.index(kv[0]))
    return ", ".join(f"{v} {n}/{f['applicable'] or n}" for v, n in order)


def _example(rep, name):
    for t in rep["trials"]:
        if t["fault"] == name and t["verdict"] in PROBLEM | REVIEW and t["verdict"] not in rep["allow"]:
            return t
    return None


ASCII = str.maketrans({"✓": "+", "✗": "x", "·": "|", "…": "...", "❌": "x", "✅": "+", "➖": "-", "⚠": "!"})


def for_terminal(text, stream=None):
    """The text as the terminal can show it: plain ASCII marks when its encoding cannot (e.g. cp1252 consoles on Windows)."""
    enc = getattr(stream or sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.translate(ASCII).encode(enc, "replace").decode(enc)


def render_text(rep):
    c = _color()
    red, yel, grn, dim, bold, end = (("\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[0m") if c else ("",) * 6)
    allowed = set(rep["allow"])
    n_trials = len(rep["trials"])
    ex = sum(p["exchanges"] for p in rep["paths"])
    out = [f"{bold}CrossTrace {rep['version']}{end} · mode {rep['mode']} · {len(rep['paths'])} recorded path(s) · {ex} exchanges · "
           f"{n_trials} trials · {rep['seconds']:.0f} s", ""]
    for p in rep["paths"]:
        if not p["stable"]:
            out.append(f"{red}✗ path {p['path']}: replay is not stable, its trials say nothing about the agent{end}")
            out += [f"    {x}" for x in p["problems"]]
    width = max([len(n) for n in rep["faults"]] + [20])
    for name, f in rep["faults"].items():
        m = _mark(f["worst"], allowed, False)
        col = red if m == "✗" else (yel if m == "!" else (dim if m == "-" else grn))
        line = f"{col}{m}{end} {name.ljust(width)}  {_counts(f)}"
        t = _example(rep, name)
        if t:
            line += f"\n  {' ' * width}  {dim}e.g. {_where(t)}: {t['detail']}{end}"
        out.append(line)
    for name, why in rep.get("not_applicable", {}).items():
        out.append(f"{dim}- {name.ljust(width)}  not applicable: {why}{end}")
    churn = [p for p in rep["paths"] if p.get("churn")]
    if churn:
        out.append(f"{yel}! connection churn{end}  {churn[0]['connections_per_call']} new connections per model call")
    out.append("")
    if rep["fixes"]:
        out.append(f"{bold}How to fix{end}")
        for v, lines in rep["fixes"].items():
            out.append(f"  {v}:")
            out += [f"    - {x}" for x in lines]
        out.append("")
    first = next((t for t in rep["trials"] if t["verdict"] in PROBLEM and t["verdict"] not in allowed), None)
    if rep["status"] == "setup_error":
        out.append(f"{red}Setup problem: fix replay stability first (see above).{end}")
    elif rep["status"] == "problems":
        out.append(f"{red}{rep['failing']} failing trial(s){end}" + (f", {rep['review']} to review" if rep["review"] else "") +
                   (f" · rerun one: {first['rerun']}" if first else ""))
    else:
        out.append(f"{grn}No problems found.{end}" + (f" {rep['review']} trial(s) to review." if rep["review"] else ""))
    return "\n".join(out)


def render_markdown(rep):
    allowed = set(rep["allow"])
    ex = sum(p["exchanges"] for p in rep["paths"])
    head = {"ok": "✅ No problems found", "problems": f"❌ {rep['failing']} failing trial(s)", "setup_error": "⚠️ Replay is not stable"}
    out = [f"### CrossTrace: {head[rep['status']]}", "",
           f"Mode `{rep['mode']}` · {len(rep['paths'])} recorded path(s) · {ex} exchanges · {len(rep['trials'])} trials · "
           f"{rep['seconds']:.0f} s · CrossTrace {rep['version']}", ""]
    for p in rep["paths"]:
        if not p["stable"]:
            out += [f"**Path {p['path']} replay is not stable:**", ""] + [f"- {x}" for x in p["problems"]] + [""]
    out += ["| | fault | verdicts | example |", "|---|---|---|---|"]
    for name, f in rep["faults"].items():
        t = _example(rep, name)
        eg = f"{_where(t)}: {t['detail']}" if t else ""
        out.append(f"| {_mark(f['worst'], allowed, True)} | `{name}` | {_counts(f)} | {eg.replace('|', '/')} |")
    for name, why in rep.get("not_applicable", {}).items():
        out.append(f"| ➖ | `{name}` | not applicable | {why} |")
    churn = [p for p in rep["paths"] if p.get("churn")]
    if churn:
        out += ["", f"⚠️ **Connection churn:** {churn[0]['connections_per_call']} new connections per model call."]
    if rep["fixes"]:
        out += ["", "<details><summary>How to fix</summary>", ""]
        for v, lines in rep["fixes"].items():
            out += [f"**{v}**", ""] + [f"- {x}" for x in lines] + [""]
        out += ["</details>"]
    first = next((t for t in rep["trials"] if t["verdict"] in PROBLEM and t["verdict"] not in allowed), None)
    if first:
        out += ["", f"Rerun one locally: `{first['rerun']}`"]
    return "\n".join(out) + "\n"


def save(rep, reports_dir):
    os.makedirs(reports_dir, exist_ok=True)
    stamp = rep["created"].replace(":", "").replace("-", "")
    paths = {}
    for name in (f"{stamp}.json", "latest.json"):
        paths[name] = os.path.join(reports_dir, name)
        with open(paths[name], "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, default=str)
    paths["latest.md"] = os.path.join(reports_dir, "latest.md")
    with open(paths["latest.md"], "w", encoding="utf-8") as f:
        f.write(render_markdown(rep))
    return paths
