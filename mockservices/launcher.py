"""Start the mock services as real subprocesses (separate PIDs, real sockets).

Subprocesses matter: later stages attribute kernel events to processes.
"""
import contextlib
import socket
import subprocess
import sys
import time


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        time.sleep(0.05)
    raise RuntimeError(f"port {port} did not open")


@contextlib.contextmanager
def services(llm_args=(), api_args=()):
    """Yield (llm_url, api_url) with both servers running."""
    llm_port, api_port = free_port(), free_port()
    procs = [
        subprocess.Popen([sys.executable, "-m", "mockservices.llm_server",
                          "--port", str(llm_port), *llm_args]),
        subprocess.Popen([sys.executable, "-m", "mockservices.api_server",
                          "--port", str(api_port), *api_args]),
    ]
    try:
        _wait_port(llm_port)
        _wait_port(api_port)
        yield f"http://127.0.0.1:{llm_port}", f"http://127.0.0.1:{api_port}"
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=5)
