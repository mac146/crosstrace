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

## Other agents checked

The same faults against other popular frameworks, each recorded once on a cheap real model and then replayed offline (recordings not published).
Every row is what CrossTrace reported after its verdicts were corrected against what the framework really did.

| Framework | Cut stream or cut tool call | Stalled or silent response | Other |
|---|---|---|---|
| LangGraph (`create_react_agent`) | **ran the tool on the cut arguments**, printed a cut answer as complete | hangs (no read timeout) | errors before the stream recovered |
| OpenAI Agents SDK (Responses API) | raised an error (it waits for the completed event) | hangs; `set_default_openai_client(AsyncOpenAI(timeout=...))` turns it into an error | errors before the stream recovered |
| pydantic-ai | refused the cut tool call and told the model (one case: arguments were already complete and it ran the tool); printed a cut answer as complete, also for a Responses `incomplete` status | hangs; `model_settings={"timeout": ...}` turns it into an error | empty reply re-prompted |
| CrewAI (native openai client) | printed a cut answer as complete | hangs; `LLM(timeout=...)` recovers | context-length error summarised and continued; a persistent outage retried 7+ times in about 3 s |

The timeout fixes were run, not assumed (with 10 seconds). In the three streamed agents (LangGraph, the Agents SDK, pydantic-ai) a stall in the middle of a stream is raised, not retried.

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
| `REPROMPTED` | answered the failure with a different request instead of retrying the same call: told the model its reply failed, had its tool arguments refused and the model told, or summarised after a context-length error (the text is shown, so you can judge it) | review |
| `DIVERGED` | after a reply that arrived but was defective, sent a request that matches nothing recorded, and nothing shows it acted on the reply or carried on as before. The run is then continued and settled by how it ends: `RECOVERED` if it finishes, `SURFACED` if it stops with an error, `RETRY_STORM` if it never stops. Only if the replay cannot answer a later request does it stay a review | review |

**What can fail your build, and what cannot.** A verdict fails the build only when it rests on something CrossTrace measured: a hang, too many
or too fast retries, a retried request that can never succeed, a write that ran twice, an answer printed cut, or an agent that acted on a
defective reply or carried on exactly as if nothing had happened. When an agent changes course in a way CrossTrace can only guess at, the
result is a **review** with the facts shown (what it sent, how many messages), never a failure. Every result in the JSON report carries
`observed`: the attempts, the status of the fault, and the agent's next move.

**The run continues past the fault.** When an agent answers a failure with a request the recording does not contain (it re-prompts the
model, summarises, rewrites its request), CrossTrace answers it with the next healthy reply of the recorded plan instead of an error, so the
run reaches its real end and the verdict comes from how it ended. A persistent fault (`"length_cut!persistent"`) keeps hitting the agent's
recovery attempts too, which tests whether its recovery has a bound: an agent that keeps asking after 8 different requests is a `RETRY_STORM`.
No model and no key are needed, so it stays deterministic and offline. It cannot tell whether a re-prompted agent would have received a good
answer from a real model; it only shows that the agent stops. To check that too, add `--live`: those requests then go to the real provider
with your own key (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, read from the environment, never stored or logged), at most 6 calls per trial
(`--live-max-calls`), so a re-prompt is answered by a real model and the run ends on its merits. It spends money, so it is opt-in and meant for a
handful of trials (`--fault`, `--path`, `--at`), not a whole run. Turn continuation off with `--no-continue` or `[test] continue = false`.

No LLM judge and no comparison of free-text answers: the verdict comes from which model call the agent made next, so it is the same
every time. Faults are injected at every model call of every recorded path (nightly) or a fixed sample (pull requests), so an agent can't
pass by getting lucky on one run. Before any fault, each recording must replay exactly; if your prompts change on every run, CrossTrace
names the field to mask instead of reporting a false pass.

Details: [docs/cli.md](docs/cli.md). Fault list: `crosstrace faults`.

## Status and limits

Early (v0.1). Tested end to end on the example agents here and the SDK matrix above; not yet on anyone else's production agent. If you
try it on yours, an issue with what it found (or got wrong) is the most useful thing you can send.

- Model calls: OpenAI chat completions and Responses, Anthropic Messages. Tool calls: MCP over stdio and streamable HTTP, see above.
- Python 3.11+. Your agent can be in any language; it just has to be runnable as a command.
- The healthy run must be repeatable enough to replay. Random values in prompts need a mask; heavy run-to-run variation in what your
  agent does needs several recorded runs.

## Tool calls (MCP): a tool that ran but whose reply was lost

