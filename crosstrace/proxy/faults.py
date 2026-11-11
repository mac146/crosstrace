"""Byte-level faults (build plan step 2), injected by the model proxy on a replayed exchange.

A fault decides exactly which bytes the agent's connection receives and how the connection ends. Realistic means: what providers and
the network actually do, at the HTTP/SSE/TCP level, so the agent's real SDK code path runs. Nothing here raises an exception inside the
agent's process.

Usage:  ModelProxy(mode="replay", recording=rec, faults=FaultPlan(make_fault("stream_cut_clean"), at=3, heal_after=1))
  at          the recorded exchange (idx) to fault; None = every exchange
  heal_after  transient fault: the first N attempts of that exchange get the fault, later attempts (retries) are served normally;
              None = persistent (every attempt faulted)
"""
import asyncio
import email.utils
import json
import time
from collections import Counter

# ---- reading recorded SSE events ------------------------------------------------------------------------------------------


def parse_event(text):
    """Raw SSE event text -> (event name or None, data string or None, parsed JSON or None)."""
    name, data = None, []
    for line in text.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    d = "\n".join(data) if data else None
    try:
        obj = json.loads(d) if d and d != "[DONE]" else None
    except ValueError:
        obj = None
    return name, d, obj


TERMINAL_TYPES = {"message_delta", "message_stop", "response.completed", "response.done", "response.incomplete", "response.failed"}


def is_terminal(text):
    """An event that tells the client the answer is complete: [DONE], a finish_reason, Anthropic message_delta/stop, Responses
    completion, or OpenAI's trailing usage-only chunk. A clean cut removes these and everything after."""
    name, d, obj = parse_event(text)
    if d == "[DONE]":
        return True
    if not isinstance(obj, dict):
        return False
    if obj.get("type") in TERMINAL_TYPES or name in TERMINAL_TYPES:
        return True
    ch = obj.get("choices")
    if isinstance(ch, list):
        if any(isinstance(c, dict) and c.get("finish_reason") for c in ch):
            return True
        if not ch and obj.get("usage"):
            return True
    return False


def is_tool_arg(text):
    """An event carrying a piece of a tool call's JSON arguments."""
    _, _, obj = parse_event(text)
    if not isinstance(obj, dict):
        return False
    if obj.get("type") == "content_block_delta" and (obj.get("delta") or {}).get("type") == "input_json_delta":
        return True
    if obj.get("type") == "response.function_call_arguments.delta":
        return True
    for c in obj.get("choices") or []:
        for tc in ((c or {}).get("delta") or {}).get("tool_calls") or []:
            if (tc.get("function") or {}).get("arguments"):
                return True
    return False


def _arg_slots(obj):
    """(container, key) pairs holding tool-argument text in one parsed event (OpenAI chat, Anthropic, OpenAI Responses)."""
    if obj.get("type") == "content_block_delta" and (obj.get("delta") or {}).get("type") == "input_json_delta":
        return [(obj["delta"], "partial_json")]
    if obj.get("type") == "response.function_call_arguments.delta":
        return [(obj, "delta")]
    return [(tc["function"], "arguments") for c in obj.get("choices") or [] for tc in ((c or {}).get("delta") or {}).get("tool_calls") or []
            if (tc.get("function") or {}).get("arguments")]


