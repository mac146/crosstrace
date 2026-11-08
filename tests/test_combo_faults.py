"""Two real faults at once: which combinations are allowed, and that bad ones are refused before anything is touched."""
import pytest

from faultlab.runner import run_one


@pytest.mark.parametrize("bad", ["net_latency+packet_loss", "dns_delay+dns_delay", "dns_delay+net_latency+packet_loss"])
def test_unsupported_real_combinations_are_refused_before_any_work(bad, tmp_path):
    from collectors import store
    con = store.connect(str(tmp_path / "t.db"))
    with pytest.raises(ValueError, match="unsupported combination"):
        run_one(bad, 1, con, "x", kernel=False, llm_kind="langgraph")


def test_combined_scoring_knows_both_causes():
    from eval.metrics import score_multi
    from faultlab.faults import truth_faults
    assert truth_faults("dns_delay+net_latency") == ["dns_delay", "net_latency"]
    s = score_multi(["net_latency", "dns_delay"], truth_faults("dns_delay+net_latency"))
    assert s["all_in_topk"] and s["top1_in_truth"] and not s["false_attr"]
    half = score_multi(["slow_model", "dns_delay"], truth_faults("dns_delay+net_latency"))
    assert not half["all_in_topk"] and half["each_in_topk"] == 0.5 and half["false_attr"]   # first cause blames the model
    right_first = score_multi(["dns_delay", "slow_model"], truth_faults("dns_delay+net_latency"))
    assert not right_first["false_attr"] and right_first["top1_in_truth"]    # false attribution judges the FIRST cause only
