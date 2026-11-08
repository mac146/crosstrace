"""The same agent task, built with LangGraph (create_react_agent) and a real model behind an OpenAI-compatible API.

Purpose: test crosstrace against an agent it did not shape. Our own agent records pid and thread id by construction;
here the framework decides which thread runs what, so this checks whether the join survives that.

Instrumentation is added the way a LangGraph user would: wrap the model call (a ChatOpenAI subclass) and each tool, so
every span is created in the thread that really makes the call. Nothing in the diagnosers or the correlator knows
about LangGraph.

Secrets: the key is read from the environment or the git-ignored .env file, only inside this process. Every exception
message is scrubbed of it before it can reach a span or the database.
"""
import os
import re
from typing import Any
from urllib.parse import urlparse

os.environ.setdefault("LANGSMITH_TRACING", "false")        # no third-party tracing from the framework
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

from langchain_core.tools import tool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402
from pydantic import Field  # noqa: E402

DEFAULT_BASE_URL = "https://opencode.ai/inference/openai/v1"
DEFAULT_MODEL = "glm-5.3-flash-free"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "z-ai/glm-5.3-flash"      # cheap, supports tools; ~$0.03 in / $0.08 out per million tokens
ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
# running totals for the cost check printed by eval/real_langgraph.py
TOTAL = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
TOKEN_CAP = 300_000    # hard stop for one process (about $0.03 at the planned model): a guard against a loop gone wrong

SYSTEM = (
    "You are a customer-support assistant. Answer using the tools, calling ONE tool per turn. "
    "Step 1: db_query to get the order total for customer 42 from the SQLite table "
    "orders(id, customer_id, total), e.g. SELECT SUM(total) FROM orders WHERE customer_id = 42. "
    "Step 2: http_api with path /shipping/42 to get the shipping status. "
    "Step 3: slow_tool with work_ms=50 as a final consistency check. "
    "Then reply with one short sentence giving the total and the shipping status."
)


def load_settings():
    """(api_key, base_url, model) from the environment, falling back to an env file. Values are never printed.

    The env file is CROSSTRACE_ENV_FILE if set (it can point at another project's .env, which is then read in place
    and never copied), else this repo's .env. OPENROUTER_API_KEY wins over OPENCODE_API_KEY when both exist. The model
    is CROSSTRACE_MODEL or a cheap default; another project's own OPENROUTER_MODEL is deliberately ignored, since it
    may be an expensive one."""
    env = dict(os.environ)
    path = os.environ.get("CROSSTRACE_ENV_FILE") or ENV_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    v = v.strip().strip("\"'")
                    if v and not env.get(k):
                        env[k] = v
    if env.get("OPENROUTER_API_KEY"):
        return (env["OPENROUTER_API_KEY"], env.get("OPENROUTER_BASE_URL") or OPENROUTER_URL,
                env.get("CROSSTRACE_MODEL") or OPENROUTER_MODEL)
    return (env.get("OPENCODE_API_KEY"), env.get("OPENCODE_BASE_URL") or DEFAULT_BASE_URL,
            env.get("CROSSTRACE_MODEL") or env.get("OPENCODE_MODEL") or DEFAULT_MODEL)


def scrub(text, secret):
    """Remove the secret (and anything shaped like a bearer token) from a string."""
    text = str(text)
    if secret:
        text = text.replace(secret, "[redacted]")
    return re.sub(r"(?i)(bearer\s+|sk-)[A-Za-z0-9._\-]{8,}", r"\1[redacted]", text)


class TracedChat(ChatOpenAI):
    """ChatOpenAI that records one 'chat' span with an HTTP child span around every model call."""

    tracing: Any = Field(default=None, exclude=True)
    secret: Any = Field(default=None, exclude=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if TOTAL["input_tokens"] + TOTAL["output_tokens"] > TOKEN_CAP:
            raise RuntimeError("token cap reached, refusing further model calls")
        u = urlparse(str(self.openai_api_base))
        port = u.port or (443 if u.scheme == "https" else 80)
        t = self.tracing
        with t.span(f"chat {self.model_name}", {"gen_ai.operation.name": "chat",
                                                 "gen_ai.request.model": self.model_name}):
            attrs = {"http.request.method": "POST", "server.address": u.hostname, "server.port": port,
                     "url.full": f"{u.scheme}://{u.hostname}{u.path}/chat/completions", "http.request.resend_count": 0}
            with t.span("POST", attrs, client=True) as http_span:
                try:
                    result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
                except Exception as e:
                    http_span.set_attribute("http.response.status_code", getattr(e, "status_code", 0) or 0)
                    raise RuntimeError(f"model call failed: {type(e).__name__}: {scrub(e, self.secret)[:300]}") from None
                http_span.set_attribute("http.response.status_code", 200)
        for gen in result.generations:
            usage = getattr(gen.message, "usage_metadata", None) or {}
            TOTAL["calls"] += 1
            TOTAL["input_tokens"] += usage.get("input_tokens", 0)
            TOTAL["output_tokens"] += usage.get("output_tokens", 0)
        return result


def build_tools(tools, tracing):
    """LangChain tools that call our Tools and record the 'execute_tool' span in the executing thread."""
    def traced(name, args):
        with tracing.span(f"execute_tool {name}", {"gen_ai.operation.name": "execute_tool",
                                                   "gen_ai.tool.name": name}):
            return tools.call(name, args)

    @tool
    def db_query(sql: str) -> str:
        """Run a read-only SQL query on the orders database."""
        return traced("db_query", {"sql": sql})

    @tool
    def http_api(path: str) -> str:
        """GET a path from the shipping API, e.g. /shipping/42."""
        return traced("http_api", {"path": path})

    @tool
    def slow_tool(work_ms: int) -> str:
        """Run the consistency-check tool for work_ms milliseconds."""
        return traced("slow_tool", {"work_ms": work_ms})

    return [db_query, http_api, slow_tool]


def run_langgraph(task, tools, tracing, max_tokens=512, endpoint=None):
    """Run the task; spans go to `tracing`. Returns the final answer text.

    endpoint=(base_url, api_key, model, max_retries) overrides the .env settings; used to point the same agent at the
    local mock model server. Without it the real endpoint from .env is used, with the client's own retries off
    (so each HTTP span is one attempt)."""
    if endpoint:
        base_url, key, model, retries = endpoint
    else:
        key, base_url, model = load_settings()
        retries = 0
    if not key:
        raise RuntimeError("no API key (OPENROUTER_API_KEY / OPENCODE_API_KEY) in the environment or the env file")
    llm = TracedChat(model=model, base_url=base_url, api_key=key, max_tokens=max_tokens, timeout=60,
                     max_retries=retries, tracing=tracing, secret=key)
    graph = create_react_agent(llm, build_tools(tools, tracing), prompt=SYSTEM)
    with tracing.span("invoke_agent langgraph", {"gen_ai.operation.name": "invoke_agent",
                                                  "gen_ai.agent.name": "langgraph-react"}):
        try:
            out = graph.invoke({"messages": [("user", task)]}, {"recursion_limit": 14})
        except Exception as e:
            raise RuntimeError(scrub(f"{type(e).__name__}: {e}", key)[:400]) from None
    return out["messages"][-1].content
