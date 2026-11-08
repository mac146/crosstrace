"""Stage 13: the pooled (host-metrics) evidence arm."""
import pytest

from diagnosers.combined import pooled


def b(**kw):
    base = {"n_events": 3, "dns_ms": None, "connect_ms": None, "retransmits": 0, "srtt_max_ms": None, "server_wait_ms": None}
    base.update(kw)
    return base


def test_pooled_takes_run_wide_maxima_and_totals_and_drops_the_server_wait():
    p = pooled({"a": b(dns_ms=5000, srtt_max_ms=1.0, retransmits=1, server_wait_ms=40),
                "b": b(srtt_max_ms=900, retransmits=2, server_wait_ms=3)})
    assert p["dns_ms"] == 5000 and p["srtt_max_ms"] == 900 and p["retransmits"] == 3 and p["n_events"] == 6
    assert p["server_wait_ms"] is None and p["_wait_if_generous"] == 40


def test_unknown_evidence_mode_is_rejected():
    from correlator.join import correlate
    from diagnosers.combined import diagnose
    tl = correlate([], [], [(0, 0, 0)])
    with pytest.raises(ValueError):
        diagnose(tl, {}, {}, source="bogus")
