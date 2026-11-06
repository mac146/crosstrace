"""Stage 6 additions: slow_api fault, subtle magnitudes, two-fault runs, tight threshold rule, multi-fault scoring."""
import random
import statistics

import pytest

from diagnosers import baseline
from diagnosers.common import COMPONENT
from diagnosers.profile import RULES, build_profile
from eval.metrics import aggregate_multi, score_multi, score_run
from faultlab.faults import (EXTENDED_FAULTS, FAULT_TYPES, SUBTLE_FAULTS, TWO_FAULT_PAIRS, make_plan, truth_faults)
from tests.helpers import healthy_run


def test_original_fault_draws_are_unchanged_since_stage1():
    # Stage 1-5 results depend on these exact draws, so extending the catalogue must not move them
    for seed in (1000, 2017):
        assert make_plan("dns_delay", seed).params["delay_ms"] == random.Random(f"dns_delay:{seed}").randint(300, 1000)
        assert make_plan("net_latency", seed).params["one_way_delay_ms"] == \
            random.Random(f"net_latency:{seed}").randint(75, 250)
        assert make_plan("packet_loss", seed).params["loss_pct"] == random.Random(f"packet_loss:{seed}").randint(20, 40)
        assert make_plan("slow_model", seed).params["delay_ms_per_call"] == \
            random.Random(f"slow_model:{seed}").randint(200, 600)
        assert make_plan("rate_limit", seed).params["first_n_429"] == random.Random(f"rate_limit:{seed}").randint(1, 3)
    assert FAULT_TYPES == ["none", "dns_delay", "net_latency", "packet_loss", "slow_tool", "slow_db",
                           "slow_model", "rate_limit"]


def test_slow_api_slows_only_the_api_server():
    p = make_plan("slow_api", 5)
    assert p.target == "api_server" and p.api_args[0] == "--delay-ms"
    assert 300 <= int(p.api_args[1]) <= 1000
    assert (p.llm_args, p.dns_args, p.slow_extra_ms, p.db_lock_ms, p.netem) == ((), (), 0, 0, None)


@pytest.mark.parametrize("fault", SUBTLE_FAULTS)
def test_subtle_magnitudes_are_small_and_differ_from_normal(fault):
    subtle = {make_plan(fault, s, "subtle").params.__repr__() for s in range(20)}
    normal = {make_plan(fault, s).params.__repr__() for s in range(20)}
    assert subtle.isdisjoint(normal)
    p = make_plan(fault, 3, "subtle")
    value = next(iter(p.params.values()))
    assert 10 <= value <= 100


@pytest.mark.parametrize("fault", ["none", "packet_loss", "rate_limit"])
def test_subtle_is_undefined_for_faults_with_fixed_timers(fault):
    with pytest.raises(ValueError):
        make_plan(fault, 1, "subtle")


def test_two_fault_plan_merges_both_mechanisms():
    p = make_plan("dns_delay+slow_db", 7)
    assert p.dns_args and p.db_lock_ms > 0 and not p.netem
    assert set(p.params) == {"dns_delay", "slow_db"}
    assert p.target == "api_net+db" and truth_faults(p.fault) == ["dns_delay", "slow_db"]
    # components are drawn exactly as when injected alone
    assert p.params["dns_delay"] == make_plan("dns_delay", 7).params


def test_every_declared_pair_builds_and_none_uses_two_netem_faults():
    for pair in TWO_FAULT_PAIRS:
        plan = make_plan(pair, 11)
        assert len(truth_faults(pair)) == 2 and plan.fault == pair
    assert len(set(TWO_FAULT_PAIRS)) == len(TWO_FAULT_PAIRS)


@pytest.mark.parametrize("bad", ["net_latency+packet_loss", "dns_delay+dns_delay", "none+slow_db", "a+b", "slow_db"])
def test_invalid_combinations_are_rejected(bad):
    if bad == "slow_db":
        pytest.skip("a single fault is valid")
    with pytest.raises(ValueError):
        make_plan(bad, 1)


