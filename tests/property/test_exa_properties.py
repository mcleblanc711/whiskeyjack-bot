"""Invariants of the Exa fallback policy and result mapper (M1-303).

The four properties CLAUDE.md requires of any hash, tiebreak, canonicalizer or
validator before its first review, stated over the two pure surfaces this item
adds: the fallback decision and the provider-result mapper.

- nothing raises outside the module's own error types;
- the reason vocabulary has one canonical order, so the persisted list is a
  function of the triggers and not of the caller's bookkeeping;
- a mapped document is replay-stable across the persisted JSON form;
- no provider value leaks into any message.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import given, strategies as st
from strategies import HOSTILE_TEXT, URL_CANDIDATES, persisted, round_trip

from whiskeyjack_bot.research.canonical import CanonicalizationError
from whiskeyjack_bot.research.exa import (
    ExaFallbackError,
    FallbackReason,
    _call_cost_usd,
    _canonical_reasons,
    _hash_source,
    _published_at_utc,
    _to_document,
    decide_fallback,
)
from whiskeyjack_bot.research.model import ResearchSchemaError, validate_document, validate_run

# The planted value every "no leak" assertion looks for. Low entropy on purpose:
# CI runs gitleaks over every branch (see the M1-301 notes).
SECRET = "privateFAKE123456"

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

REASONS: tuple[FallbackReason, ...] = (
    "primary_provider_failed",
    "primary_returned_no_documents",
    "official_source_required",
)

# Date-shaped inputs, valid and not: the API documents YYYY-MM-DD, live responses
# also carry full ISO timestamps, and a provider is free to send neither.
PUBLISHED_DATES = st.one_of(
    st.sampled_from(
        [
            "2026-07-20",
            "2026-07-20T09:30:00Z",
            "2026-07-20T09:30:00z",
            "2026-07-20T09:30:00.000+05:30",
            "2026-07-20T09:30:00",
            "  2026-07-20  ",
            "0001-01-01",
            "9999-12-31T23:59:59+14:00",
            # Boundary timestamps whose UTC conversion overflows datetime's range
            # (PR #16 round-1 finding): must degrade to None, never raise.
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59-14:00",
            "2026-13-45",
            "not a date",
            "",
        ]
    ),
    HOSTILE_TEXT,
    st.none(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.dictionaries(st.text(max_size=3), st.text(max_size=3), max_size=2),
)

COST_VALUES = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-10, max_value=10),
    # JSON integers too large for a float (PR #16 round-1 finding): float(total)
    # must degrade to None, never raise OverflowError.
    st.sampled_from([10**400, -(10**400)]),
    st.booleans(),
    st.none(),
    HOSTILE_TEXT,
    st.lists(st.floats(allow_nan=False, allow_infinity=False), max_size=2),
)


@st.composite
def exa_results(draw: st.DrawFn) -> Any:
    """A provider result, in and out of contract."""
    if draw(st.booleans()):
        # Not a mapping at all: a provider can return anything inside `results`.
        return draw(st.one_of(st.none(), HOSTILE_TEXT, st.integers(), st.lists(st.integers())))
    result: dict[str, Any] = {
        "url": draw(URL_CANDIDATES),
        "title": draw(st.none() | HOSTILE_TEXT),
        "author": draw(st.none() | HOSTILE_TEXT),
        "text": draw(st.none() | HOSTILE_TEXT),
        "publishedDate": draw(PUBLISHED_DATES),
    }
    if draw(st.booleans()):
        del result[draw(st.sampled_from(sorted(result)))]
    return result


# Errors :func:`_to_document` is allowed to raise. ValueError covers the known
# `content_sha256` lone-surrogate defect (UnicodeEncodeError is a ValueError);
# `retrieve_web` catches exactly this set and counts a drop, so a hostile result
# costs one citation rather than the run.
ALLOWED = (
    ExaFallbackError,
    ResearchSchemaError,
    CanonicalizationError,
    AttributeError,
    TypeError,
    ValueError,
    KeyError,
)


def _map(result: Any) -> dict[str, Any]:
    return _to_document(result, retrieval_run_id="run-1", retrieved_at=NOW, source_type="web")


# --- the fallback policy ----------------------------------------------------


@given(st.booleans(), st.integers(min_value=0, max_value=50), st.booleans())
def test_decision_is_total_and_canonical(failed: bool, documents: int, official: bool) -> None:
    decision = decide_fallback(
        primary_failed=failed,
        primary_documents=documents,
        official_source_required=official,
    )
    assert decision.should_run is bool(decision.reasons)
    assert set(decision.reasons) <= set(REASONS)
    # One canonical order, and no repeats: the tuple is persisted.
    assert list(decision.reasons) == [r for r in REASONS if r in set(decision.reasons)]
    assert len(set(decision.reasons)) == len(decision.reasons)


@given(st.booleans(), st.integers(min_value=0, max_value=50), st.booleans())
def test_decision_is_deterministic(failed: bool, documents: int, official: bool) -> None:
    kwargs = {
        "primary_failed": failed,
        "primary_documents": documents,
        "official_source_required": official,
    }
    assert decide_fallback(**kwargs) == decide_fallback(**kwargs)  # type: ignore[arg-type]


@given(st.one_of(st.integers(max_value=-1), st.booleans(), st.floats(), st.text(max_size=4)))
def test_a_non_count_raises_only_the_module_error(documents: Any) -> None:
    with pytest.raises(ExaFallbackError):
        decide_fallback(
            primary_failed=False,
            primary_documents=documents,
            official_source_required=False,
        )


@given(st.lists(st.sampled_from(REASONS), max_size=8))
def test_reason_normalization_is_permutation_invariant(reasons: list[FallbackReason]) -> None:
    """Same triggers, same persisted list, whatever order they were assembled in."""
    forward = _canonical_reasons(reasons)
    assert _canonical_reasons(list(reversed(reasons))) == forward
    assert _canonical_reasons(reasons * 2) == forward
    # Idempotent, so a caller may safely pass a decision's reasons back in.
    assert _canonical_reasons(forward) == forward


@given(st.lists(st.text(max_size=8), max_size=4))
def test_unknown_reasons_raise_without_echoing_the_value(reasons: list[str]) -> None:
    if all(reason in REASONS for reason in reasons):
        return
    with pytest.raises(ExaFallbackError) as excinfo:
        _canonical_reasons(reasons)
    message = str(excinfo.value)
    # The message always names the module's own fixed vocabulary (deliberate --
    # "the vocabulary itself is ours to name"), so a caller-supplied fragment
    # that happens to be a substring of one of those constant words (e.g. "pro"
    # inside "primary_provider_failed") appears regardless of the input; that is
    # not an echo of caller data, so it is excluded from the leak check.
    vocabulary_text = ", ".join(REASONS)
    for reason in reasons:
        if reason not in REASONS and len(reason) > 2 and reason not in vocabulary_text:
            assert reason not in message


def test_a_planted_secret_in_a_reason_never_reaches_the_message() -> None:
    with pytest.raises(ExaFallbackError) as excinfo:
        _canonical_reasons([SECRET])
    assert SECRET not in str(excinfo.value)


# --- the result mapper ------------------------------------------------------


@given(exa_results())
def test_mapping_raises_only_what_the_caller_catches(result: Any) -> None:
    try:
        payload = _map(result)
    except ALLOWED:
        return
    # A payload that comes out must also be a valid document, or the adapter
    # would hand the ledger something the schema rejects later.
    validate_document(payload)


@given(exa_results())
def test_a_mapped_document_is_replay_stable(result: Any) -> None:
    """Store and reload the way replay does: the persisted form must not move."""
    try:
        document = validate_document(_map(result))
    except ALLOWED:
        return
    assert persisted(round_trip(document)) == persisted(document)


@given(exa_results())
def test_a_mapped_run_config_survives_the_persisted_form(result: Any) -> None:
    """provider_config is stored as TEXT; a value that cannot round-trip is a defect."""
    try:
        document = validate_document(_map(result))
    except ALLOWED:
        return
    run = validate_run(
        {
            "retrieval_run_id": document.retrieval_run_id,
            "question_id": 1,
            "provider": "exa",
            "provider_config": {"fallback_reasons": list(REASONS)},
            "queries": [],
            "started_at_utc": NOW,
        }
    )
    encoded = json.dumps(run.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
    assert json.loads(encoded)["provider_config"]["fallback_reasons"] == list(REASONS)


@given(st.sampled_from(["url", "title", "author", "text", "publishedDate"]))
def test_no_provider_field_leaks_a_planted_secret_through_any_message(field: str) -> None:
    """Whatever a hostile field does, it must not come back out in a message."""
    result: dict[str, Any] = {
        "url": "https://example.org/a",
        "title": "t",
        "author": "a",
        "text": "body",
        "publishedDate": "2026-07-20",
    }
    result[field] = SECRET
    try:
        _map(result)
    except Exception as exc:  # noqa: BLE001 - the assertion is about any raise
        assert SECRET not in str(exc)
        assert SECRET not in repr(exc)


@given(HOSTILE_TEXT, st.none() | HOSTILE_TEXT)
def test_hash_source_prefers_text_then_title(text: str, title: str | None) -> None:
    result = {"text": text, "title": title}
    source = _hash_source(result)
    if text:
        assert source == text
    elif title:
        assert source == title
    else:
        assert source == ""


# --- field parsers ----------------------------------------------------------


@given(PUBLISHED_DATES)
def test_published_dates_are_aware_utc_or_absent(value: Any) -> None:
    """Never raises, and never yields the naive datetime the schema would reject."""
    parsed = _published_at_utc(value)
    if parsed is None:
        return
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


@given(COST_VALUES)
def test_costs_are_finite_non_negative_or_absent(value: Any) -> None:
    """Anything this returns has to be storable in ResearchRun.cost_usd."""
    cost = _call_cost_usd({"costDollars": {"total": value}})
    if cost is None:
        return
    assert isinstance(cost, float)
    run = validate_run(
        {
            "retrieval_run_id": "run-1",
            "question_id": 1,
            "provider": "exa",
            "started_at_utc": NOW,
            "cost_usd": cost,
        }
    )
    assert run.cost_usd == cost


@given(st.one_of(st.none(), st.integers(), HOSTILE_TEXT, st.lists(st.integers(), max_size=2)))
def test_a_malformed_cost_block_is_never_a_raise(block: Any) -> None:
    assert _call_cost_usd({"costDollars": block}) is None
    assert _call_cost_usd({}) is None
