# Stage 11 predictions: the four remaining untested items (written BEFORE their runs)

Disclosure first: the correlator's quadratic scaling (6.05 s for 3,200 tasks; about 200x faster after indexing, 0.94 s for 51,200
tasks, 0 wrong) was measured BEFORE this file was written, so it is not a pre-registered result. Everything below is.

## 1. Sustained high connection churn (mock agent, 16 processes x 32 threads, about 20,000 tasks = about 100,000 connections,
##    well past the ~28,000 ephemeral client ports, so ports are reused many times; real kernel events, one collector)
* The join stays correct: at least 99.9% of HTTP spans get exactly the connection they used, no foreign events attached.
* Kernel events are not lost: connect events equal HTTP spans within 0.5%.
* **Task errors from the machine itself** (ephemeral-port exhaustion, resets) are plausible: 40% that at least 1% of tasks error.
  Any such error will be reported, with its kind, not hidden.

## 2. Async agents sharing ONE thread (asyncio, 64 concurrent coroutines on one thread id, httpx.AsyncClient to the mock LLM)
* **The current join fails here.** All coroutines share one pid and thread id, so the pid+thread rule cannot tell their requests
  apart and picks the innermost span by time. Prediction: fewer than 50% of spans get their own connection (85% confidence it is
  badly wrong; 15% that time ordering happens to carry it).
* **A fix exists if spans declare their socket.** If an HTTP span carries the standard attribute `network.local.port` (OpenTelemetry
  semantic convention), the join can use it as the strongest evidence. With it: at least 99.9% correct. Spans without the attribute
  keep today's behaviour. This requires instrumentation that records the local port: not guaranteed in real deployments.

## 3. A known-slow remote server over TLS (local mock LLM behind TLS with a self-signed certificate, slow_model delays of 200-600 ms,
##    LangGraph client; server-side kernel events are removed before the join so only the client's view remains, as for a remote server)
* The client-side server wait tracks the injected delay: within 10% or 30 ms (whichever is larger) on at least 90% of model calls,
  including the first call of a run (the TLS handshake sends come first; the Stage 8 heuristic was fixed for this).
* Healthy calls (no delay) show a wait under 30 ms.

## 4. Two simultaneous faults on the real model connection (dns_delay + net_latency, dns_delay + packet_loss; 10 runs each; v5)
* dns_delay + net_latency: both named in the top two in at least 60% of runs (DNS and RTT evidence are independent).
* dns_delay + packet_loss: DNS named in at least 80%; both named in at least 40% (loss alone was 70%).
* False attribution (first cause not one of the two injected): at most 10%.

I would be wrong if: any wrong connection appears in test 1 or under the old join in test 2 stays above 90%; the client-side wait
misses on more than 30% of calls in test 3; or dns+latency finds both in under 40% in test 4.

Not covered: a real production fleet; remote servers I do not control; three or more simultaneous faults; async frameworks that
hide the socket (so `network.local.port` cannot be recorded).
