"""M1-355: the series row's AskNews sub-cap is read back out of the ledger, so it is untrusted.

The reader must accept exactly an exact ``int`` in ``1..budget`` (or an absent key, for a
series enabled before M1-355), raise only :class:`StorageFailure` for anything else, and never
echo the stored value. Each property has a reachability ``find`` for its accept branch: the
vacuity this project keeps finding is a strategy that never reaches what the assertion is about.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import find, given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.tournament_state import StorageFailure, _series_from

BUDGET = 80_000_000
BASE: dict[str, Any] = {
    "series_id": "a" * 32,
    "account_id": 42,
    "follow": "minibench",
    "budget_microusd": BUDGET,
    "project_budget_microusd": 40_000_000,
    "ends": "2027-01-01T00:00:00+00:00",
    "config_sha256": "c" * 64,
    "prompt_sha256": "p" * 64,
}

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**12), max_value=10**12)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=12),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=4), children, max_size=3)
    ),
    max_leaves=4,
)
# Weighted so the accept range is hit as often as the refusals around it.
stored = st.one_of(
    st.integers(min_value=1, max_value=BUDGET),
    st.integers(min_value=BUDGET + 1, max_value=BUDGET + 5),
    st.integers(min_value=-5, max_value=0),
    json_values,
)


def _read(value: object) -> int | None | str:
    row = dict(BASE, asknews_budget_microusd=value)
    try:
        return _series_from(row).asknews_budget_microusd
    except StorageFailure as exc:
        return str(exc)


def _accepted(value: object) -> bool:
    return type(value) is int and 1 <= value <= BUDGET


@settings(max_examples=300, deadline=None)
@given(stored)
def test_only_an_exact_int_within_the_ceiling_is_read_back(value: object) -> None:
    result = _read(value)
    if _accepted(value):
        assert result == value and type(result) is int
    else:
        assert result == "cannot read series authorization"


@settings(max_examples=200, deadline=None)
@given(stored)
def test_a_refusal_never_echoes_the_stored_value(value: object) -> None:
    result = _read(value)
    if isinstance(result, str):
        rendered = json.dumps(value, default=str)
        assert result == "cannot read series authorization"
        assert rendered not in result or rendered in {"0", "null", "true"}


def test_the_accept_and_refuse_branches_are_both_reachable() -> None:
    assert _accepted(find(stored, _accepted))
    assert not _accepted(find(stored, lambda v: not _accepted(v)))
    assert not _accepted(find(stored, lambda v: type(v) is bool))
    assert not _accepted(find(stored, lambda v: type(v) is float))


@settings(max_examples=50, deadline=None)
@given(st.sampled_from([None]))
def test_an_absent_key_reads_back_as_none(_: None) -> None:
    assert _series_from(dict(BASE)).asknews_budget_microusd is None
