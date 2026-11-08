# Design decisions

Running log. Newest at the bottom. Each entry: decision, why.

## D1. Dev environment: WSL2 Ubuntu on Windows 11 Home (2026-11-05)
- Kernel 6.6.87.2-microsoft-standard-WSL2. Feasibility script `scripts/00_feasibility.sh` result:
  - `bpftrace` 0.25.0 attaches a syscall tracepoint and sees `connect()` from `curl`: **OK**
  - `tc qdisc add dev lo root netem delay 10ms loss 1%`: **OK** (`sch_netem` loads)
- So eBPF tracing and `tc netem` faults are feasible without a custom kernel or separate VM.
- All root/network-modifying steps (eBPF, `tc`, DNS) run only inside WSL and are flagged in scripts.
- systemd is not running in this WSL, so `dnsmasq` does not auto-start. faultlab was going to launch it
  as a child process for `dns_delay`. **Superseded by D9:** a small Python stub resolver is used instead,
  so dnsmasq is not needed (and is no longer installed by the setup script).

## D2. Repo lives on the Windows filesystem (`C:\...\crosstrace`), accessed from WSL via `/mnt/c`
- User's choice (workspace already there). Cost: slower I/O and unreliable SQLite file locking on `/mnt/c`.
- Mitigation: runtime data (trace store, eval output, venv) goes to the WSL-native filesystem
  (e.g. `~/.crosstrace-data`, `~/.venvs/crosstrace`), never into the repo tree.
- Revisit if overhead measurements look noisy because of the 9p mount.

## D3. Stage 0 is standard-library only
- Mock servers use `http.server`, agent uses `urllib` and `sqlite3`. Only dev dependency: pytest.
- Why: minimal dependencies, fewer moving parts for reproducibility. OpenTelemetry arrives in Stage 1.

## D4. Mock services run as separate OS processes
- `mockservices/launcher.py` spawns them via subprocess on free ports.
- Why: the later join relies on PID/TID and real sockets; in-process servers would hide the network path.

## D5. Mock LLM is a fixed script keyed on the number of tool results in the conversation
- Same input always yields the same output; no timestamps or randomness. Fault hooks
  (`--delay-ms`, `--rate-limit-first`) exist but are off by default.

## D6. Stage 0 tracer is a plain span recorder, not OTel
- Fields mirror OTel (name, kind, start/end, pid, tid, attrs) so Stage 1 can swap it mechanically.
- Timestamps are wall-clock `time.time_ns()`. Clock alignment with kernel events (monotonic clock) is a Stage 3 problem.

## D7. Storage: SQLite (2026-11-05)
- User's choice over DuckDB. Stdlib, no extra dependency, fine for the data volume (thousands of runs).
- Database files live on the WSL-native filesystem, not `/mnt/c` (see D2), because of file-locking issues on the 9p mount.
- Revisit only if eval queries get slow.

## D8. Stage 1 tracing: real OpenTelemetry SDK, in-process, no collector process
- `opentelemetry-sdk==1.45.2` (first non-stdlib dependency), one TracerProvider per run, spans kept in an in-memory
  exporter and written to SQLite by the run harness. GenAI span names/attributes (`invoke_agent`, `chat`,
  `execute_tool`), HTTP client spans (one per attempt) and a DB span, all hand-instrumented, all with
  `process.pid` and `thread.id`.
- Why: a separate collector adds a process and a network hop with no benefit until we care about overhead.
  Revisit in Stage 4 when measuring tracing overhead.

## D9. DNS is a real UDP lookup against a mock resolver, bypassing libc
- `agent/dns.py` sends the query itself; `mockservices/dns_server.py` answers with an optional delay (`dns_delay`).
  No caching.
- Why: needs no root and no change to system resolver config, and the lookup is real packets for eBPF later.
- Limit (report in write-up): real apps resolve via libc/systemd-resolved with caching and happy-eyeballs; this is
  a simplification.

## D10. Scoring definitions and the baseline's handling of ties
- top-1, top-3, false attribution are defined in `eval/metrics.py`. Healthy runs: correct answer is "none".
  False attribution = top-1 names a component other than the faulty one (or any component on a healthy run).
  Wrong cause in the right component is a miss, not a false attribution.
- Baseline cannot separate dns_delay / net_latency / packet_loss from spans alone (they all happen inside one
  HTTP span). It reports all three with equal score and orders them by a seeded random draw. So its top-1 on
  those faults is ~1/3 by design, and its top-3 on them is 100% *by construction* (the candidate set has size 3).
  Read top-3 for network faults accordingly.
- Anomaly threshold per step type = max(p99, 3*median) + 50ms of 30 healthy calibration runs (seeds disjoint from
  eval). Rule fixed before looking at results; the same profile will be used by the combined diagnoser.

