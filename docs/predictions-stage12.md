# Stage 12 predictions: closing the biggest gaps (written BEFORE their runs)

Scope fixed by what this machine allows: no `clang`, no libbpf headers, no `bpftool`, and installing them needs a sudo password I do
not have, so **a libbpf/C rewrite of the collector is not possible here**. The three gaps are worked on within that limit.

## A. The ~0.1% of connections that lose their connect-end at 512 threads (mock server listen backlog 256, env MOCK_BACKLOG=256)
Experiment: record the sock:inet_sock_set_state tracepoint end (current) AND an fentry end on tcp_finish_connect in the same run
(16 processes x 32 threads, about 75,000 spans). For every span that lacks the tracepoint end, check whether the fentry end exists.
1. The loss reproduces: between 0.03% and 0.2% of spans lack the tracepoint connect-end (it was 0.06-0.13%).
2. **If the cause is that nested tracepoints are skipped, the fentry probe sees at least 90% of those spans' connections.** My belief:
   55% that it does; 45% that it misses them too (meaning the connection really completed some other way, or the loss is elsewhere).
3. If fentry sees them, switching the connect-end to fentry brings the missing share to under 0.02% at backlog 256.

## B. Packet loss that the retransmit count cannot separate from this network's background (real path, OpenRouter)
Current rule (v4/v5): retransmits per run above the healthy maximum (6): 70% detection, healthy false alarms 0/10.
Candidate evidence per run, all computable from events already recorded: (F1) total retransmits [the current rule]; (F2) most
retransmits on a single connection; (F3) retransmit pairs less than 1 s apart on one connection (RTO back-off bursts);
(F4) retransmits of an established connection (state 1) as opposed to a handshake (state 2).
Protocol: choose the best single feature using ONLY the design set (rf_a/rf_b/rf_c loss runs and the 32 healthy calibration runs,
already used in earlier stages); the threshold is the healthy maximum, as for F1. Judge it on a fresh set (new seeds, 10 loss runs and
10 healthy runs).
4. The best alternative to F1 on the design set detects more loss at no more healthy false alarms (60%): at least 85% detection.
5. On the fresh set it keeps at least 80% detection with at most 1 of 10 healthy false alarms. I give this 40%: heavy-tailed noise
   and a small sample can easily erase an apparent gain.

## C. CPU cost of tracing
Not rewritable in C here. Measure where the collector's CPU goes, then try only reductions that need no new tooling.
6. bpftrace's own process accounts for at least half of the collector's CPU, and about 3 ms per 60 ms run is the floor with the
   current event set; reducing events (for example dropping the send probe, or stricter RTT filtering) can cut it by at most 30%.
7. A larger gain (below 1 ms per run) is not reachable without in-kernel aggregation or a compiled collector.

I would be wrong if: the loss does not reproduce (1); fentry misses at least half of the spans with no tracepoint end AND switching
to it still leaves the gap (3); no candidate feature beats F1 on the design set (4); CPU can be cut by more than 50% by event
reduction alone (6).

## D. A compiled collector (C + libbpf, CO-RE), added after the toolchain was installed (written BEFORE it was built or run)
Context for these numbers (measured in Stage 12, bpftrace collector, per 60 ms agent run): bpftrace process 2.7-3.0 ms of CPU, the
reader threads inside the agent process 2.3-2.6 ms, so about 5.1-5.6 ms in total; with no traffic at all the same collector costs
0.08 ms + 0.12 ms, so the cost is per-activity (text formatting and parsing), not a fixed floor. The new collector emits fixed-size
binary records through a ring buffer, the Python side only appends raw bytes while the run is going, and decoding happens afterwards.
Same events, same field names, same clock (CLOCK_MONOTONIC), so the correlator and diagnosers do not change.
8. **CPU during the run falls from about 5.1-5.6 ms to at most 1.5 ms per 60 ms run** (at least 3.4x less), measured by the same
   benchmark with the collector's decode work excluded from the run (it is done offline, and I will say so next to the number).
   My honest range: 55% that it reaches 1.5 ms, 30% that it lands between 1.5 and 3 ms, 15% that it is no better than 3 ms.
9. **Same evidence**: on the same workloads the correlator gives the same result: the 8-fault join integrity check (`make stage3-check`)
   passes 100% with the C collector, and the sustained 512-thread churn at backlog 256 has no more missing connect-ends than the fentry
   bpftrace collector (3 or fewer per 74,240), with 0 ring-buffer losses reported.
10. **Start-up** is faster than bpftrace's compile-and-attach (about 1 s): under 0.5 s.
11. It needs the same root permission model (one sudoers rule for one binary) and cannot be used without it; the bpftrace collector
    stays as the fallback and as the reference the C collector is compared with.

I would be wrong if CPU per run is not at least halved (8), if integrity drops below 100% or the churn gap exceeds the bpftrace
fentry level (9), or if a verifier or attach failure forces dropping any of the probes (then the C collector is incomplete and I say so).
