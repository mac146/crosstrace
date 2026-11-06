"""Real-model adapter, tested without network access: message conversion, replay of thinking blocks, spans."""
import json
from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic")   # optional dependency (requirements-real.txt)

from agent import anthropic_llm  # noqa: E402
from agent.tracing import Tracing  # noqa: E402


def block(**kw):
    """A stand-in for an SDK content block: model_dump returns the fields."""
    return SimpleNamespace(model_dump=lambda mode="json", exclude_none=True: dict(kw), **kw)


THINKING = {"type": "thinking", "thinking": "", "signature": "sig123"}
TOOL_USE = {"type": "tool_use", "id": "tu_1", "name": "db_query", "input": {"sql": "SELECT 1"}}


def test_history_conversion_merges_tool_results_and_replays_blocks_unchanged():
    history = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "_anthropic_content": [THINKING, TOOL_USE],
         "tool_calls": [{"id": "tu_1"}]},
        {"role": "tool", "tool_call_id": "tu_1", "content": "[[30.5]]"},
        {"role": "tool", "tool_call_id": "tu_2", "content": "x"},
        {"role": "assistant", "content": "done", "_anthropic_content": [{"type": "text", "text": "done"}]},
    ]
    out = anthropic_llm.to_anthropic(history)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert out[1]["content"] == [THINKING, TOOL_USE]                    # thinking block replayed as received
    assert out[2]["content"] == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "[[30.5]]"},
                                 {"type": "tool_result", "tool_use_id": "tu_2", "content": "x"}]   # ONE message
    with pytest.raises(ValueError):
        anthropic_llm.to_anthropic([{"role": "system", "content": "x"}])


def test_response_conversion_gives_openai_style_tool_calls_and_keeps_raw_blocks():
    resp = SimpleNamespace(content=[block(**THINKING), block(**TOOL_USE)])
    msg = anthropic_llm.from_anthropic(resp)
    assert msg["role"] == "assistant" and msg["content"] is None
    assert msg["tool_calls"] == [{"id": "tu_1", "type": "function",
                                  "function": {"name": "db_query", "arguments": json.dumps({"sql": "SELECT 1"})}}]
    assert msg["_anthropic_content"] == [THINKING, TOOL_USE]
    final = anthropic_llm.from_anthropic(SimpleNamespace(content=[block(type="text", text="hi ")]))
    assert final["content"] == "hi " and "tool_calls" not in final


def test_tools_match_the_agents_tool_methods():
    from agent.tools import Tools
    for t in anthropic_llm.TOOLS:
        assert callable(getattr(Tools, t["name"]))
        assert set(t["input_schema"]["required"]) <= set(t["input_schema"]["properties"])


def test_api_key_lookup_order(monkeypatch, tmp_path):
    keyfile = tmp_path / "key"
    keyfile.write_text("file-key\n")
    monkeypatch.setattr(anthropic_llm, "KEY_FILE", str(keyfile))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert anthropic_llm.load_api_key() == "file-key"
    monkeypatch.setenv("ANTHROPIC_API_KEY", " env-key ")
    assert anthropic_llm.load_api_key() == "env-key"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    keyfile.unlink()
    assert anthropic_llm.load_api_key() is None


def test_chat_makes_one_http_span_per_attempt_and_counts_tokens(monkeypatch):
    class FakeMessages:
        def create(self, **kw):
            assert kw["model"] == anthropic_llm.MODEL and kw["tools"] == anthropic_llm.TOOLS
            return SimpleNamespace(content=[block(type="text", text="ok")], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=11, output_tokens=3))

    monkeypatch.setattr(anthropic_llm.anthropic, "Anthropic",
                        lambda **kw: SimpleNamespace(messages=FakeMessages()))
    before = dict(anthropic_llm.TOTAL)
    tr = Tracing()
    llm = anthropic_llm.AnthropicLLM(tr)
    reply = llm.chat([{"role": "user", "content": "q"}])
    assert reply["content"] == "ok"
    spans = tr.spans()
    assert len(spans) == 1
    a = spans[0]["attrs"]
    assert (a["server.address"], a["server.port"], a["http.response.status_code"]) == ("api.anthropic.com", 443, 200)
    assert anthropic_llm.TOTAL["input_tokens"] - before["input_tokens"] == 11
    assert anthropic_llm.TOTAL["calls"] - before["calls"] == 1


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_and_truncation_are_errors_not_answers(monkeypatch, stop):
    class FakeMessages:
        def create(self, **kw):
            return SimpleNamespace(content=[], stop_reason=stop, stop_details=None,
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(anthropic_llm.anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=FakeMessages()))
    with pytest.raises(RuntimeError):
        anthropic_llm.AnthropicLLM(Tracing()).chat([{"role": "user", "content": "q"}])
