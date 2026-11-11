"""Model proxy: sits between an agent and its model provider (OpenAI, Anthropic), records and replays exchanges, and (step 2)
injects byte-level faults. Agents are pointed at it with OPENAI_BASE_URL / ANTHROPIC_BASE_URL; no code changes."""
from crosstrace.proxy.faults import CATALOG, NON_RETRYABLE, FaultPlan, make_fault
from crosstrace.proxy.recording import Exchange, Recording, ReplaySession
from crosstrace.proxy.server import ModelProxy, ProxyThread

__all__ = ["CATALOG", "NON_RETRYABLE", "FaultPlan", "make_fault", "Exchange", "Recording", "ReplaySession", "ModelProxy", "ProxyThread"]
