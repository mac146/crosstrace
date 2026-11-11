"""anthropic-python: a streamed tool call through the messages.stream() helper (it assembles the JSON arguments)."""
from _common import run
from anthropic import Anthropic

TOOLS = [{"name": "refund", "description": "Refund a customer", "input_schema": {"type": "object", "properties": {
    "customer_id": {"type": "string"}, "action": {"type": "string"}, "amount": {"type": "number"}, "note": {"type": "string"}}}}]


def main():
    with Anthropic().messages.stream(model="m", max_tokens=100, tools=TOOLS, messages=[{"role": "user", "content": "refund customer 42"}]) as s:
        m = s.get_final_message()
    return {"tool_inputs": [b.input for b in m.content if b.type == "tool_use"], "stop_reason": m.stop_reason}


run(main)
