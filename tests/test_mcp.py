"""MCP tool calls: the write heuristic, the replaying wrapper (as a unit and as a real stdio process), the lost_response fault, and the
DUPLICATE verdict end to end with a real MCP server and the official client."""
import json
import os
import subprocess
import sys

import pytest

from crosstrace import config as C, runner
from crosstrace.mcp.ledger import args_key, excess_writes, is_write, tool_calls
from crosstrace.mcp.verdict import classify_tool, write_check
from crosstrace.mcp.wrap import ENV, Replayer
from crosstrace.proxy.faults import CATALOG, TOOL_FAULTS, make_fault
from crosstrace.proxy.recording import Exchange, Recording
from crosstrace.verdict import TrialResult

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = args_key({"customer_id": "42", "amount": 10})
CALLS = [
    {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}, "result": {"protocolVersion": "2025-06-18"}, "error": None},
    {"method": "tools/list", "params": None, "result": {"tools": [{"name": "refund"}, {"name": "refund_status"}]}, "error": None},
    {"method": "tools/call", "params": {"name": "refund", "arguments": {"customer_id": "42", "amount": 10}},
     "result": {"content": [{"type": "text", "text": "issued"}]}, "error": None},
    {"method": "tools/call", "params": {"name": "refund_status", "arguments": {"customer_id": "42"}},
     "result": {"content": [{"type": "text", "text": "10"}]}, "error": None},
]


def call(i, name, args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


def replayer(fault=None, calls=CALLS, tmp=None):
    ledger = str(tmp / "ledger.jsonl") if tmp else None
    return Replayer({"mode": "replay", "calls": calls, "fault": fault, "ledger": ledger, "writes": [], "reads": []}), ledger


def read_ledger(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")] if os.path.exists(path) else []


# ---- which calls are writes ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,write", [
    ("refund", True), ("send_email", True), ("submit_form", True), ("browser_click", True), ("createInvoice", True), ("delete-file", True),
    ("refund_status", False), ("get_order", False), ("list_files", False), ("searchDocs", False), ("check_status", False),
    ("browser_snapshot", False), ("frobnicate", False), ("", False),
    # found on the real Playwright MCP server: a key press (Enter) submits a form and a script can do anything
    ("browser_press_key", True), ("browser_evaluate", True), ("browser_run_code_unsafe", True), ("browser_drag", True), ("browser_drop", True),
])
def test_write_heuristic(name, write):
    assert is_write(name) is write


PLAYWRIGHT_TOOLS = {                     # the 25 tools of @playwright/mcp 0.0.85, and what a person would say each one is
    "write": ["browser_click", "browser_type", "browser_fill_form", "browser_file_upload", "browser_press_key", "browser_evaluate",
              "browser_run_code_unsafe", "browser_drag", "browser_drop"],
    "read": ["browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_take_screenshot", "browser_console_messages",
             "browser_network_requests", "browser_network_request", "browser_find", "browser_hover", "browser_tabs", "browser_wait_for",
             "browser_resize", "browser_close", "browser_emulate_media"],
    # genuinely ambiguous, so they are a config choice: [mcp] writes = ["browser_select_option", "browser_handle_dialog"]
    "ambiguous": ["browser_select_option", "browser_handle_dialog"],
}


def test_the_real_browser_server_tool_names_are_classified_as_documented():
    assert all(is_write(n) for n in PLAYWRIGHT_TOOLS["write"])
    assert not any(is_write(n) for n in PLAYWRIGHT_TOOLS["read"])
    assert not any(is_write(n) for n in PLAYWRIGHT_TOOLS["ambiguous"])                   # reads by default; the config lists fix it
    assert all(is_write(n, writes=PLAYWRIGHT_TOOLS["ambiguous"]) for n in PLAYWRIGHT_TOOLS["ambiguous"])


