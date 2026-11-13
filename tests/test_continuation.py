"""Continuation: after a fault, a request that matches nothing recorded is answered with the next healthy reply of the plan, so the run reaches
its real end and the verdict comes from the OUTCOME (it stopped, it gave up, it never stopped), not from the shape of a message."""
import os
import sys

import pytest

from crosstrace import runner
from crosstrace.proxy import FaultPlan, make_fault
from crosstrace.proxy.server import ModelProxy
from crosstrace.verdict import PROBLEM, REVIEW, classify
from mockservices.sse_server import serve_in_thread

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "examples", "broken_agents", "agent.py")


@pytest.fixture(scope="module")
def upstream():
    srv = serve_in_thread(chunks=8, gap_ms=0, tail_ms=0, echo=True)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


_recs = {}


def spec(mode, upstream, timeout_s=30.0):
    return runner.AgentSpec(command=[sys.executable, AGENT, "--mode", mode], cwd=ROOT, timeout_s=timeout_s,
                            upstreams={"openai": upstream, "anthropic": upstream}, env={"OPENAI_API_KEY": "sk-test"})


def rec(mode, upstream):
    if mode not in _recs:
        _recs[mode] = runner.record(spec(mode, upstream))
    return _recs[mode]


def trial(mode, upstream, fault, at, heal_after=1, **kw):
    return runner.trial(spec(mode, upstream), rec(mode, upstream), fault, at, heal_after=heal_after, timeout_s=30.0, **kw)


# ---- the responder itself ------------------------------------------------------------------------------------------------------

def proxy_for(recording, fault="stream_cut_clean", at=0, heal_after=1, **kw):
    plan = FaultPlan(make_fault(fault), at=at, heal_after=heal_after)
    return ModelProxy(mode="replay", recording=recording, faults=plan, continuation=True, **kw), plan


def test_nothing_is_continued_before_a_fault_has_happened(upstream):
    proxy, plan = proxy_for(rec("good", upstream))
    assert proxy._continuation({"stream": True}, {"provider": "openai"}) is None          # a miss before any fault is still a plain miss


def test_the_answers_follow_the_plan_in_order_and_keep_the_kind_of_reply(upstream):
    recording = rec("good", upstream)                          # exchanges: 0 streamed, 1 not streamed, 2 streamed
    proxy, plan = proxy_for(recording)
    plan.select({}, recording.exchanges[0])                    # the fault has been injected at exchange 0
    entry = {"provider": "openai"}
    got = []
    for stream in (True, False, True, True):
        ex, fault = proxy._continuation({"stream": stream}, dict(entry))
        got.append((ex.idx, ex.stream == stream))
        assert fault is None                                   # a transient fault does not follow the agent
    # a streamed request gets exchange 0 (the failed step's own reply), a non-streamed one exchange 1, then the next streamed one, then the last again
    assert got == [(0, True), (1, True), (2, True), (2, True)]


def test_a_persistent_fault_follows_the_agent_into_its_recovery(upstream):
    recording = rec("good", upstream)
    proxy, plan = proxy_for(recording, heal_after=None)
    plan.select({}, recording.exchanges[0])
    ex, fault = proxy._continuation({"stream": True}, {"provider": "openai"})
    assert fault is not None and fault.name == "stream_cut_clean"       # so the test can see whether its recovery stops


def test_after_the_cap_the_agent_is_flagged_and_cut_off(upstream):
    recording = rec("good", upstream)
    proxy, plan = proxy_for(recording, continuation_cap=3)
    plan.select({}, recording.exchanges[0])
    answers = []
    for _ in range(5):
        entry = {"provider": "openai"}
        answers.append((proxy._continuation({"stream": True}, entry) is not None, entry.get("unbounded", False)))
    assert answers == [(True, False), (True, False), (True, False), (False, True), (False, True)]


