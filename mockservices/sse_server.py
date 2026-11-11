"""Mock OpenAI-compatible chat server for Stage 21 (connection reuse of the SDKs when streaming).

POST /v1/chat/completions
  stream=true  -> HTTP/1.1 chunked SSE: `chunks` content events `gap_ms` apart, `data: [DONE]`, then the chunked terminator after `tail_ms`
  stream=false -> one JSON body with Content-Length
Every request's client port and its index on that connection are appended to server.log (ground truth for connection reuse).
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _chunk(obj):
    return {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 0, "model": "mock", **obj}


# tool-call arguments the mock streams in pieces (long enough that a cut in the middle leaves visibly truncated JSON)
TOOL_ARGS = json.dumps({"customer_id": "42", "action": "refund", "amount": 129.99, "note": "customer reported a damaged item on delivery"})


def _pieces(s, n=6):
    step = -(-len(s) // n)
    return [s[i:i + step] for i in range(0, len(s), step)]


def _has_tool_result(body):
    """OpenAI: a message with role tool. Anthropic: a user message whose content holds a tool_result block."""
    for m in body.get("messages", []):
        if m.get("role") == "tool":
            return True
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.n_on_conn = 0

    def _write_chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.n_on_conn += 1
        srv = self.server
        with srv.lock:
            srv.log.append({"port": self.client_address[1], "n_on_conn": self.n_on_conn, "t": time.time(), "stream": bool(body.get("stream")),
                            "path": self.path})
        # echo (off by default, so Stage 21 is unchanged): the answer ends with the last user message, so different requests get different answers
        tail = ""
        if srv.echo:
            users = [m for m in body.get("messages", []) if m.get("role") == "user"]
            last = users[-1]["content"] if users else ""
            tail = "|" + (last if isinstance(last, str) else json.dumps(last))
        # tools in the request and no tool result yet: answer with a call to the first tool (arguments streamed in pieces)
        if body.get("tools") and not _has_tool_result(body):
            if self.path.startswith("/v1/messages"):
                return self._anthropic_tool(body)
            if self.path.startswith("/v1/chat/completions"):
                return self._openai_tool(body)
        if self.path.startswith("/v1/messages"):
            return self._anthropic(body, tail)
        if self.path.startswith("/v1/responses"):
            return self._responses(body, tail)
        if not body.get("stream"):
            data = json.dumps({"id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": "mock",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok " * srv.chunks + tail}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": srv.chunks, "total_tokens": srv.chunks + 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for i in range(srv.chunks):
            ev = _chunk({"choices": [{"index": 0, "delta": {"content": "ok "}, "finish_reason": None}]})
            self._write_chunk(b"data: " + json.dumps(ev).encode() + b"\n\n")
            time.sleep(srv.gap_ms / 1000)
        if tail:
            ev = _chunk({"choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}]})
            self._write_chunk(b"data: " + json.dumps(ev).encode() + b"\n\n")
        last = _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._write_chunk(b"data: " + json.dumps(last).encode() + b"\n\n")
        if srv.tail_ms:
            self._write_chunk(b"data: [DONE]\n\n")
            time.sleep(srv.tail_ms / 1000)
            self.wfile.write(b"0\r\n\r\n")
        else:  # terminator in the same write as [DONE]
            data = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(data), data))
        self.wfile.flush()

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _openai_tool(self, body):
        name = body["tools"][0].get("function", {}).get("name", "tool")
        if not body.get("stream"):
            return self._json({"id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": "mock",
                               "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                                   "role": "assistant", "content": None, "tool_calls": [
                                       {"id": "call_mock", "type": "function", "function": {"name": name, "arguments": TOOL_ARGS}}]}}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 10, "total_tokens": 11}})
        self._sse_headers()

        def ev(delta, finish=None):
            self._write_chunk(b"data: " + json.dumps(_chunk({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})).encode() + b"\n\n")

        ev({"role": "assistant", "content": None,
            "tool_calls": [{"index": 0, "id": "call_mock", "type": "function", "function": {"name": name, "arguments": ""}}]})
        for piece in _pieces(TOOL_ARGS):
            ev({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
            time.sleep(self.server.gap_ms / 1000)
        ev({}, "tool_calls")
        self._write_chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _anthropic_tool(self, body):
        name = body["tools"][0].get("name", "tool")
        msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": "mock", "stop_sequence": None}
        if not body.get("stream"):
            return self._json({**msg, "content": [{"type": "tool_use", "id": "toolu_mock", "name": name, "input": json.loads(TOOL_ARGS)}],
                               "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 10}})
        self._sse_headers()

        def ev(etype, obj):
            self._write_chunk(b"event: " + etype.encode() + b"\ndata: " + json.dumps(obj).encode() + b"\n\n")

        ev("message_start", {"type": "message_start", "message": {**msg, "content": [], "stop_reason": None,
                                                                  "usage": {"input_tokens": 1, "output_tokens": 0}}})
        ev("content_block_start", {"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "tool_use", "id": "toolu_mock", "name": name, "input": {}}})
        for piece in _pieces(TOOL_ARGS):
            ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": piece}})
            time.sleep(self.server.gap_ms / 1000)
        ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 10}})
        ev("message_stop", {"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _responses(self, body, tail):
        """OpenAI Responses API, non-streaming only (enough for clients that default to it, e.g. the Vercel AI SDK)."""
        srv = self.server
        if not tail and srv.echo:
            inp = body.get("input")
            tail = "|" + (inp if isinstance(inp, str) else json.dumps(inp)[:200])
        text = "ok " * srv.chunks + tail
        data = json.dumps({"id": "resp_mock", "object": "response", "created_at": 0, "status": "completed", "model": "mock",
                           "output": [{"type": "message", "id": "msg_mock", "status": "completed", "role": "assistant",
                                       "content": [{"type": "output_text", "text": text, "annotations": []}]}],
                           "usage": {"input_tokens": 1, "output_tokens": srv.chunks, "total_tokens": srv.chunks + 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _anthropic(self, body, tail):
        """Anthropic Messages API: one JSON message, or the SSE event sequence message_start ... message_stop (no [DONE])."""
        srv = self.server
        text = "ok " * srv.chunks + tail
        usage = {"input_tokens": 1, "output_tokens": srv.chunks}
        msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": "mock", "stop_sequence": None}
        if not body.get("stream"):
            data = json.dumps({**msg, "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": usage}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def ev(name, obj):
            self._write_chunk(b"event: " + name.encode() + b"\ndata: " + json.dumps(obj).encode() + b"\n\n")

        ev("message_start", {"type": "message_start", "message": {**msg, "content": [], "stop_reason": None,
                                                                  "usage": {"input_tokens": 1, "output_tokens": 0}}})
        ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        pieces = ["ok "] * srv.chunks + ([tail] if tail else [])
        for p in pieces:
            ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}})
            time.sleep(srv.gap_ms / 1000)
        ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": srv.chunks}})
        ev("message_stop", {"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def make_server(port=0, chunks=20, gap_ms=2, tail_ms=5, echo=False):
    srv = Server(("127.0.0.1", port), Handler)
    srv.chunks, srv.gap_ms, srv.tail_ms, srv.echo = chunks, gap_ms, tail_ms, echo
    srv.log, srv.lock = [], threading.Lock()
    return srv


def serve_in_thread(**kw):
    srv = make_server(**kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
