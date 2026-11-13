"""The model proxy server.

Modes
  record  forward to the real provider, stream the answer back unchanged, and keep every exchange (a Recording)
  replay  answer from a Recording; no network, no API cost; requests that match nothing are logged as misses
  live    forward without recording

Hop 1 (agent -> proxy) is what we test; hop 2 (proxy -> provider) is ours. Every request is logged with its hop-1 client port and,
when forwarded, the hop-2 local port, so kernel evidence can later be joined by an exact port key (never by timing).
"""
import asyncio
import json
import threading
import time

import httpx

from crosstrace.proxy.http1 import BadRequest, ResponseWriter, read_request
from crosstrace.proxy.providers import DEFAULT_UPSTREAMS, canonical_path, compile_masks, find_event_end, provider_for, request_keys
from crosstrace.proxy.recording import Exchange, Recording, ReplaySession, keep_headers

JSON = [("Content-Type", "application/json")]
SKIP_UPSTREAM_REQUEST = {"host", "content-length", "connection", "keep-alive", "accept-encoding", "transfer-encoding", "te", "upgrade",
                         "proxy-connection"}
# retry bookkeeping some SDKs send (OpenAI and Anthropic SDKs: x-stainless-retry-count); logged for the verdict engine
LOGGED_REQUEST_HEADERS = ("x-stainless-retry-count", "x-stainless-timeout", "user-agent")


def _error_body(kind, message):
    return json.dumps({"error": {"type": kind, "message": message}}).encode()


