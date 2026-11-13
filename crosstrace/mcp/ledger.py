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
               "press", "evaluate", "drag", "drop", "unsafe",
               # data and deployment verbs that are never reads
               "upsert", "replace", "overwrite", "truncate", "grant", "revoke", "deploy", "kill"}
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


IDEMPOTENCY_FIELDS = {"idempotencykey", "idempotencytoken", "idempotencyid", "clienttoken", "requestid", "dedupekey", "deduplicationid", "clientrequestid"}


def carries_idempotency_key(key):
    """True when the arguments (an args_key string) hold a non-empty idempotency key. The same call repeated with the same key is the retry
    the pattern is made for: the server is meant to run it once. Only a top-level argument counts, matched ignoring case, '_' and '-'."""
    try:
        args = json.loads(key)
    except (TypeError, ValueError):
        return False
    return isinstance(args, dict) and any(re.sub(r"[_\-]", "", str(k)).lower() in IDEMPOTENCY_FIELDS and v not in (None, "", 0) for k, v in args.items())


def tool_calls(calls):
    """The recorded tools/call exchanges, in order (the recording also holds initialize, tools/list, ...)."""
    return [c for c in calls if c.get("method") == "tools/call"]


def call_key(call):
    p = call.get("params") or {}
    return p.get("name"), args_key(p.get("arguments"))


def tool_hints(calls):
    """What the server itself says about its tools, from the recorded tools/list: ({read-only tools}, {idempotent tools}).

    Only the two hints that can be trusted are used. readOnlyHint true is an explicit claim that the tool changes nothing, and
    idempotentHint true that repeating it is harmless. destructiveHint is not used: servers send it as true for every tool, navigate and
    hover included (the real Playwright server does), so it says nothing about which ones write."""
    read_only, idempotent = set(), set()
    for c in calls:
        if c.get("method") == "tools/list" and isinstance(c.get("result"), dict):
            for t in c["result"].get("tools") or []:
                ann = t.get("annotations") or {}
                if ann.get("readOnlyHint") is True:
                    read_only.add(t.get("name"))
                if ann.get("idempotentHint") is True:
                    idempotent.add(t.get("name"))
    return read_only, idempotent


def effective_lists(config, calls):
    """(writes, reads, idempotent) for a trial: the [mcp] config first, then what the server declares, then the name guess in is_write.

    A tool the config lists under `writes` is always counted, whatever the server claims."""
    config = config or {}
    writes = list(config.get("writes", []))
    read_only, idempotent = tool_hints(calls or [])
    reads = list(config.get("reads", [])) + sorted(t for t in read_only if t not in writes)
    return writes, reads, sorted(t for t in idempotent if t not in writes)


def excess_writes(calls, ledger, writes=(), reads=(), idempotent=()):
    """[(tool, args_key, expected, executed)] for every write that ran more often than in the healthy run (expected may be 0).

    A tool the server declares idempotent is skipped: running it again changes nothing. So is a call whose arguments carry an idempotency key:
    repeating it with the same key is what the key is for (the report does not claim the server honours it)."""
    expected = Counter(k for k in map(call_key, tool_calls(calls)) if is_write(k[0], writes, reads) and k[0] not in idempotent)
    executed = Counter((e["tool"], e["args_key"]) for e in ledger if e.get("write") and e["tool"] not in idempotent
                       and not carries_idempotency_key(e["args_key"]))
    return [(tool, key, expected.get((tool, key), 0), n) for (tool, key), n in executed.items() if n > expected.get((tool, key), 0)]
