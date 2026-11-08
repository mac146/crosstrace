# Stage 9 predictions: the join under concurrency (written BEFORE the run)

Test (`eval/concurrency.py`): configs PROCSxTHREADS = 1x1, 1x8, 1x32, 4x8, 8x8, 8x16 (up to 128 agent threads in 8
processes), about 160 agent tasks per config (5 HTTP spans each), mock services, one kernel collector (TCP sends on), all spans
joined in one call and checked against the real client port each socket used (ground truth the correlator never reads).

Predictions, in advance:
1. **1x1 passes 100%** (the old single-agent result must not regress).
2. **At every config up to 8x8 (64 threads), at least 99.5% of HTTP spans get exactly the connection they used**, with zero
   foreign events attached. Threads are told apart by thread id and processes by pid, so concurrency alone should not confuse the
   join; the pid+tid rule has no time-window ambiguity between different threads.
3. **No lost kernel events up to 64 threads** (connect events counted equal to HTTP spans); at 8x16 (128 threads) I give 35%
   that bpftrace's buffers overflow and at least 1% of connect events are lost. If events are lost, the affected spans will show
   up as "no connection", not "wrong connection".
4. **A wrong connection (a misattribution) at any config would surprise me.** The failure mode I do worry about is ephemeral
   port reuse within the 5 s tail window at high connection rates; I put it below 10% to see even one such case at these rates.
5. Task errors: zero at every config (the mock servers are threaded).

I would be wrong if any config shows a wrong connection, or the pass rate falls below 99% up to 64 threads.

Not covered: real remote servers, TLS, thousands of agents, many machines, sustained load over hours. This is a laptop-scale
concurrency test, and the word "production" does not apply to it.
