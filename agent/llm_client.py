"""Tiny OpenAI-style chat client (stdlib only). Retries on HTTP 429 (for the rate_limit fault)."""
import json
import time
import urllib.error
import urllib.request


class LLMClient:
    def __init__(self, base_url, max_retries=3, backoff_s=0.1):
        self.base_url = base_url
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.retries = 0  # total 429 retries seen, useful for tracing/ground truth checks

    def chat(self, messages):
        body = json.dumps({"model": "mock", "messages": messages}).encode()
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < self.max_retries:
                    self.retries += 1
                    time.sleep(self.backoff_s * (2 ** attempt))
                    continue
                raise
