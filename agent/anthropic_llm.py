"""Real-model client for the realism check (Stage 6): same interface as agent.llm_client.LLMClient.

Uses the official `anthropic` SDK (optional dependency, see requirements-real.txt). The agent loop speaks an
OpenAI-style message format, so this adapter converts to and from the Messages API. Two details that matter:

* Thinking blocks must be passed back unchanged, so each assistant message returned from chat() carries the raw
  content blocks under "_anthropic_content" and the converter replays them as they came.
* The SDK does its own retries by default. They are switched off (max_retries=0) so each HTTP span is exactly one
  attempt, as with the mock client; rate limits and 5xx are retried here with backoff, one span per attempt.

Credentials: ANTHROPIC_API_KEY, or the file ~/.config/crosstrace/anthropic_key (mode 600), or whatever the SDK finds
itself. The key is never printed or stored in a span.
"""
import json
import os
import time

import anthropic

MODEL = "claude-haiku-5-5"
BASE_URL = "https://api.anthropic.com"
SERVER = ("api.anthropic.com", 443)
KEY_FILE = os.path.expanduser("~/.config/crosstrace/anthropic_key")

SYSTEM = (
    "You are a customer-support assistant. Answer using the tools, calling ONE tool per turn. "
    "Step 1: db_query to get the order total for customer 42 from the SQLite table "
    "orders(id, customer_id, total), e.g. SELECT SUM(total) FROM orders WHERE customer_id = 42. "
    "Step 2: http_api with path /shipping/42 to get the shipping status. "
    "Step 3: slow_tool with work_ms=50 as a final consistency check. "
    "Then reply with one short sentence giving the total and the shipping status."
)

TOOLS = [
    {"name": "db_query", "description": "Run a read-only SQL query on the orders database.",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "http_api", "description": "GET a path from the shipping API, e.g. /shipping/42.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "slow_tool", "description": "Run the consistency-check tool for work_ms milliseconds.",
     "input_schema": {"type": "object", "properties": {"work_ms": {"type": "integer"}},
                      "required": ["work_ms"]}},
]


# running totals over the whole process, printed by eval/real_model.py as a cost check
TOTAL = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def load_api_key():
    """The key from the environment or the key file, else None (the SDK then tries its own credential chain)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            return f.read().strip() or None
    return None


def to_anthropic(messages):
    """OpenAI-style history -> Messages API history. Consecutive tool results become ONE user message."""
    out, results = [], []
    for m in messages:
        if m["role"] == "tool":
            results.append({"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]})
            continue
        if results:
            out.append({"role": "user", "content": results})
            results = []
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            out.append({"role": "assistant", "content": m["_anthropic_content"]})
        else:
            raise ValueError(f"unexpected role {m['role']!r}")
    if results:
        out.append({"role": "user", "content": results})
    return out


def from_anthropic(resp):
    """Messages API response -> OpenAI-style assistant message (+ raw blocks for replay)."""
    blocks = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
             for b in blocks if b["type"] == "tool_use"]
    text = "".join(b["text"] for b in blocks if b["type"] == "text") or None
    msg = {"role": "assistant", "content": text, "_anthropic_content": blocks}
    if calls:
        msg["tool_calls"] = calls
    return msg


class AnthropicLLM:
    def __init__(self, tracing, model=MODEL, max_retries=3, backoff_s=1.0, max_tokens=1024):
        self.tracing, self.model = tracing, model
        self.max_retries, self.backoff_s, self.max_tokens = max_retries, backoff_s, max_tokens
        self.retries = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.client = anthropic.Anthropic(api_key=load_api_key(), max_retries=0)

    def chat(self, messages):
        api_messages = to_anthropic(messages)
        for attempt in range(self.max_retries + 1):
            attrs = {"http.request.method": "POST", "server.address": SERVER[0], "server.port": SERVER[1],
                     "url.full": f"{BASE_URL}/v1/messages", "http.request.resend_count": attempt}
            status, resp, err = None, None, None
            with self.tracing.span("POST", attrs, client=True) as span:
                try:
                    resp = self.client.messages.create(
                        model=self.model, max_tokens=self.max_tokens, system=SYSTEM, tools=TOOLS,
                        messages=api_messages, output_config={"effort": "low"})
                    status = 200
                except anthropic.APIStatusError as e:   # includes RateLimitError (429) and 5xx
                    status, err = e.status_code, e
                span.set_attribute("http.response.status_code", status)
            if resp is not None:
                if resp.stop_reason == "refusal":
                    raise RuntimeError(f"model refused: {getattr(resp, 'stop_details', None)}")
                if resp.stop_reason == "max_tokens":
                    raise RuntimeError("model hit max_tokens")
                self.usage["input_tokens"] += resp.usage.input_tokens
                self.usage["output_tokens"] += resp.usage.output_tokens
                TOTAL["calls"] += 1
                TOTAL["input_tokens"] += resp.usage.input_tokens
                TOTAL["output_tokens"] += resp.usage.output_tokens
                return from_anthropic(resp)
            if (status == 429 or status >= 500) and attempt < self.max_retries:
                self.retries += 1
                time.sleep(self.backoff_s * (2 ** attempt))
                continue
            raise err
