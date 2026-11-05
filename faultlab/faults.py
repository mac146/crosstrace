"""Fault catalog: turns (fault, seed) into a concrete, reproducible injection plan.

Magnitudes are drawn from random.Random(f"{fault}:{seed}"), so the same seed always gives
the same plan. Ranges are chosen so a fault is large compared with healthy jitter (healthy steps
take 1-50ms); they are NOT tuned against either diagnoser.
"""
import random
from dataclasses import dataclass, field

from diagnosers.common import COMPONENT

FAULT_TYPES = ["none", "dns_delay", "net_latency", "packet_loss", "slow_tool", "slow_db",
               "slow_model", "rate_limit"]
NEEDS_ROOT = {"net_latency", "packet_loss"}  # these use `tc netem`


@dataclass(frozen=True)
class FaultPlan:
    fault: str
    seed: int
    target: str                       # component (see diagnosers.common.COMPONENT)
    params: dict = field(default_factory=dict)
    llm_args: tuple = ()
    dns_args: tuple = ()
    slow_extra_ms: int = 0            # slow_tool
    db_lock_ms: int = 0               # slow_db
    netem: dict = None                # {"delay_ms": .., "loss_pct": ..} applied to the API port only


def make_plan(fault, seed):
    if fault not in FAULT_TYPES:
        raise ValueError(f"unknown fault {fault!r}")
    rng = random.Random(f"{fault}:{seed}")
    target = COMPONENT[fault]
    if fault == "none":
        return FaultPlan(fault, seed, target)
    if fault == "dns_delay":
        ms = rng.randint(300, 1000)
        return FaultPlan(fault, seed, target, {"delay_ms": ms}, dns_args=("--delay-ms", str(ms)))
    if fault == "net_latency":
        ms = rng.randint(75, 250)  # one-way, added in both directions
        return FaultPlan(fault, seed, target, {"one_way_delay_ms": ms}, netem={"delay_ms": ms, "loss_pct": 0})
    if fault == "packet_loss":
        pct = rng.randint(20, 40)
        return FaultPlan(fault, seed, target, {"loss_pct": pct}, netem={"delay_ms": 0, "loss_pct": pct})
    if fault == "slow_tool":
        ms = rng.randint(300, 1000)
        return FaultPlan(fault, seed, target, {"extra_ms": ms}, slow_extra_ms=ms)
    if fault == "slow_db":
        ms = rng.randint(300, 1000)
        return FaultPlan(fault, seed, target, {"lock_hold_ms": ms}, db_lock_ms=ms)
    if fault == "slow_model":
        ms = rng.randint(200, 600)  # per model call, 4 calls per run
        return FaultPlan(fault, seed, target, {"delay_ms_per_call": ms}, llm_args=("--delay-ms", str(ms)))
    # rate_limit
    n = rng.randint(1, 3)  # first n model requests get HTTP 429
    return FaultPlan(fault, seed, target, {"first_n_429": n}, llm_args=("--rate-limit-first", str(n)))
