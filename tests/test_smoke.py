import json

from agent.agent import run_agent
from agent.llm_client import LLMClient
from agent.tools import Tools, seed_db
from mockservices.launcher import services
from mockservices.llm_server import FINAL_ANSWER, build_response

TASK = "What is customer 42's order total and shipping status?"


def _run(tmp_path, llm_args=(), api_args=()):
    db = str(tmp_path / "app.db")
    seed_db(db)
    with services(llm_args, api_args) as (llm_url, api_url):
        llm = LLMClient(llm_url, backoff_s=0.01)
        answer, tracer = run_agent(TASK, llm, Tools(db, api_url))
    return answer, tracer, llm


def test_mock_llm_is_deterministic():
    msgs = [{"role": "user", "content": "x"}]
    assert build_response(msgs) == build_response(msgs)
    done = msgs + [{"role": "tool", "content": "r"}] * 3
    assert build_response(done)["content"] == FINAL_ANSWER


def test_healthy_run_completes(tmp_path):
    answer, tracer, llm = _run(tmp_path)
    assert answer == FINAL_ANSWER
    assert llm.retries == 0
    kinds = [s["kind"] for s in tracer.spans]
    assert kinds.count("model") == 4 and kinds.count("tool") == 3
    assert all(s["end_ns"] >= s["start_ns"] for s in tracer.spans)
    assert all(s["pid"] and s["tid"] for s in tracer.spans)


def test_db_tool_returns_expected_total(tmp_path):
    db = str(tmp_path / "app.db")
    seed_db(db)
    out = Tools(db, "http://unused").db_query("SELECT SUM(total) FROM orders WHERE customer_id = 42")
    assert json.loads(out) == [[30.5]]


def test_agent_retries_on_429(tmp_path):
    answer, _, llm = _run(tmp_path, llm_args=["--rate-limit-first", "2"])
    assert answer == FINAL_ANSWER
    assert llm.retries == 2
