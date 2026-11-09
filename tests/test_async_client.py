"""Stage 17: the asyncio client puts every coroutine on one thread, with distinct sockets (no root needed)."""
import asyncio
import threading

from eval.async_conc import KIND_IP, run_agents, strip_declared
from mockservices.api_server import make_server


def test_coroutines_share_one_pid_and_thread_but_have_distinct_declared_ports():
    srv = make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tracers, errors = asyncio.run(run_agents(["H", "H", "H"], port))
    finally:
        srv.shutdown()
    assert errors == []
    spans = [s for t in tracers for s in t.spans()]
    http = [s for s in spans if "server.port" in s["attrs"]]
    assert len(http) == 12                                                    # 3 agents x 4 calls
    assert len({(s["attrs"]["process.pid"], s["attrs"]["thread.id"]) for s in http}) == 1
    assert len({s["attrs"]["network.local.port"] for s in http}) == 12         # a new connection per call
    for t in tracers:                                                           # each agent has its own invoke_agent root with 4 tool steps
        ss = t.spans()
        roots = [s for s in ss if s["attrs"].get("gen_ai.operation.name") == "invoke_agent"]
        assert len(roots) == 1 and sum(s["parent_id"] == roots[0]["span_id"] for s in ss) == 4
    stripped = strip_declared(http)
    assert all("network.local.port" not in s["attrs"] for s in stripped)
    assert all("network.local.port" in s["attrs"] for s in http)               # stripping copies, it does not modify the input
    assert KIND_IP["H"] is None