class ModelProxy:
    def __init__(self, mode="replay", recording=None, upstreams=None, masks=None, mask_defaults=True, faults=None,
                 host="127.0.0.1", port=0, speed=0.0, upstream_timeout=600.0, continuation=False, continuation_cap=8,
                 live_keys=None):
        if mode not in ("record", "replay", "live"):
            raise ValueError(f"mode must be record, replay or live, not {mode!r}")
        if mode == "replay" and recording is None:
            raise ValueError("replay needs a recording")
        self.mode = mode
        self.recording = recording if recording is not None else Recording()
        self.session = ReplaySession(self.recording) if mode == "replay" else None
        self.upstreams = {**DEFAULT_UPSTREAMS, **(upstreams or {})}
        self.masks = compile_masks(masks, defaults=mask_defaults)
        self.faults = faults                  # step 2: an object with select(entry, exchange) -> fault or None
        self.host, self.port = host, port
        self.speed = speed                    # replay pacing: 0 = as fast as possible, 1 = recorded event timing
        self.upstream_timeout = upstream_timeout
        # Continuation: after a fault, a request that matches nothing recorded (the agent changed course: re-prompted, summarised, rewrote
        # its request) is answered with the next healthy reply of the plan instead of a 502, so the run reaches its real end and the
        # verdict can come from the outcome. After `continuation_cap` such answers the agent is not stopping: it is flagged and cut off.
        self.continuation, self.continuation_cap = continuation, continuation_cap
        # Live continuation (opt-in): such a request goes to the REAL provider with the user's own key instead of getting the recorded reply, so an
        # agent's re-prompt is answered by a real model. {provider: api key}; the key is only put on the outgoing request, never logged or stored.
        self.live_keys = {k: v for k, v in (live_keys or {}).items() if v}
        self._cont_ptr, self._cont_count = None, 0
        self.connections = []                 # one dict per hop-1 connection
        self.requests = []                    # one dict per request (the log the verdict engine reads)
        self._next_idx = 0
        self._writers = set()
        self._tasks = set()
        self._server = None
        self._client = None

    # ---- lifecycle ---------------------------------------------------------------------------------------------------
    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.mode != "replay" or self.live_keys:
            t = self.upstream_timeout
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(t, connect=min(t, 15.0)), http2=False, trust_env=False)
        return self

    async def close(self):
        if self._server is not None:
            self._server.close()
            for t in list(self._tasks):          # e.g. a stall fault sleeping with the connection held open
                t.cancel()
            for w in list(self._writers):
                w.close()
            await self._server.wait_closed()
        if self._client is not None:
            await self._client.aclose()

    # ---- connections -------------------------------------------------------------------------------------------------
    async def _handle(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        conn = {"conn": len(self.connections), "port": peer[1], "t_open": time.time(), "t_close": None, "requests": 0}
        self.connections.append(conn)
        self._writers.add(writer)
        task = asyncio.current_task()
        self._tasks.add(task)
        rw = ResponseWriter(writer)
        try:
            while True:
                try:
                    req = await read_request(reader)
                except BadRequest as e:
                    await rw.send_full(400, JSON, _error_body("crosstrace_bad_request", str(e)), close=True)
                    break
                if req is None:
                    break
                conn["requests"] += 1
                keep_open = await self._dispatch(req, rw, conn)
                if not keep_open or not req.keep_alive:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            conn["t_close"] = time.time()
            self._writers.discard(writer)
            self._tasks.discard(task)
            if not writer.is_closing():
                writer.close()

    # ---- one request -------------------------------------------------------------------------------------------------
    async def _dispatch(self, req, rw, conn):
        """Returns False when the connection must not be reused (a fault closed it, or the request asked for close)."""
        path = canonical_path(req.path)
        if path != req.path:                                   # forward to the provider's real path
            req.target = path + req.target[len(req.path):]
        entry = {"conn": conn["conn"], "port": conn["port"], "n_on_conn": conn["requests"], "method": req.method, "path": path,
                 "t": time.time(), **{h: req.header(h) for h in LOGGED_REQUEST_HEADERS if req.header(h) is not None}}
        self.requests.append(entry)
        provider = provider_for(path)
        if provider is None or req.method != "POST":
            if self.mode == "replay":
                entry.update(how="not_model_api", status=404)
                await rw.send_full(404, JSON, _error_body("crosstrace_not_recorded", f"{req.method} {path} is not a model call"))
                return True
            entry["how"] = "passthrough"
            return await self._forward(req, rw, entry, provider or "openai", record=None)
        try:
            body = json.loads(req.body or b"{}")
        except ValueError:
            entry.update(how="bad_json", status=400)
            await rw.send_full(400, JSON, _error_body("crosstrace_bad_request", "request body is not JSON"))
            return True
        key, state_key, norm = request_keys(path, body, self.masks)
        entry.update(provider=provider, key=key, state_key=state_key, stream=bool(body.get("stream")))

        if self.mode == "replay":
            ex, how = self.session.match(key, state_key)
            entry.update(how=how, idx=ex.idx if ex else None)
            if ex is None:
                entry["request"] = norm            # kept only for misses: shows the user which field differs from the recording
                if self.continuation and self.live_keys.get(provider) and self._gate(entry) is not None:
                    entry.update(how="continued", live=True)
                    return await self._forward(req, rw, entry, provider, None, live_key=self.live_keys[provider])
                cont = self._continuation(body, entry) if self.continuation and not self.live_keys.get(provider) else None
                if cont is not None:
                    ex, fault = cont
                    entry.update(how="continued", served_idx=ex.idx)
                    if fault is not None:
                        entry["fault"] = fault.name
                        return await fault.serve(rw, ex, entry)
                    entry["status"] = ex.status
                    await self._serve(rw, ex)
                    return True
                entry["status"] = 502
                await rw.send_full(502, JSON, _error_body("crosstrace_replay_miss", "no recorded exchange matches this request"))
                return True
            fault = self.faults.select(entry, ex) if self.faults else None
            if fault is not None:
                entry["fault"] = fault.name
                return await fault.serve(rw, ex, entry)
            entry["status"] = ex.status
            await self._serve(rw, ex)
            return True

        idx = self._next_idx
        self._next_idx += 1
        entry.update(how=self.mode, idx=idx)
        record = None
        if self.mode == "record":
            record = Exchange(idx=idx, path=path, provider=provider, key=key, state_key=state_key, request=norm,
                              status=0, headers=[], stream=bool(body.get("stream")))
        return await self._forward(req, rw, entry, provider, record)

    def _gate(self, entry):
        """The fault plan when this unmatched request may be continued, else None. Counts it; past the cap the agent is flagged as not stopping."""
        plan = self.faults
        if plan is None or not plan.injected:
            return None
        if self._cont_ptr is None:
            self._cont_ptr = plan.at if plan.at is not None else 0
        self._cont_count += 1
        entry["continued_n"] = self._cont_count
        if self._cont_count > self.continuation_cap:
            entry["unbounded"] = True
            return None
        return plan

    def _continuation(self, body, entry):
        """(exchange, fault or None) to answer a request that matched nothing, or None for the plain 502.

        Only after a fault has been injected: before that a miss means the replay is unstable. The answer is the next recorded reply of the
        plan, starting with the reply to the failed call (a re-prompt is usually the agent asking for that step again), of the same provider
        and the same kind (streamed or not) as the request. A persistent fault keeps hitting these answers, which is how the agent's own
        recovery is tested for a bound."""
        plan = self._gate(entry)
        if plan is None:
            return None
        want_stream, exchanges = bool(body.get("stream")), self.recording.exchanges
        fits = [j for j, e in enumerate(exchanges) if e.stream == want_stream and e.provider == entry.get("provider")]
        if not fits:
            return None
        j = next((j for j in fits if j >= self._cont_ptr), fits[-1])           # past the end of the plan: the last one again
        self._cont_ptr = j + 1
        return exchanges[j], plan.select_continued(entry, exchanges[j])

    async def _serve(self, rw, ex):
        if not ex.stream:
            await rw.send_full(ex.status, ex.headers, (ex.body or "").encode())
            return
        await rw.start_chunked(ex.status, ex.headers)
        prev = 0.0
        for text, at in zip(ex.events, ex.event_ms or [0.0] * len(ex.events)):
            if self.speed > 0 and at > prev:
                await asyncio.sleep((at - prev) / 1000 * self.speed)
            prev = at
            await rw.chunk(text.encode() + b"\n\n")
        await rw.end_chunked()

    async def _forward(self, req, rw, entry, provider, record, live_key=None):
        url = self.upstreams[provider].rstrip("/") + req.target
        skip = SKIP_UPSTREAM_REQUEST | ({"authorization", "x-api-key"} if live_key else set())      # the agent's replay key is a placeholder
        headers = [(k, v) for k, v in req.headers if k.lower() not in skip] + [("Accept-Encoding", "identity")]
        if live_key:
            headers.append(("x-api-key", live_key) if provider == "anthropic" else ("Authorization", f"Bearer {live_key}"))
        t0 = time.monotonic()
        try:
            async with self._client.stream(req.method, url, headers=headers, content=req.body) as up:
                entry["status"] = up.status_code
                entry["upstream_port"] = _local_port(up)
                out_headers = list(up.headers.items())
                if up.headers.get("content-type", "").startswith("text/event-stream"):
                    events, times = [], []
                    await rw.start_chunked(up.status_code, out_headers)
                    buf = b""
                    async for data in up.aiter_bytes():
                        buf += data
                        while True:
                            i, n = find_event_end(buf)
                            if i < 0:
                                break
                            ev, buf = buf[:i], buf[i + n:]
                            events.append(ev.decode("utf-8", "replace"))
                            times.append(round((time.monotonic() - t0) * 1000, 2))
                            await rw.chunk(ev + b"\n\n")
                    if buf.strip():                        # an unterminated last event: pass it on as it came
                        events.append(buf.decode("utf-8", "replace"))
                        times.append(round((time.monotonic() - t0) * 1000, 2))
                        await rw.chunk(buf)
                    await rw.end_chunked()
                    stream_body = None
                else:
                    data = await up.aread()
                    await rw.send_full(up.status_code, out_headers, data)
                    events, times, stream_body = [], [], data.decode("utf-8", "replace")
        except httpx.HTTPError as e:
            entry.update(status=502, upstream_error=type(e).__name__)
            await rw.send_full(502, JSON, _error_body("crosstrace_upstream_error", f"{type(e).__name__}: {e}"))
            if record is not None:
                self._upstream_failure(record, type(e).__name__)
            return True
        if record is not None and not 200 <= up.status_code < 300:
            # a recording is a healthy run: a failed attempt (the agent's retry is what gets recorded) is noted, not replayed
            self._upstream_failure(record, f"HTTP {up.status_code}")
        elif record is not None:
            record.status = up.status_code
            record.headers = keep_headers(out_headers)
            record.stream = stream_body is None
            record.body = stream_body
            record.events, record.event_ms = events, times
            record.duration_ms = round((time.monotonic() - t0) * 1000, 2)
            self.recording.add(record)
        return True

    def _upstream_failure(self, record, what):
        self.recording.meta.setdefault("upstream_failures", []).append({"path": record.path, "error": what})


def _local_port(response):
    """hop-2 client port, from httpcore's network stream (None if the transport does not expose it)."""
    try:
        return response.extensions["network_stream"].get_extra_info("client_addr")[1]
    except Exception:
        return None


class ProxyThread:
    """Run a ModelProxy on its own event loop in a background thread (for synchronous callers: CLI, tests, harnesses)."""

    def __init__(self, **kwargs):
        self.proxy = ModelProxy(**kwargs)
        self.loop = None
        self.thread = None

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="crosstrace-proxy", daemon=True)
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.proxy.start(), self.loop).result(15)
        return self.proxy

    def __exit__(self, *exc):
        try:
            asyncio.run_coroutine_threadsafe(self.proxy.close(), self.loop).result(15)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(15)
            self.loop.close()
