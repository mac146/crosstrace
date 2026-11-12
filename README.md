# CrossTrace

**A crash test for AI agents.** CrossTrace records real runs of your agent, replays them with no API cost, and breaks one model call at a
time the way providers and networks really break: a stream cut short, a stalled connection, a reset, a 429, an overloaded API. For every
failure it tells you what your agent did next. It runs in CI on every pull request.

Your agent needs no code changes and no SDK. It runs as it is, with its real SDK and framework code paths.

**[crosstrace-site.vercel.app](https://crosstrace-site.vercel.app/)**: the short version, with an animated walkthrough.

## What it finds in a stock LangGraph agent

A support agent written the way most LangGraph tutorials write one (`create_react_agent`, streaming `ChatOpenAI`, default settings,
three tools; [source](examples/real_agents/langgraph_support/agent.py)),
recorded once against a real model on OpenRouter, then tested offline:

```
✗ tool_args_cut          SILENT 3/3
                         e.g. path 0 exchange 0: ran tool get_order with arguments {"order_id": 4} from a response that never
                         finished, instead of retrying exchange 0 (no retry)
✗ stream_stall           HANG 5/5
                         e.g. path 0 exchange 0: no exit within 60 s (no retry)
✗ stream_cut_clean       PARTIAL 2/5, ENDED_EARLY 3/5
                         e.g. path 0 exchange 0: printed nothing and exited 0 after exchange 0 failed
✓ server_error           RECOVERED 5/5
✓ overloaded!persistent  SURFACED 5/5
✓ rate_limit             RECOVERED 5/5
✓ reset_before_response  RECOVERED 5/5
✓ context_length         SURFACED 5/5
! connection churn       1.0 new connections per model call
```

- **The stream broke inside a tool call, and the agent ran the tool anyway.** The model asked for order 42. LangChain completed the
  half-received JSON, and the agent looked up **order 4**, with no error anywhere. The same happened with `get_order({})` and a tracking
  number cut to `"BD1234"`.
- **A stalled stream froze the agent.** `ChatOpenAI` has no read timeout by default; the openai SDK waits up to 10 minutes per attempt.
  Setting `ChatOpenAI(timeout=30)` turns the hang into an error (SURFACED 5/5), but not into a recovery: the SDK's retries stop once
  response headers have arrived, so a mid-stream timeout is never retried.
- **A cut final answer was printed as the answer.** "Your order 42" and "I'm sorry your mug arrived broken, Asha." with exit code 0, or
  nothing at all with exit code 0.
- **What it handled well:** HTTP errors before the stream starts. The SDK's retries cover those.

Each finding comes with the fix for the SDK it detected, and the exact command that reruns that one trial.

These are not exotic faults. A clean cut without the final event is what a gateway produces when its upstream dies but it closes the
response politely. The same behaviour shows up across SDKs: [what the OpenAI, Anthropic and Vercel SDKs do under each fault](docs/sdk-fault-table.md).

## Try it (no API key)

[examples/broken_agents](examples/broken_agents) is a small agent on the official openai SDK whose failure handling can be switched
between good and deliberately broken. Its recording (made against a local mock provider) is in the repo, so the test runs offline:

```bash
git clone https://github.com/mac146/crosstrace && cd crosstrace
pip install . openai
cd examples/broken_agents
crosstrace test                                   # the naive agent: hidden failures, exit code 1
sed -i 's/"naive"/"good"/' crosstrace.toml && crosstrace test     # the same recording, a fixed agent: exit code 0
```

To reproduce the LangGraph results above, record the agent once with an OpenAI-compatible key (an OpenRouter key works as is, a few
cents), then test:

```bash
pip install -r examples/real_agents/langgraph_support/requirements.txt
cd examples/real_agents/langgraph_support
OPENAI_API_KEY=... crosstrace record && crosstrace test
```

A real model answers differently from run to run, so your recordings, and the exchanges a fault lands on, will differ from ours; the
behaviour under each fault is what to compare.

## Use it on your agent

```bash
pip install git+https://github.com/mac146/crosstrace     # not on PyPI yet
crosstrace init --command "python agent.py {input}"      # writes crosstrace.toml: your inputs, timeouts, faults
crosstrace record     # runs your agent for real, once per input (uses your API key)
crosstrace test       # replays with faults; exit code 1 if your agent hides a failure
```

Your agent must read its provider base URL from the environment, which the OpenAI and Anthropic SDKs, LangChain, LangGraph, LiteLLM,
pydantic-ai and the Vercel AI SDK all do ([checked for each](docs/framework-matrix.md)). If yours uses its own variable, map it in
`crosstrace.toml`: `env = { MY_BASE_URL = "{proxy_url}/v1" }`.

Commit `crosstrace.toml` and `.crosstrace/recordings/`, and CI tests every pull request without API keys:

```yaml
# .github/workflows/crosstrace.yml
on: [pull_request]
jobs:
  resilience:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: mac146/crosstrace@main
        with:
          install: pip install -r requirements.txt
```

The report goes to the job summary. A nightly run tests every fault at every model call: [examples/github/crosstrace.yml](examples/github/crosstrace.yml).

## How it decides

CrossTrace sits between your agent and the provider as a local proxy. It breaks one model call per trial, at the byte level, so your
real SDK sees exactly what it would see in production. It then watches the agent's **next move**: did it retry that call, stop with an
error, or carry on as if nothing happened?

| verdict | meaning | |
|---|---|---|
| `RECOVERED` | retried (or fell back) and finished with the healthy answer | ok |
| `SURFACED` | stopped with an error | ok |
| `SILENT` | carried on with a missing or partial answer in its context | problem |
| `PARTIAL` | printed the cut answer as if it were complete | problem |
| `HANG` | never finished | problem |
| `RETRY_STORM`, `IGNORED_RETRY_AFTER`, `WRONG_RETRY` | retried too often, too soon, or what can't succeed | problem |
| `ENDED_EARLY`, `RECOVERED_DIFFERENT` | exited 0 without the failed step, or recovered with a different answer | review |

No LLM judge and no comparison of free-text answers: the verdict comes from which model call the agent made next, so it is the same
every time. Faults are injected at every model call of every recorded path (nightly) or a fixed sample (pull requests), so an agent can't
pass by getting lucky on one run. Before any fault, each recording must replay exactly; if your prompts change on every run, CrossTrace
names the field to mask instead of reporting a false pass.

Details: [docs/cli.md](docs/cli.md). Fault list: `crosstrace faults`.

## Status and limits

Early (v0.1). Tested end to end on the example agents here and the SDK matrix above; not yet on anyone else's production agent. If you
try it on yours, an issue with what it found (or got wrong) is the most useful thing you can send.

- Model calls only: OpenAI chat completions and Responses, Anthropic Messages. Faults on tool calls (MCP) come next.
- Python 3.11+. Your agent can be in any language; it just has to be runnable as a command.
- The healthy run must be repeatable enough to replay. Random values in prompts need a mask; heavy run-to-run variation in what your
  agent does needs several recorded runs.

## Background

CrossTrace started as research on tracing AI agents across the application and the kernel (eBPF): 21 stages, predictions written
before each run. Mining real GitHub issues showed that most agent failures in the wild are failure-*handling* bugs in SDKs and frameworks,
not slow networks, which is why the product tests failure handling. The research is in [docs/research.md](docs/research.md).

MIT licensed, see [LICENSE](LICENSE).
