"""Stage 21b: what a new TCP+TLS connection costs on the real path (predictions: docs/predictions-stage21.md).

    python -m eval.tls_cost [--pairs 30]

Unauthenticated GET /v1/models (401: no key, no spend); a reused httpx client vs a new client per request, alternating.
"""
import argparse
import json
import statistics
import time

import httpx

HOSTS = ["https://api.openai.com/v1/models", "https://api.anthropic.com/v1/models"]


def timed(client, url):
    t0 = time.perf_counter()
    r = client.get(url)
    return 1000 * (time.perf_counter() - t0), r.status_code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=30)
    a = ap.parse_args()
    out = {}
    for url in HOSTS:
        reused = httpx.Client(http2=False, timeout=20)
        timed(reused, url)                                     # warm the reused connection
        re_ms, new_ms, codes = [], [], set()
        for _ in range(a.pairs):
            ms, c = timed(reused, url); re_ms.append(ms); codes.add(c)
            with httpx.Client(http2=False, timeout=20) as fresh:
                ms, c = timed(fresh, url); new_ms.append(ms); codes.add(c)
            time.sleep(0.2)
        reused.close()
        out[url] = {"reused_median_ms": round(statistics.median(re_ms), 1), "new_median_ms": round(statistics.median(new_ms), 1),
                    "cost_ms": round(statistics.median(new_ms) - statistics.median(re_ms), 1), "status": sorted(codes), "pairs": a.pairs}
        print(json.dumps({url: out[url]}))
    json.dump(out, open("docs/data-stage21b.json", "w"), indent=1)


if __name__ == "__main__":
    main()
