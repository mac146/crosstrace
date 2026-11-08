"""Python side of the compiled collector (collectors/ctrace, C + libbpf).

!!! REQUIRES ROOT (the installed binary is allowed by scripts/setup_sudoers.sh). Dev WSL/VM only. !!!

Same interface and same event dicts as collectors.ebpf.BpftraceCollector, so the correlator and the diagnosers do not change.
While the run is going this only appends raw bytes (no parsing, no JSON); records are decoded after the run is over.
"""
import os
import signal
import socket
import struct
import subprocess
import threading

from collectors.ebpf import LIBC, sync_clocks

BINARY = os.environ.get("CROSSTRACE_CTRACE", "/usr/local/libexec/crosstrace/ctrace")
REC = struct.Struct("<QQQ II II II HH B3x 16s 48s")      # must match struct ct_rec in collectors/ctrace/ctrace.h
HDR = struct.Struct("<4sIQ")
assert REC.size == 120

(DNS_QUERY, DNS_RESPONSE, CONNECT_START, CONNECT_END, RETRANSMIT, RTT, SEND, LIBC_QUERY, LIBC_RESPONSE) = range(1, 10)


def _ip(v):
    return socket.inet_ntoa(struct.pack("<I", v))


def _s(b):
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


def decode(buf):
    """Binary records -> list of event dicts (same shape as the bpftrace JSON events). Trailing partial record is ignored."""
    n = len(buf) // REC.size
    out = []
    for (t, sk, lat, pid, tid, saddr, daddr, aux, aux2, sport, dport, typ, comm, host) in REC.iter_unpack(buf[:n * REC.size]):
        if typ == DNS_QUERY:
            ev = {"ev": "dns_query", "t": t, "pid": pid, "tid": tid, "comm": _s(comm), "daddr": _ip(daddr), "dport": dport}
        elif typ == DNS_RESPONSE:
            ev = {"ev": "dns_response", "t": t, "pid": pid, "tid": tid, "comm": _s(comm),
                  "bytes": struct.unpack("<i", struct.pack("<I", aux))[0], "lat_ns": lat}
        elif typ == CONNECT_START:
            ev = {"ev": "tcp_connect_start", "t": t, "pid": pid, "tid": tid, "comm": _s(comm), "daddr": _ip(daddr),
                  "dport": dport, "sk": sk}
        elif typ == CONNECT_END:
            ev = {"ev": "tcp_connect_end", "t": t, "pid": pid, "tid": tid, "saddr": _ip(saddr), "sport": sport,
                  "daddr": _ip(daddr), "dport": dport, "lat_ns": lat, "sk": sk}
        elif typ == RETRANSMIT:
            ev = {"ev": "tcp_retransmit", "t": t, "pid": pid, "tid": tid, "saddr": _ip(saddr), "sport": sport,
                  "daddr": _ip(daddr), "dport": dport, "state": aux}
        elif typ == RTT:
            ev = {"ev": "tcp_rtt", "t": t, "saddr": _ip(saddr), "sport": sport, "daddr": _ip(daddr), "dport": dport,
                  "srtt_us": aux, "bytes": aux2}
        elif typ == SEND:
            ev = {"ev": "tcp_send", "t": t, "pid": pid, "tid": tid, "sport": sport, "dport": dport, "bytes": aux}
        elif typ == LIBC_QUERY:
            ev = {"ev": "dns_query", "t": t, "pid": pid, "tid": tid, "comm": _s(comm), "via": "libc", "host": _s(host)}
        elif typ == LIBC_RESPONSE:
            ev = {"ev": "dns_response", "t": t, "pid": pid, "tid": tid, "comm": _s(comm), "via": "libc", "host": _s(host),
                  "ret": struct.unpack("<i", struct.pack("<I", aux))[0], "lat_ns": lat}
        else:
            continue
        if ev.get("via") == "libc" and _numeric(ev["host"]):
            continue          # getaddrinfo("127.0.0.1") is not a lookup (same rule as the bpftrace collector)
        out.append(ev)
    return out


