"""Deterministic mock LLM server (OpenAI-style /v1/chat/completions).

The "model" follows a fixed script: one tool call per step, then a final answer.
The step is the number of tool results already present in the conversation, so the
same request always gets the same response (no randomness, no timestamps).

Fault hooks (off by default, used by faultlab in Stage 1):
  --delay-ms N         sleep N ms before responding (slow_model)
  --rate-limit-first N answer the first N requests with HTTP 429 (rate_limit)
"""
import argparse
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = [
    ("db_query", {"sql": "SELECT SUM(total) FROM orders WHERE customer_id = 42"}),
    ("http_api", {"path": "/shipping/42"}),
    ("slow_tool", {"work_ms": 50}),
]
FINAL_ANSWER = "Customer 42 has an order total on file and the package is in transit."


def build_response(messages):
    """Pure function: conversation -> assistant message."""
    step = sum(1 for m in messages if m.get("role") == "tool")
    if step < len(SCRIPT):
        name, args = SCRIPT[step]
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{step}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }],
        }
    return {"role": "assistant", "content": FINAL_ANSWER}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test/run output quiet
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        cfg = self.server.cfg
        with cfg["lock"]:
            cfg["count"] += 1
            n = cfg["count"]
        if n <= cfg["rate_limit_first"]:
            return self._send(429, {"error": {"message": "rate limited"}})
        if cfg["delay_ms"]:
            time.sleep(cfg["delay_ms"] / 1000)
        msg = build_response(req.get("messages", []))
        self._send(200, {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]})


class Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5: with dozens of simultaneous clients the kernel resets the surplus
    # connections ("Connection reset by peer"), which is a limit of this test server, not of the thing being measured.
    request_queue_size = 2048    # above the largest test concurrency (512): no accept-queue overflow
    daemon_threads = True


def make_server(port, delay_ms=0, rate_limit_first=0, tls=None):
    """tls=(cert_file, key_file) serves HTTPS, so the kernel sees an encrypted conversation (handshake records, TLS records)."""
    srv = Server(("127.0.0.1", port), Handler)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.cfg = {"delay_ms": delay_ms, "rate_limit_first": rate_limit_first,
               "count": 0, "lock": threading.Lock()}
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--delay-ms", type=int, default=0)
    ap.add_argument("--rate-limit-first", type=int, default=0)
    ap.add_argument("--tls-cert", default=None)
    ap.add_argument("--tls-key", default=None)
    a = ap.parse_args()
    srv = make_server(a.port, a.delay_ms, a.rate_limit_first, (a.tls_cert, a.tls_key) if a.tls_cert else None)
    print("READY", flush=True)
    srv.serve_forever()
