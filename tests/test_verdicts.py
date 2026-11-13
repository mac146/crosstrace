"""Verdict engine (build plan step 3): every verdict produced on purpose by a deliberately broken agent, and a good agent passes.

The agent (examples/broken_agents/agent.py) is recorded once per mode against the mock provider; trials replay it with one fault at one
exchange. Exchanges: 0 plan (streamed), 1 tool decision (not streamed), 2 final answer (streamed).
"""
import os
import sys

import pytest

pytest.importorskip("openai")

from crosstrace import runner                          # noqa: E402
from crosstrace.proxy.recording import Exchange, Recording  # noqa: E402
from crosstrace.verdict import PROBLEM, classify, early_silent, summarize  # noqa: E402
from mockservices.sse_server import serve_in_thread    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "examples", "broken_agents", "agent.py")


@pytest.fixture(scope="module")
def upstream():
    srv = serve_in_thread(chunks=8, gap_ms=0, tail_ms=0, echo=True)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


_recs = {}


def spec(mode, upstream, timeout_s=20.0):
    return runner.AgentSpec(command=[sys.executable, AGENT, "--mode", mode], cwd=ROOT, timeout_s=timeout_s,
                            upstreams={"openai": upstream, "anthropic": upstream}, env={"OPENAI_API_KEY": "sk-test"})


def rec(mode, upstream):
    if mode not in _recs:
        _recs[mode] = runner.record(spec(mode, upstream))
    return _recs[mode]


def T(mode, upstream, fault, at, heal_after=1, timeout_s=20.0, **params):
    return runner.trial(spec(mode, upstream, timeout_s), rec(mode, upstream), fault, at, heal_after=heal_after, **params)


def test_recording_has_three_exchanges_and_replay_is_stable(upstream):
    r = rec("good", upstream)
    assert [e.stream for e in r.exchanges] == [True, False, True]
    s = runner.stability(spec("good", upstream), r)
    assert s["stable"], s["problems"]


def test_nondeterministic_prompt_is_reported_unstable_not_passed(upstream):
    s = runner.stability(spec("nondeterministic", upstream), rec("nondeterministic", upstream))
    assert not s["stable"] and any("matched no recording" in p for p in s["problems"])


@pytest.mark.parametrize("mode,fault,at,heal,params,expected", [
    # a cut stream the agent does not check: continues with half a plan (SILENT) or prints half an answer (PARTIAL)
    ("naive", "stream_cut_clean", 0, 1, {}, "SILENT"),
    ("naive", "stream_cut_clean", 2, 1, {}, "PARTIAL"),
    # the good agent notices (no finish_reason) and retries
    ("good", "stream_cut_clean", 0, 1, {}, "RECOVERED"),
    ("good", "stream_cut_clean", 2, 1, {}, "RECOVERED"),
    # transient server error: the SDK retries
    ("naive", "server_error", 1, 1, {}, "RECOVERED"),
    # persistent server error: surfaced by an honest agent, swallowed by a bad one
    ("naive", "server_error", 1, None, {}, "SURFACED"),
    ("swallow", "server_error", 1, None, {}, "SILENT"),
    # stalled stream: no read timeout -> HANG; a 3 s read timeout -> retry -> RECOVERED
    ("naive", "stream_stall", 2, 1, {"stall_s": 30.0}, "HANG"),
    ("good", "stream_stall", 2, 1, {"stall_s": 30.0}, "RECOVERED"),
    # retries
    ("storm", "server_error", 1, None, {}, "RETRY_STORM"),
    ("ignore_retry_after", "rate_limit", 1, 1, {}, "IGNORED_RETRY_AFTER"),
    ("good", "rate_limit", 1, 1, {}, "RECOVERED"),
    ("retry_400", "context_length", 1, 1, {}, "WRONG_RETRY"),
    ("good", "context_length", 1, 1, {}, "SURFACED"),
])
def test_each_verdict_on_purpose(upstream, mode, fault, at, heal, params, expected):
    timeout = 8.0 if fault == "stream_stall" else 30.0
    r = T(mode, upstream, fault, at, heal_after=heal, timeout_s=timeout, **params)
    assert r.verdict == expected, (r.verdict, r.detail, r.attempts, r.retry_gaps_s, r.stdout[-200:])