## D11. Fault implementations and magnitudes (all seeded)
| fault | mechanism | magnitude |
|---|---|---|
| dns_delay | mock resolver sleeps before replying | 300-1000 ms |
| net_latency | `tc netem delay` on lo, TCP to/from the API port only (root) | 75-250 ms one-way |
| packet_loss | `tc netem loss` on the same scoped path (root) | 20-40 % |
| slow_tool | sleep inside tool | +300-1000 ms |
| slow_db | another connection holds an EXCLUSIVE SQLite lock | 300-1000 ms |
| slow_model | mock LLM delays every response | 200-600 ms x 4 calls |
| rate_limit | mock LLM returns 429 to the first n requests; client backs off 0.25s*2^k | n = 1-3 |
- Ground-truth window = the agent run window (start/end of `run_agent`), for every fault type. Coarse but sufficient
  for run-level labels; per-event windows are not tracked.
- Not covered (state in write-up): loss/latency are static, not bursty; one fault at a time; mock services, not real
  providers; loopback only, so no real routing or congestion.

## D12. Kernel collector: bpftrace, one process per run, JSON lines (2026-11-05)
- User's choice (bpftrace over BCC). `collectors/net_events.bt` + `collectors/ebpf.py`. Started after the services
  (so the DNS port is known) and before the DB lock (startup takes ~1 s). Stopped with SIGINT via sudo.
- Probes: syscalls sendto/recvfrom (DNS), kprobe tcp_v4_connect + sock:inet_sock_set_state (connect), tcp:tcp_retransmit_skb,
  tcp:tcp_probe (srtt). All present on the WSL2 kernel.
- Per-run collection gives clean slicing and isolation; the cost is ~1 s per run, acceptable for a few hundred runs.

## D13. Process attribution is exact only for DNS and connect events
- tcp_probe has no socket pointer, so RTT samples have no PID; retransmits get a PID only if the socket was seen at
  connect (client side). The correlator joins those by 4-tuple/port against the span's server.address/port (D6 plan:
  PID/TID + time + destination).

## D14. Clock alignment groundwork
- Each run stores monotonic<->wall pairs taken before and after (best of 7 reads, error = half the bracketing gap).
- Passwordless sudo for `tc` and `bpftrace` (scripts/setup_sudoers.sh) is root-equivalent: dev WSL only.

## D15. Join design: strongest evidence first, strict containment before slack (Stage 3)
- Exact PID/TID events (DNS, connect) attach by thread + time; a matched connect fixes the client port, which pins
  that connection's PID-less RTT/retransmit events by 4-tuple (both directions, 5 s tail); spans whose connect never
  completed fall back to port + time. Unmatched events are returned with a reason, never dropped.
- Slack only decides when no span strictly contains the event (otherwise a neighbouring span steals it).

## D16. Clock alignment: dense sync pairs, neighbour interpolation, jump reporting
- Pair sampled every 100 ms (best of 3 reads, error = half the bracketing gap, ~0.1 us). WSL's host time sync stepped
  CLOCK_REALTIME by ~3 ms in 7 of 160 runs, so a two-point linear fit is not safe.
- Residual risk: an event within one sync interval of a step can be off by the step size. Reported as a WARNING, not hidden.

## D17. Join validation uses an independent ground truth
- Spans carry `crosstrace.debug.local_port` (the real client port). The correlator must not read it; only
  correlator/integrity.py and the tests do. Without it, the checks would only confirm the matcher's own assumptions.
- Runs that crashed must fail the check (D17 lesson: a vacuous pass hid a total failure for one iteration).

## D18. Fair comparison design (Stage 4)
- Both diagnosers score the same recorded runs (collector on), same step thresholds from the same healthy calibration
  runs. Baseline ties are broken by a seeded random draw so it gets no unearned credit and no unearned penalty.
- The combined diagnoser falls back to the baseline's behaviour whenever kernel evidence is missing or shows nothing.

## D19. The v2 rule change was post-hoc and is reported as such
- After seeing v1's packet_loss misses on seeds 1000-1029 I changed one rule (retransmits explain a high RTT). To avoid
  tuning to the evaluation set, v2 is judged on seeds 2000-2029, recorded after the change and never examined before.
  v1 results stay the headline for set 1; v2 numbers on set 1 are labelled optimistic.

## D20. Overhead measured round-paired with an A/A control
- The first draft treated iterations as independent (A/A showed +44% on api_calls). The unit of analysis is now the
  round, each arm normalised by the same round's untraced block, bootstrap over rounds, plus an untraced-ctrl arm.
- CPU is reported as main-thread vs process because the collector's helper threads live in the agent process.

## D21. Overhead target not met; no mitigation attempted yet
- OTel in-process: about +7.5% latency and +63% main-thread CPU per run. eBPF adds about 4.5 ms/run of bpftrace CPU plus
  about 3.4 ms of helper-thread CPU. Options not tried: head sampling, a lighter span set, batching, moving JSON
  parsing out of the agent process.

## D22. One shared OpenTelemetry provider is the default; per-run providers kept as `Tracing(shared=False)` (Stage 6)
- Profiling showed creating a TracerProvider costs ~0.3 ms per run. The benchmark could not resolve a gain from sharing it
  (+0.6% to +3.5% shared vs -0.3% to +2.3% per-run, intervals overlap), so this is a tidiness change, not an optimisation.