def test_config_lists_override_the_heuristic():
    assert is_write("run_report", writes=["run_report"]) is True
    assert is_write("send_digest_preview", reads=["send_digest_preview"]) is False


def test_args_key_ignores_key_order():
    assert args_key({"a": 1, "b": 2}) == args_key({"b": 2, "a": 1}) and args_key(None) == args_key({})


# ---- the replaying wrapper, as a unit -----------------------------------------------------------------------------------------

def test_replay_serves_recorded_responses_without_a_server(tmp_path):
    r, ledger = replayer(tmp=tmp_path)
    assert r.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})[0]["result"] == {"protocolVersion": "2025-06-18"}
    assert r.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0]["result"]["tools"][0]["name"] == "refund"
    out = r.handle(call(3, "refund", {"customer_id": "42", "amount": 10}))
    assert out == [{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "issued"}]}}]
    assert r.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) == []                     # a notification: no reply
    assert r.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})[0]["result"] == {}
    assert r.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})[0]["error"]["code"] == -32601
    assert [e["tool"] for e in read_ledger(ledger)] == ["refund"]


def test_a_call_that_was_never_recorded_is_flagged_not_invented(tmp_path):
    r, ledger = replayer(tmp=tmp_path)
    out = r.handle(call(1, "refund", {"customer_id": "99", "amount": 1}))[0]
    assert out["result"]["isError"] is True and "not in the recorded" in out["result"]["content"][0]["text"]
    assert read_ledger(ledger)[0]["unrecorded"] is True


def test_repeated_identical_calls_get_their_recorded_results_in_order(tmp_path):
    calls = CALLS[:2] + [{"method": "tools/call", "params": {"name": "get_order", "arguments": {}}, "result": {"n": i}, "error": None}
                         for i in (1, 2)]
    r, _ = replayer(calls=calls, tmp=tmp_path)
    got = [r.handle(call(i, "get_order", {}))[0]["result"]["n"] for i in range(3)]
    assert got == [1, 2, 2]                                                          # past the recording: the last result again


