"""Property tests for the payload a stored forecast authorizes (M2-707).

The CLAUDE.md pre-review fuzz pass for a hash and a validator: never raises outside the
module's own error type, replay-stability across the persisted form, the identity claim the
module states, and no value leak in any message *or rendered traceback*. Four things here
are specific to this item.

**The digest is what an approval holds, so the properties are about the digest.** An
approval stores 64 characters and `submission_key_for_approved_record` compares them; every
claim this module makes -- that the payload is derived from the record, that it is the same
bytes the gateway hashes, that it survives the ledger round trip -- is only worth anything
if it holds for the value that actually travels.

**Injectivity is the property the binding rests on.** If two records that derive *different*
payloads could produce one digest, an approval of the first would authorize a post of the
second, which is D33 reopened under a new name. It is asserted in both directions: equal
payloads share a digest, different payloads do not.

**Refusals are closed by their message set, not by substring search.** A property that
checks "the input does not appear in the message" passes vacuously whenever the draw is a
single common character -- M1-607's lesson. This module's fixed messages are imported rather
than transcribed, and the two that carry a tail from a lower layer are matched by prefix and
then leak-checked, because those tails are `forecast/cdf.py`'s and `submission_live.py`'s
own value-free strings and each has its own property pass.

**Every outcome is tagged.** The three question types refuse for different reasons at
different depths, so a run whose draws all converted, or all failed, would be green without
exercising the branch each property is about. `event()` tags what happened and the
assertions split on it; `--hypothesis-show-statistics` is where the distribution is read.

The fixtures are `tests/unit/test_submission_payload.py`'s, imported rather than copied:
they are built from the committed `prompts/forecaster.md` and `config.example.yaml`, and a
second spelling of them here would be a second thing to keep in step with the prompt.

Every property here was re-run against a deliberately weakened module and confirmed to fail
first; three of M1-303's ten new properties passed against the pre-fix tree
(docs/LESSONS.md, lesson 5).
"""

from __future__ import annotations

import json
import traceback
import pytest
from hypothesis import HealthCheck, assume, event, given, settings
from hypothesis import strategies as st
from strategies import ENCODABLE_TEXT, SURROGATE_TEXT  # type: ignore[import-not-found]

from whiskeyjack_bot.forecast.numeric import DECLARED_PERCENTILE_LEVELS
from whiskeyjack_bot.forecast.record import ForecastRecord
from whiskeyjack_bot.forecast.schema import ForecastSchemaError
from whiskeyjack_bot.submission_gateway import canonical_payload_json, payload_sha256
from whiskeyjack_bot.submission_live import plan_from_payload
from whiskeyjack_bot.submission_payload import (
    PayloadBuildError,
    authorized_payload,
    build_submission_payload,
    payload_sha256_for_record,
)

from tests.unit.test_submission_payload import (  # noqa: F401 - fixtures reused deliberately
    CALIBRATION,
    _binary_question,
    _calibration,
    _multiple_choice_question,
    _numeric_question,
    _record,
    _response,
)

# The fixed refusals, imported so a reworded message is a red build here rather than a
# property that silently stops closing the set it claims to close.
from whiskeyjack_bot.submission_payload import (
    _MISMATCHED_QUESTION,
    _MISMATCHED_RESPONSE,
    _UNSUPPORTED_TYPE,
)

_FIXED_MESSAGES = frozenset({_MISMATCHED_QUESTION, _MISMATCHED_RESPONSE, _UNSUPPORTED_TYPE})

# The refusals that carry a tail from a layer below. Matched by prefix and then leak-checked:
# the tails are `forecast/cdf.py`'s sanitized `problems` and `submission_live`'s own
# messages, each closed by its own property pass, and restating them here would be a second
# copy of two vocabularies this module does not own.
_PREFIXES = (
    "calibration must be",
    "record must be",
    "this forecast names one multiple-choice option twice",
    "this forecast's percentiles do not convert into a submittable CDF",
    "the payload derived from this record is not one Metaculus would accept",
)

# Planted into a record's *content* so that a leak has something unmistakable to carry. The
# text goes into the question title and the option labels; the number goes into percentile
# values, which cannot hold a string.
PLANTED_TEXT = "privateFAKE123456"
PLANTED_NUMBER = 12345.6789


