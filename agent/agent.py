"""Agent loop: ask the model, run the tool it requests, repeat until it answers."""
import json

from agent.tracing import Tracer

MAX_STEPS = 10


def run_agent(task, llm, tools, tracer=None):
    """Returns (final_answer, tracer)."""
    tracer = tracer or Tracer()
    messages = [{"role": "user", "content": task}]
    with tracer.span("invoke_agent", "agent", task=task):
        for _ in range(MAX_STEPS):
            with tracer.span("chat", "model"):
                reply = llm.chat(messages)
            messages.append(reply)
            calls = reply.get("tool_calls")
            if not calls:
                return reply["content"], tracer
            for c in calls:
                name = c["function"]["name"]
                args = json.loads(c["function"]["arguments"])
                with tracer.span(f"execute_tool {name}", "tool", tool=name):
                    result = tools.call(name, args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    raise RuntimeError("agent exceeded MAX_STEPS")
