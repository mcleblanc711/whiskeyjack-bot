"""Properties of `show`'s canonical-history merge (M1-612).

The CLAUDE.md pre-review fuzz pass for a new canonicalizer/tiebreak: never raises outside
the module's own error type (trivially true here -- the merge takes already-validated
value objects and does no I/O), a total order wherever ordering is claimed, and
replay-stability (running the merge twice on the same input gives the same output, since
it is pure). What is fuzzed is :func:`whiskeyjack_bot.show._stable_chronological`, the
sort/tiebreak :func:`merge_canonical_history` delegates to -- synthetic, minimal
``HistoryEntry`` values are enough to prove it, since only ``kind`` and
``occurred_at_utc`` matter to the ordering it produces.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from whiskeyjack_bot.show import HistoryEntry, HistoryEntryKind, _stable_chronological

_KINDS: tuple[HistoryEntryKind, ...] = (
    "approval",
    "submission_attempt",
    "submission_verification",
    "lifecycle",
    "pre_forecast_failure",
    "resolution",
    "score",
)

# Every timestamp lifecycle.py orders by is rendered through `_utc_text`: a fixed-width
# `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`. This strategy produces exactly that shape, so the
# fuzz never drifts from what the merge actually compares.
_CANONICAL_TIMESTAMPS = st.builds(
    lambda base, microseconds: (
        (base + timedelta(microseconds=microseconds))
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
    ),
    st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 1, 1),
    ).map(lambda dt: dt.replace(tzinfo=timezone.utc)),
    st.integers(min_value=0, max_value=999_999),
)

_ENTRIES = st.builds(
    HistoryEntry,
    kind=st.sampled_from(_KINDS),
    occurred_at_utc=_CANONICAL_TIMESTAMPS,
)


@given(st.lists(_ENTRIES, max_size=50))
def test_the_merge_never_raises(entries: list[HistoryEntry]) -> None:
    _stable_chronological(entries)


@given(st.lists(_ENTRIES, max_size=50))
def test_the_merge_is_a_permutation_of_its_input(entries: list[HistoryEntry]) -> None:
    merged = _stable_chronological(entries)
    assert sorted(id(entry) for entry in merged) == sorted(id(entry) for entry in entries)
    assert len(merged) == len(entries)


@given(st.lists(_ENTRIES, max_size=50))
def test_the_merge_is_non_decreasing_by_timestamp(entries: list[HistoryEntry]) -> None:
    merged = _stable_chronological(entries)
    timestamps = [entry.occurred_at_utc for entry in merged]
    assert timestamps == sorted(timestamps)


@given(st.lists(_ENTRIES, max_size=50))
def test_the_merge_is_replay_stable(entries: list[HistoryEntry]) -> None:
    assert _stable_chronological(entries) == _stable_chronological(list(entries))


@given(
    kind=st.sampled_from(_KINDS),
    timestamp=_CANONICAL_TIMESTAMPS,
    count=st.integers(min_value=2, max_value=10),
)
def test_same_category_ties_keep_their_original_relative_order(
    kind: HistoryEntryKind, timestamp: str, count: int
) -> None:
    """The claim concatenation-then-sort rests on: within one category, a tie is broken
    by input order, not scrambled. `sorted`'s documented stability is what makes this
    hold -- this pins that guarantee to the type this module actually sorts."""
    entries = [HistoryEntry(kind, timestamp) for _ in range(count)]
    merged = _stable_chronological(entries)
    assert [id(entry) for entry in merged] == [id(entry) for entry in entries]
