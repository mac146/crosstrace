"""Mock external HTTP API used by the agent's http_api tool.

GET /shipping/<customer_id> -> fixed JSON. --delay-ms is a fault hook for later stages.
"""
import argparse
import os
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
            cid = self.path.rsplit("/", 1)[1]
            # --slow-ids restricts the delay to some customers (selective fault, Stage 14); empty = every request, as before
            who = self.client_address[0]
            # --slow-from (Stage 16): delay only the requests that come from these client addresses
            if (self.server.delay_ms and (not self.server.slow_ids or cid in self.server.slow_ids)
                    and (not self.server.slow_from or who in self.server.slow_from)):
                time.sleep(self.server.delay_ms / 1000)
            return self._send(200, {"customer_id": cid, "status": "in_transit", "eta_days": 2})
        self._send(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5: with dozens of simultaneous clients the kernel resets the surplus
    # connections ("Connection reset by peer"), which is a limit of this test server, not of the thing being measured.
    # listen backlog; env MOCK_BACKLOG lowers it to provoke accept-queue overflow (Stage 12 experiments)
    request_queue_size = int(os.environ.get("MOCK_BACKLOG", "2048"))
    daemon_threads = True


def make_server(port, delay_ms=0, slow_ids=(), slow_from=()):
    srv = Server(("127.0.0.1", port), Handler)
    srv.delay_ms = delay_ms
    srv.slow_ids = set(slow_ids)
    srv.slow_from = set(slow_from)
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8802)
    ap.add_argument("--delay-ms", type=int, default=0)
    ap.add_argument("--slow-ids", default="", help="comma-separated customer ids that get the delay (default: all)")
    ap.add_argument("--slow-from", default="", help="comma-separated client addresses that get the delay (default: all)")
    a = ap.parse_args()
    srv = make_server(a.port, a.delay_ms, [x for x in a.slow_ids.split(",") if x], [x for x in a.slow_from.split(",") if x])
    print("READY", flush=True)
    srv.serve_forever()
