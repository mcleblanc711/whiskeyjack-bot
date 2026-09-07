"""Wall-clock phase limits for the single main-thread worker (LAUNCH)."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

from whiskeyjack_bot.tournament_state import TournamentError


class _PhaseExpired(BaseException):
    """Escape provider exception handlers until the phase boundary."""


@contextmanager
def phase_timeout(seconds: float) -> Iterator[None]:
    def expired(signum: int, frame: FrameType | None) -> None:
        raise _PhaseExpired

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    except _PhaseExpired:
        raise TournamentError("tournament phase exceeded its wall-clock timeout") from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
