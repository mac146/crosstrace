import json

from agent.agent import run_agent
from agent.dns import resolve
from agent.llm_client import LLMClient
from agent.tools import Tools, seed_db
from agent.tracing import Tracing
from mockservices.launcher import services
from mockservices.llm_server import FINAL_ANSWER, build_response

TASK = "What is customer 42's order total and shipping status?"


def _run(tmp_path, llm_args=(), api_args=(), dns_args=()):
    db = str(tmp_path / "app.db")
    seed_db(db)
    tracing = Tracing()
    with services(llm_args, api_args, dns_args) as svc:
        llm = LLMClient(svc.llm_url, tracing, backoff_s=0.01)
        answer = run_agent(TASK, llm, Tools(db, svc.api_url, svc.dns_server, tracing), tracing)
    return answer, tracing.spans(), llm


def test_mock_llm_is_deterministic():
    msgs = [{"role": "user", "content": "x"}]
    assert build_response(msgs) == build_response(msgs)
    done = msgs + [{"role": "tool", "content": "r"}] * 3
    assert build_response(done)["content"] == FINAL_ANSWER


def test_healthy_run_completes_with_otel_spans(tmp_path):
    answer, spans, llm = _run(tmp_path)
    assert answer == FINAL_ANSWER
    assert llm.retries == 0
    ops = [s["attrs"].get("gen_ai.operation.name") for s in spans]
    assert ops.count("invoke_agent") == 1 and ops.count("chat") == 4 and ops.count("execute_tool") == 3
    assert all(s["end_ns"] >= s["start_ns"] for s in spans)
    assert all(s["attrs"]["process.pid"] and s["attrs"]["thread.id"] for s in spans)
    http = [s for s in spans if s["attrs"].get("http.request.method")]
    assert len(http) == 5  # 4 model calls + 1 API call
    assert any(s["attrs"].get("db.system.name") == "sqlite" for s in spans)


def test_db_tool_returns_expected_total(tmp_path):
    db = str(tmp_path / "app.db")
    seed_db(db)
    t = Tools(db, "http://unused", ("127.0.0.1", 1), Tracing())
    out = t.db_query("SELECT SUM(total) FROM orders WHERE customer_id = 42")
    assert json.loads(out) == [[30.5]]


def test_agent_retries_on_429(tmp_path):
    answer, spans, llm = _run(tmp_path, llm_args=["--rate-limit-first", "2"])
    assert answer == FINAL_ANSWER
    assert llm.retries == 2
    assert sum(s["attrs"].get("http.response.status_code") == 429 for s in spans) == 2


def test_resolver_reaches_mock_dns():
    with services() as svc:
        assert resolve("api.mock.local", svc.dns_server) == "127.0.0.1"
