"""MCP end to end: a real MCP server, the official MCP client and a real model-call recording (against the mock provider). Skipped when the
`mcp` and `openai` packages are not installed (pip install mcp openai); the unit tests in test_mcp.py always run."""
import json
import os
import sys

import pytest

pytest.importorskip("mcp")
pytest.importorskip("openai")

from crosstrace import runner  # noqa: E402
from crosstrace.proxy.faults import make_fault  # noqa: E402
from crosstrace.proxy.recording import Recording  # noqa: E402
from mockservices.sse_server import serve_in_thread  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_ledger(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")] if os.path.exists(path) else []


# ---- end to end: a real MCP server, the official client, a real model-call recording (against the mock provider) ---------------------

EXAMPLE = os.path.join(REPO, "examples", "mcp_agents")


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    work = tmp_path_factory.mktemp("mcp-demo")
    ledger = str(work / "refunds.jsonl")
    srv = serve_in_thread(chunks=8, gap_ms=0, tail_ms=0, echo=False)
    os.environ["OPENAI_API_KEY"] = "sk-mock"
    env = {"REFUND_LEDGER": ledger, "PYTHONPATH": REPO}

    def spec(mode, upstream=True):
        return runner.AgentSpec(command=[sys.executable, "agent.py", "--mode", mode], cwd=EXAMPLE, env=env, timeout_s=60,
                                upstreams={"openai": f"http://127.0.0.1:{srv.server_address[1]}"} if upstream else {})

    try:
        rec = runner.record(spec("naive"))
        yield spec, rec, ledger
    finally:
        srv.shutdown()
        srv.server_close()


def refunds_made(ledger):
    return len(read_ledger(ledger))


def test_recording_captures_the_mcp_session_and_the_real_refund(demo):
    spec, rec, ledger = demo
    assert [c["method"] for c in rec.meta["mcp"]["calls"]] == ["initialize", "tools/list", "tools/call"]
    assert refunds_made(ledger) == 1                                                     # recording runs the real server, once


def test_replay_is_stable_and_never_touches_the_real_server(demo):
    spec, rec, ledger = demo
    st = runner.stability(spec("naive"), rec, runs=1)
    assert st["stable"], st["problems"]
    assert refunds_made(ledger) == 1


def test_naive_agent_refunds_twice_after_a_lost_response(demo):
    spec, rec, ledger = demo
    r = runner.tool_trial(spec("naive"), rec, "lost_response", 0, timeout_s=40)
    assert r.verdict == "DUPLICATE" and r.attempts == 2 and r.exit_code == 0
    assert "ran 2 time(s), 1 in the healthy run" in r.detail
    assert refunds_made(ledger) == 1                                                     # ... in the ledger the replay kept, not the real one


def test_careful_agent_stops_instead_of_repeating_the_write(demo):
    spec, rec, ledger = demo
    r = runner.tool_trial(spec("careful"), rec, "lost_response", 0, timeout_s=40)
    assert r.verdict == "SURFACED" and r.attempts == 1


def test_silent_loss_hangs_an_agent_with_no_tool_timeout(demo):
    spec, rec, ledger = demo
    r = runner.tool_trial(spec("careful"), rec, make_fault("lost_response", how="silent"), 0, timeout_s=6)
    assert r.verdict == "HANG"


def test_recovering_from_a_model_failure_by_starting_over_repeats_the_write(demo):
    """Counting writes after recovery: a model fault, not a tool fault. The agent recovers and prints the right answer."""
    spec, rec, ledger = demo
    r = runner.trial(spec("restart"), rec, "server_error", 1, timeout_s=40)
    # the agent starts over instead of retrying the failed call, so the model-lane verdict underneath is SILENT; the write check wins
    assert r.verdict == "DUPLICATE" and "ran 2 time(s), 1 in the healthy run" in r.detail, (r.verdict, r.detail)
    assert r.exit_code == 0
    # the same fault against an agent that does not start over is clean
    assert runner.trial(spec("careful"), rec, "server_error", 1, timeout_s=40).verdict == "RECOVERED"


def test_the_committed_example_recording_tests_clean_of_a_key(demo):
    spec, _, _ = demo
    recording = Recording.load(os.path.join(EXAMPLE, ".crosstrace", "recordings", "run-da39a3-1.json"))
    assert runner.tool_trial(spec("naive", upstream=False), recording, "lost_response", 0, timeout_s=40).verdict == "DUPLICATE"
