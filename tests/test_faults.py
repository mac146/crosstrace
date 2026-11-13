"""Byte-level faults (build plan step 2): each fault is checked on the wire with a raw socket client, so the test sees exactly the bytes
and the way the connection ends (terminator, FIN, RST, silence), independent of any SDK."""
import email.utils
import json
import socket
import time

import httpx
import pytest

from crosstrace.proxy import FaultPlan, ProxyThread, make_fault
from crosstrace.proxy.faults import text_slot, body_end, is_terminal, is_tool_arg, parse_event
from mockservices.sse_server import serve_in_thread

TOOLS_OA = [{"type": "function", "function": {"name": "refund", "parameters": {"type": "object", "properties": {}}}}]
TOOLS_AN = [{"name": "refund", "input_schema": {"type": "object", "properties": {}}}]
REQ = {
    "openai_text": ("/v1/chat/completions", {"model": "m", "stream": True, "messages": [{"role": "user", "content": "tell me"}]}),
    "anthropic_text": ("/v1/messages", {"model": "m", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "tell me"}]}),
    "openai_tool": ("/v1/chat/completions", {"model": "m", "stream": True, "tools": TOOLS_OA, "messages": [{"role": "user", "content": "refund"}]}),
    "anthropic_tool": ("/v1/messages", {"model": "m", "max_tokens": 50, "stream": True, "tools": TOOLS_AN,
                                        "messages": [{"role": "user", "content": "refund"}]}),
    "openai_plain": ("/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "not streamed"}]}),
    "anthropic_plain": ("/v1/messages", {"model": "m", "max_tokens": 50, "messages": [{"role": "user", "content": "not streamed"}]}),
    "responses_plain": ("/v1/responses", {"model": "m", "input": "not streamed"}),
}


@pytest.fixture(scope="module")
def recordings():
    """One recording per request kind, made through the proxy against the mock provider."""
    srv = serve_in_thread(chunks=8, gap_ms=0, tail_ms=0, echo=True)
    up = f"http://127.0.0.1:{srv.server_address[1]}"
    out = {}
    try:
        for kind, (path, body) in REQ.items():
            with ProxyThread(mode="record", upstreams={"openai": up, "anthropic": up}) as p:
                r = httpx.post(p.base_url + path, json=body, timeout=10)
                assert r.status_code == 200
            out[kind] = p.recording
    finally:
        srv.shutdown()
        srv.server_close()
    return out


