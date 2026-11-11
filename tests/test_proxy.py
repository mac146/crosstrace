"""Model proxy (build plan step 1): record against a mock provider, replay without it, with the real OpenAI and Anthropic SDKs."""
import asyncio
import datetime
import json
import os
import shutil
import subprocess

import pytest

from crosstrace.proxy import ProxyThread, Recording, ReplaySession
from crosstrace.proxy.providers import compile_masks, request_keys
from mockservices.sse_server import serve_in_thread

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")

SECRET = "sk-test-SECRET-do-not-store"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def upstream():
    srv = serve_in_thread(chunks=5, gap_ms=0, tail_ms=0, echo=True)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def now():
    return datetime.datetime.now().isoformat()


def oa(base):
    return openai.OpenAI(api_key=SECRET, base_url=base + "/v1", max_retries=0)


def an(base):
    return anthropic.Anthropic(api_key=SECRET, base_url=base, max_retries=0)


def openai_workload(base):
    """Two calls, non-streaming and streaming, with a timestamp in the prompt (masked by default)."""
    c = oa(base)
    a = c.chat.completions.create(model="m", messages=[{"role": "user", "content": f"order 42 at {now()}"}]).choices[0].message.content
    s = c.chat.completions.create(model="m", messages=[{"role": "user", "content": "where is it?"}], stream=True)
    b = "".join(ev.choices[0].delta.content or "" for ev in s if ev.choices)
    return [a, b]


def anthropic_workload(base):
    c = an(base)
    a = c.messages.create(model="m", max_tokens=50, messages=[{"role": "user", "content": f"hello at {now()}"}]).content[0].text
    with c.messages.stream(model="m", max_tokens=50, messages=[{"role": "user", "content": "stream please"}]) as s:
        b = s.get_final_text()
    return [a, b]


def record(upstream, workload):
    with ProxyThread(mode="record", upstreams={"openai": upstream, "anthropic": upstream}) as p:
        out = workload(p.base_url)
    return out, p


def replay(recording, workload, **kw):
    with ProxyThread(mode="replay", recording=recording, **kw) as p:
        out = workload(p.base_url)
    return out, p


@pytest.mark.parametrize("workload", [openai_workload, anthropic_workload], ids=["openai", "anthropic"])
def test_record_then_replay_offline_is_identical(upstream, workload, tmp_path):
    rec_out, rp = record(upstream, workload)
    assert all(rec_out) and len(rp.recording.exchanges) == 2
    assert [e.stream for e in rp.recording.exchanges] == [False, True]
    path = tmp_path / "run.json"
    rp.recording.save(path)
    assert SECRET not in path.read_text(encoding="utf-8")          # API keys are never stored
    loaded = Recording.load(path)
    out, p = replay(loaded, workload)                               # no upstream at all, and a new timestamp in the prompt
    assert out == rec_out
    assert [r["how"] for r in p.requests] == ["in_order", "in_order"]
    assert p.session.complete


def test_async_streaming_replay(upstream):
    async def run(base):
        c = openai.AsyncOpenAI(api_key=SECRET, base_url=base + "/v1", max_retries=0)
        text = ""
        async for ev in await c.chat.completions.create(model="m", messages=[{"role": "user", "content": "async"}], stream=True):
            if ev.choices:
                text += ev.choices[0].delta.content or ""
        return text

    rec_out, rp = record(upstream, lambda b: asyncio.run(run(b)))
    out, _ = replay(rp.recording, lambda b: asyncio.run(run(b)))
    assert out == rec_out and "|async" in out


def test_streamed_events_are_recorded_exactly(upstream):
    _, rp = record(upstream, openai_workload)
    ex = rp.recording.exchanges[1]
    assert ex.events[-1] == "data: [DONE]"
    assert len(ex.events) == len(ex.event_ms) == 5 + 1 + 1 + 1          # 5 deltas, echo tail, finish chunk, [DONE]
    assert ex.event_ms == sorted(ex.event_ms)


def test_replay_miss_is_reported_not_invented(upstream):
    _, rp = record(upstream, openai_workload)

    def other(base):
        with pytest.raises(openai.APIStatusError) as e:
            oa(base).chat.completions.create(model="m", messages=[{"role": "user", "content": "never recorded"}])
        return e.value.status_code

    status, p = replay(rp.recording, other)
    assert status == 502
    assert p.requests[0]["how"] == "miss" and p.requests[0]["idx"] is None