def truncate_tool_args(text):
    """The event with each tool-argument piece cut to its first half: what the client would have received if the stream had broken
    inside that piece. Returns the text unchanged if a piece is too short to cut."""
    name, _, obj = parse_event(text)
    if not isinstance(obj, dict):
        return text
    slots = _arg_slots(obj)
    for c, k in slots:
        if len(c[k]) >= 2:
            c[k] = c[k][:len(c[k]) // 2]
    if not slots:
        return text
    return (f"event: {name}\n" if name else "") + "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def body_end(ex):
    """Index of the first terminal event (= number of content events before it)."""
    return next((i for i, e in enumerate(ex.events) if is_terminal(e)), len(ex.events))


# ---- provider-accurate error bodies ---------------------------------------------------------------------------------------

OPENAI_ERRORS = {
    429: ("Rate limit reached for requests. Please try again later.", "requests", "rate_limit_exceeded"),
    500: ("The server had an error while processing your request.", "server_error", None),
    503: ("The engine is currently overloaded, please try again later.", "server_error", None),
    400: ("This model's maximum context length is 128000 tokens. Please reduce the length of the messages.", "invalid_request_error",
          "context_length_exceeded"),
    401: ("Incorrect API key provided.", "invalid_request_error", "invalid_api_key"),
}
ANTHROPIC_ERRORS = {
    429: ("rate_limit_error", "Number of request tokens has exceeded your per-minute rate limit."),
    500: ("api_error", "Internal server error"),
    503: ("api_error", "Service unavailable"),
    529: ("overloaded_error", "Overloaded"),
    400: ("invalid_request_error", "prompt is too long: 210000 tokens > 200000 maximum"),
    401: ("authentication_error", "invalid x-api-key"),
}


def error_body(provider, status):
    if provider == "anthropic":
        t, m = ANTHROPIC_ERRORS.get(status, ("api_error", "error"))
        return {"type": "error", "error": {"type": t, "message": m}}
    m, t, c = OPENAI_ERRORS.get(status, ("error", "server_error", None))
    return {"error": {"message": m, "type": t, "param": None, "code": c}}


def stream_error_event(provider):
    if provider == "anthropic":
        return 'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}'
    return 'data: {"error": {"message": "The server had an error while processing your request.", "type": "server_error", "param": null, "code": null}}'


# ---- faults ---------------------------------------------------------------------------------------------------------------


class Fault:
    name = "fault"
    stream_only = False
    defaults = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.p = {**self.defaults, **params}

    def applies(self, ex):
        return ex.stream or not self.stream_only

    async def serve(self, rw, ex, entry):
        """Write the faulted response. Returns False when the connection must not be reused."""
        raise NotImplementedError

    def __repr__(self):
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in self.p.items())})"


async def _events(rw, events, gap_s=0.0):
    for i, text in enumerate(events):
        if gap_s and i:
            await asyncio.sleep(gap_s)
        await rw.chunk(text.encode() + b"\n\n")


def _cut_index(ex, frac):
    end = body_end(ex)
    return max(1, min(end, round(end * frac))) if end else 0


class HttpError(Fault):
    """A provider error status with the provider's real error body and headers (Retry-After variants for 429)."""
    defaults = {"status": 500, "retry_after": None, "retry_after_ms": None}

    def __init__(self, name, **params):
        self.name = name
        super().__init__(**params)

    async def serve(self, rw, ex, entry):
        status = self.p["status"]
        if status == 529 and ex.provider != "anthropic":
            status = 503                                    # OpenAI reports overload as 503
        headers = [("Content-Type", "application/json")]
        ra = self.p["retry_after"]
        if ra == "http-date":
            ra = email.utils.formatdate(time.time() + 2, usegmt=True)
        if ra is not None:
            headers.append(("Retry-After", str(ra)))
        if self.p["retry_after_ms"] is not None:
            headers.append(("retry-after-ms", str(self.p["retry_after_ms"])))
        if ex.provider == "anthropic":
            headers.append(("request-id", "req_crosstrace_fault"))
        entry.update(status=status, fault_detail={"retry_after": ra})
        await rw.send_full(status, headers, json.dumps(error_body(ex.provider, status)).encode())
        return True


class StreamCutClean(Fault):
    """The stream ends early but correctly: valid chunked terminator, no [DONE] / finish_reason / message_stop / response.completed.
    Exactly the case where an SDK may end its iterator without error and hand over half an answer."""
    name, stream_only, defaults = "stream_cut_clean", True, {"cut": 0.5}

    async def serve(self, rw, ex, entry):
        n = _cut_index(ex, self.p["cut"])
        entry.update(status=ex.status, fault_detail={"events_sent": n, "events_total": len(ex.events)})
        await rw.start_chunked(ex.status, ex.headers)
        await _events(rw, ex.events[:n])
        await rw.end_chunked()
        return True


