# Stage 9 results: the join under concurrency

Harness: `eval/concurrency.py` (`make eval-concurrency`). Config PROCSxTHREADS = that many processes, each with that many
threads, every thread running agent tasks back to back against one set of mock services and one kernel collector (TCP sends on).
All spans are joined in a single call and checked against ground truth the agent records (the real client port of each socket,
which the correlator never reads). Predictions: `predictions-stage9.md`, committed before the run. WSL2, 10 cores, loopback.

## Result (after the mock-server fix described below)
| config | agent threads at peak | tasks | HTTP spans | spans with exactly their own connection | wrong connection | foreign events attached | pid-pinned orphans | task errors |
|---|---|---|---|---|---|---|---|---|
| 1x1 | 1 | 160 | 800 | 800 | 0 | 0 | 0 | 0 |
| 1x8 | 8 | 160 | 800 | 800 | 0 | 0 | 0 | 0 |
| 1x32 | 26 | 160 | 800 | 800 | 0 | 0 | 0 | 0 |
| 4x8 | 32 | 160 | 800 | 800 | 0 | 0 | 0 | 0 |
| 8x8 | 64 | 320 | 1,600 | 1,600 | 0 | 0 | 0 | 0 |
| 8x16 | 128 | 256 | 1,280 | 1,280 | 0 | 0 | 0 | 0 |
| 8x32 | 256 | 768 | 3,840 | 3,840 | 0 | 0 | 0 | 0 |
| 16x16 | 256 | 768 | 3,840 | 3,840 | 0 | 0 | 0 | 0 |
| 16x32 | **512** | 1,024 | 5,120 | **5,120** | 0 | 0 | 0 | 0 |

* The number of kernel connect events equalled the number of HTTP spans in every config: **no events were lost** by the collector,
  at about 15,000 attached events per second in the largest config.
* Every event that carries a pid and thread id was matched by pid + thread + time (about 40%); the rest (RTT, retransmit and
  server-side send events) were matched by the 4-tuple of the connection their span opened.

## Predictions scored
1 (1x1 passes 100%), 2 (at least 99.5% up to 64 threads; observed 100% up to 512), 3 (no lost events up to 64 threads; none lost
at 128, 256 or 512 either, so my 35% chance of loss at 128 did not happen), 4 (no wrong connection at any config) were **right**.
**5 (zero task errors) was wrong:** the first run had 15 task errors at 64 threads and 66 at 128.

## The task errors were my test servers, not the join
All were `ConnectionResetError: Connection reset by peer`. Python's `socketserver` listens with a backlog of 5, so with 55-128
simultaneous clients the kernel reset the surplus connections. I confirmed it rather than assuming it: after raising the mock
servers' backlog to 256 the same configs ran with 0 errors. (Before the fix, a connection that was reset showed up honestly as a
span with no connect event, never as a wrong connection.) The earlier single-agent evaluations never had this many simultaneous
clients and are unaffected.

## What this does not show
* **This is not production load.** Mock services on loopback, sub-second tasks, one machine; the whole 512-thread run took 3.3 s.
  Thousands of agents, many hosts, sustained load over hours and real remote servers are untested.
* **Collector CPU at this event rate was not measured**, only that it kept up without dropping connect events.
* Ephemeral-port reuse inside the 5 s tail window is the failure mode I expected might appear at high connection rates; at up to
  about 1,500 connections per second it did not (5,120 connections in 3.3 s). A longer or faster run could still hit it.
* Threads inside one process are told apart by thread id, processes by pid: an agent that does its network I/O from a shared
  event-loop thread (many requests on one thread id) would break the pid+tid rule. Not tested; async agents are a real gap.
