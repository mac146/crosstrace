"""Run the bpftrace kernel collector around a block of code.

!!! REQUIRES ROOT (passwordless `sudo bpftrace`, see scripts/setup_sudoers.sh). Dev WSL/VM only. !!!

    with KernelCollector(dns_ports=[5353]) as k:
        ...run the agent...
    k.events      # list of dicts, sorted by t (CLOCK_MONOTONIC ns)
    k.clock_sync  # [(mono_ns, wall_ns, err_ns), ...] about every 100 ms, for the Stage 3 join
"""
import json
import os
import signal
import subprocess
import threading
import time

BT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "net_events.bt")


def program(dns_ports, ports=None, reduce_rtt=False):
    """bpftrace program text. ports: restrict TCP events to these service ports (None = record all TCP).
    reduce_rtt: drop RTT samples on pure ACKs unless srtt is already above 20 ms (about half the events)."""
    dns_cond = " || ".join(f"$port == {int(p)}" for p in dns_ports)
    if ports:
        connect_cond = " || ".join(f"$port == {int(p)}" for p in ports)
        flow_cond = " || ".join(f"args->sport == {int(p)} || args->dport == {int(p)}" for p in ports)
    else:
        connect_cond = flow_cond = "1"
    rtt_cond = "args->data_len > 0 || args->srtt > 20000" if reduce_rtt else "1"
    with open(BT_FILE) as f:
        return (f.read().replace("@@DNS_COND@@", dns_cond)
                .replace("@@CONNECT_COND@@", connect_cond).replace("@@FLOW_COND@@", flow_cond)
                .replace("@@RTT_COND@@", rtt_cond))


def sync_clocks(samples=7):
    """Best (tightest) pair of CLOCK_MONOTONIC and wall clock readings.

    Brackets the monotonic read between two wall reads; the wall time is taken at the midpoint and the
    half-gap is the error bound. Returns (mono_ns, wall_ns, err_ns).
    """
    best = None
    for _ in range(samples):
        w0 = time.time_ns()
        m = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        w1 = time.time_ns()
        err = (w1 - w0) // 2
        if best is None or err < best[2]:
            best = (m, w0 + err, err)
    return best


def available():
    try:
        return subprocess.run(["sudo", "-n", "bpftrace", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


class KernelCollector:
    def __init__(self, dns_ports=(53,), start_timeout=30, sync_interval_s=0.1, ports=None,
                 reduce_rtt=False):
        self.dns_ports = list(dns_ports)
        self.ports = list(ports) if ports else None   # TCP service ports to keep; None = all
        self.reduce_rtt = reduce_rtt
        self.start_timeout = start_timeout
        # The wall clock can step relative to CLOCK_MONOTONIC mid-run (WSL host time sync stepped it by
        # 3.5 ms once in 80 runs), so sample the pair often: a step is then confined to one interval.
        self.sync_interval_s = sync_interval_s
        self.events, self.clock_sync, self.stderr = [], [], ""

    def __enter__(self):
        self._proc = subprocess.Popen(
            ["sudo", "-n", "bpftrace", "-q", "-B", "none", "-e", program(self.dns_ports, self.ports, self.reduce_rtt)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self._ready = threading.Event()
        self._err = []
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._err_reader = threading.Thread(target=lambda: self._err.extend(self._proc.stderr), daemon=True)
        self._reader.start()
        self._err_reader.start()
        if not self._ready.wait(self.start_timeout):
            self._stop()
            raise RuntimeError("bpftrace did not become ready: " + "".join(self._err)[-500:])
        self.clock_sync.append(sync_clocks())
        self._stop_sync = threading.Event()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()
        return self

    def _sync_loop(self):
        while not self._stop_sync.wait(self.sync_interval_s):
            self.clock_sync.append(sync_clocks(samples=3))

    def _read_stdout(self):
        for line in self._proc.stdout:
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # partial line at shutdown
            if ev["ev"] == "ready":
                self._ready.set()
            else:
                self.events.append(ev)

    def _stop(self):
        if self._proc.poll() is None:
            os.kill(self._proc.pid, signal.SIGINT)  # sudo relays it to bpftrace
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.kill(self._proc.pid, signal.SIGTERM)
                self._proc.wait(timeout=5)
        self._reader.join(timeout=5)
        self._err_reader.join(timeout=5)
        self.stderr = "".join(self._err)

    def __exit__(self, *exc):
        if hasattr(self, "_stop_sync"):
            self._stop_sync.set()
            self._sync_thread.join(timeout=2)
        self.clock_sync.append(sync_clocks())
        self._stop()
        self.events.sort(key=lambda e: e["t"])
        lost = [l for l in self.stderr.splitlines() if "lost" in l.lower() or "error" in l.lower()]
        self.warnings = lost
