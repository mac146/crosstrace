# CrossTrace command line

CrossTrace records real runs of your AI agent, replays them with no API cost, and injects one realistic failure at one model call at a
time: a stream cut short, a stalled connection, a reset, a rate limit, a provider error. For each trial it reports what your agent did
next.

```
pip install crosstrace            # from this repo for now: pip install .
crosstrace init --command "python agent.py {input}"
crosstrace record                 # runs your agent for real, once per input
crosstrace test                   # replays with faults; exit code 1 if your agent hides a failure
```

Your agent needs no code changes. It must read its provider base URL from the environment, which the official OpenAI and Anthropic
SDKs, LangChain, LangGraph, LiteLLM, pydantic-ai and the Vercel AI SDK all do (see [framework-matrix.md](framework-matrix.md)).
CrossTrace sets `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` to its local proxy for every run.

## Verdicts

| verdict | meaning | counts as |
|---|---|---|
| `RECOVERED` | the agent retried (or fell back to another model) and finished with the healthy output | ok |
| `SURFACED` | the agent stopped with an error (non-zero exit) | ok |
| `SILENT` | the agent moved on to its next model call without a successful answer to the failed one | problem |
| `PARTIAL` | the agent printed the cut answer and exited 0 | problem |
| `HANG` | no exit within the trial budget | problem |
| `RETRY_STORM` | more than 6 attempts at one call | problem |
| `IGNORED_RETRY_AFTER` | retried a 429 before `Retry-After` | problem |
| `WRONG_RETRY` | retried a non-retryable error (400, 401) | problem |
| `ENDED_EARLY` | exited 0 without the failed call; check its output says the task failed | review |
| `RECOVERED_DIFFERENT` | recovered, but the output differs from the healthy run | review |
| `REPLAY_UNSTABLE`, `NOT_REACHED`, `N/A` | the trial says nothing about the agent (setup) | - |

The verdict is decided from the agent's next move after the fault, not by comparing final answers, so it is objective and needs no LLM
judge. Trials whose verdict is already certain are stopped early.

## Faults for non-streamed responses

Many agents call the model without streaming, with an SDK or a hand-written HTTP client. Three faults return a response that looks fine
to the HTTP layer and is not: `length_cut` (HTTP 200, the answer cut short, with the provider's own length signal: `finish_reason: "length"`,
`stop_reason: "max_tokens"`, or Responses `status: "incomplete"`), `empty_content` (HTTP 200, a valid body with no answer text) and
`malformed_body` (HTTP 200, a correct `Content-Length`, JSON cut off part way). They do not apply to streamed calls, which have their own
faults. `length_cut` and `malformed_body` are in `pr` mode, `empty_content` in `nightly`.

## MCP tool calls

Launch your MCP server through the wrapper in your agent's MCP config: `["crosstrace", "mcp-wrap", "--", "python", "server.py"]`. Outside
CrossTrace the wrapper just runs the server. Under `crosstrace record` it passes everything through and records each request with its
response; under `crosstrace test` it answers from the recording and never starts the real server.

Your MCP client must give the wrapper `CROSSTRACE_MCP` (CrossTrace sets it for the agent process). Clients that start servers with a reduced
environment, such as the official Python SDK, need it passed on explicitly. One wrapped server per agent.

`lost_response` (`[faults.lost_response]`: `how = "error"` (default, a transport error) or `"silent"` (nothing; an agent with no tool timeout
hangs), `persistent = true` to keep failing the retries) loses the response to each recorded tool call in turn. The tool counts as having run.

`DUPLICATE`: a write ran more often than in the healthy run. It is checked in every trial, including model faults, so an agent that recovers
from a model failure by redoing a tool call is caught. Which tools are writes is guessed from their names; override it:

```toml
[mcp]
writes = ["run_report"]        # tools that change something although their name does not say so
reads = ["send_preview"]       # tools that only read although their name sounds like a write
```

## Coverage, and why one run is not enough

`crosstrace record` records each input `runs` times; every recording is one path through your agent. `crosstrace test` injects each fault
at every model call where it applies (`--mode nightly`), or at a fixed, seeded sample that always includes the first and the last call
(`--mode pr`, default). The same commit always gets the same trials. Every failing trial prints the command that reruns exactly it:

```
crosstrace test --path 0 --fault stream_cut_clean --at 2
```

## Replay stability

Before any fault, each recording is replayed with no fault and must match exactly. If a prompt contains a value that changes on every run
(a random id, a counter), the check names the field and both values; add a regex for it to `[replay] masks`. Timestamps and UUIDs are
masked by default. An unstable recording is a setup error (exit 3), never a pass.

## Exit codes

| code | meaning |
|---|---|
| 0 | no problems |
| 1 | problems found |
| 2 | usage or config error |
| 3 | setup problem: no recordings, unstable replay, or the agent failed its healthy run |
| 4 | internal error in CrossTrace (never reported as a problem with your agent) |

## Configuration (`crosstrace.toml`)

`crosstrace init` writes a commented file. Keys: `[agent] command, inputs, timeout_s, cwd, env`; `[record] runs, upstreams`;
`[replay] masks`; `[test] allow, trial_timeout_s`; `[modes.<name>] faults, max_exchanges, stability_runs, jobs`;
`[faults.<fault>] <parameters>`. Unknown keys are errors. Faults are names (`"server_error"`, heals after one failed attempt) or
`"server_error!persistent"` (never heals), or `{ name = "overloaded", heal = 2, params = { ... } }`. `crosstrace faults` lists them.

`[record] upstreams` points recording at an OpenAI-compatible gateway instead of the provider (e.g. a LiteLLM proxy or vLLM).

## Recordings and privacy

Recordings hold your prompts and the model's answers; API keys and request headers are never stored. They are meant to be committed so
CI can replay without keys. Review them, and mask anything sensitive.

## CI

See [`action.yml`](../action.yml) and [`examples/github/crosstrace.yml`](../examples/github/crosstrace.yml): `pr` mode on pull requests,
`nightly` on a schedule. The Markdown report goes to the job summary.

## Limits (v0.1)

- Model calls only (OpenAI chat completions and Responses, Anthropic Messages). Tool calls: MCP servers over stdio (see "MCP tool calls" below); other tool kinds are not tested.
- Plain HTTP/1.1 between your agent and the local proxy; connection findings carry that protocol.
- Agents that make model calls concurrently are matched correctly, but the verdict assumes the faulted call's result is needed before
  the next call.
