"""Run the bpftrace kernel collector around a block of code.

!!! REQUIRES ROOT (passwordless `sudo bpftrace`, see scripts/setup_sudoers.sh). Dev WSL/VM only. !!!

    with KernelCollector(dns_ports=[5353]) as k:
        ...run the agent...
    k.events      # list of dicts, sorted by t (CLOCK_MONOTONIC ns)
    k.clock_sync  # [(mono_ns, wall_ns, err_ns), ...] taken before and after, for the Stage 3 join
"""
import json
import os
import signal
import subprocess
import threading
import time

BT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "net_events.bt")


def program(dns_ports):
    cond = " || ".join(f"$port == {int(p)}" for p in dns_ports)
    with open(BT_FILE) as f:
        return f.read().replace("@@DNS_COND@@", cond)


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
    def __init__(self, dns_ports=(53,), start_timeout=30):
        self.dns_ports = list(dns_ports)
        self.start_timeout = start_timeout
        self.events, self.clock_sync, self.stderr = [], [], ""

    def __enter__(self):
        self._proc = subprocess.Popen(
            ["sudo", "-n", "bpftrace", "-q", "-B", "none", "-e", program(self.dns_ports)],
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
        return self

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
        self.clock_sync.append(sync_clocks())
        self._stop()
        self.events.sort(key=lambda e: e["t"])
        lost = [l for l in self.stderr.splitlines() if "lost" in l.lower() or "error" in l.lower()]
        self.warnings = lost
