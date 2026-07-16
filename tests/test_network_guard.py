"""Canary proving the session-wide CI network guard is active."""

import socket

import pytest
from pytest_socket import SocketBlockedError, SocketConnectBlockedError


def test_non_loopback_and_dns_connections_are_blocked() -> None:
    with pytest.warns(UserWarning), pytest.raises(SocketBlockedError):
        socket.getaddrinfo("example.com", 443)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        with pytest.warns(UserWarning), pytest.raises(SocketConnectBlockedError):
            client.connect(("192.0.2.1", 9))

    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        server.bind(("127.0.0.1", 0))
        server.listen()
        client.connect(server.getsockname())
