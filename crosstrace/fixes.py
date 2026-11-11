"""What to change when a verdict is a problem. Advice is specific to the SDK the agent used (read from the User-Agent the proxy logged)
and only states what the SDKs are known to do (checked in their source or our measurements; see docs/sdk-fault-table.md)."""


def sdk_family(requests):
    for r in requests or []:
        ua = (r.get("user-agent") or "").lower()
        if "anthropic/python" in ua:
            return "anthropic-python"
        if "anthropic/js" in ua:
            return "anthropic-js"
        if "openai/python" in ua:
            return "openai-python"
        if "openai/js" in ua:
            return "openai-js"
        if "ai-sdk" in ua or "ai/" in ua:
            return "vercel-ai"
    return None


TIMEOUT = {
    "openai-python": "pass a read timeout: `OpenAI(timeout=httpx.Timeout(60.0, read=20.0))` (the default is 600 s); "
                     "with LangChain: `ChatOpenAI(timeout=30)`",
    "anthropic-python": "pass a read timeout: `Anthropic(timeout=httpx.Timeout(60.0, read=20.0))` (the default is 10 minutes)",
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
GENERIC_COMPLETION = "verify the streamed response completed (a finish/stop reason was received) before using it; retry if not"


def advice(verdict, fault, requests=None):
    sdk = sdk_family(requests)
    if verdict == "HANG":
        return [TIMEOUT.get(sdk, "set a read (inactivity) timeout on model calls; SDK defaults are many minutes"),
                "make sure a timed-out call is retried or surfaced, not awaited forever",
                "if your agent already uses a long timeout on purpose, raise `[test] trial_timeout_s` above timeout x attempts"] + (
            ["the SDK waits for whatever Retry-After says; cap it, or fail fast and let your scheduler retry"]
            if fault.startswith("rate_limit") and sdk and sdk.startswith("anthropic") else [])
    if verdict in ("SILENT", "PARTIAL"):
        if fault in ("stream_cut_clean", "tool_args_cut", "idle_kill"):
            out = [COMPLETION.get(sdk, GENERIC_COMPLETION)]
            if fault == "tool_args_cut":
                out.append("never execute a tool call from a stream that did not complete; validate tool arguments against the schema "
                           "(required fields) before running the tool")
            return out
        return ["do not catch broad exceptions around model or tool calls and carry on; retry, or stop with a clear error",
                "if the agent must continue, tell the model explicitly that the step failed instead of passing empty or partial data"]
    if verdict == "RETRY_STORM":
        return ["cap retries (3-5) with exponential backoff and jitter; on a persistent outage, stop and surface the error",
                "prefer the SDK's built-in retries (`max_retries`) over a hand-written loop"]
    if verdict == "IGNORED_RETRY_AFTER":
        return ["wait at least the Retry-After header before retrying a 429; the official SDKs do this when you let them retry "
                "(`max_retries` > 0) instead of retrying yourself"]
    if verdict == "WRONG_RETRY":
        return ["do not retry 400/401/403/404/422: they fail the same way every time; retry only connection errors, timeouts, 408, 409, "
                "429 and 5xx", "for context-length errors, shrink the context (summarize, drop old turns) instead of resending"]
    if verdict == "ENDED_EARLY":
        return ["the agent exited 0 without the failed step: check that the output says the task failed, or exit non-zero"]
    if verdict == "RECOVERED_DIFFERENT":
        return ["the agent recovered but produced a different output: look for state changed by the failed attempt (half-written "
                "history, duplicated messages)"]
    return []


def churn_advice(requests):
    sdk = sdk_family(requests)
    if sdk == "openai-python":
        return "openai-python sync streaming opens a new connection per call (Stage 21; openai-python #4040): use AsyncOpenAI on "\
               "v3.30+ or HTTP/2"
    if sdk == "openai-js":
        return "openai-node 7.5+ does not reuse connections after streams (openai-node #3050)"
    return "a new TCP+TLS connection per call costs ~0.1 s on the real path: reuse one client and check your HTTP keep-alive settings"
