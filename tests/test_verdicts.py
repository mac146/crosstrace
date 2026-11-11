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
    assert early_silent(0, [{**base, "idx": 0, "fault": "x", "t": 0}, {**base, "idx": 1, "t": 1}])
    assert not early_silent(0, [{**base, "idx": 0, "fault": "x", "t": 0}, {**base, "idx": 0, "t": 1}, {**base, "idx": 1, "t": 2}])