# ── strategies ───────────────────────────────────────────────────────────────
#
# Two families, because they cost different amounts. `cheap_records()` is binary and
# multiple-choice: pure arithmetic, so the high-example properties use it. `numeric_records()`
# runs the pinned SDK's CDF conversion per example, so the properties that need it carry
# their own smaller budget rather than slowing every property to its speed.


def _text() -> st.SearchStrategy[str]:
    """The text a canonical question can actually hold: non-blank and encodable.

    Narrowed from `HOSTILE_TEXT` deliberately, and the narrowing is asserted rather than
    assumed -- see `test_a_lone_surrogate_cannot_reach_this_module_at_all`. Pydantic's
    string type refuses a lone surrogate at the question boundary, so no `ForecastRecord`
    can carry one and a strategy that drew them would spend every example on a shape that
    cannot exist. Blank strings are refused by the same models.
    """
    return ENCODABLE_TEXT.filter(lambda text: bool(text.strip()))


@st.composite
def binary_records(draw: st.DrawFn) -> ForecastRecord:
    """A binary record over the whole probability interval the response schema admits.

    Drawn from `[0.0, 1.0]` rather than from Metaculus's `[0.001, 0.999]` deliberately: the
    two disagree, the record model is the looser one, and the gap is exactly where
    `_require_postable` fires. A strategy narrowed to what posts would never reach it.
    """
    probability = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
    return _record(
        _binary_question(title=draw(_text())),
        _response("Binary schema", final_prediction={"probability_yes": probability}),
    )


@st.composite
def multiple_choice_records(draw: st.DrawFn) -> ForecastRecord:
    """A multiple-choice record whose labels are hostile and whose weights may not sum to 1.

    The labels are the mapping keys, so they are where a lone surrogate or a zero-width
    space would break the canonical rendering; the free weights are what reaches the
    distribution refusal `plan_from_payload` makes.
    """
    labels = draw(st.lists(_text(), min_size=2, max_size=4, unique=True))
    probabilities = draw(
        st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
            min_size=len(labels),
            max_size=len(labels),
        )
    )
    if draw(st.booleans()):
        # Half the draws are a real distribution, so the accepted branch is reached at all.
        total = sum(probabilities)
        assume(total > 0)
        probabilities = [value / total for value in probabilities]
    forecast = _response(
        "Multiple-choice schema",
        final_prediction={
            "options": [
                {"option": label, "probability": probability}
                for label, probability in zip(labels, probabilities, strict=True)
            ]
        },
    )
    return _record(_multiple_choice_question(labels), forecast)


@st.composite
def numeric_records(draw: st.DrawFn) -> ForecastRecord:
    """A numeric record whose percentile values may or may not convert.

    Built as a spread of strictly increasing values and then optionally *broken*, rather
    than as free floats. Free floats were the first cut and they were the wrong shape: an
    unsorted set is refused by the conversion long before this module's own branches run, so
    almost every example spent itself on one refusal and the properties that need a
    converted payload were reached a few percent of the time. The mutation is drawn and
    tagged, so what each run actually exercised is in the statistics rather than assumed.
    """
    count = len(DECLARED_PERCENTILE_LEVELS)
    start = draw(st.floats(min_value=1.0, max_value=20.0, allow_nan=False))
    gaps = draw(
        st.lists(
            st.floats(min_value=1.0, max_value=7.0, allow_nan=False),
            min_size=count - 1,
            max_size=count - 1,
        )
    )
    values = [start]
    for gap in gaps:
        values.append(values[-1] + gap)
    mutation = draw(st.sampled_from(["none", "none", "reversed", "flat", "outside"]))
    event(f"numeric shape={mutation}")
    if mutation == "reversed":
        values.reverse()
    elif mutation == "flat":
        values = [values[0]] * count
    elif mutation == "outside":
        shift = draw(st.floats(min_value=100.0, max_value=10_000.0, allow_nan=False))
        values = [value + shift for value in values]
    return _record(
        _numeric_question(title=draw(_text())),
        _response(
            "Numeric schema",
            final_prediction={
                "percentiles": [
                    {"percentile": level, "value": value}
                    for level, value in zip(DECLARED_PERCENTILE_LEVELS, values, strict=True)
                ]
            },
        ),
    )


def cheap_records() -> st.SearchStrategy[ForecastRecord]:
    return st.one_of(binary_records(), multiple_choice_records())


def any_record() -> st.SearchStrategy[ForecastRecord]:
    return st.one_of(binary_records(), multiple_choice_records(), numeric_records())


