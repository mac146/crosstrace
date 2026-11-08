"""v5: run-level, per-class evidence on hand-built timelines (four model calls per run, like the real LangGraph agent)."""
import pytest

from correlator.join import correlate
from diagnosers import combined
from diagnosers.profile import http_by_class, kernel_profile_from_timelines, run_summary

MS = 1_000_000
PAIRS = [(0, 0, 0)]
PROFILE = {"chat": 800.0, "run": 20_000.0}
KP = {"dns_ms": 50.0, "srtt_ms": 50.0, "server_wait_ms": 50.0, "retx_per_run": 0,
      "classes": {"model": {"dns_ms": 2000.0, "srtt_med_ms": 500.0}}}


def ev(name, ms, **kw):
    return {"ev": name, "t": int(ms * MS), **kw}


def run(srtt_us=(20_000,) * 4, dns_ms=None, chat_ms=(300, 300, 300, 300), wait_ms=None):
    """A run of four model calls. srtt_us: the smoothed RTT sample of each call; dns_ms: lookup before call 1;
    wait_ms: time from request to first response bytes on each call (None = no send/response events)."""
    spans, events, t = [], [], 0.0
    root = {"span_id": "r", "parent_id": None, "name": "invoke_agent", "start_ns": 0, "end_ns": 0,
            "attrs": {"process.pid": 10, "thread.id": 10, "gen_ai.operation.name": "invoke_agent"}}
    spans.append(root)
    for k in range(4):
        port, d = 50_000 + k, chat_ms[k]
        spans.append({"span_id": f"c{k}", "parent_id": "r", "name": "chat", "start_ns": int(t * MS),
                      "end_ns": int((t + d) * MS),
                      "attrs": {"process.pid": 10, "thread.id": 10, "gen_ai.operation.name": "chat"}})
        spans.append({"span_id": f"h{k}", "parent_id": f"c{k}", "name": "POST", "start_ns": int((t + 0.1) * MS),
                      "end_ns": int((t + d - 0.1) * MS),
                      "attrs": {"process.pid": 10, "thread.id": 10, "server.port": 443,
                                "server.address": "api.example.com"}})
        if k == 0 and dns_ms is not None:
            events += [ev("dns_query", t + 0.2, pid=10, tid=10, via="libc", host="api.example.com"),
                       ev("dns_response", t + 0.2 + dns_ms, pid=10, tid=10, via="libc", host="api.example.com",
                          lat_ns=int(dns_ms * MS))]
        base = t + (dns_ms or 0 if k == 0 else 0)
        events += [ev("tcp_connect_start", base + 1, pid=10, tid=10, dport=443),
                   ev("tcp_connect_end", base + 2, pid=10, tid=10, sport=port, dport=443, lat_ns=MS),
                   ev("tcp_rtt", base + 3, sport=port, dport=443, bytes=100, srtt_us=srtt_us[k])]
        if wait_ms is not None:
            events += [ev("tcp_send", base + 4, pid=10, tid=10, sport=port, dport=443, bytes=900),
                       ev("tcp_rtt", base + 4 + wait_ms, sport=port, dport=443, bytes=300, srtt_us=srtt_us[k])]
        t += d + 10
    root["end_ns"] = int(t * MS)
    return correlate(spans, events, PAIRS)


def causes(tl, **kw):
    kw.setdefault("run_level", True)
    return [c.cause for c in combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True, slow_api=True,
                                               latent_baseline=True, **kw)]


def test_healthy_run_is_none():
    assert causes(run(dns_ms=150)) == ["none"]


def test_a_persistent_delay_is_found_even_though_no_step_is_slow():
    tl = run(srtt_us=(1_050_000,) * 4)                      # every call shows a 1 s RTT; every chat step is only 300 ms
    assert causes(tl) == ["net_latency"]
    assert causes(tl, run_level=False) == ["none"]          # v4 cannot see it: nothing crossed a step threshold


def test_a_single_spike_is_ignored_because_the_run_level_statistic_is_a_median():
    assert causes(run(srtt_us=(1_240_000, 20_000, 20_000, 20_000))) == ["none"]
    assert causes(run(srtt_us=(1_240_000, 1_100_000, 20_000, 20_000))) == ["none"]   # two of four: not MORE than half


def test_three_of_four_calls_delayed_is_persistent_enough():
    assert causes(run(srtt_us=(900_000, 950_000, 1_000_000, 20_000))) == ["net_latency"]


def test_a_slow_lookup_is_dns_and_is_not_also_blamed_on_the_model():
    tl = run(dns_ms=5000, chat_ms=(5400, 300, 300, 300))     # the first model call is slow because of the lookup
    assert causes(tl) == ["dns_delay"]
    assert "slow_model" in causes(tl, run_level=False)       # v4 blames the model for the same slow call


def test_a_healthy_slow_ish_lookup_below_the_threshold_is_not_a_finding():
    assert causes(run(dns_ms=1800)) == ["none"]


def test_a_slow_model_with_a_healthy_network_is_still_slow_model():
    tl = run(chat_ms=(1300, 300, 300, 300), wait_ms=1200)    # request sent, response 1.2 s later; RTT and DNS healthy
    assert causes(tl) == ["slow_model"]


def test_without_run_level_thresholds_v5_behaves_like_v4():
    tl = run(srtt_us=(1_050_000,) * 4, dns_ms=5000, chat_ms=(5400, 300, 300, 300))
    no_classes = {k: v for k, v in KP.items() if k != "classes"}
    a = [c.cause for c in combined.diagnose(tl, PROFILE, no_classes, run_level=True, latent_baseline=True,
                                            loss_explains_rtt=True, slow_api=True)]
    b = [c.cause for c in combined.diagnose(tl, PROFILE, no_classes, run_level=False, latent_baseline=True,
                                            loss_explains_rtt=True, slow_api=True)]
    assert a == b


def test_classes_split_model_calls_from_tool_calls():
    tl = run()
    classes = http_by_class(tl)
    assert len(classes["model"]) == 4 and classes["tool"] == []


def test_run_summary_uses_max_for_the_lookup_and_median_for_rtt():
    tl = run(srtt_us=(10_000, 30_000, 50_000, 9_000_000), dns_ms=400, chat_ms=(700, 300, 300, 300))
    s = run_summary(http_by_class(tl)["model"])
    # lower median of 10, 30, 50 and 9000 ms: the one huge sample cannot move it, and half the calls cannot either
    assert s["n"] == 4 and s["dns_ms"] == pytest.approx(400.0) and s["srtt_med_ms"] == pytest.approx(30.0)
    short = run_summary(http_by_class(run())["model"][:2])
    assert short["srtt_med_ms"] is None                       # fewer than three calls: no median


def test_thresholds_are_built_from_healthy_runs_per_class():
    healthy = [run(srtt_us=(18_000 + 1000 * i,) * 4, dns_ms=140 + 10 * i, wait_ms=40) for i in range(8)]
    kp = kernel_profile_from_timelines(healthy)
    m = kp["classes"]["model"]
    # coarse rule: max(p99, 3 x median) + 50 over the per-run values
    assert m["srtt_med_ms"] == pytest.approx(max(25.0, 3 * 21.5) + 50.0)
    assert m["dns_ms"] == pytest.approx(max(210.0, 3 * 175.0) + 50.0)
    assert "retx_per_run" in kp and "dns_ms" in kp           # the v1-v4 pooled thresholds are still there


def test_too_few_healthy_runs_give_no_run_level_threshold():
    kp = kernel_profile_from_timelines([run(dns_ms=150, wait_ms=40) for _ in range(3)])
    assert kp["classes"]["model"] == {}