def test_partial_detail_and_rate_limit_spacing(upstream):
    r = T("naive", upstream, "stream_cut_clean", 2)
    assert "of" in r.detail and r.exit_code == 0
    r = T("good", upstream, "rate_limit", 1)
    assert r.retry_gaps_s and r.retry_gaps_s[0] >= 1.8          # waited for Retry-After: 2


def test_silent_trials_stop_early(upstream):
    r = T("naive", upstream, "stream_cut_clean", 0)
    assert r.verdict == "SILENT" and r.seconds < 10


def test_matrix_rates_good_vs_naive(upstream):
    faults = ["stream_cut_clean", "server_error", "rate_limit", "reset_before_response"]
    good = runner.matrix(spec("good", upstream), [rec("good", upstream)], faults)
    naive = runner.matrix(spec("naive", upstream), [rec("naive", upstream)], faults)
    assert not [r for r in good if r.verdict in PROBLEM], [(r.fault, r.at, r.verdict, r.detail) for r in good]
    s = summarize(naive)
    assert s["stream_cut_clean"]["counts"]["SILENT"] + s["stream_cut_clean"]["counts"]["PARTIAL"] == 2   # both streamed exchanges
    assert s["stream_cut_clean"]["applicable"] == 2                                                       # exchange 1 is not streamed


def _ex(i, stream=False):
    return Exchange(idx=i, path="/v1/chat/completions", provider="openai", key=f"k{i}", state_key=f"s{i}", request={}, status=200,
                    headers=[], stream=stream, body="{}")


def test_starting_over_is_a_recovery_but_a_changed_context_is_not():
    """An agent that recovers a failed model call by redoing its earlier steps is not SILENT: every request it sends is a recorded one."""
    recd = Recording(exchanges=[_ex(0), _ex(1), _ex(2)])
    p = {"provider": "openai"}
    seq = [{**p, "idx": 0, "how": "in_order", "t": 0}, {**p, "idx": 1, "how": "in_order", "t": 1},
           {**p, "idx": 2, "how": "in_order", "t": 2, "fault": "server_error", "attempt": 1}]
    restart = seq + [{**p, "idx": 0, "how": "repeat", "t": 3}, {**p, "idx": 1, "how": "repeat", "t": 4},
                     {**p, "idx": 2, "how": "repeat", "t": 5, "attempt": 2, "healed": "server_error"}]
    r = classify("server_error", 2, recd, restart, 0, "done", False, "done")
    assert r.verdict == "RECOVERED" and "after starting over from exchange 0" in r.detail
    assert classify("server_error", 2, recd, restart, 0, "different", False, "done").verdict == "RECOVERED_DIFFERENT"
    assert classify("server_error", 2, recd, restart, 1, "", False, "done").verdict == "SURFACED"
    # it starts over but never gets the failed call back: it moved on
    assert classify("server_error", 2, recd, seq + restart[3:5], 0, "done", False, "done").verdict == "SILENT"
    # a request that matches nothing in the recording means its context changed: SILENT even if the failed call is retried later
    changed = seq + [{**p, "idx": None, "how": "miss", "t": 3}, {**p, "idx": 2, "how": "repeat", "t": 4, "attempt": 2}]
    assert classify("server_error", 2, recd, changed, 0, "done", False, "done").verdict == "SILENT"
    # a plain retry is still just a retry
    plain = seq + [{**p, "idx": 2, "how": "repeat", "t": 3, "attempt": 2}]
    r = classify("server_error", 2, recd, plain, 0, "done", False, "done")
    assert r.verdict == "RECOVERED" and "starting over" not in r.detail


