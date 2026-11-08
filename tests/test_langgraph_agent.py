"""LangGraph agent adapter: secret handling and an end-to-end run against the local mock model (no internet, no key)."""
import os

import pytest

pytest.importorskip("langgraph")                  # optional dependency (requirements-real.txt)
pytest.importorskip("langchain_openai")

from agent import langgraph_agent as lg  # noqa: E402
from agent.tools import Tools, seed_db  # noqa: E402
from agent.tracing import Tracing  # noqa: E402
from mockservices.launcher import services  # noqa: E402
from mockservices.llm_server import FINAL_ANSWER  # noqa: E402


def test_scrub_removes_the_secret_and_bearer_tokens():
    secret = "abc123SECRETvalue456"
    msg = f"401 for key {secret}; header Bearer {secret} and sk-ABCDEFGH12345678 leaked"
    out = lg.scrub(msg, secret)
    assert secret not in out and "ABCDEFGH12345678" not in out and "[redacted]" in out
    assert lg.scrub("nothing secret here", None) == "nothing secret here"


def test_settings_prefer_the_environment_over_the_env_file(monkeypatch, tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text("OPENCODE_API_KEY=file-key\nOPENCODE_MODEL=file-model\nOPENCODE_BASE_URL=\n")
    monkeypatch.setattr(lg, "ENV_FILE", str(envfile))
    for k in ("OPENCODE_API_KEY", "OPENCODE_MODEL", "OPENCODE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert lg.load_settings() == ("file-key", lg.DEFAULT_BASE_URL, "file-model")   # empty value falls back to default
    monkeypatch.setenv("OPENCODE_API_KEY", "env-key")
    assert lg.load_settings()[0] == "env-key"
    monkeypatch.setattr(lg, "ENV_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("OPENCODE_API_KEY")
    assert lg.load_settings()[0] is None


def test_the_secret_file_is_git_ignored_and_not_tracked():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore")) as f:
        lines = [l.strip() for l in f]
    assert ".env" in lines and "!.env.example" in lines


def test_langgraph_agent_completes_against_the_mock_and_nests_spans(tmp_path):
    db = str(tmp_path / "app.db")
    seed_db(db)
    tracing = Tracing()
    with services() as svc:
        tools = Tools(db, svc.api_url, svc.dns_server, tracing)
        answer = lg.run_langgraph("What is customer 42's order total and shipping status?", tools, tracing,
                                  endpoint=(svc.llm_url + "/v1", "mock", "mock", 0))
    assert answer == FINAL_ANSWER
    spans = tracing.spans()
    ops = [s["attrs"].get("gen_ai.operation.name") for s in spans]
    assert ops.count("invoke_agent") == 1 and ops.count("chat") == 4 and ops.count("execute_tool") == 3
    root = next(s for s in spans if s["attrs"].get("gen_ai.operation.name") == "invoke_agent")
    steps = [s for s in spans if s["attrs"].get("gen_ai.operation.name") in ("chat", "execute_tool")]
    assert all(s["parent_id"] == root["span_id"] for s in steps)       # parenting survives the framework's threads
    http = [s for s in spans if s["attrs"].get("server.port")]
    assert len(http) == 5 and all(s["attrs"]["process.pid"] and s["attrs"]["thread.id"] for s in spans)


def test_a_failing_model_call_raises_a_scrubbed_error(tmp_path):
    db = str(tmp_path / "app.db")
    seed_db(db)
    tracing = Tracing()
    with services(llm_args=["--rate-limit-first", "99"]) as svc:     # every call answers HTTP 429
        tools = Tools(db, svc.api_url, svc.dns_server, tracing)
        with pytest.raises(RuntimeError) as ei:
            lg.run_langgraph("q", tools, tracing, endpoint=(svc.llm_url + "/v1", "sekrit-mock-key-123", "mock", 0))
    assert "sekrit-mock-key-123" not in str(ei.value) and "429" in str(ei.value)
    codes = [s["attrs"].get("http.response.status_code") for s in tracing.spans() if s["attrs"].get("server.port")]
    assert codes == [429]
