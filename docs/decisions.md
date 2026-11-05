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