## D23. In-kernel port filtering and ACK-sample dropping, on in recorded runs (Stage 6)
- bpftrace drops events for other ports, and RTT samples on pure ACKs, before printing (about half the events). Join
  integrity was rechecked (24/24). It did NOT reduce overhead (see results-stage6.md section 7); kept because it shrinks stored data.

## D24. The slow_db lock starts at the first query (Stage 6)
- Starting it with the run let it expire during earlier slow steps in two-fault runs, so slow_db never happened. Equivalent for a
  single fault to within a few ms, but Stage 1-5 slow_db runs used the old behaviour and were not rerun.

## D25. v3 frozen before the later Stage 6 suites; predictions committed first
- v3 adds one rule, written after the first slowapi set. Predictions for each later suite were committed before that suite
  ran, and v3 was not changed afterwards. Known v3 weakness left in place: the 429 hedge (results-stage6.md section 3).

## D26. Secrets (Stage 6)
- Keys live in a git-ignored `.env` (or a mode-600 file); code reads them in-process and scrubs them from errors before they
  reach spans or the database. A key is deleted from disk after use, and the user is asked to revoke it. Free models of the
  OpenCode API refuse API use; the restriction was respected, not bypassed.

## D27. What the real-model run changed in my claims (Stage 6)
- v3's latent-loss rule and its server-wait signal were designed on loopback. A real model over TLS showed that the
  latent-loss rule false-alarms on healthy runs (5 of 8) and that server wait is undefined for a remote server. Both are recorded as known
  limitations, not patched: v3 stays frozen, and a v4 would need fresh seeds and a real-network healthy baseline.
- An OpenRouter key belonging to another project was used at the user's explicit request: read in place from that project's
  .env, never copied, cheap model chosen by me, hard token cap, spend reported ($0.0028). The unused OpenCode key's .env was deleted.

## D28. v4: the latent-loss rule gets a healthy retransmit baseline (Stage 7)
- Written after the real-model run; judged on 16 fresh healthy real runs and a 150-run mock regression (identical to v3).
  False alarms on healthy real runs 69% -> 6%. The detection cost (a baseline of 5 hides mild injected loss on that network)
  is untested and stated in the results. v4 is now the version to use on a real network; v3 stays for comparison.
- A syntax error in an eval script reached a paid run once because no test imported it; tests/test_scripts_compile.py now
  compiles every module and script.

## D29. Kernel probes added for real deployments: libc DNS, TCP sends (Stage 8)
- `getaddrinfo` timing (uprobe on libc) replaces dependence on how my own resolver client sends packets; `tcp_sendmsg` gives
  pid/tid-tagged sends, which attach reused keep-alive connections to the right span and give a client-side remote-server wait.
  Both are opt-in per collector and on for real-model runs. A send must never be attached to another thread's span by the
  port+time fallback (caught by a test).

## D30. Real-network faults: scoped, self-removing, and only at the user's request (Stage 8)
- `faultlab/real_path.py` touches only traffic to the model server's resolved IPs on 443 (egress of the default interface) and UDP
  to the resolver on `lo`, behind a prio band that sends all other traffic elsewhere; refuses a dirty interface, always removes
  the rule (finally, watchdog timer, atexit) and raises if the original qdisc is not restored. The guard first matched a substring
  and refused a clean interface ("priomap"); it now matches the qdisc kind.

## D31. Concurrency harness and the mock-server backlog (Stage 9)
- The join was checked at up to 512 simultaneous threads against ground-truth ports. The first run's task errors were the mock
  servers' listen backlog (5): raised to 256, confirmed by rerunning, and recorded as a test-server limit.

## D32. v5: run-level evidence per span class (Stage 10)
- Designed from the Stage 8 real-path data (heavy healthy tails; persistent faults vs sporadic spikes), judged on fresh seeds
  (rf_b) and a 870-evaluation mock regression (identical to v4). The lower median (not the median) makes "persistent" mean more
  than half the calls. v5 is the version to use on a real network; its thresholds must be rebuilt from healthy runs on the
  deployment path, and its detection floor is whatever that path's own noise allows (about 1 s latency and 2.3 s DNS here).

## D33. The correlator indexes its lookups (Stage 11)
- Spans by (pid, thread) and by server port with a bisect over start times; connections by sorted port pair. Linear in events
  instead of events x spans; rules unchanged. `eval/join_perf.py` and `tests/test_join_perf.py` guard it.

## D34. Spans may declare their socket: `network.local.port` is the strongest join evidence (Stage 11)
- Needed when many requests share one pid and thread (async). Connect events now carry the kernel socket pointer so connect-start can be
  paired with connect-end. Spans without the attribute fall back to the older rules; requirement stated, not hidden.

## D35. Remote-server wait = the longest silence after the request (Stage 11)
- Replaced "first payload byte after the request" because TLS 1.3 session tickets arrive right after the request. Judged on fresh runs.

## D36. A fix that did not help was reverted (Stage 11)
- Emitting connect-end without the per-socket map entry changed nothing about the 0.1% missing events at 512 threads, so it was removed.
  Kept: bpftrace lost-event warnings (no `-q`) and the unparseable-line counter. The gap tracks accept-queue overflow of the mock server.
