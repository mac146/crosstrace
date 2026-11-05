# Stage 2 result: do the raw kernel events show the injected faults?

Command: `python -m eval.stage2_check --k 5` (5 seeded trials per fault with the bpftrace collector on; seeds 5000-5004,
split "kernel", separate from the evaluation runs). WSL2 kernel 6.6.87.2, loopback only. Requires `scripts/setup_sudoers.sh`.
Numbers below are the console output of that command on 2026-11-05. n=5 per fault: this is a qualitative exit check, not
an accuracy measurement.

## Median kernel signals per fault (ms; counts for *_retx and llm_connects)

| fault | dns_ms | api_connect_ms | api_retx | api_srtt_ms | api_resp_ms | llm_resp_ms | llm_connects | max_gap_ms |
|---|---|---|---|---|---|---|---|---|
| none | 0.4 | 0.0 | 0 | 0.0 | 0.5 | 1.3 | 4 | 51.5 |
| dns_delay | **656.1** | 0.1 | 0 | 0.0 | 1.5 | 1.3 | 4 | 656.7 |
| net_latency | 0.4 | **372.3** | 0 | **372.5** | **187.8** | 1.1 | 4 | 372.3 |
| packet_loss | 0.4 | **1008.7** | **2** | 1.2 | 1.5 | 1.2 | 4 | 1008.6 |
| slow_tool | 0.4 | 0.0 | 0 | 0.0 | 0.5 | 1.1 | 4 | **682.2** |
| slow_db | 0.9 | 0.1 | 0 | 0.0 | 1.2 | 1.6 | 4 | **532.7** |
| slow_model | 1.1 | 0.1 | 0 | 0.0 | 1.1 | **314.6** | 4 | 314.5 |
| rate_limit | 0.7 | 0.1 | 0 | 0.0 | 0.7 | 1.2 | **5** | 251.4 |

(llm_retx was 0 in every fault: the netem scoping affects only the API port. Columns not shown are in the console output.)

## Injected value vs measured by the kernel (per run, 5 runs each)
- dns_delay: injected 804/655/540/844/316 ms, measured 805.1/656.1/541.2/844.9/316.9 ms (within 1.2 ms).
- net_latency (connect = 2x one-way delay): injected 400/152/424/372/344, measured 400.3/152.4/424.3/372.3/344.2 ms.
- slow_model (response gap): injected 313/253/508/202/415, measured 314.6/254.8/509.4/203.6/416.6 ms.
- rate_limit (extra LLM connects = number of 429s): injected 2/1/1/3/1, measured 2/1/1/3/1.
- packet_loss: 2-3 API-port retransmits in all 5 runs.

## What this shows, and what it does not
- Network-path faults (dns_delay, net_latency, packet_loss) and model-side faults (slow_model, rate_limit) each have a
  distinct, quantitatively accurate kernel signature. These are exactly the cases the span-only baseline confuses.
- slow_tool and slow_db are **not** directly visible in the kernel: both appear only as a long quiet gap with
  healthy network signals. The kernel alone cannot tell them apart from each other; telling them apart needs the span
  that covers the gap (Stage 3 join). Kernel evidence there is "the network was fine" (elimination), not a positive.
- rate_limit is inferred from extra connects; the 429 itself is invisible to the kernel (we do not parse payloads).
- packet_loss retransmits were present in all 5 runs here. Whether they are present in the 23% of eval runs where
  loss caused no visible slowdown is not yet checked; that is a Stage 4 question.

## Limits (carry into the write-up)
- DNS is captured via sendto()/recvfrom() on our own resolver client (D9). A real libc/systemd-resolved lookup uses other
  syscalls and a different socket pattern, so the DNS probe would need to change.
- IPv4 TCP and UDP only. `tcp_rtt` events carry no PID (kernel `tcp_probe` has no socket pointer); retransmit PIDs
  are known only for client sockets seen at connect time. Joining those events to spans must use the 4-tuple/port.
- The collector adds ~1 s startup per run (bpftrace compile+attach). Runtime overhead is not measured yet (Stage 4).
- Clock alignment: events use CLOCK_MONOTONIC; spans use wall clock. `clock_sync` pairs (error ~0.1 us) are stored
  per run. A live test checks a kernel event lands within 2 ms of the wall-clock window around a real sendto().
