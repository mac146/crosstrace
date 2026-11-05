"""Synthetic span builders for diagnoser tests (no services, no timing noise)."""
from itertools import count

_ids = count(1)


def span(name, start_ms, end_ms, parent=None, **attrs):
    sid = f"{next(_ids):016x}"
    return {"span_id": sid, "parent_id": parent["span_id"] if parent else None, "name": name,
            "start_ns": int(start_ms * 1e6), "end_ns": int(end_ms * 1e6), "attrs": attrs}


def healthy_run(extra=None):
    """A run whose steps take: chat 5ms x4, db 2ms, api 3ms, slow_tool 50ms.

    extra stretches steps by the given ms. Keys: 'chat' (all four model calls), 'chat0' (only the
    first model call), 'db_query', 'http_api', 'slow_tool'. 'attempts': N makes the first model
    call contain N HTTP attempts, all but the last returning 429.
    """
    extra = extra or {}
    t = 0.0
    spans = []
    root = span("invoke_agent x", 0, 0, **{"gen_ai.operation.name": "invoke_agent"})
    spans.append(root)

    def step(added_ms, base_ms, op, tool=None, attempts=0):
        nonlocal t
        d = base_ms + added_ms
        attrs = {"gen_ai.operation.name": op}
        if tool:
            attrs["gen_ai.tool.name"] = tool
        s = span(f"{op} {tool or ''}", t, t + d, root, **attrs)
        spans.append(s)
        for i in range(attempts):
            status = 429 if i < attempts - 1 else 200
            spans.append(span("POST", t, t + 1, s, **{"http.response.status_code": status}))
        t += d

    chat = extra.get("chat", 0)
    step(chat + extra.get("chat0", 0), 5, "chat", attempts=extra.get("attempts", 0))
    step(extra.get("db_query", 0), 2, "execute_tool", "db_query")
    step(chat, 5, "chat")
    step(extra.get("http_api", 0), 3, "execute_tool", "http_api")
    step(chat, 5, "chat")
    step(extra.get("slow_tool", 0), 50, "execute_tool", "slow_tool")
    step(chat, 5, "chat")
    root["end_ns"] = int(t * 1e6)
    return spans
