"""MCP over streamable HTTP: a local stand-in for a remote MCP server, with the same record / replay / real modes as the stdio wrapper.

The agent's MCP config points at a URL. Under CrossTrace that URL is read from an environment variable,
CROSSTRACE_MCP_URL_<NAME>, so one config line changes (the way OPENAI_BASE_URL does for model calls); the real URL goes in crosstrace.toml:

    [mcp.http]
    shop = "https://mcp.example.com/mcp"

  record  forward each POST to the real server, relay the answer, and save every request with its response
  replay  no server and no network: answer from the recording; lose the response to one tool call (a 504, or a dropped connection)
  real    forward to the real server as in record, but lose the response to the faulted tool call (the server did the work)

What it handles: a POST with one JSON-RPC message or a batch, answered as JSON or as an event stream (read to the end, then relayed). A GET
(the optional server-to-client stream) gets 405, which the protocol allows; servers that push unprompted messages are not covered.
"""
import json
import re
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from crosstrace.mcp.ledger import reply_text
from crosstrace.mcp.wrap import Replayer, _append

DROP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "keep-alive", "content-encoding"}


def env_name(name):
    return "CROSSTRACE_MCP_URL_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def messages_in(body, content_type):
    """The JSON-RPC messages in a response body: JSON (one or a batch) or a text/event-stream of `data:` events."""
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or ""):
        out = []
        for event in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if data:
                try:
                    out.append(json.loads(data))
                except ValueError:
                    pass
        return [m for m in out if isinstance(m, dict)]
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    return [m for m in (payload if isinstance(payload, list) else [payload]) if isinstance(m, dict)]


class McpHttp:
    def __init__(self, name, url, control, mode):
        self.name, self.url, self.ctl, self.mode = name, url, control, mode
        self.replayer = None if mode == "record" else Replayer(control, name)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.client = httpx.Client(timeout=600.0) if mode != "replay" else None
        self.server = self.local_url = None

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"                  # the body ends when the connection does

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(405)
                self.send_header("Allow", "POST, DELETE")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_DELETE(self):
                if owner.mode == "replay":
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._relay(owner.client.request("DELETE", owner.url, headers=self._headers()))

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                batch = isinstance(payload, list)
                msgs = [m for m in (payload if batch else [payload]) if isinstance(m, dict)]
                if owner.mode == "replay":
                    self._replay(msgs, batch)
                else:
                    self._forward(body, msgs)

            def _headers(self):
                h = {k: v for k, v in self.headers.items() if k.lower() not in DROP}
                h["Accept-Encoding"] = "identity"
                return h

            def _send(self, status, body=b"", content_type=None, extra=()):
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                for k, v in extra:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _relay(self, resp):
                self._send(resp.status_code, resp.content, resp.headers.get("content-type"),
                           [(k, v) for k, v in resp.headers.items() if k.lower() not in DROP | {"content-type"}])

            def _lose(self):
                """The response never arrives, the way a real one is lost: a gateway that gives up on a slow server answers 504 (how=error,
                the default); how=drop closes the connection with no reply; how=silent leaves it open until the client gives up. The server
                did the work in every case."""
                how = owner.replayer.fault.get("how")
                if how == "silent":
                    owner.stopping.wait(600)
                elif how != "drop":
                    self._send(504, b"Gateway Timeout", "text/plain")

            def _replay(self, msgs, batch):
                out, lost = [], False
                with owner.lock:
                    for m in msgs:
                        owner.replayer.lost_now = False
                        out += owner.replayer.handle(m)
                        lost = lost or owner.replayer.lost_now
                if lost:
                    return self._lose()
                if not out:
                    return self._send(202)
                self._send(200, json.dumps(out if batch else out[0], separators=(",", ":")).encode("utf-8"), "application/json")

            def _forward(self, body, msgs):
                infos = {}
                if owner.mode == "real":
                    with owner.lock:
                        for m in msgs:
                            if m.get("method") == "tools/call" and m.get("id") is not None:
                                infos[m["id"]] = owner.replayer.track(m)
                try:
                    resp = owner.client.post(owner.url, content=body, headers=self._headers())
                except httpx.HTTPError:
                    return                                  # the real server is unreachable: the client sees a dropped connection
                answers = {m["id"]: m for m in messages_in(resp.content, resp.headers.get("content-type")) if "method" not in m and "id" in m}
                lost = False
                with owner.lock:
                    for m in msgs:
                        a = answers.get(m.get("id")) if m.get("method") and m.get("id") is not None else None
                        if a is None:
                            continue
                        if owner.mode == "record":
                            params = m.get("params")
                            if m["method"] == "tools/call" and isinstance(params, dict):
                                params = {k: v for k, v in params.items() if k != "_meta"}
                            _append(owner.ctl["record_path"], {"method": m["method"], "params": params, "result": a.get("result"),
                                                               "error": a.get("error"), "server": owner.name})
                        elif m["id"] in infos:
                            if owner.ctl.get("ledger"):
                                _append(owner.ctl["ledger"], {"seq": infos[m["id"]]["index"], "server": owner.name, "reply": reply_text(a)})
                            lost = lost or infos[m["id"]]["faulted"]
                if lost:
                    return self._lose()
                self._relay(resp)

        class Server(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        u = urllib.parse.urlparse(self.url)
        self.local_url = f"http://127.0.0.1:{self.server.server_address[1]}{u.path or '/'}" + (f"?{u.query}" if u.query else "")
        return self.local_url

    def stop(self):
        self.stopping.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.client:
            self.client.close()
