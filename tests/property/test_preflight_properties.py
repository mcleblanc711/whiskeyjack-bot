"""Invariants of the shared caller-preflight guards (M1-303, M1-309).

Both :func:`string_list` and :func:`require_run_metadata` are called from two adapters,
each binding its own module error via ``error=``. The properties this module owns:

- totality: any input raises only the bound ``error`` class, never a bare
  ``TypeError``/``AttributeError``/``OverflowError`` -- including when the offending
  value runs arbitrary code (a broken ``__iter__``, a broken ``tzinfo``);
- correctness on the valid domain: a well-formed sequence of non-blank strings
  round-trips unchanged, and a well-formed tz-aware ``now`` always comes back UTC-aware
  and denoting the same instant.

Two distinct dummy error classes are exercised throughout, to prove the ``error=``
parameter actually governs what is raised rather than one type being hardcoded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

import pytest
from hypothesis import given, strategies as st

from whiskeyjack_bot.research.preflight import require_run_metadata, string_list


class _FirstError(Exception):
    pass


class _SecondError(Exception):
    pass


ERROR_CLASSES = st.sampled_from([_FirstError, _SecondError])


class _BrokenIter:
    """Iterates by raising -- the caller-mistake shape ``list(values)`` must survive."""

    def __iter__(self) -> Any:
        raise RuntimeError("broken iterator")


class _BrokenTzInfo(tzinfo):
    """A tzinfo whose utcoffset() raises -- a caller-supplied object running code."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise RuntimeError("broken tzinfo")

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


NON_BLANK_STRINGS = st.text(min_size=1).filter(lambda s: s.strip() != "" and "\x00" not in s)

MALFORMED_SEQUENCE_VALUES = st.one_of(
    st.text(),
    st.binary(),
    st.none(),
    st.integers(),
    st.booleans(),
    st.lists(st.one_of(st.text(), st.integers(), st.none()), max_size=4),
    st.just(_BrokenIter()),
)


@given(value=MALFORMED_SEQUENCE_VALUES, error=ERROR_CLASSES)
def test_string_list_raises_only_the_bound_error(value: Any, error: type[Exception]) -> None:
    try:
        string_list(value, "message", error=error)
    except error:
        return
    except Exception as exc:  # pragma: no cover - only reached on a real defect
        pytest.fail(f"string_list raised {type(exc).__name__}, not {error.__name__}")


@given(items=st.lists(NON_BLANK_STRINGS, max_size=6), error=ERROR_CLASSES)
def test_string_list_accepts_every_non_blank_string_list(
    items: list[str], error: type[Exception]
) -> None:
    assert string_list(items, "message", error=error) == items


def test_string_list_refuses_a_bare_string_for_both_bound_errors() -> None:
    with pytest.raises(_FirstError):
        string_list("inflation", "message", error=_FirstError)
    with pytest.raises(_SecondError):
        string_list("inflation", "message", error=_SecondError)


MALFORMED_QUESTION_IDS = st.one_of(st.text(), st.none(), st.booleans(), st.floats())
MALFORMED_RUN_IDS = st.one_of(
    st.just(""), st.just("   \n\t"), st.just("bad\x00id"), st.none(), st.integers()
)
MALFORMED_NOW_VALUES = st.one_of(
    st.none(),
    st.text(),
    st.just(datetime(2026, 1, 1)),  # naive
)


@given(question_id=MALFORMED_QUESTION_IDS, error=ERROR_CLASSES)
def test_require_run_metadata_raises_only_the_bound_error_for_question_id(
    question_id: Any, error: type[Exception]
) -> None:
    try:
        require_run_metadata(
            question_id=question_id,
            retrieval_run_id="run-1",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            error=error,
        )
    except error:
        return
    except Exception as exc:  # pragma: no cover - only reached on a real defect
        pytest.fail(f"require_run_metadata raised {type(exc).__name__}, not {error.__name__}")


@given(retrieval_run_id=MALFORMED_RUN_IDS, error=ERROR_CLASSES)
def test_require_run_metadata_raises_only_the_bound_error_for_run_id(
    retrieval_run_id: Any, error: type[Exception]
) -> None:
    try:
        require_run_metadata(
            question_id=1,
            retrieval_run_id=retrieval_run_id,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            error=error,
        )
    except error:
        return
    except Exception as exc:  # pragma: no cover - only reached on a real defect
        pytest.fail(f"require_run_metadata raised {type(exc).__name__}, not {error.__name__}")


@given(now=MALFORMED_NOW_VALUES, error=ERROR_CLASSES)
def test_require_run_metadata_raises_only_the_bound_error_for_now(
    now: Any, error: type[Exception]
) -> None:
    try:
        require_run_metadata(question_id=1, retrieval_run_id="run-1", now=now, error=error)
    except error:
        return
    except Exception as exc:  # pragma: no cover - only reached on a real defect
        pytest.fail(f"require_run_metadata raised {type(exc).__name__}, not {error.__name__}")


@given(
    question_id=st.integers(),
    retrieval_run_id=NON_BLANK_STRINGS,
    offset_hours=st.integers(min_value=-14, max_value=14),
    error=ERROR_CLASSES,
)
def test_require_run_metadata_returns_the_same_instant_in_utc(
    question_id: int, retrieval_run_id: str, offset_hours: int, error: type[Exception]
) -> None:
    now = datetime(2026, 6, 1, 12, tzinfo=timezone(timedelta(hours=offset_hours)))
    result = require_run_metadata(
        question_id=question_id, retrieval_run_id=retrieval_run_id, now=now, error=error
    )
    assert result.tzinfo is timezone.utc
    assert result == now


def test_require_run_metadata_wraps_a_broken_tzinfo() -> None:
    now = datetime(2026, 1, 1, tzinfo=_BrokenTzInfo())
    with pytest.raises(_FirstError):
        require_run_metadata(question_id=1, retrieval_run_id="run-1", now=now, error=_FirstError)


def test_require_run_metadata_wraps_a_utc_conversion_overflow() -> None:
    # A syntactically valid boundary datetime whose UTC conversion falls outside
    # datetime's representable range -- the shape cross-model review round 5,
    # finding 3 found billing a call before this raised.
    now = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(-timedelta(hours=14)))
    with pytest.raises(_FirstError):
        require_run_metadata(question_id=1, retrieval_run_id="run-1", now=now, error=_FirstError)
