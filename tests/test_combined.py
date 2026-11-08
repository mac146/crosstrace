"""Combined diagnoser on hand-built timelines (kernel events with known meaning)."""
import pytest

from correlator.join import correlate
from diagnosers import baseline, combined
from diagnosers.profile import build_kernel_profile, build_profile
from tests.helpers import healthy_run

PROFILE = build_profile([healthy_run() for _ in range(5)])
KP = {"dns_ms": 50.0, "srtt_ms": 50.0, "server_wait_ms": 50.0}
MS = 1_000_000
PAIRS = [(0, 0, 0)]
PORT = 8000


def sp(sid, name, s, e, parent=None, **attrs):
    return {"span_id": sid, "parent_id": parent, "name": name, "start_ns": int(s * MS), "end_ns": int(e * MS),
            "attrs": {"process.pid": 10, "thread.id": 10, **attrs}}


def ev(name, ms, **kw):
    return {"ev": name, "t": int(ms * MS), **kw}


def http_events(t0, dns=0.4, connect=0.1, srtt_us=20, wait=1.0, retx=0, retx_state=2):
    """Kernel events of one GET starting at t0, with the given component timings (ms)."""
    t = t0 + 0.1
    out = [ev("dns_query", t, pid=10, tid=10, daddr="127.0.0.1", dport=5353),
           ev("dns_response", t + dns, pid=10, tid=10, lat_ns=int(dns * MS), bytes=48)]
    t += dns + 0.1
    out += [ev("tcp_connect_start", t, pid=10, tid=10, dport=PORT),
            ev("tcp_connect_end", t + connect, pid=10, tid=10, sport=40000, dport=PORT, lat_ns=int(connect * MS))]
    t += connect
    out += [ev("tcp_rtt", t + 0.1, sport=PORT, dport=40000, bytes=100, srtt_us=srtt_us),
            ev("tcp_rtt", t + 0.1 + wait, sport=40000, dport=PORT, bytes=200, srtt_us=srtt_us)]
    out += [ev("tcp_retransmit", t + 0.2 + i, sport=40000, dport=PORT, state=retx_state) for i in range(retx)]
    return out


def api_timeline(total_ms, kernel):
    """A run whose http_api step takes total_ms; `kernel` is the event list for its GET (or [])."""
    root = sp("r", "invoke_agent", 0, total_ms + 5, **{"gen_ai.operation.name": "invoke_agent"})
    op = sp("o", "execute_tool http_api", 1, 1 + total_ms, "r",
            **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "http_api"})
    get = sp("g", "GET", 1, 1 + total_ms, "o", **{"server.port": PORT, "server.address": "api.mock.local",
                                                   "http.response.status_code": 200})
    return correlate([root, op, get], kernel, PAIRS)


def causes(tl, seed=0):
    return [c.cause for c in combined.diagnose(tl, PROFILE, KP, tie_seed=seed)]


def test_healthy_is_none():
    assert causes(api_timeline(3, http_events(1.0))) == ["none"]


def test_dns_delay_is_identified():
    tl = api_timeline(620, http_events(1.0, dns=600))
    assert causes(tl) == ["dns_delay"]          # DNS explains the excess: nothing else is blamed


def test_net_latency_is_identified_from_rtt():
    tl = api_timeline(1000, http_events(1.0, connect=400, srtt_us=400_000, wait=200))
    assert causes(tl)[0] == "net_latency" and "packet_loss" not in causes(tl)


def test_packet_loss_is_identified_from_retransmits():
    tl = api_timeline(1200, http_events(1.0, connect=1000, srtt_us=900, retx=2))
    assert causes(tl)[0] == "packet_loss" and "net_latency" not in causes(tl)


def test_latency_and_loss_together_split_the_excess():
    tl = api_timeline(1000, http_events(1.0, connect=400, srtt_us=400_000, retx=1))
    assert sorted(causes(tl)) == ["net_latency", "packet_loss"]


def test_v2_lets_retransmits_explain_a_high_rtt():
    # a lost SYN: handshake took ~1 s, so the first RTT sample is ~1 s too, AND there are retransmits
    tl = api_timeline(1200, http_events(1.0, connect=1000, srtt_us=1_000_000, retx=2))
    v1 = combined.diagnose(tl, PROFILE, KP)
    v2 = combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True)
    assert sorted(c.cause for c in v1) == ["net_latency", "packet_loss"]      # v1: equal split (coin flip)
    assert [c.cause for c in v2] == ["packet_loss"]
    assert {combined.diagnose(tl, PROFILE, KP, tie_seed=s, loss_explains_rtt=True)[0].cause
            for s in range(30)} == {"packet_loss"}


