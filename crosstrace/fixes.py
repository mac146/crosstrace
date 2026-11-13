"""What to change when a verdict is a problem. Advice is specific to the SDK the agent used (read from the User-Agent the proxy logged)
and only states what the SDKs are known to do (checked in their source or our measurements; see docs/sdk-fault-table.md)."""


# What each SDK says about itself in its User-Agent, checked on real traffic or the SDK's source. Order matters: the first match wins, so the
# specific ones (Agents/Python, pydantic-ai) come before the generic client they are built on.
USER_AGENTS = [
    ("anthropic/python", "anthropic-python"), ("anthropic/js", "anthropic-js"),
    ("agents/python", "openai-agents"),            # the OpenAI Agents SDK sends "Agents/Python <version>"
    ("pydantic-ai", "pydantic-ai"),               # "pydantic-ai/<version>"; contains "ai/", so it must come before the Vercel entries
    ("openai/python", "openai-python"), ("openai/js", "openai-js"),
    ("ai-sdk", "vercel-ai"), ("ai/", "vercel-ai"),
]


def detect_sdk(requests, override=None):
    """{"family", "user_agent", "source"} for the SDK behind a run. `override` (from `[agent] sdk`) wins; otherwise the first User-Agent in the log
    that the table recognises; otherwise family None with the User-Agent that was seen, so the advice can say it could not tell."""
    seen = next((r.get("user-agent") for r in requests or [] if r.get("user-agent")), None)
    if override:
        return {"family": override, "user_agent": seen, "source": "config"}
    for r in requests or []:
        ua = (r.get("user-agent") or "").lower()
        for needle, family in USER_AGENTS:
            # "ai/" must be a whole product token ("ai/5.0"), never the end of another name
            if (needle in ua) if needle != "ai/" else (ua.startswith("ai/") or " ai/" in ua):
                return {"family": family, "user_agent": r.get("user-agent"), "source": "user-agent"}
    return {"family": None, "user_agent": seen, "source": "unknown"}


def sdk_family(requests, override=None):
    return detect_sdk(requests, override)["family"]


def describe_sdk(requests, override=None):
    """One line for the report: what was detected and how to correct it."""
    d = detect_sdk(requests, override)
    if d["family"] and d["source"] == "config":
        return f"SDK: {d['family']} (set in [agent] sdk)"
    if d["family"]:
        return f"SDK: {d['family']} (from the user-agent {d['user_agent']!r}; wrong? set [agent] sdk = \"...\" in crosstrace.toml)"
    return (f"SDK: not recognised (user-agent {d['user_agent']!r}), so the advice below lists the options; "
            f"set [agent] sdk = \"...\" in crosstrace.toml to get the one for yours")


def _options(table, intro):
    """Advice for an SDK that could not be recognised: every known fix, labelled, never one guessed one."""
    return intro + " " + " | ".join(f"{name}: {text}" for name, text in table.items())


TIMEOUT = {
    "openai-python": "pass a read timeout: `OpenAI(timeout=httpx.Timeout(60.0, read=20.0))` (the default is 600 s); "
                     "with LangChain: `ChatOpenAI(timeout=30)`; with CrewAI: `LLM(model=..., timeout=20)`",
    "anthropic-python": "pass a read timeout: `Anthropic(timeout=httpx.Timeout(60.0, read=20.0))` (the default is 10 minutes)",
    "pydantic-ai": "set a timeout in the model settings: `Agent(..., model_settings={\"timeout\": 20})` (the default is the openai client's 600 s). "
                   "A stall in the middle of a stream is then raised, not retried: wrap the run in your own retry if the agent should recover",
    "openai-agents": "give the SDK a client with a read timeout: `set_default_openai_client(AsyncOpenAI(timeout=httpx.Timeout(60.0, read=20.0)))` "
                     "(the default is 600 s). A stall in the middle of a stream is then raised, not retried: wrap `Runner.run` in your own retry "
                     "if the agent should recover",
    "openai-js": "pass `timeout` (ms) to the client or the request, e.g. `new OpenAI({ timeout: 20_000 })`",
    "anthropic-js": "pass `timeout` (ms) to the client or the request, e.g. `new Anthropic({ timeout: 20_000 })`",
    "vercel-ai": "pass an `abortSignal` with a timeout, e.g. `AbortSignal.timeout(20_000)`",
}
COMPLETION = {
    "openai-python": "after a stream ends, check that a chunk carried `choices[0].finish_reason`; if none did, treat it as a failed call and retry",
    "openai-js": "after a stream ends, check that a chunk carried `choices[0].finish_reason`; if none did, treat it as a failed call and retry",
    "anthropic-python": "check `message.stop_reason` on the final message (it is None when the stream was cut) and retry if it is missing",
    "anthropic-js": "check `stop_reason` on the final message and retry if it is missing",
    "vercel-ai": "check the result's `finishReason` and treat a missing or unknown reason as a failure",
}
BODY_ADVICE = {
    "length_cut": ["check the stop reason on every reply: `finish_reason == \"length\"` (Anthropic `stop_reason == \"max_tokens\"`, "
                   "Responses `status == \"incomplete\"`) means the answer was cut; retry with a higher `max_tokens` or treat it as incomplete"],
    "empty_content": ["treat an empty answer as a failed call: retry once, then stop with a clear error; never pass empty text to the next step"],
    "malformed_body": ["a body that is not valid JSON is a failed call: catch the parse error where the reply is read, and retry or stop "
                       "with a clear error (a retry rule that only covers HTTP errors will not see it)"],
}
GENERIC_COMPLETION ="verify the streamed response completed (a finish/stop reason was received) before using it; retry if not"


