# Stage 21 predictions: do the OpenAI SDKs reuse connections when streaming? (written BEFORE any code or run)

## Question
openai-node issue #3050 reports that since 7.5.0 a stream cancels its response body at `[DONE]`, so the keep-alive connection is
destroyed and every streamed call opens a new TCP connection. openai-python #4040 reported the same and was closed with a fix for
async clients only (v3.30.0); the maintainer said synchronous streaming over HTTP/1.1 is not covered. Can our kernel collector show
this per call, from outside the process, with no SDK changes, and does it agree with an independent count?

## Setup
- A mock OpenAI-compatible server (`mockservices/sse_server.py`): HTTP/1.1 keep-alive, chunked SSE, 20 content chunks 2 ms apart,
  then `data: [DONE]`, then the chunked terminator after a configurable tail (5 ms, as in the issue's reproduction, and 0 ms).
  Non-streaming answers carry Content-Length. The server records the client port of every request (ground truth the kernel side never reads).
- Arms (one client process each, 50 sequential calls, 20 ms apart, one client object, retries off):
  node 7.55.0 stream, node 7.4.0 stream (before the regression), node 7.55.0 non-stream,
  python 3.46.0 sync stream, python 3.46.0 async stream, python 3.46.0 sync non-stream.
- Measure per arm: TCP connects to the server port from the kernel collector (compiled collector, `tcp_connect_end`), distinct client
  ports seen by the server, connects per call, mean call latency.
- Loopback, no TLS: this measures connection churn, NOT its latency cost on the internet (a new TLS connection costs 2-3 round trips
  there; not measured in this stage).

## Predictions (confidence)
1. Kernel and server counts of connections agree exactly in every arm (90%).
2. node 7.55.0 stream, tail 5 ms: at least 0.9 new connections per call (80%).
3. node 7.4.0 stream and node 7.55.0 non-stream: at most 2 connections for 50 calls (80%).
4. python sync stream, tail 5 ms: at least 0.9 connections per call (65%); python async stream: at most 2 (70%).
5. python sync non-stream: at most 2 (85%).
6. Tail 0 ms (terminator sent with `[DONE]`): node 7.55.0 stream reuses (at most 2 connections) (50%, a coin flip: it depends on
   whether the terminator is read before the body is cancelled).

## I would be wrong if
- the kernel and server counts disagree (then the collector is not a trustworthy witness for this claim), or
- node 7.55.0 streaming reuses its connection at tail 5 ms (then #3050 does not reproduce here, and there is no post).
