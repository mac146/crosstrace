"""Framework matrix (build plan step 1): does each SDK/framework send its model calls through the proxy when ONLY environment
variables are set (no code change), and does a recorded run replay identically?

    python scripts/framework_matrix.py [--py ~/.venvs/crosstrace-matrix/bin/python] [--node ...]

For each framework: a tiny program that makes one model call with no base URL in code. It runs against the proxy in record mode
(in front of the mock provider) with env variant A (the standard variables); if no model call reaches the proxy, variant B (the older
*_API_BASE names) and C (base URL with /v1 for Anthropic) are tried. A routed framework is then replayed twice with no provider
at all, and its output must be identical. Writes docs/framework-matrix.md.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crosstrace.proxy import ProxyThread                    # noqa: E402
from mockservices.sse_server import serve_in_thread        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_DIR = os.path.join(ROOT, "scripts", "matrix_node")
MSG = "[{'role':'user','content':'hi'}]"

PY = {
    "openai-python": f"from openai import OpenAI\nprint(OpenAI().chat.completions.create(model='m', messages={MSG}).choices[0].message.content)",
    "anthropic-python": f"from anthropic import Anthropic\nprint(Anthropic().messages.create(model='m', max_tokens=20, messages={MSG}).content[0].text)",
    "langchain-openai": "from langchain_openai import ChatOpenAI\nprint(ChatOpenAI(model='m').invoke('hi').content)",
    "langchain-anthropic": "from langchain_anthropic import ChatAnthropic\nprint(ChatAnthropic(model='m').invoke('hi').content)",
    "langgraph react agent (OpenAI)": (
        "from langgraph.prebuilt import create_react_agent\nfrom langchain_openai import ChatOpenAI\nfrom langchain_core.tools import tool\n"
        "@tool\ndef ping(x: str) -> str:\n    '''echo'''\n    return x\n"
        "print(create_react_agent(ChatOpenAI(model='m'), [ping]).invoke({'messages': [('user', 'hi')]})['messages'][-1].content)"),
    "litellm openai/": f"import litellm\nprint(litellm.completion(model='openai/m', messages={MSG}).choices[0].message.content)",
    "litellm anthropic/": f"import litellm\nprint(litellm.completion(model='anthropic/m', max_tokens=20, messages={MSG}).choices[0].message.content)",
    "pydantic-ai openai": "from pydantic_ai import Agent\nprint(Agent('openai:m').run_sync('hi').output)",
    "pydantic-ai anthropic": "from pydantic_ai import Agent\nprint(Agent('anthropic:m').run_sync('hi').output)",
}
JS = {
    "openai-node": "import OpenAI from 'openai';\nconst r = await new OpenAI().chat.completions.create({model:'m', messages:[{role:'user',content:'hi'}]});\nconsole.log(r.choices[0].message.content);",
    "anthropic-typescript": "import Anthropic from '@anthropic-ai/sdk';\nconst r = await new Anthropic().messages.create({model:'m', max_tokens:20, messages:[{role:'user',content:'hi'}]});\nconsole.log(r.content[0].text);",
    "vercel ai sdk + @ai-sdk/openai (default: Responses API)": "import {generateText} from 'ai'; import {openai} from '@ai-sdk/openai';\nconsole.log((await generateText({model: openai('m'), prompt: 'hi'})).text);",
    "vercel ai sdk + @ai-sdk/openai .chat()": "import {generateText} from 'ai'; import {openai} from '@ai-sdk/openai';\nconsole.log((await generateText({model: openai.chat('m'), prompt: 'hi'})).text);",
    "vercel ai sdk + @ai-sdk/anthropic": "import {generateText} from 'ai'; import {anthropic} from '@ai-sdk/anthropic';\nconsole.log((await generateText({model: anthropic('m'), prompt: 'hi'})).text);",
}
UNTESTED = {"crewai": "not installable here: on Python 3.14 it builds an old tiktoken from source and fails"}


def variants(base):
    a = {"OPENAI_BASE_URL": base + "/v1", "ANTHROPIC_BASE_URL": base}
    return [("A: OPENAI_BASE_URL, ANTHROPIC_BASE_URL", a),
            ("B: + OPENAI_API_BASE, ANTHROPIC_API_BASE", {**a, "OPENAI_API_BASE": base + "/v1", "ANTHROPIC_API_BASE": base}),
            ("C: ANTHROPIC_BASE_URL with /v1", {**a, "ANTHROPIC_BASE_URL": base + "/v1"})]


def clean_env(extra):
    env = {k: v for k, v in os.environ.items() if not any(s in k for s in ("BASE_URL", "API_BASE", "API_KEY", "PROXY"))}
    env.update(OPENAI_API_KEY="sk-matrix-fake", ANTHROPIC_API_KEY="sk-ant-matrix-fake", LITELLM_LOG="ERROR", **extra)
    return env


def run(kind, code, env, a):
    if kind == "py":
        argv, cwd, inp = [a.py, "-c", code], ROOT, None
    else:
        argv, cwd, inp = [a.node, "--input-type=module"], NODE_DIR, code
    try:
        p = subprocess.run(argv, cwd=cwd, input=inp, capture_output=True, text=True, timeout=120, env=env)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


def check(name, kind, code, upstream, a):
    for label, extra in variants("BASE"):
        with ProxyThread(mode="record", upstreams={"openai": upstream, "anthropic": upstream}) as p:
            env = clean_env({k: v.replace("BASE", p.base_url) for k, v in extra.items()})
            rc, out, err = run(kind, code, env, a)
        model_calls = [r for r in p.requests if r.get("provider")]
        paths = sorted({r["path"] for r in p.requests})
        if model_calls:
            break
    row = {"name": name, "variant": label if model_calls else "none worked", "paths": paths, "routed": bool(model_calls),
           "ok": rc == 0, "err": "" if rc == 0 else (err.splitlines() or [""])[-1][:160], "replay": "-"}
    if model_calls:
        outs = []
        for _ in range(2):
            with ProxyThread(mode="replay", recording=p.recording) as rp:
                env = clean_env({k: v.replace("BASE", rp.base_url) for k, v in extra.items()})
                rc2, out2, err2 = run(kind, code, env, a)
            outs.append((rc2, out2, all(r.get("how") in ("in_order", "out_of_order") for r in rp.requests if r.get("provider")),
                         rp.session.complete))
        same = all(o == (rc, out, True, True) for o in outs)
        row["replay"] = "identical x2" if same else f"DIFFERS: {[(o[0], o[2], o[3]) for o in outs]}"
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--py", default=os.path.expanduser("~/.venvs/crosstrace-matrix/bin/python"))
    ap.add_argument("--node", default=os.path.expanduser("~/.local/node-v22.11.0-linux-x64/bin/node"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "framework-matrix.md"))
    a = ap.parse_args()
    srv = serve_in_thread(chunks=3, gap_ms=0, tail_ms=0, echo=True)
    upstream = f"http://127.0.0.1:{srv.server_address[1]}"
    rows = []
    try:
        for name, code in PY.items():
            rows.append(check(name, "py", code, upstream, a)); print(rows[-1], file=sys.stderr, flush=True)
        for name, code in JS.items():
            rows.append(check(name, "js", code, upstream, a)); print(rows[-1], file=sys.stderr, flush=True)
    finally:
        srv.shutdown()
        srv.server_close()
    versions = subprocess.run([a.py, "-m", "pip", "list"], capture_output=True, text=True).stdout
    keep = ("openai ", "anthropic ", "langchain-openai", "langchain-anthropic", "langgraph ", "litellm", "pydantic-ai ")
    vers = [l.split()[0] + " " + l.split()[1] for l in versions.splitlines() if l.lower().startswith(keep)]
    lines = ["# Framework matrix: do model calls go through the proxy with environment variables only?", "",
             "Generated by `scripts/framework_matrix.py` (build plan step 1). Each framework makes one model call with no base URL in code.",
             "Variant A sets only `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL`; B adds `OPENAI_API_BASE`/`ANTHROPIC_API_BASE`; C sets "
             "`ANTHROPIC_BASE_URL` with `/v1`. The first variant that routes a model call through the proxy is shown. Routed frameworks are "
             "then replayed twice with no provider at all.", "",
             "| framework | routes through proxy | env needed | paths seen | call succeeded (mock) | replay | error |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['name']} | {'yes' if r['routed'] else '**no**'} | {r['variant']} | {', '.join(r['paths'])} | "
                     f"{'yes' if r['ok'] else 'no'} | {r['replay']} | {r['err'].replace('|', '/')} |")
    for n, why in UNTESTED.items():
        lines.append(f"| {n} | untested | - | - | - | - | {why} |")
    lines += ["", "Python packages: " + ", ".join(vers) + ".", "JS packages: see scripts/matrix_node (openai, @anthropic-ai/sdk, ai, @ai-sdk/openai, @ai-sdk/anthropic)."]
    open(a.out, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
