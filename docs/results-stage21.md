# Stage 21 results: do the OpenAI SDKs reuse connections when streaming?

Predictions: [predictions-stage21.md](predictions-stage21.md), written before any code or run. Harness: [eval/sdk_conn.py](../eval/sdk_conn.py)
(`python -m eval.sdk_conn`, ROOT for the compiled collector), server [mockservices/sse_server.py](../mockservices/sse_server.py), clients
[eval/sdk_conn_node/client.mjs](../eval/sdk_conn_node/client.mjs) and [eval/sdk_conn_py.py](../eval/sdk_conn_py.py). Node 22.11.0, openai-node 7.55.0 and
7.4.0, openai-python 3.46.0 (httpx 0.28.1). A plumbing smoke test (5 calls per arm, server counts only) ran first and is not part of the results.
Data: [data-stage21.json](data-stage21.json).

## Connections per 50 sequential calls (one client object, loopback, HTTP/1.1 keep-alive)
| client | tail 5 ms: server / kernel | tail 0 ms: server / kernel |
|---|---|---|
| **node 7.55.0, streaming** | **50 / 51** (a new connection every call) | 1 / 1 |
| node 7.4.0, streaming | 1 / 1 | 1 / 1 |
| node 7.55.0, non-streaming | 1 / 1 | 1 / 1 |
| **python 3.46.0 sync, streaming** | **50 / 50** | **50 / 50** |
| python 3.46.0 async, streaming | 1 / 1 | 1 / 1 |
| python 3.46.0 sync, non-streaming | 1 / 1 | 1 / 1 |

"tail" is the time between `data: [DONE]` and the chunked terminator. All 600 calls returned their text. Mean call latency on loopback differed
by a few ms at most between arms (45-56 ms, dominated by the server's 2 ms chunk gaps): without TLS and a real round trip, a new connection
costs almost nothing here, so **this stage shows the churn, not its cost on the internet.**

## Predictions against results
1. Kernel and server counts agree exactly in every arm: **wrong in one arm** (node 7.55.0 streaming, tail 5 ms). The kernel saw one connection
   more than the server; in three reruns it was again exactly one connection (3.5-3.7 s into a ~3.6 s run, i.e. around the last call) on
   which no request ever arrived. Its cause is not established. In one rerun the server also counted 49 distinct ports for 50 single-request
   connections, i.e. an ephemeral port number was reused, so distinct server-side ports can undercount and the kernel's connect events are the
   better count. The other 11 arms agreed exactly, port for port.
2. node 7.55.0 streaming, tail 5 ms, at least 0.9 connections per call: **held** (1.02).
3. node 7.4.0 streaming and node non-streaming at most 2: **held** (1 each).
4. python sync streaming at least 0.9 per call: **held** (1.00); python async streaming at most 2: **held** (1).
5. python sync non-streaming at most 2: **held** (1).
6. node 7.55.0 streaming reuses at tail 0 ms (a coin flip): **held** (1). Not predicted: **python sync streaming opened a new connection on every
   call even at tail 0 ms**, so it is not the same timing race as in node; synchronous streaming did not reuse its connection at all here.

## What this supports
- openai-node #3050 reproduces: from 7.5.0 on, a stream whose server sends the chunked terminator a few ms after `[DONE]` costs a new TCP
  connection per call, while 7.4.0 and non-streaming calls reuse one connection. Whether real OpenAI endpoints leave such a gap was not measured.
- The maintainer's statement on openai-python #4040 holds in this version: async streaming reuses, sync streaming does not (both tails).
- The kernel collector shows this from outside the process with no SDK change, and it saw a connection the server never saw.

## Limits
Loopback mock server, no TLS, no HTTP/2, one Node and one Python version each, sequential calls only, one run per arm in the main table (the
node arm was rerun three times). Latency cost on a real TLS path, and the gap real providers leave after `[DONE]`, are not measured.
