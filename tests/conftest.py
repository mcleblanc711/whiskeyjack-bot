"""Session-wide DNS guard supplementing pytest-socket's host allowlist, the wall-clock
deadline the special-file tests need, and the tmpfs temp root the suite runs on."""

from __future__ import annotations

import os
import shutil
import signal
import socket
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Final

import pytest
from pytest_socket import SocketBlockedError

# Where to put pytest's temp root when the platform offers a memory-backed filesystem.
TMPFS_ROOT: Final = Path("/dev/shm")

# Below this much free space, leave the temp root alone. A run retains three numbered
# directories and measured 1.8 MB, so the floor is not a real bound -- it is there so a
# tmpfs already nearly full is never made the target.
MIN_FREE_BYTES: Final = 512 * 1024 * 1024


def _tmpfs_temproot() -> Path | None:
    """Return a memory-backed temp root, or ``None`` to leave pytest's default alone.

    Kept separate from the hook so the fallbacks can be table-tested; every one of them
    returns ``None``, so the failure mode of this whole mechanism is "slow", never "wrong".
    """
    try:
        if not TMPFS_ROOT.is_dir():
            return None
        usage = shutil.disk_usage(TMPFS_ROOT)
    except OSError:
        # A tmpfs that cannot be stat'd is not one to write several hundred databases to.
        return None
    if usage.free < MIN_FREE_BYTES:
        return None
    return TMPFS_ROOT


def pytest_configure(config: pytest.Config) -> None:
    """Put pytest's temp root on tmpfs when the platform has one.

    This is not an optimization of the tests; it is an optimization of the filesystem
    *under* them. ``ledger.connect()`` opens WAL with ``synchronous = NORMAL`` -- real
    fsyncs -- and the suite builds several hundred ledgers per run, so the suite was
    paying disk-durability costs for databases that are discarded seconds later.
    Measured 2026-08-17 on this machine:

        tests/unit/test_lifecycle.py   96.5s -> 3.2s
        tests/unit                    374.3s -> 28.2s   (fixture setup 202.3s -> 5.3s)
        full suite                    497.7s -> 81.3s

    Nothing about what is asserted changes; only where the bytes land. This supersedes
    the session-scoped migrated-template fixture parked in docs/LESSONS.md: that returned
    ~30s for ~45 call-site conversions across four files, because the fixture was never
    the only path to a database. The fsync cost sat *below* every path, including the ones
    a cached template could not reach.

    ``PYTEST_DEBUG_TEMPROOT`` rather than ``--basetemp`` deliberately. pytest reads it in
    ``TempPathFactory.getbasetemp()`` and still creates its own per-run numbered directory
    underneath (``/dev/shm/pytest-of-<user>/pytest-<N>``), so the parallel worktrees keep
    separate roots and the three-run retention still applies. ``--basetemp`` names one
    shared path and wipes it on entry, which two concurrent worktrees would do to each
    other mid-run.

    An explicit ``--basetemp``, an already-set ``PYTEST_DEBUG_TEMPROOT``, a platform with
    no ``/dev/shm`` (macOS, Windows) and a tmpfs below the free-space floor all keep
    pytest's own default.

    Which of those happened is recorded on the config as
    ``whiskeyjack_temproot_source``. That exists for one test: without it, a shell that
    already exports ``PYTEST_DEBUG_TEMPROOT=/dev/shm`` makes the end-to-end check pass
    whether this hook runs or not -- the temp root is on tmpfs either way. A test that
    cannot tell the two apart is testing nothing (docs/LESSONS.md, lesson 5), and the
    project's own .claude/settings.local.json shim is exactly the shell that triggers it.
    """
    if config.option.basetemp is not None:
        config.whiskeyjack_temproot_source = "basetemp"  # type: ignore[attr-defined]
        return
    if os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        config.whiskeyjack_temproot_source = "preset"  # type: ignore[attr-defined]
        return
    root = _tmpfs_temproot()
    if root is None:
        config.whiskeyjack_temproot_source = "unavailable"  # type: ignore[attr-defined]
        return
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(root)
    config.whiskeyjack_temproot_source = "hook"  # type: ignore[attr-defined]


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
