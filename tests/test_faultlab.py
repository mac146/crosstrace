import pytest

from agent import dns
from agent.tracing import Tracing
from collectors import store
from diagnosers.common import COMPONENT
from faultlab.faults import FAULT_TYPES, make_plan
from faultlab.netem import tc_commands


def test_plans_are_reproducible_and_seed_dependent():
    for f in FAULT_TYPES:
        assert make_plan(f, 5) == make_plan(f, 5)
    assert make_plan("slow_db", 1) != make_plan("slow_db", 2) or make_plan("slow_db", 1) != make_plan("slow_db", 3)


def test_every_fault_has_a_component_and_none_injects_nothing():
    for f in FAULT_TYPES:
        assert make_plan(f, 1).target == COMPONENT[f]
    p = make_plan("none", 1)
    assert (p.llm_args, p.dns_args, p.slow_extra_ms, p.db_lock_ms, p.netem) == ((), (), 0, 0, None)


def test_exactly_one_mechanism_per_fault():
    for f in FAULT_TYPES[1:]:
        p = make_plan(f, 3)
        active = [bool(p.llm_args), bool(p.dns_args), bool(p.slow_extra_ms), bool(p.db_lock_ms), bool(p.netem)]
        assert sum(active) == 1, f


def test_unknown_fault_rejected():
    with pytest.raises(ValueError):
        make_plan("cosmic_ray", 1)


def test_netem_commands_scope_to_one_port_in_both_directions():
    cmds = tc_commands(8123, delay_ms=100, loss_pct=0)
    flat = [" ".join(c) for c in cmds]
    assert "delay 100ms" in flat[1] and "loss" not in flat[1]
    assert sum("dport 8123" in c for c in flat) == 1 and sum("sport 8123" in c for c in flat) == 1
    assert "loss 30%" in " ".join(tc_commands(1, loss_pct=30)[1])


def test_dns_codec_roundtrip():
    q = dns.build_query("api.mock.local", 0x1234)
    assert dns.parse_a_response(dns.build_response(q, "10.1.2.3")) == ["10.1.2.3"]


def test_store_roundtrip_and_replace(tmp_path):
    con = store.connect(str(tmp_path / "t.db"))
    tr = Tracing()
    with tr.span("a", {"k": 1}):
        with tr.span("b"):
            pass
    run = {"run_id": "r1", "fault_type": "slow_db", "seed": 1, "split": "eval",
           "started_ns": 1, "ended_ns": 2, "status": "ok"}
    truth = {"fault_type": "slow_db", "target": "db", "params": {"x": 1}, "start_ns": 1, "end_ns": 2}
    store.save_run(con, run, tr.spans(), truth)
    store.save_run(con, run, tr.spans(), truth)  # replace, no duplicates
    spans = store.load_spans(con, "r1")
    assert [s["name"] for s in spans] == ["a", "b"]
    assert spans[1]["parent_id"] == spans[0]["span_id"]
    assert spans[0]["attrs"]["k"] == 1 and spans[0]["attrs"]["process.pid"]
    assert store.load_truth(con, "r1")["params"] == {"x": 1}
    assert [r["run_id"] for r in store.list_runs(con, "eval", "slow_db")] == ["r1"]
    assert store.list_runs(con, "eval", "none") == []