A model call that fails is annoying. A tool call that **succeeded but whose response was lost** can do damage: the email was sent, the
card was charged, the form was submitted, the reply never reached the agent, and a retry does it a second time.

CrossTrace tests this for MCP servers over stdio and over streamable HTTP. For a stdio server, one change in your agent's MCP config:
launch it as `crosstrace mcp-wrap -- <its usual command>`. For a remote server, list its URL in `[mcp.http]` and have the agent read its
server address from `CROSSTRACE_MCP_URL_<NAME>`. The wrapper (or the local stand-in for the URL) records the tool calls in
`crosstrace record`, and in `crosstrace test` it **replays them without starting or calling the real server**, so a test cannot cause a
side effect. An agent with several servers names each one (`mcp-wrap --name github -- ...`, or the key in `[mcp.http]`).

- **`lost_response`:** one tool call is made (and counted), and its reply is replaced by a transport error. Then we watch what the agent does.
- **Write counting:** every call to a tool that changes something is logged. A write that ran more often than in the healthy run is a
  **`DUPLICATE`**, in **every** trial, not only this one. A run that recovers and prints the right answer can still have written twice, so
  recovering is not enough to pass. The same check catches an agent that recovers from a *model* failure by starting the task over.
- **Which tools are writes:** your `[mcp]` lists first, then what the server says about itself (a tool it declares `readOnlyHint` is a read),
  then a guess from the name (`send`, `submit`, `pay`, `delete`, `click`, ...; `get`, `list`, `status` are reads).
  List the ones it gets wrong in `[mcp] writes` / `reads`. For browser tools, click, type, fill_form, file_upload, press_key (Enter submits a
  form), evaluate and run_code are writes; navigate, snapshot and screenshot are reads. `select_option` and `handle_dialog` are ambiguous and
  count as reads: list them under `writes` if a choice or a confirmation can change something.

```
✗ lost_response   DUPLICATE 1/1
                  e.g. path 0 tool call 0: refund({"amount":129.99,"customer_id":"42",...}) ran 2 time(s), 1 in the healthy run;
                  the agent recovered and finished, but the write was repeated
```

Runnable without any key: [examples/mcp_agents](examples/mcp_agents) (`pip install mcp openai`). Also checked by hand against the official
`@playwright/mcp` 0.0.85 (25 tools): a scripted agent, a real Chrome and a local order form. Recording placed one real order; replays and
trials placed none. Losing the response to the click made the retrying agent **DUPLICATE** and the checking agent stop. That agent was
scripted, so it tests the tool-call handling, not how well a model drives a browser. **What this does not do yet:**

- Streamable HTTP is covered for what an agent sends as a POST (answered as JSON or an event stream). A server that pushes messages on its
  own GET stream is not: that stream gets a 405, which the protocol allows. Checked against the official Python SDK's server and client only.
  A lost response over HTTP is a 504 by default; with `how = "drop"` the connection is closed, which the official client turns into a crash
  of the whole session, so the agent cannot retry at all.
- Several servers are handled by name. Each name must be used in both `record` and `test`; two unnamed stdio servers are not told apart.
- Your MCP client must start the wrapper with `CROSSTRACE_MCP` in its environment. The official Python SDK starts servers with a reduced
  environment, so pass it on (the example does).
- Replay cannot answer a state check. An agent that asks a read tool "did it go through?" before retrying gets the recorded answer, or
  "not recorded", so it shows as stopping with an error, not as recovering safely.
- A retry counts as a repeat unless the server declares the tool idempotent (`idempotentHint`) or the call carries an idempotency key as a
  top-level argument (`idempotency_key`, `request_id`, `client_token` and similar). CrossTrace takes the key at its word: it cannot tell
  whether the server honours it. The official Playwright server declares `readOnlyHint` for 6 of its 25 tools and no `idempotentHint`.
- By default the repeat is counted in replay, from a log of the tool calls. `crosstrace test --real-tools` runs the real servers in the
  `lost_response` trials (the model is still replayed), so the retry really happens twice and the report quotes what the server answered.
  It is opt-in because it does the work for real: point it at a test account or a sandbox. Model-fault trials always stay in replay. If the
  real server answers differently from the recording, the agent's next model request may match nothing recorded.
- The lost response is a transport error on a live session, not a dead server process.

## Background

CrossTrace started as research on tracing AI agents across the application and the kernel (eBPF): 21 stages, predictions written
before each run. Mining real GitHub issues showed that most agent failures in the wild are failure-*handling* bugs in SDKs and frameworks,
not slow networks, which is why the product tests failure handling. The research is in [docs/research.md](docs/research.md).

MIT licensed, see [LICENSE](LICENSE).