def _numeric(host):
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def available():
    """True if the installed collector can be run without a password (it exits with usage status 2 on a bad flag)."""
    try:
        return subprocess.run(["sudo", "-n", BINARY, "--bad-flag"], capture_output=True, timeout=20).returncode == 2
    except (OSError, subprocess.SubprocessError):
        return False


class CCollector:
    def __init__(self, dns_ports=(53,), start_timeout=30, sync_interval_s=0.1, ports=None,
                 reduce_rtt=False, libc_dns=False, tcp_send=False, connect_fentry=False, connect_end=None):
        self.dns_ports = list(dns_ports)
        self.ports = list(ports) if ports else None
        self.reduce_rtt, self.libc_dns, self.tcp_send = reduce_rtt, libc_dns, tcp_send
        self.connect_end = "fentry"
        self.start_timeout = start_timeout
        self.sync_interval_s = sync_interval_s
        self.events, self.clock_sync, self.stderr = [], [], ""
        self.bad_lines = 0
        self.lost = 0
        self.warnings = []

    def _argv(self):
        a = ["sudo", "-n", BINARY]
        for p in self.dns_ports:
            a += ["--dns-port", str(int(p))]
        for p in self.ports or []:
            a += ["--port", str(int(p))]
        if self.reduce_rtt:
            a.append("--reduce-rtt")
        if self.tcp_send:
            a.append("--tcp-send")
        if self.libc_dns:
            a += ["--libc-dns", LIBC]
        return a

    def __enter__(self):
        self._proc = subprocess.Popen(self._argv(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self._chunks = []
        self._ready = threading.Event()
        self._err = []
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._err_reader = threading.Thread(target=lambda: self._err.extend(self._proc.stderr), daemon=True)
        self._reader.start()
        self._err_reader.start()
        if not self._ready.wait(self.start_timeout):
            self._stop()
            raise RuntimeError("ctrace did not become ready: " + b"".join(self._err)[-500:].decode("utf-8", "replace"))
        self.clock_sync.append(sync_clocks())
        self._stop_sync = threading.Event()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()
        return self

    def _sync_loop(self):
        while not self._stop_sync.wait(self.sync_interval_s):
            self.clock_sync.append(sync_clocks(samples=3))

    def _read_stdout(self):
        out, head = self._proc.stdout, b""
        while len(head) < HDR.size:
            b = out.read(HDR.size - len(head))
            if not b:
                return
            head += b
        magic, size, _ = HDR.unpack(head)
        if magic != b"CTRC" or size != REC.size:
            self.bad_lines += 1
            return
        self._ready.set()
        while True:
            b = out.read1(1 << 16)
            if not b:
                return
            self._chunks.append(b)        # raw bytes only; decoded after the run

    def _stop(self):
        if self._proc.poll() is None:
            os.kill(self._proc.pid, signal.SIGINT)  # sudo relays it to ctrace
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.kill(self._proc.pid, signal.SIGTERM)
                self._proc.wait(timeout=5)
        self._reader.join(timeout=5)
        self._err_reader.join(timeout=5)
        self.stderr = b"".join(self._err).decode("utf-8", "replace")

    def __exit__(self, *exc):
        if hasattr(self, "_stop_sync"):
            self._stop_sync.set()
            self._sync_thread.join(timeout=2)
        self.clock_sync.append(sync_clocks())
        self._stop()
        raw = b"".join(self._chunks)
        if len(raw) % REC.size:
            self.bad_lines += 1           # a torn record at shutdown, counted not hidden
        self.events = decode(raw)
        self.events.sort(key=lambda e: e["t"])
        for line in self.stderr.splitlines():
            if line.startswith("lost="):
                self.lost = int(line.split("=")[1])
        warn = [l for l in self.stderr.splitlines() if "failed" in l.lower() or "error" in l.lower()]
        if self.lost:
            warn.append(f"ring buffer dropped {self.lost} event(s)")
        if self.bad_lines:
            warn.append(f"{self.bad_lines} unparseable chunk(s)")
        self.warnings = warn