# Anything at all in a parameter position, including the values that have broken this
# project's code before. Both parameters are annotated, and neither annotation is a check.
ANYTHING = st.one_of(
    ENCODABLE_TEXT,
    SURROGATE_TEXT,
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=8),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=4), st.integers(), max_size=3),
    st.just(object()),
)


def _build(record: object, calibration: object = CALIBRATION) -> dict[str, object] | None:
    """Build, tagging the outcome and refusing anything that is not this module's error.

    Returns the payload, or ``None`` when the module refused. The `except` clauses are the
    property: a `PayloadBuildError` is the contract, and every other exception propagates
    and fails the test as itself rather than being counted as a refusal.
    """
    try:
        payload = build_submission_payload(record, calibration=calibration)  # type: ignore[arg-type]
    except PayloadBuildError as error:
        event(f"refused: {str(error)[:40]}")
        _assert_message_is_this_modules_own(error)
        return None
    event("built")
    return payload


def _assert_message_is_this_modules_own(error: PayloadBuildError) -> None:
    message = str(error)
    assert message in _FIXED_MESSAGES or message.startswith(_PREFIXES), message
    _assert_nothing_chains_through(error)


def _assert_nothing_chains_through(error: PayloadBuildError) -> None:
    """No underlying exception is rendered with this one -- the `from None` half.

    Checked structurally rather than only by searching the rendered traceback for a planted
    value, and that distinction was measured: replacing every `from None` in the module with
    `from exc` leaves the search-based property green, because the two layers underneath --
    `forecast/cdf.py` and `submission_live.py` -- sanitize their own messages first. So a
    leak search here is really a test of *their* hygiene, and the rule this module is
    actually asked to keep is that nothing chains through it at all. That rule is one this
    project's own defect history says will matter the day a lower layer stops sanitizing.

    `__context__` is checked alongside `__cause__` because a bare `raise` inside an `except`
    block leaks through the implicit chain, which a rendered traceback prints just as
    happily as an explicit cause.
    """
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


# ── invariant 1: nothing escapes as anything but a PayloadBuildError ─────────


@given(record=any_record())
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_derivation_raises_only_a_payload_build_error(record: ForecastRecord) -> None:
    """Over records the record model accepts and the payload builder may not.

    Every one of these is a *validated* record: it is what a ledger row reconstructs into.
    So a raw `ValidationError`, `KeyError` or `AttributeError` escaping here is not a
    hypothetical -- it is what an operator would see from `wj approve` on a real forecast.
    """
    _build(record)


@given(value=ANYTHING, position=st.sampled_from(["record", "calibration"]))
@settings(max_examples=120, deadline=None)
def test_a_malformed_argument_arrives_as_this_modules_error(value: object, position: str) -> None:
    """The project rule, at the boundary a CLI command catches `SubmissionError` around."""
    event(f"position={position}")
    if position == "record":
        _build(value)
    else:
        _build(_record(_binary_question(), _response("Binary schema")), value)


@given(record=cheap_records())
@settings(max_examples=100, deadline=None)
def test_a_record_whose_type_disagrees_with_its_contents_is_refused_not_crashed(
    record: ForecastRecord,
) -> None:
    """A record read back out of the ledger is untrusted, so the mismatch is fuzzed.

    `question_type` is rewritten to each of the other two without touching the response, the
    shape the row's own validator forbids and a hand-edited ledger can still hold. Both
    branches must refuse, and refuse as this module's error.
    """
    for other in ("binary", "multiple_choice", "numeric"):
        if other == record.question_type:
            continue
        mismatched = record.model_copy(update={"question_type": other})
        with pytest.raises(PayloadBuildError) as excinfo:
            build_submission_payload(mismatched, calibration=CALIBRATION)
        _assert_message_is_this_modules_own(excinfo.value)


# ── invariant 2: the digest is the gateway's digest of the payload ───────────


@given(record=any_record())
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_three_fields_describe_one_payload(record: ForecastRecord) -> None:
    """What an operator is shown, what an approval binds to and what is posted agree.

    The identity claim `AuthorizedPayload` exists to make: `sha256` is over `canonical`,
    `canonical` is `canonical_payload_json(payload)` -- the same rendering `submission_key`
    derives its material from -- and `payload_sha256_for_record` is a delegate rather than a
    second derivation.
    """
    try:
        authorized = authorized_payload(record, calibration=CALIBRATION)
    except PayloadBuildError as error:
        _assert_message_is_this_modules_own(error)
        event("refused")
        return
    event("built")
    assert authorized.canonical == canonical_payload_json(authorized.payload)
    assert authorized.sha256 == payload_sha256(authorized.payload)
    assert json.loads(authorized.canonical) == authorized.payload
    assert payload_sha256_for_record(record, calibration=CALIBRATION) == authorized.sha256


