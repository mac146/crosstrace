"""What the proxy knows about provider APIs: which paths are model exchanges, and how a request becomes a stable key.

A key must be equal for the "same" request across runs, although real prompts carry timestamps, UUIDs and transport fields.
Two keys per request:
  key        the whole normalized request (model and sampling included): exact replay
  state_key  only the conversation state (messages/input/system/tools): a retry with another model (fallback) has the same state_key
"""
import hashlib
import json
import re

API_PATHS = {"/v1/chat/completions": "openai", "/v1/responses": "openai", "/v1/messages": "anthropic"}
DEFAULT_UPSTREAMS = {"openai": "https://api.openai.com", "anthropic": "https://api.anthropic.com"}

# transport or bookkeeping fields: they do not change what the model is asked
IGNORE_FIELDS = {"user", "metadata", "stream_options", "store", "service_tier", "prompt_cache_key", "safety_identifier"}
# the conversation state, for state_key
STATE_FIELDS = ("messages", "input", "instructions", "system", "tools", "tool_choice", "functions", "previous_response_id")

DEFAULT_MASKS = [
    (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<uuid>"),
    (r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b", "<ts>"),
]


def canonical_path(path):
    """Clients disagree on whether the base URL ends in /v1 (the Vercel AI SDK's Anthropic provider wants it, Anthropic's own SDK
    does not), so a model path without /v1 is treated as the same path. One ANTHROPIC_BASE_URL then works for both."""
    if path not in API_PATHS and "/v1" + path in API_PATHS:
        return "/v1" + path
    return path


def provider_for(path):
    return API_PATHS.get(canonical_path(path))


def compile_masks(extra=None, defaults=True):
    """extra: list of regex strings (replaced by <masked>) or (regex, replacement) pairs."""
    out = [(re.compile(p), r) for p, r in (DEFAULT_MASKS if defaults else [])]
    for m in extra or []:
        p, r = (m, "<masked>") if isinstance(m, str) else m
        out.append((re.compile(p), r))
    return out


def _mask(value, masks):
    if isinstance(value, str):
        for rx, repl in masks:
            value = rx.sub(repl, value)
        return value
    if isinstance(value, list):
        return [_mask(v, masks) for v in value]
    if isinstance(value, dict):
        return {k: _mask(v, masks) for k, v in value.items()}
    return value


def normalize(body, masks):
    return _mask({k: v for k, v in body.items() if k not in IGNORE_FIELDS}, masks)


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]


def request_keys(path, body, masks):
    """-> (key, state_key, normalized body)."""
    norm = normalize(body, masks)
    key = _digest({"path": path, "body": norm})
    state_key = _digest({"path": path, "state": {k: norm[k] for k in STATE_FIELDS if k in norm}})
    return key, state_key, norm


def find_event_end(buf):
    """End of the first complete SSE event in buf: (index of the separator, its length), or (-1, 0)."""
    hits = [(i, n) for i, n in ((buf.find(b"\n\n"), 2), (buf.find(b"\r\n\r\n"), 4)) if i >= 0]
    return min(hits) if hits else (-1, 0)
