"""MCP tool calls: a transparent stdio wrapper (`crosstrace mcp-wrap`) that records tool calls and, in replay, serves them from the
recording, injects the `lost_response` fault and keeps a ledger of writes. See wrap.py, ledger.py and verdict.py."""
