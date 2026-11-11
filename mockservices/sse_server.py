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
            srv.log.append({"port": self.client_address[1], "n_on_conn": self.n_on_conn, "t": time.time(), "stream": bool(body.get("stream"))})
        if not body.get("stream"):
            data = json.dumps({"id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": "mock",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok " * srv.chunks}, "finish_reason": "stop"}],
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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def make_server(port=0, chunks=20, gap_ms=2, tail_ms=5):
    srv = Server(("127.0.0.1", port), Handler)
    srv.chunks, srv.gap_ms, srv.tail_ms = chunks, gap_ms, tail_ms
    srv.log, srv.lock = [], threading.Lock()
    return srv


def serve_in_thread(**kw):
    srv = make_server(**kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
