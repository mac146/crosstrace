"""langchain-openai: a streamed tool call, chunks accumulated the standard way (LangChain parses partial JSON arguments)."""
from _common import run
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


@tool
def refund(customer_id: str, action: str, amount: float, note: str) -> str:
    """Refund a customer."""
    return "done"


def main():
    acc = None
    for ch in ChatOpenAI(model="m").bind_tools([refund]).stream("refund customer 42"):
        acc = ch if acc is None else acc + ch
    return {"tool_calls": [{"name": t["name"], "args": t["args"]} for t in acc.tool_calls],
            "invalid_tool_calls": [{"name": t.get("name"), "args": t.get("args"), "error": t.get("error")} for t in acc.invalid_tool_calls]}


run(main)
