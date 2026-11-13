"""A tiny MCP server: one tool that changes money (`refund`) and one that only reads (`refund_status`).

A refund "happens" by appending a line to a file, so a refund that ran twice is visible: the file has two lines.
"""
import json
import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("refunds")
LEDGER = os.environ.get("REFUND_LEDGER", "refunds.jsonl")


@server.tool()
def refund(customer_id: str, action: str, amount: float, note: str = "") -> str:
    """Refund a customer. This moves money: calling it twice refunds twice."""
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps({"customer_id": customer_id, "amount": amount, "note": note}) + "\n")
    return f"refund of {amount} issued to customer {customer_id}"


@server.tool()
def refund_status(customer_id: str) -> str:
    """How much has been refunded to this customer so far."""
    total = 0.0
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            total = sum(r["amount"] for r in map(json.loads, f) if r["customer_id"] == customer_id)
    return f"customer {customer_id} has been refunded {total} in total"


if __name__ == "__main__":
    server.run()
