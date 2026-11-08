"""Mock external HTTP API used by the agent's http_api tool.

GET /shipping/<customer_id> -> fixed JSON. --delay-ms is a fault hook for later stages.
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path.startswith("/shipping/"):
            if self.server.delay_ms:
                time.sleep(self.server.delay_ms / 1000)
            cid = self.path.rsplit("/", 1)[1]
            return self._send(200, {"customer_id": cid, "status": "in_transit", "eta_days": 2})
        self._send(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5: with dozens of simultaneous clients the kernel resets the surplus
    # connections ("Connection reset by peer"), which is a limit of this test server, not of the thing being measured.
    request_queue_size = 2048    # above the largest test concurrency (512): no accept-queue overflow
    daemon_threads = True


def make_server(port, delay_ms=0):
    srv = Server(("127.0.0.1", port), Handler)
    srv.delay_ms = delay_ms
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8802)
    ap.add_argument("--delay-ms", type=int, default=0)
    a = ap.parse_args()
    srv = make_server(a.port, a.delay_ms)
    print("READY", flush=True)
    srv.serve_forever()
