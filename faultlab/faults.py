"""Fault catalog: turns (fault, seed) into a concrete, reproducible injection plan.

Magnitudes are drawn from random.Random(f"{fault}:{seed}"), so the same seed always gives the same plan.
Ranges for the "normal" magnitude are large compared with healthy jitter (healthy steps take 1-50 ms) and were NOT
tuned against either diagnoser. The original 8 fault types and their draws are unchanged since Stage 1.

Extensions (Stage 6, see docs/results-stage6.md):
  slow_api   the API server itself answers slowly; the network path is healthy.
  "subtle"   magnitudes of 20-100 ms, close to normal noise. Not defined for packet_loss (delays come from TCP's
             retransmit timers, >= 200 ms) or rate_limit (client backoff is a fixed 250 ms).
  "a+b"      two faults at once, e.g. "dns_delay+slow_db". Two netem faults cannot be combined (one qdisc).
"""
import random
from dataclasses import dataclass, field

from diagnosers.common import COMPONENT

FAULT_TYPES = ["none", "dns_delay", "net_latency", "packet_loss", "slow_tool", "slow_db",
               "slow_model", "rate_limit"]
EXTENDED_FAULTS = FAULT_TYPES + ["slow_api"]
NEEDS_ROOT = {"net_latency", "packet_loss"}  # these use `tc netem`
SUBTLE_FAULTS = ["dns_delay", "net_latency", "slow_tool", "slow_db", "slow_model", "slow_api"]

# Chosen to cover network+non-network, two network causes, and two causes on the same step. Fixed in advance.
TWO_FAULT_PAIRS = [
    "dns_delay+slow_db",          # different steps
    "net_latency+slow_model",     # different steps
    "packet_loss+slow_tool",      # different steps
    "slow_api+slow_db",           # different steps, one server-side
    "rate_limit+slow_db",         # different steps
    "slow_model+slow_tool",       # no network involved
    "dns_delay+packet_loss",      # same step, two network causes
    "dns_delay+slow_api",         # same step, network + server
]

# (low, high) in ms; net_latency is one-way and applies in both directions
RANGES = {
    "normal": {"dns_delay": (300, 1000), "net_latency": (75, 250), "slow_tool": (300, 1000),
               "slow_db": (300, 1000), "slow_model": (200, 600), "slow_api": (300, 1000)},
    "subtle": {"dns_delay": (20, 100), "net_latency": (10, 50), "slow_tool": (20, 100),
               "slow_db": (20, 100), "slow_model": (10, 50), "slow_api": (20, 100)},
}


@dataclass(frozen=True)
class FaultPlan:
    fault: str
    seed: int
    target: str                       # component (see diagnosers.common.COMPONENT)
    params: dict = field(default_factory=dict)
    llm_args: tuple = ()
    dns_args: tuple = ()
    api_args: tuple = ()
    slow_extra_ms: int = 0            # slow_tool
    db_lock_ms: int = 0               # slow_db
    netem: dict = None                # {"delay_ms": .., "loss_pct": ..} applied to the API port only


def truth_faults(fault):
    """The injected fault names behind a (possibly combined) fault string."""
    return fault.split("+")


def make_plan(fault, seed, magnitude="normal"):
    if "+" in fault:
        return _combine(fault, seed, magnitude)
    if fault not in EXTENDED_FAULTS:
        raise ValueError(f"unknown fault {fault!r}")
    if magnitude not in RANGES:
        raise ValueError(f"unknown magnitude {magnitude!r}")
    if magnitude != "normal" and fault in ("none", "packet_loss", "rate_limit"):
        raise ValueError(f"no {magnitude} magnitude for {fault}")
    rng = random.Random(f"{fault}:{seed}" if magnitude == "normal" else f"{fault}:{seed}:{magnitude}")
    target = COMPONENT[fault]
    if fault == "none":
        return FaultPlan(fault, seed, target)
    if fault == "packet_loss":
        pct = rng.randint(20, 40)
        return FaultPlan(fault, seed, target, {"loss_pct": pct}, netem={"delay_ms": 0, "loss_pct": pct})
    if fault == "rate_limit":
        n = rng.randint(1, 3)  # first n model requests get HTTP 429
        return FaultPlan(fault, seed, target, {"first_n_429": n}, llm_args=("--rate-limit-first", str(n)))
    ms = rng.randint(*RANGES[magnitude][fault])
    if fault == "dns_delay":
        return FaultPlan(fault, seed, target, {"delay_ms": ms}, dns_args=("--delay-ms", str(ms)))
    if fault == "net_latency":
        return FaultPlan(fault, seed, target, {"one_way_delay_ms": ms}, netem={"delay_ms": ms, "loss_pct": 0})
    if fault == "slow_tool":
        return FaultPlan(fault, seed, target, {"extra_ms": ms}, slow_extra_ms=ms)
    if fault == "slow_db":
        return FaultPlan(fault, seed, target, {"lock_hold_ms": ms}, db_lock_ms=ms)
    if fault == "slow_model":
        return FaultPlan(fault, seed, target, {"delay_ms_per_call": ms}, llm_args=("--delay-ms", str(ms)))
    # slow_api
    return FaultPlan(fault, seed, target, {"delay_ms": ms}, api_args=("--delay-ms", str(ms)))


def _combine(fault, seed, magnitude):
    parts = fault.split("+")
    if len(parts) != 2 or parts[0] == parts[1] or "none" in parts:
        raise ValueError(f"a combined fault is two different non-none faults, got {fault!r}")
    a, b = (make_plan(p, seed, magnitude) for p in parts)
    if a.netem and b.netem:
        raise ValueError("net_latency and packet_loss both use the one netem qdisc and cannot be combined")
    return FaultPlan(fault, seed, "+".join((a.target, b.target)), {a.fault: a.params, b.fault: b.params},
                     llm_args=a.llm_args + b.llm_args, dns_args=a.dns_args + b.dns_args,
                     api_args=a.api_args + b.api_args, slow_extra_ms=a.slow_extra_ms + b.slow_extra_ms,
                     db_lock_ms=a.db_lock_ms + b.db_lock_ms, netem=a.netem or b.netem)
