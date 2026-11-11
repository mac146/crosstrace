"""Stage 21 client: N sequential chat calls with one openai-python client; one JSON line per call (ms).

    python -m eval.sdk_conn_py <sync-stream|async-stream|sync-nonstream|async-nonstream> <n> <base_url> <gap_ms>
"""
import asyncio
import json
import sys
import time

from openai import AsyncOpenAI, OpenAI

MSG = [{"role": "user", "content": "hi"}]


def run_sync(stream, n, base, gap):
    client = OpenAI(api_key="mock", base_url=base, max_retries=0)
    for i in range(n):
        t0 = time.perf_counter()
        if stream:
            text = "".join(ev.choices[0].delta.content or "" for ev in client.chat.completions.create(model="mock", messages=MSG, stream=True) if ev.choices)
        else:
            text = client.chat.completions.create(model="mock", messages=MSG).choices[0].message.content
        print(json.dumps({"i": i, "ms": 1000 * (time.perf_counter() - t0), "chars": len(text)}), flush=True)
        time.sleep(gap / 1000)


async def run_async(stream, n, base, gap):
    client = AsyncOpenAI(api_key="mock", base_url=base, max_retries=0)
    for i in range(n):
        t0 = time.perf_counter()
        if stream:
            text = ""
            async for ev in await client.chat.completions.create(model="mock", messages=MSG, stream=True):
                if ev.choices:
                    text += ev.choices[0].delta.content or ""
        else:
            text = (await client.chat.completions.create(model="mock", messages=MSG)).choices[0].message.content
        print(json.dumps({"i": i, "ms": 1000 * (time.perf_counter() - t0), "chars": len(text)}), flush=True)
        await asyncio.sleep(gap / 1000)


if __name__ == "__main__":
    mode, n, base, gap = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    kind, flavour = mode.split("-")
    if kind == "sync":
        run_sync(flavour == "stream", n, base, gap)
    else:
        asyncio.run(run_async(flavour == "stream", n, base, gap))
