# Stage 15 predictions: the real OpenTelemetry eBPF Instrumentation (OBI) against our join (written BEFORE it was ever run)

Binary: OBI v1.0.0 linux-amd64 from the official release. The release tarball's SHA-256 matched the published SHA256SUMS; the extracted
binary's SHA-256 is b239d223...4462 (pinned in scripts/setup_sudoers.sh, which installs it only on an exact match). The Sigstore signature was
NOT cryptographically verified (no cosign here); the bundle's certificate names the project's release workflow at tag v1.0.0. It runs as root,
on the dev WSL only, against our mock services only.

What is compared: the Stage 14 scenarios (sel_slow_api, sel_latency, two_servers, mixed_one_server; same seeds, same calibration), scoring with
the same rules as v3. New arms, built from what real OBI emits while the scenario runs:
- `obi_real`      per-request server duration from OBI's HTTP spans for each agent call (matched by request path and time), nothing else.
- `obi_real+net`  as above plus OBI's network metrics (TCP RTT, retransmit counters) as host-wide numbers, if OBI exposes them in a form we can read.
The emulated `obi`, `pool`, `pool_port` and `v3` arms are recomputed on the same runs for a fair side by side.

Predictions:
1. OBI starts and reports HTTP spans for the mock API server and the agent's client calls on this WSL kernel (6.6.87.2): 65%. If it does not,
   the stage reports that and the arm stays emulated.
2. If it runs: OBI's per-request server duration is within max(10%, 30 ms) of our own server-wait for at least 90% of the slow calls (70%).
   Hence `obi_real` matches the emulated `obi` on sel_slow_api (at least 90% top-1).
3. `obi_real` alone names no network cause: at most 30% top-1 on sel_latency (80%).
4. `obi_real+net`, using host-wide metrics, behaves like `pool`: at least 90% on sel_latency, at most 30% "both in top 2" on mixed_one_server (65%;
   the weakest prediction, because I do not know what OBI's network metrics expose per flow or per destination).
5. Neither real-OBI arm beats `v3` in any scenario (90%), and `v3` is at least 80 points ahead on mixed_one_server (75%).
6. Healthy runs: no real-OBI arm false-alarms on more than 1 of 20 (85%).

I would be wrong, and say so, if OBI's per-request duration differs from our server-wait by more than 30% (then the Stage 14 emulation misstated
OBI), or if `obi_real+net` matches `v3` on mixed_one_server (then OBI already carries per-call network evidence that the join was supposed to add).

## Part 2: OBI with its network statistics (written BEFORE any run with them; the Stage 15 runs above had OBI's stats mode off)
Feasibility probe (a few requests, no scoring): OBI's `stats` mode works on loopback. It prints one line per connection event with source and
destination address and port, and `srtt` (microseconds, the kernel's smoothed RTT) on connection close, one line for each end of the connection
(about 30 µs healthy, about 300,000 µs through a 150 ms-per-direction latency window). Its Prometheus metrics (`obi_stat_tcp_rtt_seconds`,
`obi_stat_tcp_retransmits_total`) are aggregated by address pair only, so on this machine every mock service is one pair. The printed lines carry no
event type, so retransmits cannot be read from them; none of the scenarios injects loss, so this does not matter here.

New arms (same rules and calibration procedure as `obi_real`; OBI's own healthy runs give the RTT threshold):
- `obi_real+net`    `obi_real` plus the run-wide maximum RTT over all connections to the run's servers, given to every call: the information the
                    Prometheus metrics hold (no per-destination or per-connection split).
- `obi_real+flows`  `obi_real` plus each call's own connection RTT, matched by destination port and order of connection (sequential calls, one new
                    connection each). Not a claim about concurrent agents, where order matching would not work; untested.
New recording: the Stage 14 scenarios again, OBI with stats mode and the printer, so the same runs give both the OBI evidence and our own.

Predictions:
7. OBI prints a client-side RTT line for at least 95% of the scenario connections (75%).
8. sel_latency: both new arms at least 90% top-1 (80%): any RTT evidence names latency.
9. two_servers: `obi_real+flows` at least 85% both in top 2 (65%) and `obi_real+net` at most 30% (65%): the run-wide RTT is blamed on both servers.
10. **mixed_one_server: `obi_real+flows` at least 85% (65%) and `obi_real+net` at most 30% (75%).** If +flows reaches the join's level, then OBI's own
    per-connection statistics replace our correlator in the sequential case, and the join's remaining advantage is the matching under concurrency
    (untested here). I give that a real chance, which is why it is a prediction and not an assumption.
11. No new arm false-alarms on more than 1 of 20 healthy runs (85%).
I would be wrong, and say so, if `obi_real+flows` is below 50% on mixed_one_server (the printed statistics are not usable the way I assume) or if
`obi_real+net` reaches 85% there (run-wide RTT would then be enough, contradicting the Stage 13 structure of the argument).
