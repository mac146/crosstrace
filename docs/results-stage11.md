# Stage 11 results: the four items that were still untested

Predictions: `predictions-stage11.md` (committed before the runs, except where stated). WSL2, 10 cores, loopback unless noted.

## 1. Production-scale load
**The correlator was quadratic, and is now linear.** (Measured before the predictions file was written, so not pre-registered.)
Per-event cost grew 21 -> 69 -> 378 us as the run grew 4x each time: 3,200 tasks took 6.05 s, about an hour extrapolated to
100,000. Lookups are now indexed (spans by thread and by server port with a bisect, connections by port pair); every existing test
passed unchanged. `make eval-join-perf`: 3,200 tasks in 0.03 s (200x faster); **51,200 tasks (256,000 events, each client port
reused twice) in 0.94 s, 0 wrong, 0 orphans.**

**Sustained churn with real kernel events** (16 processes x 32 threads = 512 simultaneous agents, about 75,000-100,000 connections
per run over about 28,000 ephemeral ports, so ports are reused many times):

| concurrent threads | spans | wrong connection | spans with no connection evidence |
|---|---|---|---|
| 8, 32, 64, 256 | about 75,000 each | 0 | **0** |
| 512, listen backlog 256 (five runs) | 74,000-99,800 | **0** | 57-94 (0.06-0.13%) |
| 512, listen backlog 2,048 (two runs) | 74,240 each | 0 | **0** |

* Predictions: the join stays at least 99.9% correct (right, 99.91-99.94% at worst, and the shortfall was always "no evidence",
  never "wrong evidence"); connect events within 0.5% of spans (right); at least 1% task errors (wrong in the good direction:
  at most 1 task in 20,000).
* **The 0.1% with no connection evidence is tied to accept-queue overflow, not to the join.** Every connect-start and every send
  was captured; only the connect-end event was missing, and only when 512 simultaneous connection attempts exceeded my mock server's
  listen backlog of 256. With the backlog above the concurrency the gap disappeared in two full runs. **The mechanism is not
  established.** Hypotheses I tested and rejected: my reader dropping lines (0 unparseable), bpftrace buffer overflow (no loss
  warnings once `-q`, which hid them, was removed), the per-socket map filling (raising its limit changed nothing), and SYN
  retransmission itself (a forced test with 2-3 s connects emitted every connect-end). A speculative fix (emit the end without the
  map entry) did nothing and was reverted. A BPF program skipped because it fired while another ran on the same CPU fits the
  pattern but is unverified (needs privileges not available here).
* What it means: under connection-queue overflow a small share of connections show up as "no evidence", never as wrong
  evidence. Overflowing accept queues are themselves a fault worth detecting.

## 2. Async agents on ONE thread (asyncio + httpx, 64 coroutines, 1,280 spans)
| coroutines on one thread | A: spans with their own connection, no declared port | B: with `network.local.port` on the spans |
|---|---|---|
| 1 | 1,280 / 1,280 | 1,280 / 1,280 |
| 8 | **7** / 1,280 (222 wrong) | **1,280** / 1,280 |
| 64 | **0** / 1,280 (20 wrong, 1,260 none) | **1,280** / 1,280 |

Predicted exactly this (A under 50%, B at least 99.9%). **Pid and thread id carry no information when requests overlap on one thread.**
The fix is that the HTTP span records the client port of its socket (`network.local.port`, an OpenTelemetry semantic-convention
attribute); the join then matches on it first, pairing connect-start to connect-end through the kernel socket pointer (now emitted
on both events). **That is an instrumentation requirement:** an async agent whose HTTP client does not expose its socket cannot be
joined this way, and asynchronous DNS lookups run in a thread pool, so their thread ids do not match the span's either (not tested).

## 3. A known-slow remote server over TLS (mock LLM behind a self-signed certificate, LangGraph client, server-side events removed before the join)
* Healthy calls (96): wait median 0.5 ms, max 1.3 ms (predicted under 30 ms: right).
* First run, 48 slow calls (injected 200-600 ms): **46/48 within max(10%, 30 ms)**; the two misses measured 0-1 ms.
  Cause: TLS 1.3 sends a small session-ticket record right after the handshake; when it arrives after the request, "first payload
  bytes after the request" mistakes it for the response. The kernel cannot tell them apart by size.
* Fix: the wait is now the **longest silence after the request**. Judged on FRESH runs (different delays): **95/96 (99%)**, against
  85/96 (89%) for the old definition on the very same calls. The one miss: injected 280 ms, measured 241 ms (-39 ms).
* Caveats: loopback TLS, HTTP/1.0-style one request per connection, delays of 200-600 ms. A streamed answer with a long pause in the
  middle would report the longer of first-token latency and that pause.

## 4. Two simultaneous faults on the real model connection (real path, OpenRouter, 10 runs per combination, v5, thresholds from 32 healthy runs)
| combination | scored runs | v5: first cause is one of the two | v5: both in the top two | DNS named | second fault named | v4 both | spans-only both |
|---|---|---|---|---|---|---|---|
| dns_delay + net_latency | 8 (2 excluded: fault not on the path used) | 100% | **7/8 (87.5%)** | 8/8 | 7/8 | 1/8 | 0/8 |
| dns_delay + packet_loss | 10 | 100% | **6/10 (60%)** | 10/10 | 6/10 | 2/10 | 0/10 |

False attribution 0% for v5. All predictions held (both-in-top-two at least 60% and at least 40%; DNS named at least 80%; false
attribution at most 10%). The misses are the known limit: loss under the healthy retransmit baseline is not reported, so v5 names only
the DNS fault in those runs. Cost: $0.0016 for 76 calls.

## Mistakes caught on the way (all fixed or reverted)
* My collector ran bpftrace with `-q`, which silences its lost-events warning, so I could not tell loss from other causes; removed,
  and unparseable lines are now counted.
* Three hypotheses about the missing connect-ends were wrong (above), and one speculative fix was reverted because it did nothing.
* Test bugs: a test that expected a misleading false-attribution result; fixtures with a lookup longer than its span; a diagnosis tool
  that crashed on a span from a failed request.

## What is still untested
Real production traffic and fleets; remote servers other than the one mock and OpenRouter; async agents whose HTTP library does not
expose the socket (no `network.local.port`), and asynchronous resolvers (lookups on pool threads); more than two simultaneous faults;
accept-queue overflow as a fault to detect (here it showed up as missing evidence, not a finding); collector CPU at these event rates
(no drops were seen at 512 threads once the queue did not overflow, but its CPU cost was not measured).
