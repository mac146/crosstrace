# Stage 3 result: the join (spans + kernel events on one timeline)

Command: `make stage3-check` (= `python -m eval.stage3_check --k 20`): 20 seeded runs for each of the 8 fault types
(160 runs, seeds 5000-5019, split "kernel") with the bpftrace collector on, each joined and checked.
WSL2, loopback only, mock services. Console output of 2026-11-06.

## Result
- **160/160 runs pass all join integrity checks** (after the fixes below).
- Attached kernel events: 1,992 by exact PID/TID + time, 7,152 by flow (4-tuple). Unattached: 13,186 "unrelated traffic"
  (other loopback chatter on WSL, ~80 events per run, no port belongs to us) and 5 events that were another process's
  traffic (Docker, port 2376) whose peer port number happened to equal this run's random API port.
- Wall clock stepped >1 ms against the monotonic clock in 7 of 160 runs (4%); all 7 still pass.

## What "pass" means (correlator/integrity.py)
1. Every HTTP span has exactly one tcp_connect_start and one tcp_connect_end attached.
2. A span gets DNS events if and only if its target is a hostname.
3. **Independent ground truth:** the agent records the real client port of the socket each HTTP span used
   (`crosstrace.debug.local_port`, validation only; the correlator never reads it). The connection the join attached
   to a span must be that port, and every RTT/retransmit attached to it must belong to that connection.
4. Nothing our agent did is left unattached (orphans are only tolerated for flows our agent did not open).
5. The agent run completed and has all its HTTP spans (a crashed run would pass 1-4 vacuously; see below).

Plus 16 hand-built unit tests (tests/test_correlator.py) with known answers: decoy processes and threads, wrong port,
outside the window, concurrent requests to one port, a connect that never completes, clock offset really applied,
drift, a wall-clock step, tail events after span end.

## Problems the checks found (and what was changed)
1. **Neighbour stealing.** The +-1 ms clock slack let an event fall inside two adjacent spans and the shortest span won,
   so connect/DNS events were attached to the wrong span (first real run: 16/40 passed). Fix: strict containment first,
   slack only as a fallback, nearest span wins; DNS only goes to spans that look up a hostname.
2. **Late teardown retransmits.** A lost FIN is retransmitted hundreds of ms after its span ends. Flow tail extended
   from 10 ms to 5 s.
3. **Float precision in the clock map.** `offset + fraction` as a float lost ~50 ns at 1.8e18 ns. Fixed (found by a unit test).
4. **Wall-clock steps.** In 1 of the first 80 runs CLOCK_REALTIME stepped 3.5 ms against CLOCK_MONOTONIC (WSL host time
   sync) between two sync pairs; linear interpolation over the whole run misaligned events by up to 3.5 ms and sent
   every connection to the neighbouring span. Fix: the collector samples the clock pair every 100 ms, the map interpolates
   only between adjacent pairs, and the timeline reports the largest jump (rendered as a WARNING).
5. **My validation was vacuous at first.** Adding the local-port attribute crashed the agent (the port was read after
   http.client had detached the socket), and the check still reported 80/80 because it ran on one-span runs. The check
   now rejects runs that did not complete or have fewer than 5 HTTP spans. Read the port right after `conn.request()`.
6. **A flaw in my own check** (not in the join): it flagged an orphan whenever it touched a service port number. Docker
   traffic collided with a random port once (1 of 160 runs). It now counts only flows whose client port our agent opened.
   This was changed after seeing the failure, so it is a post-hoc refinement of the check; the join code was not touched.

## What a reader sees (excerpt, kernel-dns_delay-5000)
    execute_tool http_api        +      2.7ms     807.2ms
      GET                          +      2.7ms     807.2ms
        | +      2.7ms  DNS query -> 127.0.0.1:35876   [pid+time]
        | +    807.7ms  DNS response after 805.0ms   [pid+time]
        = evidence: dns 805.0ms, connect 0.1ms, retransmits 0, server wait 1.2ms, unexplained 0.9ms
Similarly net_latency shows `connect 400.5ms ... max srtt 400.4ms`, packet_loss shows a SYN retransmit at +1009 ms
and later retransmits, slow_db shows a 532 ms db_query span with no kernel events inside it.

## Limits
- The system under test is small: one sequential agent thread, one request per span. Concurrency (several threads,
  same port) is only tested with synthetic events, not live.
- Order-based matching (k-th connect <-> k-th span on a thread) would be robust to clock error but fails if events are
  lost; not implemented. Time alignment is the only mechanism, so a clock step near a span boundary can still misattach.
- Retransmits after a span ends (teardown) are credited to the span's evidence; the evidence line does not separate
  "during" from "after".
- TLS is not involved (plain HTTP on loopback), so this does not test the TLS case from the brief.
- The attributes needed (process.pid, thread.id, server.address/port) are ones our hand-instrumented agent emits;
  off-the-shelf instrumentation may not carry thread.id.