def test_cut_non_streamed_answer_is_partial_when_printed_and_ended_early_when_not():
    """Found on a real non-streaming client: a length_cut answer printed as complete was reported as ENDED_EARLY because only streamed
    answers could be measured."""
    import json
    text = "Your order 42 ships on Friday and costs 12 euros."
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
    ex = Exchange(idx=0, path="/v1/chat/completions", provider="openai", key="k0", state_key="s0", request={}, status=200,
                  headers=[], stream=False, body=body)
    recd = Recording(exchanges=[ex])
    sent = 22

    def reqs(fault):
        return [{"provider": "openai", "idx": 0, "how": "in_order", "t": 0, "fault": fault, "attempt": 1,
                 "fault_detail": {"chars_sent": sent, "chars_total": len(text)}}]

    r = classify("length_cut", 0, recd, reqs("length_cut"), 0, text[:sent], False, text)
    assert r.verdict == "PARTIAL" and f"{sent} of {len(text)}" in r.detail
    assert classify("length_cut", 0, recd, reqs("length_cut"), 0, "", False, text).verdict == "ENDED_EARLY"
    assert classify("length_cut", 0, recd, reqs("length_cut"), 0, text, False, text).verdict == "ENDED_EARLY"        # printed the whole answer anyway
    # chars_sent of a malformed body counts JSON characters, not answer text: never reported as a cut answer
    assert classify("malformed_body", 0, recd, reqs("malformed_body"), 0, text[:sent], False, text).verdict == "ENDED_EARLY"


def test_classify_unit_cases():
    recd = Recording(exchanges=[_ex(0), _ex(1)])
    base = {"provider": "openai"}
    reqs = [{**base, "idx": 0, "how": "in_order", "t": 0}, {**base, "idx": 1, "how": "in_order", "t": 1, "fault": "server_error",
                                                             "attempt": 1}]
    assert classify("server_error", 1, recd, reqs, 1, "", False, "ok").verdict == "SURFACED"
    assert classify("server_error", 1, recd, reqs, None, "", True, "ok").verdict == "HANG"
    # a miss before the faulted exchange: the trial says nothing about the agent
    bad = [{**base, "idx": None, "how": "miss", "t": 0}] + reqs[1:]
    assert classify("server_error", 1, recd, bad, 0, "", False, "ok").verdict == "REPLAY_UNSTABLE"
    # never reached exchange 1
    assert classify("server_error", 1, recd, reqs[:1], 0, "ok", False, "ok").verdict == "NOT_REACHED"
    # fallback to another model counts as a retry of the same exchange
    fb = reqs + [{**base, "idx": 1, "how": "fallback", "t": 2}]
    r = classify("server_error", 1, recd, fb, 0, "ok", False, "ok")
    assert r.verdict == "RECOVERED" and "fallback" in r.detail
    # the agent ran a tool on cut arguments: the detail names the tool and the arguments it used
    first = {"role": "user", "content": "where is order 42"}
    recd2 = Recording(exchanges=[Exchange(idx=0, path="/v1/chat/completions", provider="openai", key="k0", state_key="s0",
                                          request={"messages": [first]}, status=200, headers=[], stream=True, events=[])])
    nxt = {"messages": [first, {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "get_order", "arguments": '{"order_'}}]},
                        {"role": "tool", "tool_call_id": "c1", "content": "order not found"}]}
    reqs2 = [{**base, "idx": 0, "how": "in_order", "t": 0, "fault": "tool_args_cut", "attempt": 1},
             {**base, "idx": None, "how": "miss", "t": 1, "request": nxt}]
    r = classify("tool_args_cut", 0, recd2, reqs2, 0, "", False, "ok")
    assert r.verdict == "SILENT" and "ran tool get_order with arguments {\"order_" in r.detail
    assert early_silent(0, [{**base, "idx": 0, "fault": "x", "t": 0}, {**base, "idx": 1, "t": 1}])
    assert not early_silent(0, [{**base, "idx": 0, "fault": "x", "t": 0}, {**base, "idx": 0, "t": 1}, {**base, "idx": 1, "t": 2}])