def advice(verdict, fault, requests=None, sdk=None):
    """`sdk`: the family set in `[agent] sdk`, which wins over what the User-Agent says."""
    sdk = sdk_family(requests, sdk)
    if verdict == "HANG":
        return [TIMEOUT[sdk] if sdk in TIMEOUT else _options(TIMEOUT, "set a read (inactivity) timeout on model calls (SDK defaults are many "
                                                            "minutes); which SDK this is could not be told, so by SDK:"),
                "make sure a timed-out call is retried or surfaced, not awaited forever",
                "if your agent already uses a long timeout on purpose, raise `[test] trial_timeout_s` above timeout x attempts"] + (
            ["the SDK waits for whatever Retry-After says; cap it, or fail fast and let your scheduler retry"]
            if fault.startswith("rate_limit") and sdk and sdk.startswith("anthropic") else [])
    if verdict in ("SILENT", "PARTIAL"):
        if fault in BODY_ADVICE:
            return BODY_ADVICE[fault]
        if fault in ("stream_cut_clean", "tool_args_cut", "idle_kill"):
            out = [COMPLETION[sdk] if sdk in COMPLETION else GENERIC_COMPLETION]
            if fault == "tool_args_cut":
                out.append("never execute a tool call from a stream that did not complete; validate tool arguments against the schema "
                           "(required fields) before running the tool")
            return out
        return ["do not catch broad exceptions around model or tool calls and carry on; retry, or stop with a clear error",
                "if the agent must continue, tell the model explicitly that the step failed instead of passing empty or partial data"]
    if verdict == "DUPLICATE":
        return ["do not blindly retry a tool that changes something: when its response is lost you cannot know whether it ran",
                "before retrying, check the state with a read tool (an order status, a sent-mail list, the page after a submit); retry only "
                "if the change did not happen",
                "give each write an idempotency key (a request id the tool remembers) so a repeat is ignored, and pass the same key on a retry",
                "if you cannot check, stop with a clear error and say that the outcome is unknown"]
    if verdict == "RETRY_STORM":
        return ["cap retries (3-5) with exponential backoff and jitter; on a persistent outage, stop and surface the error",
                "prefer the SDK's built-in retries (`max_retries`) over a hand-written loop"]
    if verdict == "IGNORED_RETRY_AFTER":
        return ["wait at least the Retry-After header before retrying a 429; the official SDKs do this when you let them retry "
                "(`max_retries` > 0) instead of retrying yourself"]
    if verdict == "WRONG_RETRY":
        return ["do not retry 400/401/403/404/422: they fail the same way every time; retry only connection errors, timeouts, 408, 409, "
                "429 and 5xx", "for context-length errors, shrink the context (summarize, drop old turns) instead of resending"]
    if verdict == "DIVERGED":
        return ["the agent changed course after the failure in a way CrossTrace cannot classify: read what it sent next (shown above) and check "
                "that it handled the failure and that it stops if the model keeps failing",
                "if the right response is simply to try the same call again, retry it (a retry is cheaper and cannot change the conversation)"]
    if verdict == "REPROMPTED":
        return ["re-prompting the model with the error is a valid way to recover; check that nothing ran on the failed reply first",
                "make sure the re-prompt is bounded: if the model keeps failing, stop with a clear error instead of asking forever",
                "if the failure is a transport problem (a cut or empty body), retrying the same request is usually cheaper than re-prompting"]
    if verdict == "ENDED_EARLY":
        return ["the agent exited 0 without the failed step: check that the output says the task failed, or exit non-zero"]
    if verdict == "RECOVERED_DIFFERENT":
        return ["the agent recovered but produced a different output: look for state changed by the failed attempt (half-written "
                "history, duplicated messages)"]
    return []


def churn_advice(requests, sdk=None):
    sdk = sdk_family(requests, sdk)
    if sdk == "openai-python":
        return "openai-python sync streaming opens a new connection per call (Stage 21; openai-python #4040): use AsyncOpenAI on "\
               "v3.30+ or HTTP/2"
    if sdk == "openai-js":
        return "openai-node 7.5+ does not reuse connections after streams (openai-node #3050)"
    return "a new TCP+TLS connection per call costs ~0.1 s on the real path: reuse one client and check your HTTP keep-alive settings"
