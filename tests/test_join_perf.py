"""The correlator must scale linearly: it was quadratic (6 s for 3,200 tasks, an hour extrapolated to 100,000)."""
from eval.join_perf import measure


def test_a_large_run_joins_in_seconds_and_correctly():
    spans, events, seconds, wrong, orphans = measure(6400)
    assert (spans, events) == (6400, 32000)
    assert wrong == 0 and orphans == 0
    assert seconds < 5.0          # about 0.07 s now; the quadratic version needed about 25 s here


def test_ports_are_reused_without_misattribution():
    # 51,200 tasks reuse each of 25,000 client ports twice, spaced apart in time
    spans, events, seconds, wrong, orphans = measure(51200)
    assert wrong == 0 and orphans == 0
    assert seconds < 30.0
