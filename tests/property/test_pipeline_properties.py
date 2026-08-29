"""Invariants of the one pure function T-903 adds (`pipeline._as_of_from_request`).

The function recovers ``as_of_utc`` from a stored, already-rendered reasoning packet so the
rebuilt packet can be compared to it byte for byte. Two things make it worth fuzzing rather
than covering with examples:

* **Its input is untrusted.** A stored request is a value read back out of an artifact, which
  ``CLAUDE.md``'s threat boundary classes as untrusted alongside provider JSON. Every
  malformed shape must arrive as ``PipelineError`` -- a raw ``ValueError`` or ``KeyError``
  escaping a public boundary is a review finding in this project, and has been twice.
* **It must not leak.** The request it parses is the whole reasoning packet: the question
  text and every retrieved document body. A message quoting any of it would put document
  text into a log line.

The alternative design -- comparing the rebuilt request to the stored one with ``as_of_utc``
excluded -- is what makes this function exist, and is why the round trip below is the
load-bearing property: an exclusion list would grow by default with every new field on
``ForecastModelInput``, while recovering the one value that cannot be re-derived keeps the
comparison total.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from strategies import ENCODABLE_TEXT, SURROGATE_TEXT

from whiskeyjack_bot.pipeline import PipelineError, _as_of_from_request

# Aware UTC instants across the range the renderer will ever see, carrying microseconds and
# both values of ``fold``. Microseconds matter here in a way they do not elsewhere: the
# comparison this feeds is byte equality, so a recovered value that lost precision would
# rebuild a request that differs from the stored one by a few digits and refuse a run that
# should have succeeded.
AWARE_UTC = st.builds(
    lambda base, micros, fold: (
        datetime(2026, 1, 1, tzinfo=timezone.utc, fold=fold)
        + timedelta(seconds=base, microseconds=micros)
    ),
    st.integers(min_value=0, max_value=60 * 60 * 24 * 365 * 4),
    st.sampled_from([0, 1, 999999, 500000, 123456]),
    st.sampled_from([0, 1]),
)

# Whatever a stored request might turn out to be once it is no longer what it should be.
# `st.text()` alone would almost never produce a JSON object, so the branches past the
# `json.loads` are reached by construction rather than by luck -- the vacuity failure this
# project keeps paying for (docs/LESSONS.md, lesson 9).
NOT_A_TIMESTAMP = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    ENCODABLE_TEXT,
    st.lists(ENCODABLE_TEXT, max_size=2),
    st.dictionaries(ENCODABLE_TEXT, ENCODABLE_TEXT, max_size=2),
)

MALFORMED_REQUESTS = st.one_of(
    # Not JSON at all.
    ENCODABLE_TEXT,
    SURROGATE_TEXT,
    # JSON, but not an object.
    st.builds(json.dumps, st.one_of(st.integers(), st.booleans(), st.none())),
    st.builds(lambda items: json.dumps(items), st.lists(ENCODABLE_TEXT, max_size=3)),
    # An object with no `as_of_utc`, or one carrying something that is not a timestamp.
    st.builds(
        lambda payload: json.dumps(payload),
        st.dictionaries(ENCODABLE_TEXT.filter(lambda k: k != "as_of_utc"), st.integers()),
    ),
    st.builds(lambda value: json.dumps({"as_of_utc": value}), NOT_A_TIMESTAMP),
    # An object whose `as_of_utc` is a plausible-but-wrong timestamp: naive, or offset.
    st.builds(
        lambda value: json.dumps({"as_of_utc": value}),
        st.sampled_from(
            [
                "2026-08-20T09:00:00",
                "2026-08-20T09:00:00+01:00",
                "2026-08-20T09:00:00-05:00",
                "2026-08-20",
                "20260820T090000Z",
                "not-a-timestamp",
                "",
            ]
        ),
    ),
)


@given(MALFORMED_REQUESTS)
def test_only_the_modules_own_error_escapes(request: str) -> None:
    """The caller contract: `run_replay` handles `PipelineError` and nothing else.

    A `ValueError` from `datetime.fromisoformat`, a `TypeError` from indexing a list, a
    `UnicodeEncodeError` from a lone surrogate -- each would escape a public boundary as
    itself. That is the defect class M1-308 and M1-311 both closed.
    """
    try:
        recovered = _as_of_from_request(request)
    except PipelineError:
        return
    # If it did not raise, it must have returned a usable aware-UTC instant rather than
    # something the caller will trip over later.
    assert isinstance(recovered, datetime)
    assert recovered.tzinfo is not None
    assert recovered.utcoffset() == timedelta(0)


# A token no constant message could contain by coincidence, planted as a *value* wherever a
# request can carry one. Asserting on values rather than on arbitrary substrings is the
# distinction the rule actually draws: a message may name the field it was looking for --
# `as_of_utc` appears in "records no as_of_utc timestamp" and that is a field path, which
# `CLAUDE.md` permits and which a naive substring check would flag. What it may never do is
# echo what was stored there.
MARKER = "Qz7leakcanaryZq"

LEAKY_REQUESTS = st.one_of(
    # The marker as the offending `as_of_utc` value itself -- the most tempting thing to
    # quote, since it is the value the function rejected.
    st.just(json.dumps({"as_of_utc": MARKER})),
    st.just(json.dumps({"as_of_utc": [MARKER]})),
    st.just(json.dumps({"as_of_utc": {"nested": MARKER}})),
    # The marker as a sibling field's value: the question text and document bodies live
    # here in a real request.
    st.just(json.dumps({"question_text": MARKER})),
    st.just(json.dumps({"research_documents": [{"body": MARKER}], "as_of_utc": MARKER})),
    # And in requests that fail earlier, before any field is read.
    st.just(MARKER),
    st.just(f'["{MARKER}"]'),
    st.just(f'{{"as_of_utc": "{MARKER}"'),  # truncated: not valid JSON
    st.builds(lambda pad: json.dumps({"as_of_utc": MARKER, "pad": pad}), ENCODABLE_TEXT),
)


@given(LEAKY_REQUESTS)
def test_no_message_echoes_a_stored_value(request: str) -> None:
    """The request carries the question text and every document body, so no value in it may
    reach a message -- not even the offending fragment, which is the tempting version."""
    with pytest.raises(PipelineError) as caught:
        _as_of_from_request(request)
    assert MARKER not in str(caught.value)
    assert MARKER not in repr(caught.value)


def test_the_leak_canary_can_actually_fail() -> None:
    """Anti-vacuity: the property above passes trivially if `MARKER` never reaches the
    function, or if the requests do not reach the raising branches at all.

    So: every generated request really does raise, and a message that *did* echo the value
    would be caught. The second half is checked against a deliberately leaky message rather
    than against the product, because the product is correct and cannot demonstrate it.
    """
    reached = 0
    for request in (
        json.dumps({"as_of_utc": MARKER}),
        json.dumps({"question_text": MARKER}),
        MARKER,
        f'{{"as_of_utc": "{MARKER}"',
    ):
        with pytest.raises(PipelineError):
            _as_of_from_request(request)
        reached += 1
    assert reached == 4
    assert MARKER in str(PipelineError(f"leaked: {MARKER}"))


@given(AWARE_UTC)
def test_a_rendered_timestamp_round_trips_exactly(as_of: datetime) -> None:
    """The property the byte-for-byte comparison rests on.

    ``isoformat`` is what the renderer writes, so recovering it must return the same instant
    to the microsecond. A recovered value that differed anywhere would rebuild a request that
    is not the stored one, and the run would be refused for a difference the pipeline itself
    introduced.
    """
    request = json.dumps({"as_of_utc": as_of.isoformat(), "question_id": 1})
    recovered = _as_of_from_request(request)
    assert recovered == as_of
    assert recovered.isoformat() == as_of.isoformat()
    # And re-rendering it is stable, which is the form the comparison actually takes.
    assert json.dumps({"as_of_utc": recovered.isoformat(), "question_id": 1}) == request


@given(AWARE_UTC, st.dictionaries(ENCODABLE_TEXT, st.integers(), max_size=4))
def test_other_fields_do_not_affect_recovery(as_of: datetime, extra: dict[str, int]) -> None:
    """Recovery reads one field and ignores the rest, which is what lets the packet grow.

    The point of recovering ``as_of_utc`` rather than excluding it from the comparison is
    that a new field on ``ForecastModelInput`` joins the comparison automatically. That only
    holds if new fields cannot disturb this function.
    """
    extra.pop("as_of_utc", None)
    request = json.dumps({**extra, "as_of_utc": as_of.isoformat()})
    assert _as_of_from_request(request) == as_of


@pytest.mark.parametrize("value", [b"{}", 42, None, ["{}"], {"as_of_utc": "x"}])
def test_a_request_that_is_not_text_is_refused(value: object) -> None:
    """Typed `str`, checked at runtime. A `bytes` request would sail through `json.loads`
    and produce a recovered timestamp from a value the renderer never wrote."""
    with pytest.raises(PipelineError, match="not text"):
        _as_of_from_request(value)  # type: ignore[arg-type]
