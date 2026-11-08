# Stage 12 results: closing the biggest gaps

Predictions: [predictions-stage12.md](predictions-stage12.md), committed before each run. Section D (the compiled collector) was
written after the toolchain was installed and before the collector was built.

## A. Missing connect-ends at 512 threads: found and fixed
- The loss reproduced (0.06-0.13%). In a run that recorded both probes, the fentry hook on `tcp_finish_connect` saw **all 62** connections
  the `sock:inet_sock_set_state` tracepoint missed (prediction 2 held). Likely cause: a kprobe/tracepoint is skipped when another BPF
  program is already running on that CPU; fentry has its own recursion guard. This is a hypothesis that fits the data, not a proven mechanism.
- fentry is now the default connect-end: missing share 0.1% -> 0.003-0.004% (3 of 74,240) at backlog 256 (prediction 3 held).
  The remaining 3 are connections whose handshake needed a SYN retransmit (about 1 s); all three collectors (tracepoint, fentry in
  bpftrace, fentry in C) show the same residual, so it is not a collector bug I understand yet.

## B. Packet loss on the real (NAT'd WSL) path: negative result
- On the design set, a per-run feature other than the plain retransmit count looked better; on a fresh set it did not hold
  (6/10 loss runs detected, 2/10 healthy false alarms for both rules). Predictions 4 and 5 were wrong or unconfirmed (I gave 5 only 40%).
  The current rule (v4/v5: retransmits per run above the healthy maximum) stays: about 70% detection, 0/10 false alarms.
  Untested idea: count spurious-RTO events from the kernel instead of retransmits. Packet loss stays a documented limit on this path.

## C. CPU: where it goes
- bpftrace collector, per 60 ms agent run: bpftrace process 2.7-3.0 ms + reader threads in the agent 2.2-2.6 ms = about 5 ms.
  With no traffic it costs 0.08 ms + 0.12 ms, so the cost is per event (formatting and parsing text), not a fixed floor.
  Event reduction alone cut at most a little (prediction 6 held, 7 held: below 1 ms was not reachable by trimming events).

## D. The compiled collector (C + libbpf, CO-RE)
Code: [collectors/ctrace/](../collectors/ctrace) (BPF program + loader, binary records through a 16 MB ring buffer),
[collectors/cebpf.py](../collectors/cebpf.py) (appends raw bytes during the run, decodes after it). Same event dicts, same clock.
Same probes: connect (kprobe), connect-end (fentry), retransmit and RTT (tp_btf), TCP send (fentry, optional), stub-resolver DNS
(syscall tracepoints), libc `getaddrinfo` (uprobe, optional). Selected by `CROSSTRACE_COLLECTOR` = `auto` (default: compiled if
installed), `c` or `bpftrace`.

| measure (same machine, 12 rounds x 40 runs) | bpftrace (fentry) | compiled C | prediction |
|---|---|---|---|
| collector process CPU per 60 ms run | 2.85 ms | 1.00 ms | - |
| collector work inside the agent process | 2.19 ms | 0.72 ms | - |
| **total collector CPU per run** | **5.05 ms** | **1.72 ms (2.9x less)** | at most 1.5 ms: **missed** |
| latency overhead vs untraced | +3.6% [2.7, 4.4] | +1.6% [-0.6, 3.2] | - |
| start-up (median of 5) | 0.94 s | 0.31 s | under 0.5 s: met |
| `make stage3-check` join integrity | 100% | 160/160 runs | 100%: met |
| 512-thread churn, backlog 256, missing connect-ends | 3 / 74,240 | 3 / 74,240 | at most 3: met; ring-buffer losses 0 |
| full test suite (live DNS/libc/send probes on the C path) | - | 252 passed, 1 skipped | - |

- I put 55% on reaching 1.5 ms and 30% on the 1.5-3 ms band; the result is in the band, so prediction 8 was wrong, not by much.
  The remaining 0.72 ms inside the agent process is the Python reader thread and the 100 ms clock-sync sampling; I did not
  separate the two (untested).
- The decoding cost is not in these numbers: it is done after the run (about 0.1 s per 100k events), which is the point of the design.
- api_calls workload (very short calls): collector CPU is still visible (+86% main-thread CPU vs +101% for bpftrace); the benchmark has
  wide intervals there and I do not claim more than "not worse".

## Bug found on the way (worth knowing)
- The first compiled build joined 0/160 runs: it reported pids as the kernel's global ids, but this WSL session runs in its own pid
  namespace, so every pid-pinned event was an orphan. bpftrace translates pids into the caller's namespace; the loader now does the same
  (`bpf_get_ns_current_pid_tgid` with the loader's pid-namespace dev/inode). The integrity check caught it; the unit and live tests did not,
  because they compared a connection's start with its end, both wrong in the same way.

## Security note
`setup_sudoers.sh` now copies the compiled collector to the root-owned `/usr/local/libexec/crosstrace/ctrace` and allows only that path;
a binary in a user-writable folder could be replaced and then run as root. Re-run the script after every rebuild. As before, passwordless
`bpftrace` is root-equivalent: dev WSL/VM only.

## E. Where the compiled collector's in-process CPU goes (prediction 12: wrong)
Same benchmark, 12 rounds x 40 runs, one session (this session's ctrace process read 1.2-1.3 ms instead of 1.0, so compare within it):
in-process CPU per run is 0.89 ms with the 100 ms clock sampling and 0.78 ms with it effectively off, a fall of 0.11 ms (12%).
I predicted at least a third (55%). So the clock-sync thread is a minor part; most of the cost is the reader thread reading the pipe
(and starting the process), which I have not split further. The 100 ms sampling stays (it is what protects the join from clock steps).
A candidate fix, not built: let ctrace write to a file or shared memory and read it after the run, so Python does nothing during the run.
