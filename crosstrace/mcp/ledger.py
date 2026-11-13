"""Which tool calls are writes, and which writes ran more often than in the healthy run.

A write is a call that changes something outside the agent: send, submit, pay, delete, click. Running a write twice is the failure this
module exists to find, so it also counts writes that the healthy run never made.
"""
import json
import re
from collections import Counter

WRITE_WORDS = {"create", "send", "submit", "pay", "charge", "refund", "delete", "remove", "update", "post", "write", "insert", "book",
               "order", "transfer", "cancel", "publish", "upload", "click", "fill", "type", "set", "add", "save", "approve", "reject",
               "execute", "commit", "push", "merge", "invite", "assign", "move", "rename", "patch", "put", "reply", "email", "purchase",
               # browser tools: a key press can submit a form, a script can do anything, a drag or drop can trigger an action
               "press", "evaluate", "drag", "drop", "unsafe"}
READ_WORDS = {"get", "list", "read", "search", "find", "lookup", "fetch", "status", "check", "view", "describe", "query", "show", "count",
              "info", "details", "history", "snapshot", "screenshot", "inspect", "preview", "validate", "verify"}


def tokens(name):
    """'refund_status' / 'submitForm' / 'browser-click' -> ['refund', 'status'] / ['submit', 'form'] / ['browser', 'click']."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def is_write(name, writes=(), reads=()):
    """Config lists decide first. Otherwise: a read word first or last ('get_order', 'refund_status') makes it a read; any write word makes it
    a write; an unknown name is treated as a read, so an unknown tool never causes a false DUPLICATE."""
    if name in writes:
        return True
    if name in reads:
        return False
    t = tokens(name)
    if not t or t[0] in READ_WORDS or t[-1] in READ_WORDS:
        return False
    return any(w in WRITE_WORDS for w in t)


def args_key(arguments):
    """Canonical form of a tool call's arguments: two calls with the same key are the same call."""
    return json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tool_calls(calls):
    """The recorded tools/call exchanges, in order (the recording also holds initialize, tools/list, ...)."""
    return [c for c in calls if c.get("method") == "tools/call"]


def call_key(call):
    p = call.get("params") or {}
    return p.get("name"), args_key(p.get("arguments"))


def excess_writes(calls, ledger, writes=(), reads=()):
    """[(tool, args_key, expected, executed)] for every write that ran more often than in the healthy run (expected may be 0)."""
    expected = Counter(k for k in map(call_key, tool_calls(calls)) if is_write(k[0], writes, reads))
    executed = Counter((e["tool"], e["args_key"]) for e in ledger if e.get("write"))
    return [(tool, key, expected.get((tool, key), 0), n) for (tool, key), n in executed.items() if n > expected.get((tool, key), 0)]
