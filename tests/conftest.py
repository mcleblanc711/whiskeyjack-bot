"""Session-wide DNS guard supplementing pytest-socket's host allowlist."""

import socket

import pytest
from pytest_socket import SocketBlockedError


@pytest.fixture(autouse=True)
def block_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_resolution(*args: object, **kwargs: object) -> None:
        raise SocketBlockedError("A test tried to resolve a network hostname.")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_resolution)
    monkeypatch.setattr(socket, "gethostbyname", refuse_resolution)
