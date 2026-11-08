"""Launcher robustness: a port collision at start-up must be retried, not fatal."""
import socket

import pytest

from mockservices import launcher


def test_services_retry_when_a_port_is_taken_between_selection_and_bind(monkeypatch):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    real = launcher.free_port
    calls = {"n": 0}

    def flaky(kind=socket.SOCK_STREAM):
        calls["n"] += 1
        # first attempt: hand out a port that is already in use for the LLM server; later attempts are honest
        return taken if calls["n"] == 1 and kind == socket.SOCK_STREAM else real(kind)

    monkeypatch.setattr(launcher, "free_port", flaky)
    try:
        with launcher.services() as svc:
            assert svc.llm_port != taken and svc.api_port != taken
            assert calls["n"] > 3                     # the second attempt picked fresh ports
    finally:
        blocker.close()


def test_services_give_up_after_the_attempt_limit(monkeypatch):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    real = launcher.free_port
    monkeypatch.setattr(launcher, "START_ATTEMPTS", 2)
    monkeypatch.setattr(launcher, "free_port", lambda kind=socket.SOCK_STREAM: taken if kind == socket.SOCK_STREAM
                        else real(kind))
    try:
        with pytest.raises(RuntimeError):
            with launcher.services():
                pass
    finally:
        blocker.close()
