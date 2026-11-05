import pytest

from diagnosers import baseline
from diagnosers.profile import MARGIN_MS, build_profile
from tests.helpers import healthy_run

PROFILE = build_profile([healthy_run() for _ in range(5)])


def causes(spans, seed=0):
    return [c.cause for c in baseline.diagnose(spans, PROFILE, tie_seed=seed)]


def test_profile_threshold_is_p99_or_3x_median_plus_margin():
    assert PROFILE["tool:db_query"] == pytest.approx(max(2, 3 * 2) + MARGIN_MS)
    assert PROFILE["chat"] == pytest.approx(15 + MARGIN_MS)
    assert PROFILE["tool:slow_tool"] == pytest.approx(150 + MARGIN_MS)


def test_empty_calibration_is_an_error():
    with pytest.raises(ValueError):
        build_profile([])


def test_healthy_run_is_none():
    assert causes(healthy_run()) == ["none"]


def test_small_slowdown_below_threshold_is_ignored():
    assert causes(healthy_run({"db_query": MARGIN_MS - 1})) == ["none"]


@pytest.mark.parametrize("step,cause", [("db_query", "slow_db"), ("slow_tool", "slow_tool"),
                                        ("chat", "slow_model")])
def test_single_slow_step_is_blamed_on_matching_cause(step, cause):
    assert causes(healthy_run({step: 500}))[0] == cause


def test_slow_api_blames_exactly_the_three_network_causes():
    ranked = causes(healthy_run({"http_api": 500}))
    assert sorted(ranked) == ["dns_delay", "net_latency", "packet_loss"]


def test_api_tie_break_is_seeded_and_not_always_the_same():
    spans = healthy_run({"http_api": 500})
    assert causes(spans, seed=7) == causes(spans, seed=7)
    firsts = {causes(spans, seed=s)[0] for s in range(40)}
    assert firsts == {"dns_delay", "net_latency", "packet_loss"}  # no hidden favourite


def test_429_blames_rate_limit_first_with_slow_model_second():
    ranked = causes(healthy_run({"chat0": 750, "attempts": 3}))
    assert ranked[:2] == ["rate_limit", "slow_model"]


def test_429_on_all_slow_calls_still_ranks_slow_model_by_its_score():
    # a different shape: every model call is slow, only the first retried. slow_model has more excess.
    assert causes(healthy_run({"chat": 400, "attempts": 2}))[0] == "slow_model"


def test_two_faults_are_ranked_by_attributed_delay():
    ranked = causes(healthy_run({"db_query": 900, "slow_tool": 300}))
    assert ranked == ["slow_db", "slow_tool"]


def test_missing_root_span_raises():
    with pytest.raises(ValueError):
        baseline.diagnose([], PROFILE)
