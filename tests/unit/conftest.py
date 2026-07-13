"""Unit-test session guards.

Every unit test runs with socket connections blocked: any code path that
tries to reach the network fails the test instead of silently spending money
or leaking data. (The CI-level enforcement of this property is M0-003,
Codex-owned; this fixture is the implementer's own honesty check.)
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_connect(self: socket.socket, address: object) -> None:
        raise RuntimeError(f"unit test attempted a network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
