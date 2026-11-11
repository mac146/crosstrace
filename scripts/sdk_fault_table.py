"""What do real SDKs do under each byte-level fault? (build plan step 2 done criterion; evidence for launch post 2)

    python scripts/sdk_fault_table.py [--timeout 20]

Each client program makes one streamed model call with its SDK's defaults (retries included, no timeouts changed). It is recorded once
against the mock provider, then replayed once per fault through the proxy (fault on the first attempt only: a retry gets the normal
answer). The outcome is classified from what the program returned and what the proxy saw:
  OK / recovered  same answer as healthy (recovered = after SDK retries)
  error           the SDK raised: the fault was surfaced (acceptable)
  SILENT          no error, but a different (partial) answer: the failure was hidden
  HANG            no answer and no error within the harness timeout (no effective timeout in the SDK)
  WRONG RETRY     a non-retryable error (400, 401) was retried
Writes docs/sdk-fault-table.md.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crosstrace.proxy import NON_RETRYABLE, FaultPlan, ProxyThread, make_fault   # noqa: E402
from mockservices.sse_server import serve_in_thread                            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYDIR = os.path.join(ROOT, "scripts", "fault_clients")
JSDIR = os.path.join(ROOT, "scripts", "matrix_node")

CLIENTS = {   # name: (kind, file)
    "openai-python stream": ("py", "openai_text.py"),
    "anthropic-python stream helper": ("py", "anthropic_text.py"),
    "openai-node stream": ("js", "clients/openai_text.mjs"),
    "anthropic-ts stream helper": ("js", "clients/anthropic_text.mjs"),
    "vercel ai sdk streamText": ("js", "clients/vercel_openai_text.mjs"),
    "anthropic-python tool call": ("py", "anthropic_tool.py"),
    "langchain-openai tool call": ("py", "langchain_openai_tool.py"),
}
FAULTS = [   # (name, params)
    ("stream_cut_clean", {}), ("tool_args_cut", {}), ("stream_cut_fin", {}), ("stream_cut_rst", {}),
    ("stream_error_event", {}), ("malformed_event", {}), ("stream_stall", {"stall_s": 60.0}), ("idle_kill", {"idle_s": 8.0}),
    ("reset_before_response", {}), ("server_error", {}), ("overloaded", {}), ("rate_limit", {}), ("rate_limit_long", {}),
    ("context_length", {}),
]


def run_client(kind, file, base, a):
    env = {k: v for k, v in os.environ.items() if not any(s in k for s in ("BASE_URL", "API_BASE", "API_KEY", "PROXY"))}
    env.update(OPENAI_BASE_URL=base + "/v1", ANTHROPIC_BASE_URL=base, OPENAI_API_KEY="sk-fake", ANTHROPIC_API_KEY="sk-ant-fake")
    argv, cwd = ([a.py, file], PYDIR) if kind == "py" else ([a.node, file], JSDIR)
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=a.timeout, env=env)
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        res = json.loads(lines[-1]) if lines else {"ok": False, "error": "no output", "msg": p.stderr[-200:]}
    except subprocess.TimeoutExpired:
        res = {"ok": False, "error": "HANG"}
    res["secs"] = round(time.monotonic() - t0, 1)
    return res


def size(out):
    return len(out) if isinstance(out, str) else len(json.dumps(out))


def classify(fault, res, healthy, attempts, retry_gap):
    if res.get("error") == "HANG":
        return "HANG", f"no answer or error within {res['secs']:.0f} s"
    retried = f", {attempts - 1} retr{'y' if attempts == 2 else 'ies'}" if attempts > 1 else ""
    gap = f" after {retry_gap:.1f} s" if retry_gap is not None else ""
    if fault in NON_RETRYABLE and attempts > 1:
        return "WRONG RETRY", f"retried a non-retryable error{gap}"
    if not res["ok"]:
        return "error", f"{res['error']}{retried}{gap}"
    if res["out"] == healthy:
        return ("recovered", f"same answer{retried}{gap}") if attempts > 1 else ("OK", "same answer")
    if isinstance(res["out"], str):
        return "SILENT", f"no error; returned {size(res['out'])} of {size(healthy)} chars"
    return "SILENT", "no error; " + json.dumps(res["out"])[:220]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--py", default=os.path.expanduser("~/.venvs/crosstrace-matrix/bin/python"))
    ap.add_argument("--node", default=os.path.expanduser("~/.local/node-v22.11.0-linux-x64/bin/node"))
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "sdk-fault-table.md"))
    ap.add_argument("--clients", nargs="+", default=list(CLIENTS))
    ap.add_argument("--faults", nargs="+", default=[f for f, _ in FAULTS])
    a = ap.parse_args()
    srv = serve_in_thread(chunks=8, gap_ms=0, tail_ms=0, echo=True)
    up = f"http://127.0.0.1:{srv.server_address[1]}"
    table = {}
    try:
        for cname in a.clients:
            kind, file = CLIENTS[cname]
            with ProxyThread(mode="record", upstreams={"openai": up, "anthropic": up}) as p:
                h = run_client(kind, file, p.base_url, a)
            assert h["ok"], f"{cname} healthy run failed: {h}"
            rec, healthy = p.recording, h["out"]
            for fname, params in FAULTS:
                if fname not in a.faults:
                    continue
                plan = FaultPlan(make_fault(fname, **params), at=0, heal_after=1)
                if not plan.fault.applies(rec.exchanges[0]):
                    table[(cname, fname)] = ("n/a", "")
                    continue
                with ProxyThread(mode="replay", recording=rec, faults=plan) as rp:
                    res = run_client(kind, file, rp.base_url, a)
                ts = [r["t"] for r in rp.requests if r.get("idx") == 0]
                gap = ts[1] - ts[0] if len(ts) > 1 else None
                table[(cname, fname)] = classify(fname, res, healthy, plan.attempts[0], gap)
                print(cname, fname, table[(cname, fname)], file=sys.stderr, flush=True)
    finally:
        srv.shutdown()
        srv.server_close()
    write(a, table)


MARK = {"OK": "✓ OK", "recovered": "✓ recovered", "error": "✓ error", "SILENT": "✗ **SILENT**", "HANG": "✗ **HANG**",
        "WRONG RETRY": "✗ **WRONG RETRY**", "n/a": "–"}


def write(a, table):
    vers = subprocess.run([a.py, "-m", "pip", "list"], capture_output=True, text=True).stdout.splitlines()
    pyv = [" ".join(l.split()[:2]) for l in vers if l.lower().startswith(("openai ", "anthropic ", "langchain-openai", "langchain-core"))]
    jsv = []
    for pkg in ("openai", "@anthropic-ai/sdk", "ai", "@ai-sdk/openai"):
        try:
            jsv.append(pkg + " " + json.load(open(os.path.join(JSDIR, "node_modules", pkg, "package.json")))["version"])
        except OSError:
            pass
    lines = ["# What real SDKs do under byte-level faults", "",
             "Generated by `scripts/sdk_fault_table.py` (build plan step 2). One streamed model call per client, SDK defaults (retries on, "
             "timeouts unchanged). Recorded once against the mock provider, then replayed per fault through the CrossTrace proxy; the fault "
             f"hits the first attempt only, so a retry gets the normal answer. Harness timeout {a.timeout:.0f} s. Faults: "
             "`stream_stall` holds the connection silent for 60 s mid-stream; `idle_kill` sends headers then closes after 8 s; "
             "`rate_limit` is 429 with `Retry-After: 2`; `rate_limit_long` is 429 with `Retry-After: 3600`.", "",
             "✓ = the failure was handled or surfaced. ✗ = it was hidden (SILENT), never ended (HANG), or a non-retryable error was retried.", ""]
    clients = [c for c in CLIENTS if any(k[0] == c for k in table)]
    faults = [f for f, _ in FAULTS if any(k[1] == f for k in table)]
    lines.append("| fault | " + " | ".join(clients) + " |")
    lines.append("|---|" + "---|" * len(clients))
    for f in faults:
        cells = []
        for c in clients:
            v, d = table.get((c, f), ("n/a", ""))
            cells.append(MARK[v] + (f"<br>{d}" if d else ""))
        lines.append(f"| `{f}` | " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    lines += ["", "Python: " + ", ".join(pyv) + ". JS: " + ", ".join(jsv) + ".",
              "Mock provider responses (text: 8 deltas; tool call: JSON arguments in 6 pieces). Loopback, HTTP/1.1.", "",
              "Notes (checked in the SDK source, not inferred from the table):",
              "- `stream_stall`: openai-python and anthropic-python default to a 600 s read timeout (`DEFAULT_TIMEOUT` in `_constants.py`), so a "
              "stalled stream holds an agent for up to 10 minutes per attempt.",
              "- `rate_limit_long`: anthropic-python's `_calculate_retry_timeout` sleeps for whatever `Retry-After` says (\"just do what it says\", "
              "capped only at about 49 days); openai-python honours it only up to 2 minutes. A HANG here means the SDK is sleeping as told.",
              "- `stream_cut_clean` (a correctly terminated chunked body without the completion event) is what a gateway or proxy produces when its "
              "upstream dies but it ends the response cleanly; the finish/stop reason is missing, which callers can check but these SDKs do not.",
              "- Tool-call rows: the healthy arguments are `customer_id`, `action`, `amount`, `note`; SILENT rows returned a tool call with fields "
              "missing and no error or invalid-call marker."]
    open(a.out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
