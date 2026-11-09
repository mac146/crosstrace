# Stage 16 predictions: OBI plus matching against our join, with concurrent agents in DIFFERENT conditions (written BEFORE any code or run)

Why: Stage 15 showed that in sequential scenarios real OBI (per-request server duration, plus per-connection RTT from its printed statistics) matched
to calls by destination port and order comes within one run of our join. Order matching cannot work when agents run at once. And in the Stage 14
concurrency run all agents faced the same conditions at each step, so even a wrong match would have been right by symmetry. This stage makes the
conditions differ between agents at the same moment, on the same server and the same request path, so that matching actually matters.

Scenario "interleaved" (4 calls per agent, agents in lockstep; one API server; every agent requests /shipping/1../shipping/4):
- agent kind H: healthy (default source address 127.0.0.1). Truth: none.
- agent kind N: source address 127.0.0.2; netem delays all traffic to and from that address by 75-250 ms one way, the whole run. Truth: net_latency.
- agent kind S: source address 127.0.0.3; the server delays its answers to that address by 300-1000 ms. Truth: slow_api.
Kinds are interleaved evenly (agent i is kind H, N, S for i mod 3 = 0, 1, 2). C = 9 (12 runs, 108 agent-runs) and C = 24 (8 runs, 192 agent-runs);
healthy calibration at the same C (8 runs of all-H agents). Each agent is scored against its own truth (top-1, "none" for H).
Needed harness changes (tested separately): the client can bind a source address, netem can filter by address instead of port, the API server can delay
by client address.

Arms (all use v3's rules and the same healthy calibration procedure; only the evidence differs):
- `v3`               our join (pid/thread/time/connection), as always; does NOT use any declared port.
- `pool`, `pool_port` host-wide counters (all agents), as in Stages 13-14.
- `obi_real+flows`   real OBI: per-request server duration and per-connection RTT, matched to calls by server port, path and ORDER (the Stage 15 matching).
- `obi_real+port`    the same OBI evidence matched EXACTLY by the client's source port. That needs the span to carry its local port
                     (`network.local.port`, here read from the validation-only attribute the agent already records), an instrumentation requirement.

Predictions (per-agent top-1 over all kinds):
12. `v3` at least 90% at both C (70%); false alarms on kind H at most 5% (80%).
13. `pool` and `pool_port` between 55% and 75% (80%): kind N and H come out right, kind S is blamed on the network (at most 20% top-1 for kind S).
14. `obi_real+flows` at least 15 points below `v3` at C = 24 (65%) and at C = 9 (60%): connections of different kinds interleave, so order matching assigns
    the wrong RTT and the wrong server duration to many calls.
15. `obi_real+port` at least 90% (65%): exact matching removes the problem. If so, "OBI plus one span attribute" replicates the join, and the
    correlator's remaining advantage is doing without that attribute (it matches by process, thread and connection).
Joint belief that 14 and 15 both hold: 50%. That outcome would say: the per-span join is worth having only when the instrumentation cannot declare the
socket; with it, OBI plus glue is as good.
I would be wrong, and say so, if `v3` is below 80% at C = 9 (the join fails when agents really differ), if `obi_real+flows` is within 5 points of `v3`
(order matching is enough after all), or if `obi_real+port` is below 70% (OBI's lines cannot be matched even exactly).