def raw_post(port, path, body, timeout=10.0):
    """POST with a raw socket; read until the response is complete, the peer closes (FIN), resets (RST) or the timeout passes."""
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(timeout)
    data = json.dumps(body).encode()
    s.sendall(f"POST {path} HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
    buf, how, t0, first = b"", None, time.monotonic(), None
    try:
        while True:
            d = s.recv(65536)
            if not d:
                how = "fin"
                break
            first = first or time.monotonic() - t0
            buf += d
            head, _, rest = buf.partition(b"\r\n\r\n")
            if b"transfer-encoding: chunked" in head.lower() and rest.endswith(b"0\r\n\r\n"):
                how = "complete"
                break
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:") and len(rest) >= int(line.split(b":")[1]):
                    how = "complete"
            if how:
                break
    except ConnectionResetError:
        how = "rst"
    except socket.timeout:
        how = "timeout"
    finally:
        s.close()
    return {"bytes": buf, "how": how, "elapsed": time.monotonic() - t0, "first_byte": first}


def events_of(raw):
    """Decode a chunked SSE body into event texts (only complete chunks)."""
    _, _, rest = raw.partition(b"\r\n\r\n")
    out = b""
    while rest:
        size_line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        size = int(size_line or b"0", 16)
        if size == 0 or len(rest) < size + 2:
            break
        out, rest = out + rest[:size], rest[size + 2:]
    return [e for e in out.decode().split("\n\n") if e.strip()]


def faulted(recordings, kind, fault, heal_after=1, **params):
    rec = recordings[kind]
    path, body = REQ[kind]
    plan = FaultPlan(make_fault(fault, **params), at=0, heal_after=heal_after)
    with ProxyThread(mode="replay", recording=rec, faults=plan) as p:
        r = raw_post(p.port, path, body)
    return r, p, rec.exchanges[0]


@pytest.mark.parametrize("kind", ["openai_text", "anthropic_text"])
def test_stream_cut_clean_ends_correctly_without_completion_markers(recordings, kind):
    r, p, ex = faulted(recordings, kind, "stream_cut_clean")
    assert r["how"] == "complete"                                   # a valid chunked terminator: nothing looks broken on the wire
    evs = events_of(r["bytes"])
    assert evs == ex.events[:len(evs)] and 0 < len(evs) < body_end(ex)
    assert not any(is_terminal(e) for e in evs)                     # no [DONE], finish_reason, message_delta or message_stop
    assert p.requests[0]["fault"] == "stream_cut_clean"


@pytest.mark.parametrize("kind", ["openai_tool", "anthropic_tool"])
def test_tool_args_cut_leaves_invalid_json_arguments(recordings, kind):
    r, _, ex = faulted(recordings, kind, "tool_args_cut")
    assert r["how"] == "complete"
    evs = events_of(r["bytes"])
    pieces = []
    for e in evs:
        _, _, obj = parse_event(e)
        if is_tool_arg(e):
            pieces.append(obj["delta"]["partial_json"] if "delta" in obj and "partial_json" in obj["delta"]
                          else obj["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
    args = "".join(pieces)
    assert 0 < len(pieces) < sum(is_tool_arg(e) for e in ex.events)
    with pytest.raises(ValueError):
        json.loads(args)
    assert not any(is_terminal(e) for e in evs)


def test_tool_args_cut_cuts_inside_arguments_sent_in_one_piece():
    """Real models (seen on OpenRouter) often send the whole arguments in one event. The cut must still truncate them, not just drop
    finish_reason (which would make it a stream_cut_clean and the verdict misleading)."""
    from crosstrace.proxy.recording import Exchange, Recording
    chunk = lambda delta, fin=None: "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": fin}]})
    events = [chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_order", "arguments": ""}}]}),
              chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"order_id":42}'}}]}),
              chunk({}, "tool_calls"), "data: [DONE]"]
    path, body = REQ["openai_tool"]
    from crosstrace.proxy.providers import request_keys
    key, state_key, norm = request_keys(path, body, [])
    rec = Recording(exchanges=[Exchange(idx=0, path=path, provider="openai", key=key, state_key=state_key, request=norm, status=200,
                                        headers=[["content-type", "text/event-stream"]], stream=True, events=events)])
    with ProxyThread(mode="replay", recording=rec, faults=FaultPlan(make_fault("tool_args_cut"), at=0)) as p:
        r = raw_post(p.port, path, body)
    evs = events_of(r["bytes"])
    args = "".join(parse_event(e)[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] for e in evs if is_tool_arg(e))
    assert args == '{"order' and not any(is_terminal(e) for e in evs)


def test_text_stream_has_no_tool_args_so_the_fault_does_not_apply(recordings):
    r, p, _ = faulted(recordings, "openai_text", "tool_args_cut")
    assert r["how"] == "complete" and p.requests[0].get("fault_na") == "tool_args_cut" and "fault" not in p.requests[0]


@pytest.mark.parametrize("fault,how", [("stream_cut_fin", "fin"), ("stream_cut_rst", "rst")])
@pytest.mark.parametrize("kind", ["openai_text", "anthropic_text"])
def test_abortive_cuts_end_mid_chunk(recordings, kind, fault, how):
    r, _, ex = faulted(recordings, kind, fault)
    assert r["how"] == how
    if how == "fin":
        assert not r["bytes"].endswith(b"0\r\n\r\n")
        tail = r["bytes"].rsplit(b"\r\n", 1)[-1]                    # the half-written chunk
        assert tail and tail in (ex.events[len(events_of(r["bytes"]))] + "\n\n").encode()


def test_stream_stall_holds_the_connection_then_continues(recordings):
    r, _, ex = faulted(recordings, "openai_text", "stream_stall", stall_s=1.0)
    assert r["how"] == "complete" and r["elapsed"] >= 1.0
    assert events_of(r["bytes"]) == ex.events


def test_stream_stall_then_close(recordings):
    r, _, _ = faulted(recordings, "anthropic_text", "stream_stall", stall_s=0.5, then="close")
    assert r["how"] == "fin" and r["elapsed"] >= 0.5


def test_idle_kill_sends_headers_then_closes(recordings):
    r, _, _ = faulted(recordings, "openai_text", "idle_kill", idle_s=0.5)
    assert r["how"] == "fin" and r["elapsed"] >= 0.5
    assert r["bytes"].startswith(b"HTTP/1.1 200") and events_of(r["bytes"]) == []


