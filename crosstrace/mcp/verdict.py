"""Verdicts for tool calls: DUPLICATE, and the outcome of a `lost_response` trial.

The write check runs on every trial, not only tool faults. A run that recovers from a model failure by redoing a step can write twice
and still print the right answer; recovering is not enough to pass.
"""

from crosstrace.mcp.ledger import excess_writes
from crosstrace.verdict import TrialResult, same_output


def _show(tool, key, limit=60):
    args = key if len(key) <= limit else key[:limit - 3] + "..."
    return f"{tool}({args})"


def write_check(res, calls, ledger, writes=(), reads=(), idempotent=()):
    """Turn any verdict into DUPLICATE when a write ran more often than in the healthy run. Returns res."""
    extra = excess_writes(calls, ledger, writes, reads, idempotent)
    if not extra:
        return res
    tool, key, expected, got = max(extra, key=lambda e: e[3] - e[2])
    before = res.verdict
    new = "a write the healthy run never made" if expected == 0 else f"{expected} in the healthy run"
    res.detail = (f"{_show(tool, key)} ran {got} time(s), {new}"
                  + ("; the agent recovered and finished, but the write was repeated" if before in ("RECOVERED", "RECOVERED_DIFFERENT")
                     else f"; the run otherwise ended as {before}"))
    res.verdict = "DUPLICATE"
    return res


def classify_tool(fault, at, calls, ledger, exit_code, stdout, hang, healthy_stdout, path=0, seconds=0.0, writes=(), reads=(), idempotent=()):
    """The outcome of losing the response to tool call `at` (the tool ran; the agent never heard)."""
    res = TrialResult(path=path, fault=fault, at=at, verdict="N/A", exit_code=exit_code, seconds=round(seconds, 2), stdout=stdout[-2000:])
    lost = next((e for e in ledger if e.get("faulted")), None)
    if lost is None:
        res.verdict, res.detail = "NOT_REACHED", f"the agent never made tool call {at}"
        return res
    key = (lost["tool"], lost["args_key"])
    res.attempts = sum(1 for e in ledger if (e["tool"], e["args_key"]) == key)
    name = _show(*key)
    if hang:
        res.verdict, res.detail = "HANG", f"no exit within the budget after the response to {name} was lost"
    elif exit_code != 0:
        res.verdict, res.detail = "SURFACED", f"stopped with an error (exit {exit_code}) after the response to {name} was lost"
    elif res.attempts > 1 and same_output(stdout, healthy_stdout):
        res.verdict, res.detail = "RECOVERED", f"retried {name} and finished with the healthy output"
    elif res.attempts > 1:
        res.verdict, res.detail = "RECOVERED_DIFFERENT", f"retried {name}; the output differs from the healthy run"
    else:
        res.verdict = "ENDED_EARLY"
        res.detail = f"carried on after the response to {name} was lost, without retrying it; check the output says whether it worked"
    return write_check(res, calls, ledger, writes, reads, idempotent)