class ToolArgsCut(Fault):
    """Clean cut in the middle of a tool call's streamed JSON arguments: partial-JSON parsers may execute truncated arguments.
    The cut is always inside the arguments: models often send them in one piece, so the middle piece itself is cut in half."""
    name, stream_only = "tool_args_cut", True

    def applies(self, ex):
        return ex.stream and any(is_tool_arg(e) for e in ex.events)

    async def serve(self, rw, ex, entry):
        args = [i for i, e in enumerate(ex.events) if is_tool_arg(e)]
        mid = args[(len(args) - 1) // 2]                    # the middle argument piece, sent cut in half
        events = ex.events[:mid] + [truncate_tool_args(ex.events[mid])]
        entry.update(status=ex.status, fault_detail={"events_sent": len(events), "arg_events_sent": (len(args) + 1) // 2,
                                                     "arg_events": len(args)})
        await rw.start_chunked(ex.status, ex.headers)
        await _events(rw, events)
        await rw.end_chunked()
        return True


class StreamCutAbort(Fault):
    """Half of the next chunk's bytes, then the connection ends: FIN (orderly close) or RST (reset)."""
    stream_only, defaults = True, {"cut": 0.5}

    def __init__(self, name, how, **params):
        self.name, self.how = name, how
        super().__init__(**params)

    async def serve(self, rw, ex, entry):
        n = _cut_index(ex, self.p["cut"])
        entry.update(status=ex.status, fault_detail={"events_sent": n, "events_total": len(ex.events), "close": self.how})
        await rw.start_chunked(ex.status, ex.headers)
        await _events(rw, ex.events[:n])
        nxt = (ex.events[n] + "\n\n").encode() if n < len(ex.events) else b"data: {\"partial\n\n"
        await rw.raw(b"%x\r\n" % len(nxt) + nxt[: max(1, len(nxt) // 2)])
        if self.how == "rst":
            rw.reset()
        else:
            await rw.close_fin()
        return False


class StreamStall(Fault):
    """The stream starts, then nothing for stall_s seconds while the connection stays open; then it continues (or closes)."""
    name, stream_only, defaults = "stream_stall", True, {"cut": 0.5, "stall_s": 60.0, "then": "continue"}

    async def serve(self, rw, ex, entry):
        n = _cut_index(ex, self.p["cut"])
        entry.update(status=ex.status, fault_detail={"events_before_stall": n, "stall_s": self.p["stall_s"]})
        await rw.start_chunked(ex.status, ex.headers)
        await _events(rw, ex.events[:n])
        await asyncio.sleep(self.p["stall_s"])
        if self.p["then"] == "close":
            await rw.close_fin()
            return False
        await _events(rw, ex.events[n:])
        await rw.end_chunked()
        return True


class SlowDrip(Fault):
    """Every event arrives gap_s after the previous one: never silent for long, but very slow in total."""
    name, stream_only, defaults = "slow_drip", True, {"gap_s": 2.0}

    async def serve(self, rw, ex, entry):
        entry.update(status=ex.status, fault_detail={"gap_s": self.p["gap_s"], "events": len(ex.events)})
        await rw.start_chunked(ex.status, ex.headers)
        await _events(rw, ex.events, gap_s=self.p["gap_s"])
        await rw.end_chunked()
        return True


class IdleKill(Fault):
    """Response headers, then silence (a long time to first token), and a load balancer closes the idle connection after idle_s."""
    name, stream_only, defaults = "idle_kill", True, {"idle_s": 60.0}

    async def serve(self, rw, ex, entry):
        entry.update(status=ex.status, fault_detail={"idle_s": self.p["idle_s"]})
        await rw.start_chunked(ex.status, ex.headers)
        await asyncio.sleep(self.p["idle_s"])
        await rw.close_fin()
        return False


class SlowFirstByte(Fault):
    """Nothing at all (not even headers) for delay_s, then the normal response."""
    name, defaults = "slow_first_byte", {"delay_s": 10.0}

    async def serve(self, rw, ex, entry):
        entry.update(status=ex.status, fault_detail={"delay_s": self.p["delay_s"]})
        await asyncio.sleep(self.p["delay_s"])
        if ex.stream:
            await rw.start_chunked(ex.status, ex.headers)
            await _events(rw, ex.events)
            await rw.end_chunked()
        else:
            await rw.send_full(ex.status, ex.headers, (ex.body or "").encode())
        return True


class ResetBeforeResponse(Fault):
    """The request is read, then the connection is reset before any byte of a response."""
    name = "reset_before_response"

    async def serve(self, rw, ex, entry):
        entry.update(status=None, fault_detail={"close": "rst"})
        rw.reset()
        return False


class MalformedEvent(Fault):
    """One event in the middle of the stream is invalid JSON; the rest of the stream is normal."""
    name, stream_only, defaults = "malformed_event", True, {"at": 0.5}

    async def serve(self, rw, ex, entry):
        n = _cut_index(ex, self.p["at"])
        bad = "event: content_block_delta\ndata: {\"type\": \"content_block_delta\", \"index\": 0, \"delta\": {" \
            if ex.provider == "anthropic" else "data: {\"choices\": [{\"index\": 0, \"delta\": {\"content\": "
        entry.update(status=ex.status, fault_detail={"malformed_at": n})
        await rw.start_chunked(ex.status, ex.headers)
        await _events(rw, ex.events[:n] + [bad] + ex.events[n:])
        await rw.end_chunked()
        return True


class StreamErrorEvent(Fault):
    """HTTP 200 and a normal start, then the provider's in-stream error event (e.g. Anthropic overloaded mid-stream), then the end."""
    name, stream_only, defaults = "stream_error_event", True, {"cut": 0.5}

    async def serve(self, rw, ex, entry):
        n = _cut_index(ex, self.p["cut"])
        entry.update(status=ex.status, fault_detail={"events_sent": n})
        await rw.start_chunked(ex.status, ex.headers)
        await _events(rw, ex.events[:n] + [stream_error_event(ex.provider)])
        await rw.end_chunked()
        return True


CATALOG = {
    "stream_cut_clean": lambda **p: StreamCutClean(**p),
    "tool_args_cut": lambda **p: ToolArgsCut(**p),
    "stream_cut_fin": lambda **p: StreamCutAbort("stream_cut_fin", "fin", **p),
    "stream_cut_rst": lambda **p: StreamCutAbort("stream_cut_rst", "rst", **p),
    "stream_stall": lambda **p: StreamStall(**p),
    "slow_drip": lambda **p: SlowDrip(**p),
    "idle_kill": lambda **p: IdleKill(**p),
    "slow_first_byte": lambda **p: SlowFirstByte(**p),
    "reset_before_response": lambda **p: ResetBeforeResponse(**p),
    "malformed_event": lambda **p: MalformedEvent(**p),
    "stream_error_event": lambda **p: StreamErrorEvent(**p),
    "rate_limit": lambda **p: HttpError("rate_limit", **{"status": 429, "retry_after": "2", **p}),
    "rate_limit_http_date": lambda **p: HttpError("rate_limit_http_date", **{"status": 429, "retry_after": "http-date", **p}),
    "rate_limit_long": lambda **p: HttpError("rate_limit_long", **{"status": 429, "retry_after": "3600", **p}),
    "server_error": lambda **p: HttpError("server_error", **{"status": 500, **p}),
    "unavailable": lambda **p: HttpError("unavailable", **{"status": 503, **p}),
    "overloaded": lambda **p: HttpError("overloaded", **{"status": 529, **p}),
    "context_length": lambda **p: HttpError("context_length", **{"status": 400, **p}),
    "auth": lambda **p: HttpError("auth", **{"status": 401, **p}),
}
NON_RETRYABLE = {"context_length", "auth"}


def make_fault(name, **params):
    if name not in CATALOG:
        raise ValueError(f"unknown fault {name!r}; known: {', '.join(sorted(CATALOG))}")
    return CATALOG[name](**params)


class FaultPlan:
    """Which exchange gets the fault, and when it heals. Counts every attempt per exchange (the verdict engine reads attempts)."""

    def __init__(self, fault, at=None, heal_after=1):
        self.fault = make_fault(fault) if isinstance(fault, str) else fault
        self.at, self.heal_after = at, heal_after
        self.attempts = Counter()
        self.injected = []                    # (exchange idx, attempt number)

    def select(self, entry, ex):
        if self.at is not None and ex.idx != self.at:
            return None
        if not self.fault.applies(ex):
            entry["fault_na"] = self.fault.name
            return None
        self.attempts[ex.idx] += 1
        entry["attempt"] = self.attempts[ex.idx]
        if self.heal_after is not None and self.attempts[ex.idx] > self.heal_after:
            entry["healed"] = self.fault.name
            return None
        self.injected.append((ex.idx, self.attempts[ex.idx]))
        return self.fault
