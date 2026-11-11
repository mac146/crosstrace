"""openai-python: stream a text answer and join the deltas (the common pattern)."""
from _common import run
from openai import OpenAI


def main():
    s = OpenAI().chat.completions.create(model="m", messages=[{"role": "user", "content": "tell me"}], stream=True)
    return "".join(e.choices[0].delta.content or "" for e in s if e.choices)


run(main)
