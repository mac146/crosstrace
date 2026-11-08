# Stage 15 results: the real OpenTelemetry eBPF Instrumentation (OBI) against our join

Predictions: [predictions-stage15.md](predictions-stage15.md), committed before OBI was ever run. Harness: [eval/selective_obi.py](../eval/selective_obi.py)
(`python -m eval.selective_obi --n 20 --cal 30`; recorded runs are re-scored with `--no-record`). OBI v1.0.0 (linux-amd64, official release;
tarball SHA-256 matched the published file; Sigstore signature NOT cryptographically verified, no cosign) ran as root for the whole batch on the
dev WSL (kernel 6.6.87.2), next to our collector, on the Stage 14 scenarios: 30 healthy calibration runs, 20 runs per scenario, same seeds.
OBI saw all 120 calibration calls and every one of the 400 scenario calls (0 missed after the warm-up described below).
The arm `obi_real` is v3's rules fed only OBI's per-request server duration (matched by server port and request path); OBI's own healthy
calibration gave its server-wait threshold (51 ms, ours 52 ms).

## Results
| scenario (truth) | spans only | **v3 joined** | pool_port | obi (Stage 14 emulation) | **obi_real** |
|---|---|---|---|---|---|
| none: said "none" | 20/20 | 20/20 | 20/20 | 20/20 | 20/20 |
| sel_slow_api (top-1) | 25% | **100%** | 100% | 100% | **100%** |
| sel_latency (top-1) | 15% | **100%** | 100% | 0% | **15%** |
| two_servers (both in top 2) | 35% | **100%** | 100% | 0% | **40%** |
| mixed_one_server (both in top 2) | 15% | **100%** | 0% | 0% | **25%** |

Intervals at n = 20: 100% = [84, 100], 0% = [0, 16]. Agreement of OBI's per-request server duration with our collector's server wait, on the 118
calls the server answered slowly: within max(10%, 30 ms) of ours 118/118, and of the injected delay 118/118.

## Predictions against results
1. OBI starts and reports HTTP requests for the mock servers on this kernel: **held** (see the practical notes below for what it needed).
2. OBI's duration within tolerance on at least 90% of slow calls and `obi_real` at least 90% on sel_slow_api: **held** (118/118, 100%).
3. `obi_real` alone at most 30% top-1 on sel_latency: **held** (15%, the spans-only level).
4. `obi_real+net` (OBI's network metrics host-wide): **not built, untested.** I did not work out how to read OBI's TCP RTT/retransmit metrics per run.
5. No real-OBI arm beats v3: **held**. v3 at least 80 points ahead on mixed_one_server: **missed narrowly** (100% vs 25% = 75 points).
6. No real-OBI arm false-alarms on more than 1 of 20 healthy runs: **held** (0/20).

## What this says
- **The emulation misstated OBI on latency, in OBI's favour.** My Stage 14 emulation took the per-request wait from our own kernel events, and on a
  call inside a latency window that wait includes the network delay (130-215 ms), so the emulated arm blamed the slow server for latency
  and scored 0% on those scenarios. Real OBI measures the server's own handling time (about 0.4 ms for those calls), so it does not do that. Its
  "answers" on network scenarios (15%, 40%, 25%) are the fallback equal split among DNS, latency, loss and slow server drawn with a seeded tie
  break, that is **chance**, not network diagnosis. The conclusion stands, but the claim "obi gets 0% on latency" in Stage 14 was too pessimistic
  and is corrected there.
- **Real OBI does what I credited it with on slow servers** (100%, with agreement to the injected delay on every slow call) and **cannot name a
  network cause from request timing alone** (chance level on the three scenarios with network faults).
- The comparison with `v3` is therefore the same as before: per-request kernel evidence (RTT, DNS, retransmits) plus per-call attribution
  is what names network causes and what separates mixed causes at one destination.
- Not tested: OBI's network metrics (prediction 4). A fair "OBI plus its network metrics" arm may do better than `obi_real` and is the
  most important open item of this stage.

## Practical notes (what real OBI needed on this setup)
- OBI keys its probes by **executable**, so every Python server shares one instrumentation; new server processes log "already instrumented" and
  give no attach line. Without a warm-up OBI missed the first calls, or all calls, of runs on freshly started servers (about half of the requests in
  the first smoke test). The runner sends `/health` requests until OBI has reported one for each server, then starts the scenario.
- The default route-harvesting step cost 10 s per new Python process and serialised attaches; it is turned off (`route_harvester_timeout: 0s`),
  with `poll_interval: 500ms`.
- OBI instruments any process listening on a port in the configured range, including unrelated ones (another tool of mine, the time daemon): those
  two were excluded. On a shared machine this is something to scope deliberately.
- The warm-up requests add unattached connections to our collector's log (orphans in the table above, 910-2,090 per scenario). They do not enter
  the diagnosis; `v3` results equal the earlier Stage 14 run (100% everywhere), so the extra traffic did not disturb the join.
- One server port was reused by two different runs; matching is chronological and single-use, and re-scoring gave identical numbers.

## Limits
Same as Stage 14 (mock services, loopback, sequential single agent, scenarios designed to discriminate), plus: one OBI version and one configuration,
tuned by me to work on this machine; OBI's application-agent features (client spans, trace propagation) were not used; the signature was not verified.
