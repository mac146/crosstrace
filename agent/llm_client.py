"""Tiny OpenAI-style chat client (stdlib http.client) with one HTTP span per attempt.

Retries on HTTP 429 with exponential backoff (this is what the rate_limit fault exercises).
"""
import http.client
import json
import time
from urllib.parse import urlparse


class LLMClient:
    def __init__(self, base_url, tracing, max_retries=3, backoff_s=0.25):
        self.base_url = base_url
        self.tracing = tracing
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.retries = 0

    def chat(self, messages):
        u = urlparse(self.base_url)
        body = json.dumps({"model": "mock", "messages": messages}).encode()
        for attempt in range(self.max_retries + 1):
            attrs = {"http.request.method": "POST", "server.address": u.hostname,
                     "server.port": u.port, "url.full": f"{self.base_url}/v1/chat/completions",
                     "http.request.resend_count": attempt}
            with self.tracing.span("POST", attrs, client=True) as span:
                conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
                try:
                    conn.request("POST", "/v1/chat/completions", body,
                                 {"Content-Type": "application/json"})
                    resp = conn.getresponse()
                    data = resp.read()
                    span.set_attribute("http.response.status_code", resp.status)
                finally:
                    conn.close()
            if resp.status == 200:
                return json.loads(data)["choices"][0]["message"]
            if resp.status == 429 and attempt < self.max_retries:
                self.retries += 1
                time.sleep(self.backoff_s * (2 ** attempt))
                continue
            raise RuntimeError(f"LLM call failed: HTTP {resp.status}")
