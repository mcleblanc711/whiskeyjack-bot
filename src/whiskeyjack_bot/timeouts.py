"""Wall-clock phase limits for the single main-thread worker (LAUNCH)."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

from whiskeyjack_bot.tournament_state import TournamentError


@contextmanager
def phase_timeout(seconds: float) -> Iterator[None]:
    def expired(signum: int, frame: FrameType | None) -> None:
        raise TournamentError("tournament phase exceeded its wall-clock timeout")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
