"""anthropic-python: the messages.stream() helper and its final text."""
from _common import run
from anthropic import Anthropic


def main():
    with Anthropic().messages.stream(model="m", max_tokens=100, messages=[{"role": "user", "content": "tell me"}]) as s:
        return s.get_final_text()


run(main)
