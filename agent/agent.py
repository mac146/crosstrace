"""Agent loop: ask the model, run the tool it requests, repeat until it answers.

Span names and attributes follow the OTel GenAI conventions (invoke_agent / chat / execute_tool).
"""
import json

MAX_STEPS = 10


def run_agent(task, llm, tools, tracing):
    """Returns the final answer. Spans go to `tracing`."""
    messages = [{"role": "user", "content": task}]
    with tracing.span("invoke_agent crosstrace-agent",
                      {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "crosstrace-agent"}):
        for _ in range(MAX_STEPS):
            with tracing.span("chat mock", {"gen_ai.operation.name": "chat",
                                            "gen_ai.request.model": "mock"}):
                reply = llm.chat(messages)
            messages.append(reply)
            calls = reply.get("tool_calls")
            if not calls:
                return reply["content"]
            for c in calls:
                name = c["function"]["name"]
                args = json.loads(c["function"]["arguments"])
                with tracing.span(f"execute_tool {name}", {"gen_ai.operation.name": "execute_tool",
                                                            "gen_ai.tool.name": name}):
                    result = tools.call(name, args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    raise RuntimeError("agent exceeded MAX_STEPS")
