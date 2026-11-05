# Design decisions

Running log. Newest at the bottom. Each entry: decision, why.

## D1. Dev environment: WSL2 Ubuntu on Windows 11 Home (2026-11-05)
- Kernel 6.6.87.2-microsoft-standard-WSL2. Feasibility script `scripts/00_feasibility.sh` result:
  - `bpftrace` 0.25.0 attaches a syscall tracepoint and sees `connect()` from `curl`: **OK**
  - `tc qdisc add dev lo root netem delay 10ms loss 1%`: **OK** (`sch_netem` loads)
- So eBPF tracing and `tc netem` faults are feasible without a custom kernel or separate VM.
- All root/network-modifying steps (eBPF, `tc`, DNS) run only inside WSL and are flagged in scripts.
- systemd is not running in this WSL, so `dnsmasq` does not auto-start. faultlab will launch it
  as a child process for `dns_delay`.

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
