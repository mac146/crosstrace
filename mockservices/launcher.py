"""Start the mock services as real subprocesses (separate PIDs, real sockets).

Subprocesses matter: later stages attribute kernel events to processes.
Each server prints READY on stdout once its socket is bound.
"""
import contextlib
import socket
import subprocess
import sys
from dataclasses import dataclass

API_HOST = "api.mock.local"  # resolved through the mock DNS server


@dataclass(frozen=True)
class Services:
    llm_url: str
    api_url: str      # http://api.mock.local:<port>, needs the DNS server
    dns_server: tuple  # (ip, udp_port)
    llm_port: int
    api_port: int
    pids: dict


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(module, port, extra):
    p = subprocess.Popen([sys.executable, "-m", module, "--port", str(port), *extra],
                         stdout=subprocess.PIPE, text=True)
    if p.stdout.readline().strip() != "READY":
        p.kill()
        p.wait()
        p.stdout.close()
        raise RuntimeError(f"{module} failed to start")
    return p


def _stop(procs):
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(timeout=5)
        p.stdout.close()


START_ATTEMPTS = 5


@contextlib.contextmanager
def services(llm_args=(), api_args=(), dns_args=()):
    # A free port is picked, released, and bound a moment later by the server process, so another process can grab it in
    # between ("Address already in use", seen about once in 200 launches). Retry with fresh ports.
    for attempt in range(START_ATTEMPTS):
        llm_port, api_port = free_port(), free_port()
        dns_port = free_port(socket.SOCK_DGRAM)
        procs = []
        try:
            for module, port, extra in (("mockservices.llm_server", llm_port, llm_args),
                                        ("mockservices.api_server", api_port, api_args),
                                        ("mockservices.dns_server", dns_port, dns_args)):
                procs.append(_spawn(module, port, extra))
            break
        except RuntimeError:
            _stop(procs)
            if attempt == START_ATTEMPTS - 1:
                raise
    try:
        yield Services(f"http://127.0.0.1:{llm_port}", f"http://{API_HOST}:{api_port}",
                       ("127.0.0.1", dns_port), llm_port, api_port,
                       {"llm": procs[0].pid, "api": procs[1].pid, "dns": procs[2].pid})
    finally:
        _stop(procs)