@given(record=any_record())
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_every_built_payload_is_one_the_live_path_accepts(record: ForecastRecord) -> None:
    """The postability claim as a property rather than as three examples.

    An approval that bound to a payload the last gate would refuse is a refusal arriving
    after a human decision instead of before it. The hostile labels are the half that
    matters: a payload whose key does not survive the canonical rendering is refused by
    `plan_from_payload`, so if this module could return one, this is where it shows.
    """
    payload = _build(record)
    if payload is None:
        return
    assert plan_from_payload(payload, expected_cdf_points=CALIBRATION.expected_cdf_points)


# ── invariant 3: replay-stability across the persisted form ──────────────────


@given(record=any_record())
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_digest_survives_the_form_the_record_is_stored_in(record: ForecastRecord) -> None:
    """M1-305's rule, applied to the value an approval holds.

    The digest is taken at approve time against an object in memory and compared at submit
    time against a record rebuilt from `record_json`. If those two disagreed for any record
    -- a surrogate pair, a `-0.0`, a float that round-trips imprecisely -- an approval would
    stop binding to its own forecast and no operator could tell why.
    """
    persisted = json.dumps(record.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
    replayed = ForecastRecord.model_validate(json.loads(persisted))
    first = _build(record)
    second = _build(replayed)
    assert (first is None) == (second is None)
    if first is not None:
        assert first == second
        assert payload_sha256_for_record(
            record, calibration=CALIBRATION
        ) == payload_sha256_for_record(replayed, calibration=CALIBRATION)


# ── invariant 4: injectivity, in both directions ─────────────────────────────


@given(first=cheap_records(), second=cheap_records())
@settings(max_examples=150, deadline=None)
def test_two_records_share_a_digest_exactly_when_they_derive_one_payload(
    first: ForecastRecord, second: ForecastRecord
) -> None:
    """The property the whole binding rests on.

    If two records deriving *different* payloads could produce one digest, an approval of
    the first would authorize a post of the second -- D33 reopened under a new name. The
    converse matters too and is the direction a naive implementation breaks: two records
    that derive the same payload must share a digest, or an approval would stop binding to
    a forecast whose payload never changed.
    """
    left = _build(first)
    right = _build(second)
    assume(left is not None and right is not None)
    assert left is not None and right is not None
    left_digest = payload_sha256_for_record(first, calibration=CALIBRATION)
    right_digest = payload_sha256_for_record(second, calibration=CALIBRATION)
    event(f"same payload={left == right}")
    assert (left == right) == (left_digest == right_digest)


@given(record=numeric_records())
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_calibration_is_part_of_what_a_numeric_record_derives(record: ForecastRecord) -> None:
    """A numeric payload is a conversion of the stored percentiles, not a copy of them.

    So the digest is a function of `(record, calibration)`, and a changed calibration fails
    safe: the rebuilt payload hashes differently, the approval stops binding, and the
    operator approves again rather than posting an array nobody reviewed. Asserted as
    "either it refuses or the digest is stable for one calibration" plus the pairing below,
    rather than as "the two digests differ" -- for some percentile sets the SDK's
    standardization is the identity, and a property asserting a difference would be asserting
    something this module does not promise.
    """
    unstandardized = _calibration(use_forecasting_tools_standardization=False)
    try:
        under_default = payload_sha256_for_record(record, calibration=CALIBRATION)
    except PayloadBuildError as error:
        _assert_message_is_this_modules_own(error)
        event("refused under the committed calibration")
        return
    try:
        under_alternative = payload_sha256_for_record(record, calibration=unstandardized)
    except PayloadBuildError as error:
        _assert_message_is_this_modules_own(error)
        event("refused under the alternative calibration")
        return
    event(f"digest differs={under_default != under_alternative}")
    assert under_default == payload_sha256_for_record(record, calibration=CALIBRATION)
    assert under_alternative == payload_sha256_for_record(record, calibration=unstandardized)


# ── invariant 5: no value reaches a message or a rendered traceback ──────────


@given(
    text=st.sampled_from(
        [
            PLANTED_TEXT,
            PLANTED_TEXT * 200,
            f"  {PLANTED_TEXT}  ",
            f"{PLANTED_TEXT}\N{ZERO WIDTH SPACE}",
        ]
    ),
    weights=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False), min_size=2, max_size=2
    ),
)
@settings(max_examples=60, deadline=None)
def test_a_planted_label_never_reaches_a_message_or_a_traceback(
    text: str, weights: list[float]
) -> None:
    """Option labels are question content, and the duplicate refusal is about one of them.

    The traceback half is what `from None` exists for; a message-only assertion passes
    against code that re-raises with its cause chain intact, which is how a pydantic
    `ValidationError` would reprint the label through a rendered stack.
    """
    labels = [text, f"{text}-other"]
    record = _record(
        _multiple_choice_question(labels),
        _response(
            "Multiple-choice schema",
            final_prediction={
                "options": [
                    {"option": label, "probability": weight}
                    for label, weight in zip(labels, weights, strict=True)
                ]
            },
        ),
    )
    try:
        build_submission_payload(record, calibration=CALIBRATION)
    except PayloadBuildError as error:
        event("refused")
        _assert_message_is_this_modules_own(error)
        assert PLANTED_TEXT not in str(error)
        assert PLANTED_TEXT not in "".join(traceback.format_exception(error))
        _assert_nothing_chains_through(error)
        return
    event("built")


