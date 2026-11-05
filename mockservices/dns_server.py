"""Mock DNS resolver: answers every A query with 127.0.0.1 (--delay-ms is the dns_delay fault hook)."""
import argparse
import socketserver
import time

from agent.dns import build_response


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        if self.server.delay_ms:
            time.sleep(self.server.delay_ms / 1000)
        sock.sendto(build_response(data, "127.0.0.1"), self.client_address)


class Server(socketserver.ThreadingUDPServer):
    daemon_threads = True


def make_server(port, delay_ms=0):
    srv = Server(("127.0.0.1", port), Handler)
    srv.delay_ms = delay_ms
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8803)
    ap.add_argument("--delay-ms", type=int, default=0)
    a = ap.parse_args()
    srv = make_server(a.port, a.delay_ms)
    print("READY", flush=True)
    srv.serve_forever()