def test_a_request_of_another_provider_or_kind_with_no_fitting_reply_is_not_invented(upstream):
    proxy, plan = proxy_for(rec("good", upstream))
    plan.select({}, rec("good", upstream).exchanges[0])
    assert proxy._continuation({"stream": True}, {"provider": "anthropic"}) is None


# ---- the outcome decides: a real agent, every way a change of course can end -----------------------------------------------------

def test_an_agent_that_asks_again_once_and_carries_on_is_a_review_with_its_outcome(upstream):
    r = trial("reprompt", upstream, "stream_cut_clean", 0)
    assert r.verdict == "REPROMPTED" and "then finished (exit 0) after 1 more request" in r.detail and r.exit_code == 0
    assert r.observed["next_move"] == "told_model" and r.observed["continued"] == 1 and r.observed["unbounded"] is False
    assert r.verdict in REVIEW and r.verdict not in PROBLEM


def test_an_agent_whose_recovery_is_bounded_but_keeps_failing_gives_up_with_an_error(upstream):
    r = trial("reprompt", upstream, "stream_cut_clean", 0, heal_after=None)         # every answer it gets is cut again
    assert r.verdict == "SURFACED" and "stopped with an error" in r.detail and r.exit_code == 1
    assert r.observed["continued"] == 2                                              # it asked twice, then stopped


def test_an_agent_that_never_stops_asking_is_a_retry_storm(upstream):
    r = trial("reprompt_forever", upstream, "stream_cut_clean", 0, heal_after=None)
    assert r.verdict == "RETRY_STORM" and "never stopped" in r.detail and r.observed["unbounded"] is True
    assert r.verdict in PROBLEM and r.seconds < 25                                   # and the trial ended promptly, it did not wait for a hang


def test_a_change_of_course_nothing_can_read_is_settled_by_how_it_ends(upstream):
    r = trial("rewrite", upstream, "stream_cut_clean", 0)       # a different prompt: not a retry, not a message about the failure
    assert r.verdict == "RECOVERED" and "changed course after the failure and then finished" in r.detail
    assert r.observed["next_move"] == "diverged" and r.observed["continued"] == 1


def test_without_continuation_the_old_behaviour_is_kept(upstream):
    r = trial("rewrite", upstream, "stream_cut_clean", 0, continuation=False)
    assert r.verdict == "DIVERGED" and "continued" not in r.observed


def test_an_agent_that_retries_the_same_request_is_unchanged_by_continuation(upstream):
    r = trial("good", upstream, "stream_cut_clean", 0)
    assert r.verdict == "RECOVERED" and "continued" not in r.observed


def test_a_silent_agent_is_still_silent_and_stops_at_once(upstream):
    r = trial("naive", upstream, "stream_cut_clean", 0)
    assert r.verdict == "SILENT" and r.observed["next_move"] in ("continued_healthy_flow", "moved_to_recorded_next")


# ---- the settling logic, on synthetic logs --------------------------------------------------------------------------------------

def test_outcome_rules_on_synthetic_logs(upstream):
    recording = rec("good", upstream)
    base = [{"provider": "openai", "idx": 0, "how": "in_order", "t": 0, "fault": "stream_cut_clean", "attempt": 1, "status": 200},
            {"provider": "openai", "idx": None, "how": "continued", "t": 1, "status": 200, "served_idx": 0}]

    def run(extra, exit_code=0, hang=False, stdout="ok"):
        return classify("stream_cut_clean", 0, recording, base + extra, exit_code, stdout, hang, "ok")

    # a later request the replay could not answer: how it ends is unknown, so it stays a review and says so
    r = run([{"provider": "openai", "idx": None, "how": "miss", "t": 2, "status": 502}], exit_code=1)
    assert r.verdict in REVIEW and "could not answer a later request" in r.detail
    # still running when the time ran out after being continued: it did not stop
    assert run([], exit_code=-9, hang=True).verdict in ("HANG", "RETRY_STORM")
