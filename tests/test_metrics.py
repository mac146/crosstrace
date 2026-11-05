import pytest

from eval.metrics import aggregate, score_run, wilson


def test_exact_hit():
    assert score_run(["slow_db", "slow_tool"], "slow_db") == {"top1": True, "top3": True, "false_attr": False}


def test_hit_at_rank_three_is_top3_only():
    s = score_run(["dns_delay", "net_latency", "packet_loss"], "packet_loss")
    assert s == {"top1": False, "top3": True, "false_attr": False}  # same component: miss, not false attribution


def test_rank_four_is_not_top3():
    assert not score_run(["slow_db", "slow_tool", "slow_model", "rate_limit"], "rate_limit")["top3"]


def test_blaming_a_healthy_component_is_false_attribution():
    s = score_run(["slow_db"], "slow_model")
    assert s == {"top1": False, "top3": False, "false_attr": True}


def test_same_component_wrong_cause_is_not_false_attribution():
    assert not score_run(["slow_model"], "rate_limit")["false_attr"]


def test_healthy_run_correctly_called_none():
    assert score_run(["none"], "none") == {"top1": True, "top3": True, "false_attr": False}


def test_healthy_run_blamed_on_something_is_false_attribution():
    assert score_run(["slow_tool"], "none")["false_attr"]


def test_missing_a_real_fault_by_saying_none_is_a_miss_not_a_false_attribution():
    assert score_run(["none"], "slow_db") == {"top1": False, "top3": False, "false_attr": False}


def test_empty_ranking_is_an_error():
    with pytest.raises(ValueError):
        score_run([], "slow_db")


def test_aggregate_counts():
    scores = [score_run(["slow_db"], "slow_db"), score_run(["slow_tool"], "slow_db"),
              score_run(["slow_db"], "slow_db"), score_run(["slow_model"], "slow_db")]
    a = aggregate(scores)
    assert a["n"] == 4 and a["top1"] == 0.5 and a["false_attr"] == 0.5


def test_wilson_interval_known_values():
    lo, hi = wilson(0, 30)
    assert lo == 0.0 and hi == pytest.approx(0.1135, abs=1e-3)
    lo, hi = wilson(15, 30)
    assert lo == pytest.approx(0.3323, abs=1e-3) and hi == pytest.approx(0.6677, abs=1e-3)
    assert wilson(0, 0) == (0.0, 0.0)
