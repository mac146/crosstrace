"""Stage 0/1 smoke demo: one healthy end-to-end run that prints its OTel trace.

Usage: python scripts/run_healthy.py   (from repo root)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import run_agent  # noqa: E402
from agent.llm_client import LLMClient  # noqa: E402
from agent.tools import Tools, seed_db  # noqa: E402
from agent.tracing import Tracing, render  # noqa: E402
from mockservices.launcher import services  # noqa: E402

TASK = "What is customer 42's order total and shipping status?"


def main():
    with tempfile.TemporaryDirectory() as d, services() as svc:
        db = os.path.join(d, "app.db")
        seed_db(db)
        tracing = Tracing()
        answer = run_agent(TASK, LLMClient(svc.llm_url, tracing),
                           Tools(db, svc.api_url, svc.dns_server, tracing), tracing)
        print(render(tracing.spans()))
        print(f"\nanswer: {answer}")


if __name__ == "__main__":
    main()