def test_lost_response_error_the_tool_runs_but_the_client_hears_a_transport_error(tmp_path):
    r, ledger = replayer({"name": "lost_response", "at": 0, "how": "error"}, tmp=tmp_path)
    out = r.handle(call(1, "refund", {"customer_id": "42", "amount": 10}))
    assert out == [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Connection closed"}}]
    entry = read_ledger(ledger)[0]
    assert entry["faulted"] is True and entry["write"] is True and entry["attempt"] == 1


def test_lost_response_silent_sends_nothing(tmp_path):
    r, ledger = replayer({"name": "lost_response", "at": 0, "how": "silent"}, tmp=tmp_path)
    assert r.handle(call(1, "refund", {"customer_id": "42", "amount": 10})) == []
    assert read_ledger(ledger)[0]["faulted"] is True


def test_the_retry_gets_the_recorded_result_and_is_counted_as_a_second_write(tmp_path):
    r, ledger = replayer({"name": "lost_response", "at": 0, "how": "error"}, tmp=tmp_path)
    args = {"customer_id": "42", "amount": 10}
    r.handle(call(1, "refund", args))
    out = r.handle(call(2, "refund", args))
    assert out[0]["result"]["content"][0]["text"] == "issued"
    entries = read_ledger(ledger)
    assert [(e["attempt"], e["faulted"]) for e in entries] == [(1, True), (2, False)]
    assert excess_writes(CALLS, entries) == [("refund", KEY, 1, 2)]


def test_a_persistent_loss_keeps_failing_the_retries(tmp_path):
    r, _ = replayer({"name": "lost_response", "at": 0, "how": "error", "persistent": True}, tmp=tmp_path)
    args = {"customer_id": "42", "amount": 10}
    assert all("error" in r.handle(call(i, "refund", args))[0] for i in range(3))


def test_only_the_chosen_call_is_faulted(tmp_path):
    r, ledger = replayer({"name": "lost_response", "at": 1, "how": "error"}, tmp=tmp_path)
    assert "result" in r.handle(call(1, "refund", {"customer_id": "42", "amount": 10}))[0]
    assert "error" in r.handle(call(2, "refund_status", {"customer_id": "42"}))[0]
    assert [e["faulted"] for e in read_ledger(ledger)] == [False, True]


# ---- DUPLICATE and the trial verdicts -----------------------------------------------------------------------------------------

def ledger_of(*entries):
    return [{"seq": i, "tool": t, "args_key": KEY if t == "refund" else args_key({"customer_id": "42"}), "write": w, "faulted": f,
             "attempt": 1, "unrecorded": u} for i, (t, w, f, u) in enumerate(entries)]


def test_no_excess_when_each_write_ran_as_often_as_in_the_healthy_run():
    assert excess_writes(CALLS, ledger_of(("refund", True, False, False), ("refund_status", False, False, False))) == []


def test_a_write_the_healthy_run_never_made_counts():
    led = ledger_of(("refund", True, False, False)) + [{"seq": 1, "tool": "send_email", "args_key": "{}", "write": True, "faulted": False,
                                                        "attempt": 1, "unrecorded": True}]
    assert excess_writes(CALLS, led) == [("send_email", "{}", 0, 1)]


def test_write_check_turns_a_recovery_into_duplicate_and_leaves_clean_runs_alone():
    twice = ledger_of(("refund", True, False, False), ("refund", True, False, False))
    res = write_check(TrialResult(path=0, fault="server_error", at=1, verdict="RECOVERED"), CALLS, twice)
    assert res.verdict == "DUPLICATE" and "ran 2 time(s), 1 in the healthy run" in res.detail and "recovered and finished" in res.detail
    once = ledger_of(("refund", True, False, False))
    assert write_check(TrialResult(path=0, fault="server_error", at=1, verdict="RECOVERED"), CALLS, once).verdict == "RECOVERED"


def trial_result(ledger, exit_code=0, stdout="done", hang=False, healthy="done"):
    return classify_tool("lost_response", 0, CALLS, ledger, exit_code, stdout, hang, healthy)


def test_lost_response_outcomes():
    lost = ledger_of(("refund", True, True, False))
    retried = ledger_of(("refund", True, True, False), ("refund", True, False, False))
    assert trial_result([]).verdict == "NOT_REACHED"
    assert trial_result(retried).verdict == "DUPLICATE"
    assert trial_result(lost, exit_code=1).verdict == "SURFACED"
    assert trial_result(lost, hang=True, exit_code=-9).verdict == "HANG"
    assert trial_result(lost).verdict == "ENDED_EARLY"                                   # carried on, never retried, never said so
    # a lost READ that is retried is harmless
    read_calls = CALLS[:2] + [CALLS[3]]
    read_led = ledger_of(("refund_status", False, True, False), ("refund_status", False, False, False))
    assert classify_tool("lost_response", 0, read_calls, read_led, 0, "done", False, "done").verdict == "RECOVERED"
    assert classify_tool("lost_response", 0, read_calls, read_led, 0, "other", False, "done").verdict == "RECOVERED_DIFFERENT"


# ---- the wrapper as a real process, speaking MCP's newline-delimited JSON-RPC over stdio -----------------------------------------

def wrap_process(argv, control=None, tmp=None):
    env = {**os.environ, "PYTHONPATH": REPO}
    env.pop(ENV, None)
    if control is not None:
        path = tmp / "control.json"
        path.write_text(json.dumps(control), encoding="utf-8")
        env[ENV] = str(path)
    return subprocess.Popen([sys.executable, "-m", "crosstrace", "mcp-wrap", "--"] + argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=REPO)


def talk(proc, messages, timeout=20):
    out, err = proc.communicate("".join(json.dumps(m) + "\n" for m in messages), timeout=timeout)
    return [json.loads(line) for line in out.splitlines() if line.strip()], err


def test_replay_process_never_starts_the_real_server(tmp_path):
    marker = tmp_path / "server-started"
    server = ["python", "-c", f"open({str(marker)!r}, 'w').write('x')"]
    control = {"mode": "replay", "calls": CALLS, "fault": None, "ledger": str(tmp_path / "ledger.jsonl"), "writes": [], "reads": []}
    out, err = talk(wrap_process(server, control, tmp_path),
                    [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, call(2, "refund", {"customer_id": "42", "amount": 10})])
    assert [m["id"] for m in out] == [1, 2] and out[1]["result"]["content"][0]["text"] == "issued"
    assert not marker.exists(), err                                                      # the whole point: replay cannot cause side effects


FAKE_SERVER = '''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("id") is not None and "method" in m:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"echo": m["method"], "args": (m.get("params") or {}).get("arguments")}}), flush=True)
'''


def test_without_crosstrace_the_wrapper_is_just_the_server(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    out, _ = talk(wrap_process([sys.executable, str(script)]), [call(1, "refund", {"x": 1})])
    assert out == [{"jsonrpc": "2.0", "id": 1, "result": {"echo": "tools/call", "args": {"x": 1}}}]


def test_record_passes_everything_through_and_saves_requests_with_responses(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    record = tmp_path / "record.jsonl"
    control = {"mode": "record", "record_path": str(record), "calls": [], "fault": None}
    sent = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"a": 1}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "refund", "arguments": {"x": 1}, "_meta": {"progressToken": 7}}}]
    out, _ = talk(wrap_process([sys.executable, str(script)], control, tmp_path), sent)
    assert [m["id"] for m in out] == [1, 2] and out[1]["result"]["args"] == {"x": 1}
    saved = [json.loads(line) for line in open(record, encoding="utf-8")]
    assert [c["method"] for c in saved] == ["initialize", "tools/call"]
    assert saved[1]["params"] == {"name": "refund", "arguments": {"x": 1}} and saved[1]["result"]["echo"] == "tools/call"   # no _meta