def test_retry_of_a_served_exchange_is_served_again(upstream):
    def once(base):
        return oa(base).chat.completions.create(model="m", messages=[{"role": "user", "content": "same"}]).choices[0].message.content

    rec_out, rp = record(upstream, once)
    out, p = replay(rp.recording, lambda b: [once(b), once(b)])
    assert out == [rec_out, rec_out]
    assert [r["how"] for r in p.requests] == ["in_order", "repeat"]


def test_keep_alive_one_connection_for_many_calls(upstream):
    def three(base):
        c = oa(base)
        return [c.chat.completions.create(model="m", messages=[{"role": "user", "content": f"q{i}"}]).choices[0].message.content
                for i in range(3)]

    _, rp = record(upstream, three)
    _, p = replay(rp.recording, three)
    assert len(p.connections) == 1 and p.connections[0]["requests"] == 3
    assert len({r["port"] for r in p.requests}) == 1


def test_keys_ignore_volatile_and_transport_fields():
    masks = compile_masks()
    a = {"model": "m", "messages": [{"role": "user", "content": "at 2026-11-11T09:00:00Z id 123e4567-e89b-12d3-a456-426614174000"}],
         "user": "u1", "stream_options": {"include_usage": True}}
    b = {"model": "m", "messages": [{"role": "user", "content": "at 2027-01-02T10:11:12.5+05:30 id 00000000-0000-0000-0000-000000000000"}],
         "user": "u2"}
    assert request_keys("/v1/chat/completions", a, masks)[0] == request_keys("/v1/chat/completions", b, masks)[0]
    fallback = dict(b, model="other")                     # another model, same conversation: same state_key, different key
    kb, sb, _ = request_keys("/v1/chat/completions", b, masks)
    kf, sf, _ = request_keys("/v1/chat/completions", fallback, masks)
    assert kb != kf and sb == sf


def test_paths_without_v1_are_the_same_model_call(upstream):
    """The Vercel AI SDK's Anthropic provider wants /v1 in the base URL, Anthropic's SDK does not: both must work with one setting."""
    def raw(base):
        import httpx
        r = httpx.post(base + "/messages", json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "no v1"}]})
        return r.status_code, r.json()["content"][0]["text"]

    rec_out, rp = record(upstream, raw)
    assert rec_out[0] == 200 and rp.requests[0]["path"] == "/v1/messages" and rp.requests[0]["provider"] == "anthropic"
    out, p = replay(rp.recording, raw)
    assert out == rec_out and p.requests[0]["how"] == "in_order"


def test_user_masks():
    masks = compile_masks([r"order-\d+"])
    k1 = request_keys("/v1/messages", {"messages": [{"role": "user", "content": "order-17"}]}, masks)[0]
    k2 = request_keys("/v1/messages", {"messages": [{"role": "user", "content": "order-99"}]}, masks)[0]
    assert k1 == k2


def test_session_matches_out_of_order_for_concurrent_calls():
    from crosstrace.proxy.recording import Exchange
    rec = Recording(exchanges=[Exchange(idx=i, path="/v1/messages", provider="anthropic", key=k, state_key=k, request={}, status=200,
                                        headers=[], stream=False, body=k) for i, k in enumerate(["a", "b", "c"])])
    s = ReplaySession(rec)
    assert [(e.idx, how) for e, how in (s.match("b"), s.match("a"), s.match("c"))] == [(1, "out_of_order"), (0, "in_order"), (2, "in_order")]
    assert s.complete and s.match("zzz") == (None, "miss")


NODE = os.environ.get("NODE", os.path.expanduser("~/.local/node-v22.11.0-linux-x64/bin/node"))
NODE_DIR = os.path.join(ROOT, "eval", "sdk_conn_node")


@pytest.mark.skipif(not (os.path.exists(NODE) and os.path.isdir(os.path.join(NODE_DIR, "node_modules"))), reason="node clients not installed")
def test_openai_node_records_and_replays(upstream):
    def run(base):
        out = subprocess.run([NODE, "client.mjs", "openai-new", "stream", "2", base + "/v1", "0"], cwd=NODE_DIR, capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return [json.loads(l)["chars"] for l in out.stdout.splitlines()]

    rec_out, rp = record(upstream, run)
    out, p = replay(rp.recording, run)
    assert out == rec_out and all(out)
    # the node client sends the same prompt twice; both were recorded, so both are served in order (not as a repeat)
    assert [r["how"] for r in p.requests] == ["in_order", "in_order"] and p.session.complete