@given(offset=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False))
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_planted_percentile_never_reaches_a_message_or_a_traceback(offset: float) -> None:
    """The one that matters most: the SDK interpolates the percentiles it refused.

    Every failure inside `NumericDistribution` arrives as a pydantic `ValidationError`
    carrying the values, the bounds and the question's own numbers. `forecast/cdf.py` fences
    the call and emits value-free problem strings, and this module passes those through with
    `from None` -- so both layers have to hold for this to pass.
    """
    values = [PLANTED_NUMBER + offset + index for index in range(len(DECLARED_PERCENTILE_LEVELS))]
    record = _record(
        _numeric_question(),
        _response(
            "Numeric schema",
            final_prediction={
                "percentiles": [
                    {"percentile": level, "value": value}
                    for level, value in zip(DECLARED_PERCENTILE_LEVELS, values, strict=True)
                ]
            },
        ),
    )
    try:
        build_submission_payload(record, calibration=CALIBRATION)
    except PayloadBuildError as error:
        event("refused")
        rendered = "".join(traceback.format_exception(error))
        for value in values:
            assert str(value) not in str(error)
            assert str(value) not in rendered
        assert str(record.question.upper_bound) not in rendered
        _assert_nothing_chains_through(error)
        return
    event("built")


@given(surrogate=SURROGATE_TEXT)
@settings(max_examples=25, deadline=None)
def test_a_lone_surrogate_cannot_reach_this_module_at_all(surrogate: str) -> None:
    """Why the strategies above are narrowed, stated as a test instead of as a comment.

    `hashing.content_sha256` raises on a lone surrogate and that defect is open (CLAUDE.md),
    so "does a surrogate reach the payload builder" is a question worth an answer rather
    than an assumption. It does not: the canonical question model refuses one at the
    boundary, so no `ForecastRecord` can carry it in a title or an option label and every
    draw of one would be an example spent on an unreachable shape.

    If that ever changes -- a model that admits surrogates, a builder reached from somewhere
    that skips the model -- this test goes red and the narrowing above stops being honest,
    which is the point of writing it down here rather than in a comment.
    """
    with pytest.raises(Exception) as excinfo:
        _binary_question(title=surrogate)
    assert not isinstance(excinfo.value, PayloadBuildError)


def test_the_closed_message_set_is_closed_over_real_constants() -> None:
    """The refusals above are imported private names, so a rename is a red build here.

    A message set closed against constants that no longer exist would close nothing, and
    `_assert_message_is_this_modules_own` would keep passing while checking less. Asserted
    rather than left to the import: an import error is a collection error, and a collection
    error is easy to read as unrelated to the property it disabled.
    """
    assert len(_FIXED_MESSAGES) == 3
    assert all(isinstance(message, str) and message for message in _FIXED_MESSAGES)
    # A refusal from this module must never arrive as the type of the layer it wrapped --
    # that is what `PayloadBuildError` subclassing `SubmissionError` and nothing else buys.
    assert not issubclass(PayloadBuildError, ForecastSchemaError)
