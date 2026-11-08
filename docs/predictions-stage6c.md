# Stage 6c predictions: a third-party agent framework (written BEFORE the run)

Test: the same agent task built with LangGraph 1.2 (`create_react_agent`) and an OpenAI-compatible client, pointed at our
local mock model server (a real model was not reachable: the OpenCode key has no credit, and its free models refuse API
use). Instrumentation wraps the model call and each tool in the framework's own execution thread (agent/langgraph_agent.py).
Suite: `slowapi` (none, slow_api, dns_delay, net_latency, packet_loss), 30 runs each, diagnosers base and v3 (frozen),
healthy calibration runs recorded with the same LangGraph agent. Seeds 9000+, split `lg_a`.

What I expect, in advance:
1. **Spans and parenting work.** LangGraph copies contextvars into its worker threads, so the chat and tool spans stay
   children of `invoke_agent`. Probability I am right: about 70%. If parenting breaks, every diagnoser answers "none"
   and accuracy collapses; that would be the headline finding.
2. **Thread attribution works** for the tools (the span is created in the thread that opens the socket), so the join
   attaches DNS and connect events to the API tool's HTTP spans: at least 95% of them.
3. **Connection reuse costs evidence.** The OpenAI client keeps its HTTP connection alive, so only the FIRST model call
   has a connect event. My correlator only follows flows it saw connect, so RTT samples of the later model calls should
   be orphaned or unattached. I expect kernel evidence attached to roughly 1 of the 4 model-call spans per run.
4. **Accuracy for faults on the API tool stays near the mock-agent level** (dns_delay, net_latency, slow_api, packet_loss
   at 85-100% for v3), because that tool's code is unchanged. v3 beats spans-only on them, as before.
5. **Hidden retries:** the OpenAI client retries 429 itself inside one HTTP span, so a rate limit would be invisible to the
   429 rule. I do not run that fault here, but I expect a model-side fault to be harder than in the mock agent.
6. Healthy runs: 0 false alarms for both diagnosers (coarse rule).

I would be wrong if parenting breaks (1), if the join attaches under 90% of the API tool spans (2), or if later model
calls still get kernel evidence (3).