def test_slow_drip_is_slow_but_complete(recordings):
    r, _, ex = faulted(recordings, "openai_text", "slow_drip", gap_s=0.05)
    assert r["how"] == "complete" and events_of(r["bytes"]) == ex.events
    assert r["elapsed"] >= 0.05 * (len(ex.events) - 1) * 0.9


@pytest.mark.parametrize("kind", ["openai_text", "openai_plain"])
def test_slow_first_byte(recordings, kind):
    r, _, _ = faulted(recordings, kind, "slow_first_byte", delay_s=0.5)
    assert r["how"] == "complete" and r["first_byte"] >= 0.5


@pytest.mark.parametrize("kind", ["openai_plain", "anthropic_text"])
def test_reset_before_response(recordings, kind):
    r, _, _ = faulted(recordings, kind, "reset_before_response")
    assert r["how"] == "rst" and r["bytes"] == b""


@pytest.mark.parametrize("kind", ["openai_text", "anthropic_text"])
def test_malformed_event_and_stream_error_event(recordings, kind):
    r, _, ex = faulted(recordings, kind, "malformed_event")
    evs = events_of(r["bytes"])
    assert r["how"] == "complete" and len(evs) == len(ex.events) + 1
    assert sum(parse_event(e)[2] is None and parse_event(e)[1] not in (None, "[DONE]") for e in evs) == 1
    r, _, _ = faulted(recordings, kind, "stream_error_event")
    last = events_of(r["bytes"])[-1]
    assert r["how"] == "complete" and "error" in parse_event(last)[2]


@pytest.mark.parametrize("kind,fault,status,etype", [
    ("openai_plain", "rate_limit", 429, "requests"), ("anthropic_plain", "rate_limit", 429, "rate_limit_error"),
    ("openai_plain", "overloaded", 503, "server_error"), ("anthropic_plain", "overloaded", 529, "overloaded_error"),
    ("openai_text", "context_length", 400, "invalid_request_error"), ("anthropic_text", "server_error", 500, "api_error"),
    ("openai_plain", "auth", 401, "invalid_request_error"),
])
def test_http_errors_are_provider_accurate(recordings, kind, fault, status, etype):
    r, _, _ = faulted(recordings, kind, fault)
    head, _, body = r["bytes"].partition(b"\r\n\r\n")
    assert r["how"] == "complete" and head.startswith(b"HTTP/1.1 %d" % status)
    obj = json.loads(body)
    assert (obj["error"]["type"]) == etype
    if fault == "rate_limit":
        assert b"\r\nRetry-After: 2" in head


def test_retry_after_http_date(recordings):
    r, _, _ = faulted(recordings, "openai_plain", "rate_limit_http_date")
    ra = [l for l in r["bytes"].split(b"\r\n") if l.startswith(b"Retry-After:")][0].split(b": ", 1)[1].decode()
    assert 0 < email.utils.parsedate_to_datetime(ra).timestamp() - time.time() <= 3


def test_heal_policy_transient_persistent_and_placement(recordings):
    rec = recordings["openai_plain"]
    path, body = REQ["openai_plain"]
    plan = FaultPlan("server_error", at=0, heal_after=1)
    with ProxyThread(mode="replay", recording=rec, faults=plan) as p:
        a, b = raw_post(p.port, path, body), raw_post(p.port, path, body)
    assert a["bytes"].startswith(b"HTTP/1.1 500") and b["bytes"].startswith(b"HTTP/1.1 200")
    assert [r.get("attempt") for r in p.requests] == [1, 2] and p.requests[1]["healed"] == "server_error"

    plan = FaultPlan("server_error", at=0, heal_after=None)
    with ProxyThread(mode="replay", recording=rec, faults=plan) as p:
        rs = [raw_post(p.port, path, body) for _ in range(3)]
    assert all(r["bytes"].startswith(b"HTTP/1.1 500") for r in rs) and plan.injected == [(0, 1), (0, 2), (0, 3)]

    plan = FaultPlan("server_error", at=5)                      # another exchange: this one is untouched
    with ProxyThread(mode="replay", recording=rec, faults=plan) as p:
        assert raw_post(p.port, path, body)["bytes"].startswith(b"HTTP/1.1 200")


def test_stream_only_fault_on_a_plain_exchange_is_not_applied(recordings):
    r, p, _ = faulted(recordings, "openai_plain", "stream_cut_clean")
    assert r["bytes"].startswith(b"HTTP/1.1 200") and p.requests[0]["fault_na"] == "stream_cut_clean"


def test_unknown_fault_and_parameter_are_rejected():
    with pytest.raises(ValueError):
        make_fault("nope")
    with pytest.raises(ValueError):
        make_fault("stream_stall", stall=3)