# ---- config, catalog, planning ------------------------------------------------------------------------------------------------

def test_lost_response_is_a_tool_fault_that_never_applies_to_model_exchanges():
    f = make_fault("lost_response")
    assert "lost_response" in CATALOG and f.tool_fault and TOOL_FAULTS == {"lost_response"}
    ex = Exchange(idx=0, path="/v1/chat/completions", provider="openai", key="k", state_key="s", request={}, status=200, headers=[],
                  stream=False, body="{}")
    assert f.applies(ex) is False
    with pytest.raises(ValueError):
        make_fault("lost_response", how="sideways")


def test_config_reads_the_mcp_section_and_rejects_unknown_keys():
    base = {"agent": {"command": ["python", "agent.py"]}}
    cfg = C.parse({**base, "mcp": {"writes": ["run_report"], "reads": ["refund_status"]}}, ".")
    assert cfg.mcp == {"writes": ["run_report"], "reads": ["refund_status"]}
    assert C.parse(base, ".").mcp == {"writes": [], "reads": []}
    with pytest.raises(C.ConfigError):
        C.parse({**base, "mcp": {"write": ["x"]}}, ".")
    with pytest.raises(C.ConfigError):
        C.parse({**base, "mcp": {"writes": "x"}}, ".")


def test_lost_response_is_planned_at_every_recorded_tool_call_and_only_there():
    plain = Recording(exchanges=[])
    with_tools = Recording(exchanges=[], meta={"mcp": {"calls": CALLS + [CALLS[2]]}})
    fs = C.FaultSpec("lost_response")
    assert [(pi, k) for pi, _, k in runner.plan_trials([plain, with_tools], [fs])] == [(1, 0), (1, 1), (1, 2)]
    assert len(tool_calls(with_tools.meta["mcp"]["calls"])) == 3
