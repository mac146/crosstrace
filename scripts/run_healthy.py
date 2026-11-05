"""Stage 0 exit criterion: one healthy end-to-end run that prints a trace.

Usage: python scripts/run_healthy.py   (from repo root)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import run_agent  # noqa: E402
from agent.llm_client import LLMClient  # noqa: E402
from agent.tools import Tools, seed_db  # noqa: E402
from mockservices.launcher import services  # noqa: E402

TASK = "What is customer 42's order total and shipping status?"


def main():
    with tempfile.TemporaryDirectory() as d, services() as (llm_url, api_url):
        db = os.path.join(d, "app.db")
        seed_db(db)
        answer, tracer = run_agent(TASK, LLMClient(llm_url), Tools(db, api_url))
        print(tracer.render())
        print(f"\nanswer: {answer}")


if __name__ == "__main__":
    main()