def test_naming_the_network_for_a_slow_server_is_a_false_attribution_and_vice_versa():
    assert COMPONENT["slow_api"] != COMPONENT["dns_delay"]
    assert score_run(["dns_delay", "slow_api"], "slow_api") == {"top1": False, "top3": True, "false_attr": True}
    assert score_run(["slow_api"], "dns_delay")["false_attr"]
    assert not score_run(["net_latency"], "dns_delay")["false_attr"]      # same component, still a miss only
    assert set(EXTENDED_FAULTS) - set(FAULT_TYPES) == {"slow_api"}


def test_score_multi():
    s = score_multi(["slow_db", "dns_delay", "slow_tool"], ["dns_delay", "slow_db"])
    assert s == {"top1_in_truth": True, "all_in_topk": True, "each_in_topk": 1.0, "false_attr": False}
    half = score_multi(["slow_db", "slow_tool", "dns_delay"], ["dns_delay", "slow_db"])
    assert half["all_in_topk"] is False and half["each_in_topk"] == 0.5 and half["top1_in_truth"] is True
    wrong = score_multi(["slow_model", "slow_db"], ["dns_delay", "slow_db"])
    assert wrong["top1_in_truth"] is False and wrong["false_attr"] is True       # model was healthy
    same_component = score_multi(["net_latency", "slow_db"], ["dns_delay", "slow_db"])
    assert same_component["false_attr"] is False                                  # right component, wrong cause
    assert score_multi(["none"], ["dns_delay", "slow_db"])["false_attr"] is False  # silence is a miss, not an accusation
    with pytest.raises(ValueError):
        score_multi([], ["a", "b"])


def test_aggregate_multi_counts():
    sc = [score_multi(["slow_db", "slow_tool"], ["slow_db", "slow_tool"]),
          score_multi(["slow_db", "slow_model"], ["slow_db", "slow_tool"])]
    a = aggregate_multi(sc)
    # false attribution looks only at the FIRST-ranked cause: slow_db is injected in both runs, so 0 of 2
    assert a["n"] == 2 and a["all_in_topk"] == 0.5 and a["each_in_topk"] == 0.75 and a["false_attr"] == 0.0


def test_tight_rule_is_p99_plus_5_and_coarse_is_unchanged():
    vals = [50.0, 50.2, 50.4, 51.0]
    assert RULES["tight"](vals) == pytest.approx(51.0 + 5.0)
    assert RULES["coarse"](vals) == pytest.approx(max(51.0, 3 * statistics.median(vals)) + 50.0)
    runs = [healthy_run() for _ in range(5)]
    coarse, tight = build_profile(runs), build_profile(runs, "tight")
    assert coarse["tool:slow_tool"] == pytest.approx(150 + 50)      # 3 x median dominates: blind below +100 ms
    assert tight["tool:slow_tool"] == pytest.approx(50 + 5)         # sensitive to ~+6 ms
    with pytest.raises(KeyError):
        build_profile(runs, "nope")


def test_subtle_slow_tool_is_invisible_to_coarse_and_visible_to_tight():
    runs = [healthy_run() for _ in range(5)]
    slow = healthy_run({"slow_tool": 40})                           # a subtle +40 ms
    assert [c.cause for c in baseline.diagnose(slow, build_profile(runs))] == ["none"]
    assert [c.cause for c in baseline.diagnose(slow, build_profile(runs, "tight"))] == ["slow_tool"]


def test_baseline_four_way_split_includes_slow_api_and_default_is_unchanged():
    runs = [healthy_run() for _ in range(5)]
    profile = build_profile(runs)
    slow_api_step = healthy_run({"http_api": 500})
    assert sorted(c.cause for c in baseline.diagnose(slow_api_step, profile)) == \
        ["dns_delay", "net_latency", "packet_loss"]
    four = baseline.diagnose(slow_api_step, profile, api_causes=("dns_delay", "net_latency", "packet_loss", "slow_api"))
    assert sorted(c.cause for c in four) == ["dns_delay", "net_latency", "packet_loss", "slow_api"]
    assert len({round(c.score, 6) for c in four}) == 1                           # equal shares, no favourite
