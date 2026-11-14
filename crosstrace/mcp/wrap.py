"""`crosstrace mcp-wrap -- <server command>`: a stdio wrapper between an MCP client and an MCP server.

The agent's MCP config launches this instead of the server. Outside CrossTrace (no CROSSTRACE_MCP in the environment) it just runs the
server, so the config can stay as it is. CrossTrace controls it through a JSON control file named by CROSSTRACE_MCP:

  record  run the real server, pass every message through unchanged and save each request with its response
  replay  no server: answer from the recording; optionally lose the response to one tool call; keep a ledger of every tool call
  real    (only with `crosstrace test --real-tools`) run the real server and pass calls through, but lose the response to one tool call as in
          replay, so a retry really runs twice; the ledger also keeps what the server answered

Several servers: give each wrapper a name, `crosstrace mcp-wrap --name github -- <server>`. Recorded calls carry the name and each wrapper
replays only its own. With one server the name can be left out.

MCP over stdio is newline-delimited JSON-RPC. The wrapper only parses what it needs (id, method, params), and forwards the original line.
Note: the official Python SDK starts servers with a reduced environment, so an agent that uses it must pass CROSSTRACE_MCP on.
"""
import json
import os
import subprocess
import sys
import threading

from crosstrace.mcp.ledger import args_key, call_key, is_write, reply_text

ENV = "CROSSTRACE_MCP"
LOST = {"code": -32000, "message": "Connection closed"}        # what an MCP client reports when the transport dies under a request
NO_RECORD = "crosstrace: this tool call was not in the recorded healthy run"


def _parse(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def _send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class Replayer:
    """Answers a client from the recorded calls of one named server (None: the only one). One instance per wrapper process, or per
    HTTP server."""

    def __init__(self, control, name=None):
        self.ctl = control
        self.name = name
        self.writes, self.reads = control.get("writes", []), control.get("reads", [])
        fault = control.get("fault")
        self.fault = fault if fault and fault.get("server") == name else None        # a fault is aimed at one server's n-th call
        self.lost_now = False
        self.by_method = {}
        self.results = {}                               # (tool, args_key) -> [recorded response, ...] in order
        for c in control.get("calls", []):
            if c.get("server") != name:
                continue
            resp = {"error": c["error"]} if c.get("error") is not None else {"result": c.get("result")}
            if c.get("method") == "tools/call":
                self.results.setdefault(call_key(c), []).append(resp)
            else:
                self.by_method.setdefault(c.get("method"), resp)
        self.arrivals = 0                               # tools/call arrivals so far, retries included
        self.seen = {}                                  # (tool, args_key) -> arrivals that were not retries of a lost call
        self.lost = None                                # (tool, args_key, occurrence) of the call whose response was lost, until retried

    def handle(self, msg):
        """The responses to send for one client message (none for notifications and for a lost response)."""
        method, mid = msg.get("method"), msg.get("id")
        if method is None or mid is None:               # a notification, or the client's reply to a server request
            return []
        if method == "tools/call":
            return self.tool_call(msg)
        if method == "ping":
            return [{"jsonrpc": "2.0", "id": mid, "result": {}}]
        if method in self.by_method:
            return [{"jsonrpc": "2.0", "id": mid, **self.by_method[method]}]
        return [{"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}]

    def track(self, msg):
        """Count one tools/call arrival, decide whether its response is the lost one, and write the ledger line. Returns what was decided."""
        p = msg.get("params") or {}
        name, key = p.get("name"), args_key(p.get("arguments"))
        index = self.arrivals
        self.arrivals += 1
        write = is_write(name, self.writes, self.reads)
        faulted, attempt = False, 1
        if self.lost and self.lost[:2] == (name, key):          # the agent is calling again what it lost the response to
            attempt, occ = 2, self.lost[2]
            if self.fault.get("persistent"):
                faulted = True
            else:
                self.lost = None
        else:
            occ = self.seen.get((name, key), 0)
            self.seen[(name, key)] = occ + 1
            if self.fault and self.fault.get("at") == index:
                faulted, self.lost = True, (name, key, occ)
        recorded = self.results.get((name, key), [])
        resp = recorded[occ] if occ < len(recorded) else (recorded[-1] if recorded else None)
        if self.ctl.get("ledger"):
            _append(self.ctl["ledger"], {"seq": index, "server": self.name, "tool": name, "args_key": key, "write": write, "faulted": faulted,
                                         "attempt": attempt, "unrecorded": resp is None})
        return {"index": index, "tool": name, "key": key, "faulted": faulted, "attempt": attempt, "resp": resp}

    def lost_reply(self, msg):
        """What the client gets in place of a lost response: nothing (silent) or the error an MCP client reports when its transport dies."""
        return [] if self.fault.get("how") == "silent" else [{"jsonrpc": "2.0", "id": msg["id"], "error": dict(LOST)}]

    def tool_call(self, msg):
        info = self.track(msg)
        self.lost_now = info["faulted"]
        resp = info["resp"]
        if info["faulted"]:                                     # the tool ran (the ledger says so); the client never hears
            return self.lost_reply(msg)
        if resp is None:
            return [{"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": NO_RECORD}], "isError": True}}]
        return [{"jsonrpc": "2.0", "id": msg["id"], **resp}]


