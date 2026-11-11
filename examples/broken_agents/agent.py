"""A small support agent (plan -> tool call -> answer) whose failure handling can be switched between good and deliberately broken.

    python agent.py --mode good|naive|swallow|storm|ignore_retry_after|retry_400|nondeterministic

Each mode is one way real agents mishandle failures; CrossTrace's verdict engine must name each one. Uses the official openai SDK with
its base URL taken from OPENAI_BASE_URL, as most agents do.

Exchanges (in order): 0 plan (streamed), 1 tool decision (not streamed, returns a tool call), 2 final answer (streamed).
"""
import argparse
import json
import random
import sys
import time

import openai
from openai import OpenAI

TOOLS = [{"type": "function", "function": {"name": "lookup_order", "description": "Look up an order",
                                           "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}}}}]

MODES = {
    # checks the stream completed, sets a real read timeout, retries a transient failure once, surfaces anything else
    "good": dict(check_finish=True, sdk_retries=2, timeout=3.0, own_retry="transient_once", on_error="raise"),
    # joins streamed text without checking that it completed; SDK default timeout (600 s)
    "naive": dict(check_finish=False, sdk_retries=2, timeout=None, own_retry=None, on_error="raise"),
    # like naive, and swallows errors to "keep going"
    "swallow": dict(check_finish=False, sdk_retries=2, timeout=3.0, own_retry=None, on_error="continue"),
    # its own retry loop: immediately, up to 30 times, on any API error
    "storm": dict(check_finish=True, sdk_retries=0, timeout=3.0, own_retry="storm", on_error="raise"),
    # retries a 429 immediately, ignoring Retry-After
    "ignore_retry_after": dict(check_finish=True, sdk_retries=0, timeout=3.0, own_retry="immediate_429", on_error="raise"),
    # retries every error after a pause, including 400 and 401
    "retry_400": dict(check_finish=True, sdk_retries=0, timeout=3.0, own_retry="all_slow", on_error="raise"),
    # puts a random number in its prompt: replay can never match (CrossTrace must report the run as unstable, not pass it)
    "nondeterministic": dict(check_finish=True, sdk_retries=2, timeout=3.0, own_retry="transient_once", on_error="raise"),
}


class IncompleteStream(Exception):
    pass


def stream_text(client, messages, cfg):
    s = client.chat.completions.create(model="gpt-mock", messages=messages, stream=True)
    text, finish = "", None
    for ev in s:
        if ev.choices:
            text += ev.choices[0].delta.content or ""
            finish = ev.choices[0].finish_reason or finish
    if cfg["check_finish"] and finish is None:
        raise IncompleteStream("stream ended without a finish_reason")
    return text


def with_retry(fn, cfg):
    tries = 0
    while True:
        try:
            return fn()
        except (openai.APIError, IncompleteStream) as e:
            tries += 1
            mode = cfg["own_retry"]
            if mode == "storm" and tries < 30:
                continue
            if mode == "immediate_429" and isinstance(e, openai.RateLimitError) and tries < 3:
                continue
            if mode == "all_slow" and tries < 3:
                time.sleep(2.5)
                continue
            if mode == "transient_once" and tries < 2 and isinstance(e, (openai.APIConnectionError, IncompleteStream)):
                continue
            raise


def step(fn, cfg, default):
    if cfg["on_error"] == "continue":
        try:
            return with_retry(fn, cfg)
        except Exception:                                  # the bug this mode demonstrates
            return default
    return with_retry(fn, cfg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=sorted(MODES), default="good")
    ap.add_argument("--question", default="Where is order 42 and when will it arrive?")
    a = ap.parse_args()
    cfg = MODES[a.mode]
    kw = {"max_retries": cfg["sdk_retries"]}
    if cfg["timeout"] is not None:
        kw["timeout"] = cfg["timeout"]
    client = OpenAI(**kw)
    question = a.question + (f" (ref {random.random()})" if a.mode == "nondeterministic" else "")
    msgs = [{"role": "system", "content": "You are a support agent."}, {"role": "user", "content": question}]

    plan = step(lambda: stream_text(client, msgs + [{"role": "user", "content": "Plan your steps briefly."}], cfg), cfg, "")
    msgs.append({"role": "assistant", "content": plan})

    r = step(lambda: client.chat.completions.create(model="gpt-mock", messages=msgs, tools=TOOLS), cfg, None)
    if r is not None and r.choices[0].message.tool_calls:
        tc = r.choices[0].message.tool_calls[0]
        args = json.loads(tc.function.arguments)
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}]})
        msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"customer_id": args.get("customer_id"), "status": "in transit"})})
    else:
        msgs.append({"role": "user", "content": "The order system did not respond. Answer anyway."})

    answer = step(lambda: stream_text(client, msgs, cfg), cfg, "")
    print(answer)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                                 # a surfaced failure: clear message, non-zero exit
        print(f"agent failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
