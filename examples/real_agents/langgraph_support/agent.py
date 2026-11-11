"""A support agent written the way a LangGraph user writes one: create_react_agent, a streaming ChatOpenAI with the library's
default retries and timeout, and three tools. Nothing in it knows about CrossTrace.

The tools are local and deterministic (an in-memory order table), so a replay of a healthy run is exact.

  OPENAI_API_KEY=... python agent.py "Where is order 42 and when will it arrive?"

It talks to any OpenAI-compatible endpoint: OPENAI_BASE_URL (the openai SDK reads it), default OpenRouter. MODEL picks the model.
"""
import os
import sys

os.environ.setdefault("LANGSMITH_TRACING", "false")

from langchain_core.tools import tool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402

ORDERS = {
    42: {"customer": "Asha", "items": ["kettle", "2x mug"], "total": 61.50, "status": "shipped", "carrier": "BlueDart",
         "tracking": "BD123456789IN", "eta": "2026-11-14"},
    43: {"customer": "Ravi", "items": ["desk lamp"], "total": 24.00, "status": "processing", "carrier": None, "tracking": None,
         "eta": None},
}
TRACKING = {"BD123456789IN": "In transit: left the Delhi hub, out for delivery on 2026-11-14"}


@tool
def get_order(order_id: int) -> str:
    """Look up an order: items, total, status, carrier and tracking number."""
    o = ORDERS.get(order_id)
    return f"order {order_id}: {o}" if o else f"order {order_id} not found"


@tool
def track_shipment(tracking_number: str) -> str:
    """Get the latest carrier update for a tracking number."""
    return TRACKING.get(tracking_number, "no tracking information")


@tool
def refund_policy(reason: str) -> str:
    """The refund policy for a given reason (damaged, late, changed_mind)."""
    return {"damaged": "full refund, no return needed", "late": "shipping refunded if more than 3 days late",
            "changed_mind": "refund within 14 days of delivery, item returned unused"}.get(reason, "ask a human agent")


SYSTEM = ("You are a customer-support agent for an online shop. Use the tools to look up facts; never guess order details. "
          "Answer in at most three sentences.")


def main():
    question = " ".join(sys.argv[1:]) or "Where is order 42 and when will it arrive?"
    # LLM_TIMEOUT: the fix CrossTrace suggests for HANG (unset = the library default, which is what most agents run with)
    timeout = float(os.environ["LLM_TIMEOUT"]) if os.environ.get("LLM_TIMEOUT") else None
    llm = ChatOpenAI(model=os.environ.get("MODEL", "z-ai/glm-5.3-flash"), streaming=True, temperature=0, timeout=timeout,
                     base_url=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"))
    agent = create_react_agent(llm, [get_order, track_shipment, refund_policy], prompt=SYSTEM)
    result = agent.invoke({"messages": [("user", question)]}, {"recursion_limit": 12})
    print(result["messages"][-1].content)


if __name__ == "__main__":
    main()