def run_replay(control, name=None):
    r = Replayer(control, name)
    for line in sys.stdin:
        msg = _parse(line)
        if msg is None:
            continue
        for resp in r.handle(msg):
            _send(resp)
    return 0


def run_live(cmd, control, name=None):
    """`real` mode: the real server runs and every message passes through, but the response to the faulted tool call never reaches the
    client, so the client's retry makes the server do the work again. The ledger keeps what the server answered."""
    r = Replayer(control, name)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    pending, lock = {}, threading.Lock()

    def from_server():
        for line in proc.stdout:
            msg = _parse(line)
            if msg is not None and msg.get("id") is not None and "method" not in msg:
                with lock:
                    info = pending.pop(msg["id"], None)
                if info:
                    if control.get("ledger"):
                        _append(control["ledger"], {"seq": info["index"], "server": name, "reply": reply_text(msg)})
                    if info["faulted"]:
                        for out in r.lost_reply(msg):
                            _send(out)
                        continue
            sys.stdout.write(line)
            sys.stdout.flush()

    t = threading.Thread(target=from_server, daemon=True)
    t.start()
    for line in sys.stdin:
        msg = _parse(line)
        if msg is not None and msg.get("method") == "tools/call" and msg.get("id") is not None:
            info = r.track(msg)
            with lock:
                pending[msg["id"]] = info
        proc.stdin.write(line)
        proc.stdin.flush()
    try:
        proc.stdin.close()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        proc.terminate()
    t.join(timeout=2)
    return proc.returncode or 0


def run_record(cmd, control, name=None):
    """Run the real server and pass everything through; save every client request that gets a response."""
    path = control["record_path"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    pending, lock = {}, threading.Lock()

    def from_server():
        for line in proc.stdout:
            msg = _parse(line)
            if msg is not None and msg.get("id") is not None and "method" not in msg:       # a response to a client request
                with lock:
                    req = pending.pop(msg["id"], None)
                if req:
                    _append(path, {"method": req[0], "params": req[1], "result": msg.get("result"), "error": msg.get("error"),
                                   **({"server": name} if name else {})})
            sys.stdout.write(line)
            sys.stdout.flush()

    t = threading.Thread(target=from_server, daemon=True)
    t.start()
    for line in sys.stdin:
        msg = _parse(line)
        if msg is not None and msg.get("method") and msg.get("id") is not None:
            params = msg.get("params")
            if msg["method"] == "tools/call" and isinstance(params, dict):
                params = {k: v for k, v in params.items() if k != "_meta"}                 # a progress token differs on every run
            with lock:
                pending[msg["id"]] = (msg["method"], params)
        proc.stdin.write(line)
        proc.stdin.flush()
    try:
        proc.stdin.close()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        proc.terminate()
    t.join(timeout=2)
    return proc.returncode or 0


def main(argv):
    argv, name = list(argv), None
    if len(argv) >= 2 and argv[0] == "--name":
        name, argv = argv[1], argv[2:]
    cmd = list(argv[1:] if argv and argv[0] == "--" else argv)
    if not cmd:
        print("usage: crosstrace mcp-wrap [--name NAME] -- <server command> [args...]", file=sys.stderr)
        return 2
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8", newline="\n")
    path = os.environ.get(ENV)
    if not path:
        return subprocess.call(cmd)                             # not under CrossTrace: just the server
    with open(path, encoding="utf-8") as f:
        control = json.load(f)
    if control.get("mode") == "replay":
        return run_live(cmd, control, name) if control.get("real") else run_replay(control, name)
    return run_record(cmd, control, name)
