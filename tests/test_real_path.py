"""Real-path fault controller: command construction, refusal to touch a dirty interface, guaranteed cleanup."""
import subprocess

import pytest

from faultlab import real_path
from faultlab.netem import available as tc_available
from faultlab.real_path import PathFault, real_params, tc_commands


def test_commands_steer_only_the_matched_flows_into_the_netem_band():
    cmds = tc_commands("eth0", [(6, "104.18.2.115", 443), (6, "104.18.3.115", 443)], delay_ms=900)
    flat = [" ".join(c) for c in cmds]
    assert flat[0].startswith("qdisc replace dev eth0 root handle 1: prio bands 3 priomap " + " ".join(["1"] * 16))
    assert "netem delay 900ms" in flat[1] and "loss" not in flat[1] and "parent 1:1" in flat[1]
    filters = [f for f in flat if f.startswith("filter")]
    assert len(filters) == 2
    assert all("flowid 1:1" in f and "match ip protocol 6 0xff" in f and "dport 443 0xffff" in f for f in filters)
    assert "dst 104.18.2.115/32" in filters[0] and "dst 104.18.3.115/32" in filters[1]
    assert "loss 30%" in " ".join(tc_commands("eth0", [(6, "1.2.3.4", 443)], loss_pct=30)[1])


def test_priomap_sends_everything_unfiltered_to_band_two():
    # 16 priorities, all mapped to index 1 (class 1:2); only explicit filters reach class 1:1
    prio = tc_commands("eth0", [(6, "1.2.3.4", 443)], delay_ms=10)[0]
    mapping = prio[prio.index("priomap") + 1:]
    assert mapping == ["1"] * 16


def test_dns_fault_matches_udp_to_the_resolver_only():
    cmd = " ".join(tc_commands("lo", [(17, "10.255.255.254", 53)], delay_ms=5000)[2])
    assert "match ip protocol 17 0xff" in cmd and "dst 10.255.255.254/32" in cmd and "dport 53 0xffff" in cmd


def test_real_parameters_are_seeded_and_in_range():
    for fault, (lo, hi) in real_path.REAL_RANGES.items():
        vals = {next(iter(real_params(fault, s).values())) for s in range(40)}
        assert all(lo <= v <= hi for v in vals) and len(vals) > 5
        assert real_params(fault, 3) == real_params(fault, 3)
    with pytest.raises(KeyError):
        real_params("slow_db", 1)


def test_refuses_to_touch_an_interface_that_already_has_a_fault(monkeypatch):
    seen = []

    def fake_tc(args, check=True):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="qdisc netem 10: root refcnt 2 delay 5ms", stderr="")

    monkeypatch.setattr(real_path, "_tc", fake_tc)
    with pytest.raises(RuntimeError, match="already has a prio/netem"):
        with PathFault("eth0", [(6, "1.2.3.4", 443)], delay_ms=10):
            pass
    assert all(a[0] == "qdisc" and a[1] == "show" for a in seen)           # it changed nothing


def test_the_fault_is_removed_even_when_the_body_raises_and_a_failed_restore_is_loud(monkeypatch):
    log = []
    state = {"installed": False}

    def fake_tc(args, check=True):
        log.append(" ".join(args))
        if args[:2] == ["qdisc", "replace"]:
            state["installed"] = True
        if args[:2] == ["qdisc", "del"]:
            state["installed"] = False
        out = "qdisc prio 1: root" if state["installed"] else "qdisc mq 0: root"
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(real_path, "_tc", fake_tc)
    with pytest.raises(ValueError):
        with PathFault("eth0", [(6, "1.2.3.4", 443)], delay_ms=10):
            raise ValueError("run crashed")
    assert any(c.startswith("qdisc del dev eth0 root") for c in log) and state["installed"] is False

    # restore that does not work: must raise, not pass silently
    state["installed"] = False

    def stuck(args, check=True):
        return subprocess.CompletedProcess(args, 0, stdout="qdisc prio 1: root" if args[:2] != ["qdisc", "show"] or
                                           state.get("after") else "qdisc mq 0: root", stderr="")
    calls = {"n": 0}

    def stuck_after_enter(args, check=True):
        if args[:2] == ["qdisc", "show"]:
            calls["n"] += 1
            return subprocess.CompletedProcess(args, 0, stdout="qdisc mq 0: root" if calls["n"] == 1
                                               else "qdisc prio 1: root", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(real_path, "_tc", stuck_after_enter)
    with pytest.raises(RuntimeError, match="FAILED to restore"):
        with PathFault("eth0", [(6, "1.2.3.4", 443)], delay_ms=10):
            pass


@pytest.mark.skipif(not tc_available(), reason="needs passwordless sudo tc (scripts/setup_sudoers.sh)")
def test_live_fault_on_loopback_is_applied_and_fully_removed():
    """Applies a real qdisc to lo for a flow nobody uses (127.0.0.99:9), checks it exists, and that it is gone."""
    def show():
        return subprocess.run(["sudo", "-n", "tc", "qdisc", "show", "dev", "lo"], capture_output=True, text=True).stdout
    assert "netem" not in show()
    with PathFault("lo", [(6, "127.0.0.99", 9)], delay_ms=10):
        inside = show()
        assert "prio" in inside and "netem" in inside
    assert "netem" not in show() and "prio" not in show()
