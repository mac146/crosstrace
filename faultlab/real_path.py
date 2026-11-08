"""Faults on the REAL network path to a REAL remote server, for the real-model test (Stage 8).

!!! REQUIRES ROOT (passwordless `sudo tc`, scripts/setup_sudoers.sh) and TEMPORARILY changes network settings of a real
interface. Dev WSL / VM only. !!!

What it touches, exactly:
  net_latency, packet_loss   egress of the default interface (eth0): TCP to the model server's resolved IPs on port 443 only.
  dns_delay                  egress of the loopback interface: UDP to the resolver (WSL's DNS tunnel, 10.255.255.254:53) only.
Everything else on those interfaces goes through an untouched band (the priomap sends all unfiltered traffic to band 2).
The fault lives for the duration of one run (seconds). Safety nets, in order: refuses to start if the interface already has
a prio/netem qdisc; always removed in __exit__; a watchdog timer removes it after `max_seconds` even if the run hangs; an
atexit hook removes it if the process dies; after removal the qdisc is checked and a failure is raised loudly.
Manual cleanup if ever needed:  sudo tc qdisc del dev eth0 root   (and the same for lo).
"""
import atexit
import os
import random
import socket
import subprocess
import threading

DEFAULT_DEV = "eth0"
RESOLVER = ("10.255.255.254", 53)       # WSL2's DNS tunnel; read from /etc/resolv.conf at runtime when possible

# (low, high). Chosen to be unambiguous against real model latency (0.8-3.3 s, noisy), not subtle: a 300 ms fault on a
# call that already varies by 2 s cannot be told apart from noise by any step-level rule. net_latency is one-way (egress).
REAL_RANGES = {"dns_delay": (4000, 8000), "net_latency": (800, 1500), "packet_loss": (15, 30)}
REAL_FAULTS = tuple(REAL_RANGES)


def real_params(fault, seed):
    rng = random.Random(f"real:{fault}:{seed}")
    lo, hi = REAL_RANGES[fault]
    key = {"dns_delay": "delay_ms", "net_latency": "one_way_delay_ms", "packet_loss": "loss_pct"}[fault]
    return {key: rng.randint(lo, hi)}


def resolver_address():
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.startswith("nameserver"):
                    return (line.split()[1], 53)
    except OSError:
        pass
    return RESOLVER


def resolve_ipv4(host):
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)})


def tc_commands(dev, matches, delay_ms=0, loss_pct=0):
    """Pure function: the `tc` arguments (without the leading tc) that set the fault up.

    matches: [(proto_number, dst_ip, dst_port)], e.g. [(6, "104.18.2.115", 443)].
    The priomap sends ALL unfiltered traffic to band 2 (class 1:2); only filtered flows reach the netem band (1:1).
    """
    netem = ["qdisc", "add", "dev", dev, "parent", "1:1", "handle", "10:", "netem"]
    if delay_ms:
        netem += ["delay", f"{int(delay_ms)}ms"]
    if loss_pct:
        netem += ["loss", f"{int(loss_pct)}%"]
    cmds = [["qdisc", "replace", "dev", dev, "root", "handle", "1:", "prio", "bands", "3",
             "priomap", *(["1"] * 16)], netem]
    for proto, ip, port in matches:
        cmds.append(["filter", "add", "dev", dev, "protocol", "ip", "parent", "1:0", "prio", "1", "u32",
                     "match", "ip", "protocol", str(proto), "0xff", "match", "ip", "dst", f"{ip}/32",
                     "match", "ip", "dport", str(port), "0xffff", "flowid", "1:1"])
    return cmds


def _tc(args, check=True):
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run(prefix + ["tc", *args], check=check, capture_output=True, text=True)


class PathFault:
    """Context manager: apply a scoped netem fault to `dev`, always remove it."""

    def __init__(self, dev, matches, delay_ms=0, loss_pct=0, max_seconds=420):
        self.dev, self.cmds, self.max_seconds = dev, tc_commands(dev, matches, delay_ms, loss_pct), max_seconds
        self._active = False
        self._timer = None

    def _remove(self):
        if self._active:
            self._active = False
            _tc(["qdisc", "del", "dev", self.dev, "root"], check=False)

    def __enter__(self):
        current = _tc(["qdisc", "show", "dev", self.dev]).stdout
        if "prio" in current or "netem" in current:
            raise RuntimeError(f"{self.dev} already has a prio/netem qdisc, refusing to touch it:\n{current.strip()}\n"
                               f"Clean up with: sudo tc qdisc del dev {self.dev} root")
        self._before = current
        self._active = True
        atexit.register(self._remove)
        self._timer = threading.Timer(self.max_seconds, self._remove)
        self._timer.daemon = True
        self._timer.start()
        try:
            for c in self.cmds:
                _tc(c)
        except BaseException:
            self._remove()
            raise
        return self

    def __exit__(self, *exc):
        if self._timer:
            self._timer.cancel()
        self._remove()
        after = _tc(["qdisc", "show", "dev", self.dev]).stdout
        if "prio" in after or "netem" in after:
            raise RuntimeError(f"FAILED to restore {self.dev}! Run: sudo tc qdisc del dev {self.dev} root\n{after}")


def default_dev():
    """The interface of the default route, falling back to eth0."""
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout.split()
        return out[out.index("dev") + 1]
    except (ValueError, IndexError, OSError):
        return DEFAULT_DEV


def real_path_fault(fault, seed, model_host):
    """(context manager, params, target_ips) for one real-network fault against `model_host`:443."""
    params = real_params(fault, seed)
    if fault == "dns_delay":
        ip, port = resolver_address()
        return PathFault("lo", [(17, ip, port)], delay_ms=params["delay_ms"]), params, [ip]
    ips = resolve_ipv4(model_host)
    matches = [(6, ip, 443) for ip in ips]
    if fault == "net_latency":
        return PathFault(default_dev(), matches, delay_ms=params["one_way_delay_ms"]), params, ips
    if fault == "packet_loss":
        return PathFault(default_dev(), matches, loss_pct=params["loss_pct"]), params, ips
    raise ValueError(f"unknown real fault {fault!r}")
