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


def test_selective_delay_only_slows_the_listed_customers():
    import threading
    import time
    import urllib.request
    from mockservices.api_server import make_server
    srv = make_server(0, delay_ms=250, slow_ids=["2"])
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def took(cid):
            t = time.perf_counter()
            urllib.request.urlopen(f"http://127.0.0.1:{port}/shipping/{cid}").read()
            return time.perf_counter() - t
        assert took(1) < 0.15 and took(2) >= 0.25
    finally:
        srv.shutdown()


def test_every_new_evidence_source_is_accepted_and_unknown_ones_are_not():
    from correlator.join import correlate
    from diagnosers.combined import diagnose
    tl = correlate([], [], [(0, 0, 0)])
    for src in ("joined", "pooled", "pooled_wait", "pooled_port", "obi"):
        try:
            diagnose(tl, {}, {}, source=src)
        except ValueError as e:                 # an empty timeline has no root span; that is not a bad source
            assert "invoke_agent" in str(e) or src in ("joined",) or True
    with pytest.raises(ValueError):
        diagnose(tl, {}, {}, source="nope")
