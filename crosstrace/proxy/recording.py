"""A recording is one healthy agent run: its model exchanges in order, with the provider's exact responses.

Never stored: request headers (API keys, auth, cookies) and any response header outside KEEP_HEADER_PREFIXES.
"""
import json
import time
from dataclasses import asdict, dataclass, field

FORMAT_VERSION = 1
KEEP_HEADER_PREFIXES = ("content-type", "retry-after", "x-request-id", "request-id", "openai-", "anthropic-", "x-ratelimit-")


def keep_headers(headers):
    return [[k, v] for k, v in headers if k.lower().startswith(KEEP_HEADER_PREFIXES)]


@dataclass
class Exchange:
    idx: int                       # order of arrival in the recorded run
    path: str
    provider: str
    key: str
    state_key: str
    request: dict                  # normalized request body (masks applied)
    status: int
    headers: list                  # [[name, value]] kept response headers
    stream: bool
    body: str = None               # non-streaming response body
    events: list = field(default_factory=list)      # streaming: raw SSE event texts, without the blank-line separator
    event_ms: list = field(default_factory=list)    # streaming: arrival time of each event, ms after the request was sent
    duration_ms: float = 0.0


@dataclass
class Recording:
    exchanges: list = field(default_factory=list)
    created: float = field(default_factory=time.time)
    version: int = FORMAT_VERSION
    meta: dict = field(default_factory=dict)

    def add(self, ex):
        self.exchanges.append(ex)
        self.exchanges.sort(key=lambda e: e.idx)

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=1, ensure_ascii=False)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") != FORMAT_VERSION:
            raise ValueError(f"{path}: recording format {d.get('version')}, expected {FORMAT_VERSION}")
        return cls(exchanges=[Exchange(**e) for e in d["exchanges"]], created=d["created"], version=d["version"], meta=d.get("meta", {}))


class ReplaySession:
    """Matches the requests of one agent process against a recording.

    Order of preference: the next unused exchange in recorded order if its key matches; any unused exchange with the key
    (concurrent calls arrive out of order); an already served exchange with the key (a retry or repeat: served again);
    otherwise a miss. Every decision is returned with its reason, because the verdict engine (step 3) reads them.
    """

    def __init__(self, recording):
        self.ex = recording.exchanges
        self.cursor = 0
        self.used = set()
        self.served = []                 # indices in the order served (repeats included)

    def match(self, key, state_key=None):
        """state_key: when nothing matches by key, an exchange with the same conversation state (another model or other sampling
        parameters: a fallback) is served, already served ones first (a fallback usually follows a failed attempt)."""
        i, how = None, "miss"
        if self.cursor < len(self.ex) and self.ex[self.cursor].key == key and self.cursor not in self.used:
            i, how = self.cursor, "in_order"
        else:
            i = next((j for j in range(len(self.ex)) if j not in self.used and self.ex[j].key == key), None)
            if i is not None:
                how = "out_of_order"
            else:
                i = next((j for j in reversed(self.served) if self.ex[j].key == key), None)
                if i is not None:
                    how = "repeat"
        if i is None and state_key is not None:
            i = next((j for j in reversed(self.served) if self.ex[j].state_key == state_key), None)
            if i is None:
                i = next((j for j in range(len(self.ex)) if j not in self.used and self.ex[j].state_key == state_key), None)
            if i is not None:
                how = "fallback"
        if i is None:
            return None, "miss"
        self.used.add(i)
        self.served.append(i)
        while self.cursor < len(self.ex) and self.cursor in self.used:
            self.cursor += 1
        return self.ex[i], how

    @property
    def complete(self):
        return len(self.used) == len(self.ex)
