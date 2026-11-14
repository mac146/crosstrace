"""MCP over streamable HTTP, several named servers, and real-tools mode. The end-to-end part needs `mcp` and `openai` (pip install mcp openai)."""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from crosstrace.mcp.http import env_name, messages_in
from crosstrace.mcp.ledger import args_key, excess_writes, locate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(name, args, result, server=None):
    c = {"method": "tools/call", "params": {"name": name, "arguments": args},
         "result": {"content": [{"type": "text", "text": result}]}, "error": None}
    return {**c, "server": server} if server else c


# ---- pieces -------------------------------------------------------------------------------------------------------------------------

def test_messages_are_read_from_json_and_from_an_event_stream():
    assert messages_in(b'{"jsonrpc":"2.0","id":1,"result":{}}', "application/json")[0]["id"] == 1
    assert [m["id"] for m in messages_in(b'[{"id":1,"result":{}},{"id":2,"result":{}}]', "application/json")] == [1, 2]
    sse = b'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"a":1}}\n\nevent: message\ndata: {"id":8,"result":{}}\n\n'
    assert [m["id"] for m in messages_in(sse, "text/event-stream")] == [7, 8]
    assert messages_in(b"not json", "application/json") == [] and messages_in(b": ping\n\n", "text/event-stream") == []


def test_the_agent_finds_each_server_by_a_variable_made_from_its_name():
    assert env_name("shop") == "CROSSTRACE_MCP_URL_SHOP" and env_name("git-hub.v2") == "CROSSTRACE_MCP_URL_GIT_HUB_V2"


def test_a_fault_names_one_servers_nth_call():
    calls = [call("send", {}, "x", "mail"), call("send", {}, "y", "chat"), call("send", {}, "z", "chat")]
    assert locate(calls, 0) == ("mail", 0) and locate(calls, 1) == ("chat", 0) and locate(calls, 2) == ("chat", 1)


def test_the_same_tool_on_two_servers_is_counted_apart():
    calls = [call("send", {"to": 1}, "x", "mail"), call("send", {"to": 1}, "y", "chat")]
    led = lambda server, n: [{"seq": i, "server": server, "tool": "send", "args_key": args_key({"to": 1}), "write": True} for i in range(n)]
    assert excess_writes(calls, led("mail", 1) + led("chat", 1)) == []
    assert excess_writes(calls, led("mail", 2) + led("chat", 1)) == [("send", args_key({"to": 1}), 1, 2)]


def run_wrapper(name, control_path, requests):
    env = {**os.environ, "CROSSTRACE_MCP": control_path, "PYTHONPATH": REPO}
    out = subprocess.run([sys.executable, "-m", "crosstrace", "mcp-wrap", "--name", name, "--", sys.executable, "-c", "raise SystemExit(9)"],
                         input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True, env=env, timeout=30)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def test_two_named_wrappers_each_replay_only_their_own_calls_and_the_fault_hits_one(tmp_path):
    calls = [call("send", {"to": 1}, "mail says sent", "mail"), call("send", {"to": 1}, "chat says sent", "chat")]
    ledger = tmp_path / "ledger.jsonl"
    ctl = tmp_path / "control.json"
    ctl.write_text(json.dumps({"mode": "replay", "calls": calls, "ledger": str(ledger), "writes": [], "reads": [],
                               "fault": {"name": "lost_response", "server": "chat", "at": 0, "how": "error", "persistent": False}}))
    req = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send", "arguments": {"to": 1}}}]
    mail = run_wrapper("mail", str(ctl), req)
    chat = run_wrapper("chat", str(ctl), req)
    assert mail[0]["result"]["content"][0]["text"] == "mail says sent"
    assert chat[0]["error"]["message"] == "Connection closed"                       # only the chat server lost its response
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [(r["server"], r["faulted"]) for r in rows] == [("mail", False), ("chat", True)]


# ---- end to end: a real remote MCP server over HTTP, the official client, a model-call recording ----------------------------------------

pytest.importorskip("mcp")
pytest.importorskip("openai")

from crosstrace import runner  # noqa: E402
from mockservices.sse_server import serve_in_thread  # noqa: E402

EXAMPLE = os.path.join(REPO, "examples", "mcp_agents")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lines(path):
    return [json.loads(x) for x in open(path, encoding="utf-8")] if os.path.exists(path) else []


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    work = tmp_path_factory.mktemp("mcp-http")
    ledger, port = str(work / "refunds.jsonl"), free_port()
    server = subprocess.Popen([sys.executable, os.path.join(EXAMPLE, "server.py")], cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env={**os.environ, "REFUND_LEDGER": ledger, "REFUND_HTTP_PORT": str(port)})
    mock = serve_in_thread(chunks=8, gap_ms=0, tail_ms=0, echo=False)
    os.environ["OPENAI_API_KEY"] = "sk-mock"
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    def spec(mode):
        return runner.AgentSpec(command=[sys.executable, "agent.py", "--mode", mode, "--transport", "http"], cwd=EXAMPLE, timeout_s=60,
                                env={"REFUND_URL": url, "PYTHONPATH": REPO}, mcp={"http": {"refunds": url}},
                                upstreams={"openai": f"http://127.0.0.1:{mock.server_address[1]}"})

    try:
        yield spec, runner.record(spec("naive")), ledger
    finally:
        server.kill()
        mock.shutdown()
        mock.server_close()


def test_a_remote_server_is_recorded_and_the_real_refund_happened_once(remote):
    spec, rec, ledger = remote
    calls = rec.meta["mcp"]["calls"]
    assert [c["method"] for c in calls if c["method"] != "notifications/initialized"][-1] == "tools/call"
    assert all(c.get("server") == "refunds" for c in calls)
    assert len(lines(ledger)) == 1


def test_replay_of_a_remote_server_is_stable_and_never_reaches_it(remote):
    spec, rec, ledger = remote
    st = runner.stability(spec("naive"), rec, runs=1)
    assert st["stable"], st["problems"]
    assert len(lines(ledger)) == 1


def test_a_lost_response_from_a_remote_server_makes_the_naive_agent_refund_twice(remote):
    spec, rec, ledger = remote
    r = runner.tool_trial(spec("naive"), rec, "lost_response", 0, timeout_s=40)
    assert r.verdict == "DUPLICATE" and r.attempts == 2, (r.verdict, r.detail)
    assert len(lines(ledger)) == 1                              # counted in the replay's ledger; the real server was not touched
    careful = runner.tool_trial(spec("careful"), rec, "lost_response", 0, timeout_s=40)
    assert careful.verdict == "SURFACED" and careful.attempts == 1


def test_real_tools_shows_the_remote_server_really_refunding_twice(remote):
    spec, rec, ledger = remote
    before = len(lines(ledger))
    r = runner.tool_trial(spec("naive"), rec, "lost_response", 0, timeout_s=60, real_tools=True)
    assert r.verdict == "DUPLICATE", (r.verdict, r.detail)
    assert len(lines(ledger)) == before + 2                     # the server really ran it twice
    assert "the real server ran it 2 times and answered the last one" in r.detail and "refund of" in r.detail
    assert r.observed.get("real_tools") is True


def test_a_dropped_connection_stops_the_official_client_even_when_the_agent_would_retry(remote):
    """how=drop: this client turns a closed connection into a crash of the whole session, so the agent never gets to retry."""
    from crosstrace.proxy.faults import make_fault
    spec, rec, ledger = remote
    r = runner.tool_trial(spec("naive"), rec, make_fault("lost_response", how="drop"), 0, timeout_s=40)
    assert r.verdict == "SURFACED" and r.attempts == 1, (r.verdict, r.detail)
