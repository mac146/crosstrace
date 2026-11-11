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
                 host="127.0.0.1", port=0, speed=0.0, upstream_timeout=600.0):
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
        self.connections = []                 # one dict per hop-1 connection
        self.requests = []                    # one dict per request (the log the verdict engine reads)
        self._next_idx = 0
        self._writers = set()
        self._server = None
        self._client = None

    # ---- lifecycle ---------------------------------------------------------------------------------------------------
    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.mode != "replay":
            t = self.upstream_timeout
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(t, connect=min(t, 15.0)), http2=False, trust_env=False)
        return self

    async def close(self):
        if self._server is not None:
            self._server.close()
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
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            conn["t_close"] = time.time()
            self._writers.discard(writer)
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
            ex, how = self.session.match(key)
            entry.update(how=how, idx=ex.idx if ex else None)
            if ex is None:
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

    async def _forward(self, req, rw, entry, provider, record):
        url = self.upstreams[provider].rstrip("/") + req.target
        headers = [(k, v) for k, v in req.headers if k.lower() not in SKIP_UPSTREAM_REQUEST] + [("Accept-Encoding", "identity")]
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
            return True
        if record is not None:
            record.status = up.status_code
            record.headers = keep_headers(out_headers)
            record.stream = stream_body is None
            record.body = stream_body
            record.events, record.event_ms = events, times
            record.duration_ms = round((time.monotonic() - t0) * 1000, 2)
            self.recording.add(record)
        return True


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
