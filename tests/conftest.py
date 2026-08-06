"""Session-wide DNS guard supplementing pytest-socket's host allowlist, plus the
wall-clock deadline the special-file tests need."""

import signal
import socket
from collections.abc import Iterator
from types import FrameType

import pytest
from pytest_socket import SocketBlockedError


@pytest.fixture
def deadline() -> Iterator[None]:
    """Fail the test after 10 wall-clock seconds instead of hanging forever.

    The M1-308 round-6 regression is a *block*, not a wrong answer: reading a FIFO waits
    for a writer that never comes. A test for it that regresses would hang CI rather than
    fail it, which is strictly worse than no test. There is no pytest-timeout dependency
    and this branch must not add one -- uv.lock serializes the parallel tracks -- so the
    deadline is SIGALRM directly. Unix-only, like the FIFOs it guards.
    """

    def expire(signum: int, frame: FrameType | None) -> None:
        raise AssertionError("test exceeded its 10s deadline -- something is blocking")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, 10.0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def block_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_resolution(*args: object, **kwargs: object) -> None:
        raise SocketBlockedError("A test tried to resolve a network hostname.")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_resolution)
    monkeypatch.setattr(socket, "gethostbyname", refuse_resolution)
