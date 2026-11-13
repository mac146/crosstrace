# CrossTrace

**A crash test for AI agents.** CrossTrace records real runs of your agent, replays them offline with no API cost, and breaks one model call
at a time the way providers and networks really break: a stream cut short, a stalled connection, a reset, a rate limit, an overloaded API, a
reply cut at the token limit. For every failure it tells you what your agent did next, and the fix for the SDK it detected. It runs in CI.

Your agent needs no code changes. It only has to read its provider base URL from the environment (the OpenAI and Anthropic SDKs, LangChain,
LangGraph, LiteLLM, pydantic-ai, the OpenAI Agents SDK and the Vercel AI SDK all do).

```bash
pip install crosstrace
crosstrace init --command "python agent.py {input}"
crosstrace record     # runs your agent for real, once per input (uses your API key)
crosstrace test       # replays with faults, offline; exit code 1 if your agent hides a failure
```

What it found in a stock LangGraph agent: a stream cut inside a tool call made it run the tool on the truncated arguments (the model asked for
order 42, the agent looked up order 4, no error); a stalled stream froze it; a cut answer was printed as the whole answer.

It also tests MCP tool calls over stdio: when a tool succeeded but its response was lost, a retry can repeat a write (a second email, a second
form submit). CrossTrace counts writes after recovery and reports `DUPLICATE`.

Early (v0.1), open source (MIT).

- Source, full README and examples: https://github.com/mac146/crosstrace
- Short version with an animated walkthrough: https://crosstrace-site.vercel.app/
- Command reference: https://github.com/mac146/crosstrace/blob/main/docs/cli.md
- Issues: https://github.com/mac146/crosstrace/issues