def test_v2_still_blames_latency_when_there_are_no_retransmits():
    tl = api_timeline(1000, http_events(1.0, connect=400, srtt_us=400_000, wait=200))
    assert [c.cause for c in combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True)] == ["net_latency"]


def test_no_kernel_events_falls_back_to_baseline_three_way_split():
    tl = api_timeline(600, [])
    assert sorted(causes(tl)) == ["dns_delay", "net_latency", "packet_loss"]


def test_slow_step_with_healthy_kernel_signals_is_a_low_confidence_three_way_split():
    tl = api_timeline(600, http_events(1.0))
    out = combined.diagnose(tl, PROFILE, KP)
    assert sorted(c.cause for c in out) == ["dns_delay", "net_latency", "packet_loss"]
    assert all("low confidence" in c.evidence for c in out)


def test_latent_loss_is_reported_even_when_nothing_was_slow():
    tl = api_timeline(3, http_events(1.0, retx=1))
    assert causes(tl) == ["packet_loss"]


def test_latent_loss_does_not_displace_a_real_cause():
    tl = api_timeline(620, http_events(1.0, dns=600, retx=1))
    assert causes(tl) == ["dns_delay", "packet_loss"]


def test_combined_never_ranks_a_cause_the_kernel_rules_out_ahead_of_the_evidence():
    # baseline cannot tell; combined must put the evidenced cause first on every tie-break seed
    tl = api_timeline(620, http_events(1.0, dns=600))
    spans = [e.span for e in tl.entries]
    assert {baseline.diagnose(spans, PROFILE, tie_seed=s)[0].cause for s in range(30)} == \
        {"dns_delay", "net_latency", "packet_loss"}
    assert {causes(tl, s)[0] for s in range(30)} == {"dns_delay"}


def chat_timeline(chat_ms, kernel, status=200, attempts=1):
    root = sp("r", "invoke_agent", 0, chat_ms + 5, **{"gen_ai.operation.name": "invoke_agent"})
    chat = sp("c", "chat mock", 1, 1 + chat_ms, "r", **{"gen_ai.operation.name": "chat"})
    https = [sp(f"h{i}", "POST", 1 + i, 2 + i, "c", **{"server.port": PORT, "server.address": "127.0.0.1",
                                                      "http.response.status_code": 429 if i < attempts - 1 else status})
             for i in range(attempts)]
    return correlate([root, chat, *https], kernel, PAIRS)


def llm_events(wait):
    return [ev("tcp_connect_start", 1.1, pid=10, tid=10, dport=PORT),
            ev("tcp_connect_end", 1.2, pid=10, tid=10, sport=40000, dport=PORT, lat_ns=100_000),
            ev("tcp_rtt", 1.3, sport=PORT, dport=40000, bytes=100, srtt_us=20),
            ev("tcp_rtt", 1.3 + wait, sport=40000, dport=PORT, bytes=200, srtt_us=20)]


def test_slow_model_confirmed_by_server_wait():
    out = combined.diagnose(chat_timeline(500, llm_events(wait=480)), PROFILE, KP)
    assert out[0].cause == "slow_model" and "server held the response" in out[0].evidence


def test_slow_model_without_other_explanation_is_still_slow_model():
    assert causes(chat_timeline(500, llm_events(wait=1))) == ["slow_model"]


def test_429_is_rate_limit_then_slow_model():
    assert causes(chat_timeline(750, llm_events(wait=1), attempts=3)) == ["rate_limit", "slow_model"]


def test_kernel_profile_thresholds_follow_the_healthy_data():
    bd = [{"dns_ms": 0.4, "srtt_max_ms": 0.1, "server_wait_ms": 1.0},
          {"dns_ms": 0.6, "srtt_max_ms": 0.2, "server_wait_ms": 2.0},
          {"dns_ms": None, "srtt_max_ms": 0.05, "server_wait_ms": 1.5}]
    kp = build_kernel_profile(bd)
    assert kp["dns_ms"] == pytest.approx(max(0.6, 3 * 0.5) + 50)
    assert kp["srtt_ms"] == pytest.approx(max(0.2, 3 * 0.1) + 50)
    with pytest.raises(ValueError):
        build_kernel_profile([{"dns_ms": None, "srtt_max_ms": None, "server_wait_ms": None}])


# ---- v3: slow_api ------------------------------------------------------------------------------------------------