def test_proxy_shuts_down_while_a_stall_holds_a_connection(recordings):
    import threading
    rec = recordings["openai_text"]
    path, body = REQ["openai_text"]
    t0 = time.monotonic()
    with ProxyThread(mode="replay", recording=rec, faults=FaultPlan("stream_stall", at=0)) as p:
        th = threading.Thread(target=raw_post, args=(p.port, path, body, 5), daemon=True)
        th.start()
        time.sleep(0.3)
    assert time.monotonic() - t0 < 5                           # close() cancels the sleeping handler instead of waiting 60 s


# ---- "200 OK but unusable": faults for non-streamed responses ---------------------------------------------------------------

PLAIN_KINDS = ["openai_plain", "anthropic_plain", "responses_plain"]


def split_response(r):
    head, _, rest = r["bytes"].partition(b"\r\n\r\n")
    return head.decode(), rest


def length_signal(obj):
    if "choices" in obj:
        return obj["choices"][0]["finish_reason"]
    if obj.get("object") == "response":
        return obj["status"]
    return obj["stop_reason"]


@pytest.mark.parametrize("kind,signal", [("openai_plain", "length"), ("anthropic_plain", "max_tokens"), ("responses_plain", "incomplete")])
def test_length_cut_is_a_valid_200_with_the_providers_length_signal(recordings, kind, signal):
    r, p, ex = faulted(recordings, kind, "length_cut")
    head, rest = split_response(r)
    assert head.startswith("HTTP/1.1 200") and r["how"] == "complete"
    obj = json.loads(rest)                                          # valid JSON: nothing looks broken to the HTTP layer or the parser
    box, key = text_slot(obj)
    full_box, full_key = text_slot(json.loads(ex.body))
    short, full = box[key], full_box[full_key]
    assert 0 < len(short) < len(full) and full.startswith(short)
    assert length_signal(obj) == signal and length_signal(json.loads(ex.body)) != signal
    assert p.requests[0]["fault"] == "length_cut"


@pytest.mark.parametrize("kind", PLAIN_KINDS)
def test_empty_content_is_a_valid_200_with_no_answer_text(recordings, kind):
    r, p, ex = faulted(recordings, kind, "empty_content")
    head, rest = split_response(r)
    assert head.startswith("HTTP/1.1 200")
    obj = json.loads(rest)
    assert text_slot(obj) is None                                  # the answer text is gone ...
    full_box, full_key = text_slot(json.loads(ex.body))
    assert full_box[full_key] and full_box[full_key] not in rest.decode()
    assert length_signal(obj) not in ("length", "max_tokens", "incomplete")      # ... and the provider claims a normal finish


@pytest.mark.parametrize("kind", PLAIN_KINDS)
def test_malformed_body_has_a_correct_content_length_but_is_not_json(recordings, kind):
    r, p, ex = faulted(recordings, kind, "malformed_body")
    head, rest = split_response(r)
    assert head.startswith("HTTP/1.1 200") and r["how"] == "complete"
    declared = int(next(l.split(":")[1] for l in head.split("\r\n") if l.lower().startswith("content-length:")))
    assert declared == len(rest) < len(ex.body.encode())            # the HTTP layer sees a complete response
    assert ex.body.encode().startswith(rest)
    with pytest.raises(ValueError):
        json.loads(rest)


@pytest.mark.parametrize("fault", ["length_cut", "empty_content", "malformed_body"])
def test_body_faults_do_not_apply_to_streams(recordings, fault):
    assert not make_fault(fault).applies(recordings["openai_text"].exchanges[0])
    assert not make_fault(fault).applies(recordings["anthropic_text"].exchanges[0])
    assert make_fault(fault).applies(recordings["openai_plain"].exchanges[0])


@pytest.mark.parametrize("fault", ["length_cut", "empty_content", "malformed_body"])
def test_body_faults_heal_on_the_retry(recordings, fault):
    rec = recordings["openai_plain"]
    path, body = REQ["openai_plain"]
    plan = FaultPlan(make_fault(fault), at=0, heal_after=1)
    with ProxyThread(mode="replay", recording=rec, faults=plan) as p:
        first = raw_post(p.port, path, body)
        second = raw_post(p.port, path, body)
    assert split_response(first)[1] != rec.exchanges[0].body.encode()
    assert split_response(second)[1] == rec.exchanges[0].body.encode()       # the retry gets the real recorded answer
    assert p.requests[1].get("healed") == fault
