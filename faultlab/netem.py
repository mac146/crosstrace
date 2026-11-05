"""Scoped `tc netem` fault injection on the loopback interface.

!!! REQUIRES ROOT and changes network settings. Dev VM / WSL only. !!!

Only TCP traffic to/from one port (the mock API's) is degraded; everything else on `lo`
(LLM calls, DNS) is untouched, so ground truth is precise: "the API path is degraded".
Structure: root prio qdisc; band 1 has netem; u32 filters steer the API port into band 1.
`delay_ms` is added per direction, so a request/response round trip gains 2x.

Always cleaned up in __exit__. If a crash left a stale qdisc: `sudo tc qdisc del dev lo root`.
"""
import os
import subprocess

DEV = "lo"


def tc_commands(port, delay_ms=0, loss_pct=0):
    """Pure function: the tc invocations (without the leading `tc`) that set up the fault."""
    netem = ["qdisc", "add", "dev", DEV, "parent", "1:1", "handle", "10:", "netem"]
    if delay_ms:
        netem += ["delay", f"{delay_ms}ms"]
    if loss_pct:
        netem += ["loss", f"{loss_pct}%"]
    cmds = [["qdisc", "add", "dev", DEV, "root", "handle", "1:", "prio"], netem]
    for field in ("dport", "sport"):
        cmds.append(["filter", "add", "dev", DEV, "protocol", "ip", "parent", "1:0", "prio", "1",
                     "u32", "match", "ip", "protocol", "6", "0xff",
                     "match", "ip", field, str(port), "0xffff", "flowid", "1:1"])
    return cmds


def _tc(args, check=True):
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run(prefix + ["tc", *args], check=check, capture_output=True, text=True)


def available():
    """True if we can run tc with root privileges without a password prompt."""
    try:
        return _tc(["qdisc", "show", "dev", DEV], check=False).returncode == 0
    except FileNotFoundError:
        return False


class Netem:
    def __init__(self, port, delay_ms=0, loss_pct=0):
        self.cmds = tc_commands(port, delay_ms, loss_pct)

    def __enter__(self):
        current = _tc(["qdisc", "show", "dev", DEV]).stdout
        if "prio" in current or "netem" in current:
            raise RuntimeError(f"stale qdisc on {DEV}, refusing to touch it: {current.strip()}\n"
                               f"Clean up with: sudo tc qdisc del dev {DEV} root")
        try:
            for c in self.cmds:
                _tc(c)
        except BaseException:
            _tc(["qdisc", "del", "dev", DEV, "root"], check=False)
            raise
        return self

    def __exit__(self, *exc):
        _tc(["qdisc", "del", "dev", DEV, "root"], check=False)