def test_v3_blames_a_slow_server_when_the_network_is_healthy():
    tl = api_timeline(620, http_events(1.0, wait=600))              # DNS, connect, RTT all healthy; server held 600 ms
    out = combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True, slow_api=True)
    assert [c.cause for c in out] == ["slow_api"]
    assert "server held the response" in out[0].evidence


def test_v2_cannot_name_a_slow_server_and_blames_the_network():
    tl = api_timeline(620, http_events(1.0, wait=600))
    out = combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True)       # slow_api off: the v2 behaviour
    assert sorted(c.cause for c in out) == ["dns_delay", "net_latency", "packet_loss"]


def test_v3_still_prefers_network_evidence_over_server_wait():
    # added latency makes the response late too (server wait ~ one-way delay); the RTT says it is the network
    tl = api_timeline(1000, http_events(1.0, connect=400, srtt_us=400_000, wait=200))
    assert [c.cause for c in combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True, slow_api=True)] \
        == ["net_latency"]
    tl = api_timeline(620, http_events(1.0, dns=600))
    assert [c.cause for c in combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True, slow_api=True)] \
        == ["dns_delay"]


def test_v3_finds_both_dns_delay_and_slow_server_on_one_step():
    tl = api_timeline(1220, http_events(1.0, dns=600, wait=600))
    out = [c.cause for c in combined.diagnose(tl, PROFILE, KP, loss_explains_rtt=True, slow_api=True)]
    assert sorted(out) == ["dns_delay", "slow_api"]


def test_v3_fallback_with_no_kernel_evidence_includes_slow_api_in_the_split():
    out = combined.diagnose(api_timeline(600, []), PROFILE, KP, slow_api=True)
    assert sorted(c.cause for c in out) == ["dns_delay", "net_latency", "packet_loss", "slow_api"]


def test_slow_api_is_never_blamed_for_a_slow_model_call():
    # a slow model call whose connection looks like a network problem must not get the server rule
    out = combined.diagnose(chat_timeline(500, llm_events(wait=480)), PROFILE, KP, slow_api=True)
    assert [c.cause for c in out] == ["slow_model"]


# ---- v4: latent loss needs a healthy retransmit baseline -----------------------------------------------------------

def test_v4_latent_loss_only_fires_above_the_healthy_baseline():
    tl = api_timeline(3, http_events(1.0, retx=3))                       # 3 retransmits, nothing slow
    kp = dict(KP, retx_per_run=4)                                        # healthy runs on this network show up to 4
    assert [c.cause for c in combined.diagnose(tl, PROFILE, kp, latent_baseline=True)] == ["none"]
    assert [c.cause for c in combined.diagnose(tl, PROFILE, dict(KP, retx_per_run=2), latent_baseline=True)] \
        == ["packet_loss"]
    assert [c.cause for c in combined.diagnose(tl, PROFILE, kp)] == ["packet_loss"]    # v3: any retransmit fires


def test_v4_on_a_healthy_loopback_behaves_like_v3():
    tl = api_timeline(3, http_events(1.0, retx=1))
    for kp in (dict(KP), dict(KP, retx_per_run=0)):                      # no baseline key, or a baseline of 0
        assert [c.cause for c in combined.diagnose(tl, PROFILE, kp, latent_baseline=True)] == ["packet_loss"]


def test_v4_does_not_touch_the_slow_step_loss_attribution():
    tl = api_timeline(1200, http_events(1.0, connect=1000, srtt_us=900, retx=2))
    kp = dict(KP, retx_per_run=4)
    assert combined.diagnose(tl, PROFILE, kp, latent_baseline=True)[0].cause == "packet_loss"


def test_kernel_profile_baseline_is_the_largest_healthy_retransmit_count():
    from diagnosers.profile import build_kernel_profile
    bd = [{"dns_ms": 0.4, "srtt_max_ms": 0.1, "server_wait_ms": 1.0}] * 3
    assert "retx_per_run" not in build_kernel_profile(bd)
    assert build_kernel_profile(bd, run_retx=[0, 0, 0, 0])["retx_per_run"] == 0
    assert build_kernel_profile(bd, run_retx=[0, 1, 4, 2, 0, 3, 1, 0])["retx_per_run"] == 4


def test_v3_leaves_healthy_runs_alone():
    assert [c.cause for c in combined.diagnose(api_timeline(3, http_events(1.0)), PROFILE, KP,
                                                loss_explains_rtt=True, slow_api=True)] == ["none"]
