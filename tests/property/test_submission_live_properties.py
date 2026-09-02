"""Property tests for the package-backed gateway's pure pieces (M2-704).

The CLAUDE.md pre-review fuzz pass applied to what this item added that is a validator, a
canonicalizer or a comparison rule: :func:`plan_from_payload`, :func:`read_my_forecasts`,
:func:`classify_refetch`, :func:`build_verification_snapshot` / :func:`read_verification_
snapshot`, :func:`storable_text`, :func:`classify_error` and :func:`live_attempt_id`.

Four claims, and each one is here because a unit test cannot make it:

**Nothing escapes as a foreign error type.** Every one of these is reached with untrusted
input -- a payload the operator wrote, provider JSON, an exception from a transport this
package does not import -- and three of them are reached *after* a live post, where any
raise at all costs the ledger row.

**No-leak is asserted by closing the message set.** A property that searches a message for
its input passes vacuously whenever the draw is one common character, which is most draws
(M1-607). The messages this module can produce are finite and written out below; every
refusal must match one.

**The snapshot is replay-stable and always storable.** It is what
:func:`verify_uncertain_attempt` re-judges an attempt from, so a rendering that does not
survive its own round trip would make a later verification judge by different numbers than
the attempt did.

**Confirmation is never reachable without both of its halves.** `classify_refetch` returns
``confirmed`` only for a strictly newer entry whose values match, and the property asserts
the implication in the direction that matters: if it says confirmed, both held.

Every property here was re-run against a deliberately weakened module and confirmed to
fail first; three of M1-303's ten new properties passed against the pre-fix tree
(docs/LESSONS.md, lesson 5).
"""

from __future__ import annotations

import json
import math
import re
import traceback
from typing import Any

from hypothesis import assume, given
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.bounds import MAX_BODY_LENGTH, MAX_IDENTIFIER_LENGTH
from whiskeyjack_bot.submission_gateway import GatewayError
from whiskeyjack_bot.submission_live import (
    _CATEGORY_SUM_TOLERANCE,
    _LIVE_ERROR_TYPES,
    _MAX_CATEGORIES,
    _MAX_OPTION_LABEL,
    _MAX_SNAPSHOT_VALUES,
    BinaryPost,
    ForecastEntry,
    ForecastHistory,
    LiveSubmissionError,
    MultipleChoicePost,
    NumericPost,
    RefetchResult,
    build_verification_snapshot,
    classify_error,
    classify_refetch,
    expected_option_labels,
    expected_values,
    live_attempt_id,
    observed_values,
    plan_from_payload,
    read_my_forecasts,
    read_verification_snapshot,
    storable_text,
    values_match,
)

CDF_POINTS = 201

# Anything at all, including everything the accepted domain excludes. The point is that
# none of it may escape as anything but a LiveSubmissionError.
ANY_VALUE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | HOSTILE_TEXT | st.binary(max_size=8),
    lambda children: (
        st.lists(children, max_size=4)
        | st.tuples(children)
        | st.dictionaries(HOSTILE_TEXT | st.integers() | st.none(), children, max_size=4)
    ),
    max_leaves=10,
)
ANY_PAYLOAD = st.dictionaries(HOSTILE_TEXT | st.integers() | st.none(), ANY_VALUE, max_size=4)

# Payloads shaped like the real thing, so the *accepting* branches are exercised too. A
# strategy that only ever produces refusals proves the error type and nothing else.
PROBABILITIES = st.floats(min_value=0.001, max_value=0.999, allow_nan=False, allow_infinity=False)


@st.composite
def plausible_payloads(draw: st.DrawFn) -> dict[str, Any]:
    kind = draw(st.sampled_from(["binary", "numeric", "multiple_choice"]))
    if kind == "binary":
        return {"question_type": "binary", "probability_yes": draw(PROBABILITIES)}
    if kind == "numeric":
        raw = sorted(draw(st.lists(st.floats(0.0, 1.0), min_size=CDF_POINTS, max_size=CDF_POINTS)))
        return {"question_type": "numeric", "continuous_cdf": raw}
    labels = draw(
        st.lists(HOSTILE_TEXT.filter(lambda s: s.strip()), min_size=1, max_size=4, unique=True)
    )
    share = 1.0 / len(labels)
    return {
        "question_type": "multiple_choice",
        "probability_yes_per_category": {label: share for label in labels},
    }


PAYLOADS = ANY_PAYLOAD | plausible_payloads()

# Everything `read_my_forecasts` might be handed back by a platform that changed shape.
API_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | HOSTILE_TEXT,
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(
            st.sampled_from(
                ["question", "my_forecasts", "history", "start_time", "forecast_values", "other"]
            )
            | HOSTILE_TEXT,
            children,
            max_size=4,
        )
    ),
    max_leaves=14,
)


class _Question:
    def __init__(self, api_json: Any) -> None:
        self.api_json = api_json


# Every message `plan_from_payload` can produce, as patterns that capture nothing from the
# input. The only interpolations are this module's own literals: a question type from the
# closed `SupportedQuestionType` vocabulary, a wire key from `_WIRE_KEY_FOR_TYPE`, and
# numeric bounds written in the module.
_PLAN_MESSAGE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^payload must be a JSON object$",
        r"^payload\.question_type must be one of [a-z_, ]+ "
        r"\(offending value withheld: it is payload content\)$",
        r"^a [a-z_]+ payload carries exactly question_type and [a-z_]+; this one carries "
        r"more, and a key this module would drop is a forecast nobody reviewed "
        r"\(offending keys withheld\)$",
        r"^a [a-z_]+ payload must carry [a-z_]+$",
        r"^payload\.[a-z_]+ must be a number \(offending value withheld\)$",
        r"^payload\.[a-z_]+ must be a finite number$",
        r"^payload\.[a-z_]+ must be between [\d.]+ and [\d.]+, which is what Metaculus "
        r"accepts \(offending value withheld\)$",
        r"^payload\.[a-z_]+ must be a JSON array$",
        r"^payload\.[a-z_]+ must hold exactly \d+ points; this one holds \d+$",
        r"^payload\.[a-z_]+ must hold only numbers \(offending value withheld\)$",
        r"^payload\.[a-z_]+ must hold only finite numbers$",
        r"^payload\.[a-z_]+ must hold only values between 0 and 1 "
        r"\(offending value withheld\)$",
        r"^payload\.[a-z_]+ must be monotonically non-decreasing; a CDF that decreases "
        r"describes a negative probability$",
        r"^payload\.[a-z_]+ must be a JSON object$",
        r"^payload\.[a-z_]+ must name at least one option$",
        r"^payload\.[a-z_]+ names more than \d+ options, which is more than any Metaculus "
        r"multiple-choice question carries$",
        r"^payload\.[a-z_]+ keys must be non-blank option labels \(offending key withheld\)$",
        r"^payload\.[a-z_]+ must be a distribution: its probabilities must sum to 1 within "
        r"[\d.e-]+ \(observed sum withheld\)$",
        # Inherited from `canonical_payload_json`, which owns the accepted JSON domain.
        r"^payload must be a JSON object \(a mapping\)$",
        r"^payload could not be read as a mapping \(detail withheld: it can echo the payload\)$",
        r"^payload contains an object key that is not a string; JSON silently coerces such "
        r"keys and can collapse two entries into one \(offending key withheld\)$",
        r"^payload nests deeper than the \d+-level limit "
        r"\(a self-referential payload reaches this first\)$",
        r"^payload contains a non-finite number, which JSON cannot represent "
        r"\(offending value withheld\)$",
        r"^payload contains a [A-Za-z_][A-Za-z0-9_]*, which is not a JSON value; objects "
        r"must be mappings and arrays must be lists$",
        r"^the submission payload could not be rendered as canonical JSON "
        r"\(detail withheld: it can echo the payload\)$",
        r"^the submission payload does not survive its own canonical rendering, so a replay "
        r"could not reproduce it; two object keys most likely differ only in how they spell "
        r"one character \(detail withheld: it can echo the payload\)$",
    )
)


# ── nothing escapes, and nothing leaks ───────────────────────────────────────


@given(payload=PAYLOADS)
def test_a_payload_is_accepted_or_refused_as_this_modules_error(payload: dict[str, Any]) -> None:
    try:
        plan = plan_from_payload(payload, expected_cdf_points=CDF_POINTS)
    except LiveSubmissionError:
        return
    assert plan.question_type == payload["question_type"]


@given(payload=PAYLOADS)
def test_every_payload_refusal_is_a_member_of_the_closed_message_set(
    payload: dict[str, Any],
) -> None:
    """Closing the set, not searching for the input.

    A refusal whose message is not one of these is a message nobody has read for leaks --
    which is the state a substring assertion cannot detect.
    """
    try:
        plan_from_payload(payload, expected_cdf_points=CDF_POINTS)
    except LiveSubmissionError as exc:
        message = str(exc)
        assert any(pattern.match(message) for pattern in _PLAN_MESSAGE_PATTERNS), message
        # `from None` everywhere: no cause chain can reprint a value the message withheld.
        assert exc.__cause__ is None, "a sanitizing raise must use `from None`"
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert "During handling of the above exception" not in rendered


@given(api_json=API_JSON)
def test_reading_a_forecast_history_never_raises(api_json: Any) -> None:
    """It runs after a post, so an exception here costs the ledger row.

    The second half is the discriminating one, and it was added after mutation: asserting
    only "None or a ForecastHistory" passes against a reader that answers *empty* for
    everything it cannot parse -- which is the collapse that turns a lost connection into a
    permanent claim that a live forecast does not exist. So the two shapes that must be
    unreadable, and the one that must be empty, are stated.
    """
    history = read_my_forecasts(_Question(api_json))
    assert history is None or isinstance(history, ForecastHistory)
    if history is not None:
        for entry in history.entries:
            assert math.isfinite(entry.start_time)
            assert all(math.isfinite(value) for value in entry.values)

    inner = api_json.get("question") if isinstance(api_json, dict) else None
    if not isinstance(api_json, dict) or not isinstance(inner, dict):
        assert history is None, "an unparseable response is unreadable, never empty"
        return
    if "my_forecasts" not in inner or inner.get("my_forecasts") is None:
        assert history == ForecastHistory(()), (
            "a question the operator has never forecast on has an empty history, not an "
            "unreadable one -- which is the ordinary case for a first submission"
        )


@given(exception=st.sampled_from([ValueError, TypeError, RuntimeError, KeyError, OSError]))
def test_every_exception_classifies_into_the_closed_vocabulary(
    exception: type[BaseException],
) -> None:
    assert classify_error(exception("x")) in _LIVE_ERROR_TYPES


# Deliberately includes text far longer than any limit below. Without it the truncation
# branch is never reached and the property passes against a `storable_text` that does not
# truncate at all -- confirmed by mutation, which is why the strategy is separate from
# `ANY_VALUE` rather than folded into it.
OVERSIZED_TEXT = st.text(min_size=65, max_size=400) | HOSTILE_TEXT.map(lambda t: t * 200)


@given(value=ANY_VALUE | OVERSIZED_TEXT, limit=st.integers(min_value=2, max_value=64))
def test_storable_text_never_raises_and_always_fits(value: Any, limit: int) -> None:
    """What keeps a completed post recordable: the ledger must never refuse a receipt."""
    result = storable_text(value, limit)
    if result is None:
        assert not isinstance(value, str) or not value.strip()
        return
    assert len(result) <= limit
    assert result.strip()
    assert "\x00" not in result
    result.encode("utf-8")  # the probe `lifecycle._require_text` performs


@given(values=st.lists(st.floats(0.001, 0.999), min_size=1, max_size=6))
def test_a_multiple_choice_payload_is_accepted_exactly_when_it_is_a_distribution(
    values: list[float],
) -> None:
    """Set equality, not a one-sided check.

    Added after mutation: deleting the sum rule entirely left every property passing,
    because the generators only ever produced vectors that already summed to one. A
    one-sided property is vacuous against "the rule was removed" (M1-501's lesson), so the
    accepted set is asserted in both directions.
    """
    payload = {
        "question_type": "multiple_choice",
        "probability_yes_per_category": {f"opt-{i}": value for i, value in enumerate(values)},
    }
    is_distribution = abs(math.fsum(values) - 1.0) <= _CATEGORY_SUM_TOLERANCE
    try:
        plan = plan_from_payload(payload, expected_cdf_points=CDF_POINTS)
    except LiveSubmissionError:
        assert not is_distribution
        return
    assert is_distribution
    assert expected_values(plan) == tuple(sorted(values))


@given(probability=st.floats(allow_nan=False, allow_infinity=False, width=32))
def test_a_binary_payload_is_accepted_exactly_within_the_platform_bounds(
    probability: float,
) -> None:
    payload = {"question_type": "binary", "probability_yes": probability}
    in_bounds = 0.001 <= probability <= 0.999
    try:
        plan = plan_from_payload(payload, expected_cdf_points=CDF_POINTS)
    except LiveSubmissionError:
        assert not in_bounds
        return
    assert in_bounds
    assert expected_values(plan) == (probability,)


@given(
    values=st.lists(
        st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
        min_size=CDF_POINTS,
        max_size=CDF_POINTS,
    )
)
def test_a_cdf_is_accepted_exactly_when_it_is_non_decreasing(values: list[float]) -> None:
    payload = {"question_type": "numeric", "continuous_cdf": values}
    monotone = all(later >= earlier for earlier, later in zip(values, values[1:]))
    try:
        plan_from_payload(payload, expected_cdf_points=CDF_POINTS)
    except LiveSubmissionError:
        assert not monotone
        return
    assert monotone


@given(length=st.integers(min_value=0, max_value=400))
def test_a_cdf_is_accepted_exactly_at_the_configured_point_count(length: int) -> None:
    payload = {"question_type": "numeric", "continuous_cdf": [0.5] * length}
    try:
        plan_from_payload(payload, expected_cdf_points=CDF_POINTS)
    except LiveSubmissionError:
        assert length != CDF_POINTS
        return
    assert length == CDF_POINTS


# ── the comparison rule ──────────────────────────────────────────────────────


ENTRIES = st.builds(
    ForecastEntry,
    start_time=st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False),
    # Was `max_size=6`. Six is below every real vector this module sees -- 201 for a CDF,
    # up to `_MAX_CATEGORIES` for multiple choice -- so the comparison properties were
    # judging only lengths the platform never sends. Round 4.
    values=st.lists(
        st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False), min_size=1, max_size=32
    ).map(tuple),
)

HISTORIES = st.lists(ENTRIES, max_size=5).map(lambda entries: ForecastHistory(tuple(entries)))


@given(
    question_type=st.sampled_from(["binary", "numeric", "multiple_choice"]),
    expected=st.lists(
        st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False), min_size=1, max_size=6
    ),
    baseline=st.none() | st.floats(0.0, 1e12, allow_nan=False, allow_infinity=False),
    observed=st.none() | HISTORIES,
)
def test_confirmation_requires_both_halves(
    question_type: str,
    expected: list[float],
    baseline: float | None,
    observed: ForecastHistory | None,
) -> None:
    """`confirmed` implies a strictly newer entry **and** matching values.

    Stated as an implication from the verdict back to its evidence, which is the direction
    that can fail: a rule that dropped either half would still return `confirmed` on the
    cases a positive example test happens to pick.
    """
    result = classify_refetch(
        question_type=question_type,
        expected=expected,
        baseline_latest_start_time=baseline,
        observed=observed,
    )
    assert isinstance(result, RefetchResult)
    if question_type == "multiple_choice":
        # No option labels are supplied here, so nothing can be aligned to a category and
        # a confirmation is not available at any draw. Asserted rather than left implicit:
        # after the round-1 alignment fix these draws all take the `unreadable` branch, and
        # an implication whose antecedent is never true is a property that passes against a
        # rule that has been deleted (M1-501). The alignment itself has its own iff below.
        assert result.outcome != "confirmed"
        return
    if result.outcome != "confirmed":
        assert result.detail_code is not None
        return
    assert result.detail_code is None
    assert observed is not None
    latest = observed.latest
    assert latest is not None
    assert baseline is None or latest.start_time > baseline
    actual = observed_values(question_type, latest)
    assert actual is not None and values_match(expected, actual)


MC_LABELS = st.lists(
    st.text(alphabet="abcdef", min_size=1, max_size=2), min_size=1, max_size=4, unique=True
)
MC_PROBABILITY = st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False, width=32)


@given(data=st.data())
def test_multiple_choice_confirmation_is_by_category_never_by_position(
    data: st.DataObject,
) -> None:
    """`confirmed` **iff** the platform's per-option values, aligned by label, match.

    An iff, not an implication, and that is the whole point: the rule this replaces sorted
    both sides into a multiset, which is an implication that holds for every honest post
    and *also* for a transposed one. Round-1 finding 2 was exactly that gap, and a
    one-sided property could not have found it.

    The platform is free to list options in its own order -- that is drawn as a permutation
    -- so a reordered option list must still confirm while a reordered *forecast* must not.
    Sorting cannot tell those apart; alignment by label can, and that difference is what
    this property pins.
    """
    labels = data.draw(MC_LABELS)
    count = len(labels)
    probabilities = data.draw(st.lists(MC_PROBABILITY, min_size=count, max_size=count))
    plan = MultipleChoicePost(
        probability_yes_per_category=tuple(zip(labels, probabilities, strict=True))
    )
    platform_order = tuple(data.draw(st.permutations(labels)))
    by_label = dict(zip(labels, probabilities, strict=True))
    honest = tuple(by_label[label] for label in platform_order)
    reported = data.draw(
        st.one_of(
            st.just(honest),
            st.lists(MC_PROBABILITY, min_size=count, max_size=count).map(tuple),
        )
    )

    result = classify_refetch(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_latest_start_time=100.0,
        observed=ForecastHistory((ForecastEntry(200.0, reported),), platform_order),
        expected_labels=expected_option_labels(plan),
    )

    aligned = tuple(dict(zip(platform_order, reported, strict=True))[label] for label in labels)
    assert (result.outcome == "confirmed") == values_match(expected_values(plan), aligned)


@given(data=st.data())
def test_a_multiple_choice_snapshot_always_reproduces_its_own_verdict(
    data: st.DataObject,
) -> None:
    """The stored row recomputes the verdict, for any option order and any observation.

    Replayed out of the rendered JSON by hand rather than through `classify_refetch`: the
    claim is about what the *row* carries, and calling this module's own comparison would
    assert nothing about it. This is the ledger's whole purpose, and aligning by label
    would have quietly cost it -- `expected_values` is in the payload's order while
    `latest_values` is in the platform's, and until round 2 nothing recorded the second.
    """
    labels = data.draw(MC_LABELS)
    count = len(labels)
    probabilities = data.draw(st.lists(MC_PROBABILITY, min_size=count, max_size=count))
    plan = MultipleChoicePost(
        probability_yes_per_category=tuple(zip(labels, probabilities, strict=True))
    )
    platform_order = tuple(data.draw(st.permutations(labels)))
    by_label = dict(zip(labels, probabilities, strict=True))
    honest = tuple(by_label[label] for label in platform_order)
    reported = data.draw(
        st.one_of(
            st.just(honest),
            st.lists(MC_PROBABILITY, min_size=count, max_size=count).map(tuple),
        )
    )
    result = classify_refetch(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_latest_start_time=100.0,
        observed=ForecastHistory((ForecastEntry(200.0, reported),), platform_order),
        expected_labels=expected_option_labels(plan),
    )
    snapshot = json.loads(
        build_verification_snapshot(
            question_type="multiple_choice",
            expected=expected_values(plan),
            baseline_entry_count=0,
            baseline_latest_start_time=100.0,
            result=result,
            expected_labels=expected_option_labels(plan),
        )
    )
    observed = snapshot["observed"]
    aligned = [0.0] * len(snapshot["expected_labels"])
    for position, index in enumerate(observed["label_order"]):
        aligned[index] = observed["latest_values"][position]
    replayed = values_match(snapshot["expected_values"], aligned)
    assert replayed == (result.outcome == "confirmed")


@given(data=st.data())
def test_a_multiple_choice_refetch_never_confirms_without_an_aligned_label_set(
    data: st.DataObject,
) -> None:
    """No label list, or a label set that is not the payload's, establishes nothing.

    Never `confirmed`, and never `mismatched` either: a comparison that could not be made
    is not one that failed, and `mismatched` is what `verify_uncertain_attempt` refuses on
    with a message asserting the platform holds a *different* forecast.
    """
    labels = data.draw(MC_LABELS)
    count = len(labels)
    probabilities = data.draw(st.lists(MC_PROBABILITY, min_size=count, max_size=count))
    plan = MultipleChoicePost(
        probability_yes_per_category=tuple(zip(labels, probabilities, strict=True))
    )
    platform_labels = data.draw(
        st.one_of(
            st.none(),
            # a set that is not the payload's: a different arity, or a renamed member
            st.lists(
                st.text(alphabet="xyz", min_size=1, max_size=2), min_size=1, max_size=4, unique=True
            ).map(tuple),
        )
    )
    result = classify_refetch(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_latest_start_time=100.0,
        observed=ForecastHistory((ForecastEntry(200.0, tuple(probabilities)),), platform_labels),
        expected_labels=expected_option_labels(plan),
    )
    assert result.outcome == "unreadable"
    assert result.detail_code == "malformed_response"


@given(observed=st.none() | HISTORIES)
def test_an_unreadable_refetch_is_never_reported_as_absent(
    observed: ForecastHistory | None,
) -> None:
    """The distinction a lost connection depends on.

    `absent` carries a record to terminal `failed`, so reporting an unperformed refetch as
    absent would end a live forecast version on no evidence at all. Found by mutation: the
    unit suite could not reach this branch, because the submit path never hands
    `classify_refetch` a `None`.
    """
    result = classify_refetch(
        question_type="binary",
        expected=[0.4],
        baseline_latest_start_time=None,
        observed=observed,
    )
    assert (result.outcome == "unreadable") == (observed is None)


# ── the snapshot the ledger stores and a later verification re-reads ─────────


PLANS = (
    st.builds(BinaryPost, probability_yes=PROBABILITIES)
    | st.builds(
        NumericPost,
        continuous_cdf=st.lists(
            st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
            min_size=CDF_POINTS,
            max_size=CDF_POINTS,
        ).map(lambda values: tuple(sorted(values))),
    )
    | st.builds(
        MultipleChoicePost,
        probability_yes_per_category=st.lists(
            st.tuples(HOSTILE_TEXT, PROBABILITIES), min_size=1, max_size=6
        ).map(tuple),
    )
)


@given(plan=PLANS, baseline=st.none() | st.floats(0.0, 1e12, allow_nan=False, allow_infinity=False))
def test_a_snapshot_is_storable_and_survives_its_own_round_trip(
    plan: Any, baseline: float | None
) -> None:
    """Replay stability, in the persisted form (M1-305's rule).

    `verify_uncertain_attempt` re-judges an attempt from these bytes, so a rendering whose
    reparse renders differently would make a later verification compare different numbers
    than the attempt did -- and it is stored in a `TEXT` column, so it must also survive
    what `lifecycle` will do to it.
    """
    result = classify_refetch(
        question_type=plan.question_type,
        expected=expected_values(plan),
        baseline_latest_start_time=baseline,
        observed=ForecastHistory(()),
    )
    snapshot = build_verification_snapshot(
        question_type=plan.question_type,
        expected=expected_values(plan),
        baseline_entry_count=0,
        baseline_latest_start_time=baseline,
        result=result,
    )
    assert len(snapshot) <= MAX_BODY_LENGTH
    assert storable_text(snapshot, MAX_BODY_LENGTH) == snapshot
    reparsed = json.dumps(
        json.loads(snapshot),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert reparsed == snapshot

    parsed = read_verification_snapshot(snapshot)
    assert parsed["question_type"] == plan.question_type
    assert tuple(parsed["expected_values"]) == expected_values(plan)  # type: ignore[arg-type]


# ── the evidence envelope ────────────────────────────────────────────────────
#
# Round 4. Three consecutive review rounds found the same class by hand -- an unbounded
# provider-supplied field rendered into a fixed-size envelope, pushing it past `MAX_BODY_LENGTH`
# and collapsing it to the reduced form that names no values at all. Round 1 found it in
# the option list, round 3 in the label strings, round 4 in the observed vector.
#
# The reason a fuzzer never found it is written in round 3's review: the multiple-choice
# properties bounded generated labels to a couple of characters, and the snapshot
# round-trip property passed `ForecastHistory(())` -- an *empty* history, so the branch
# that renders observed values was never entered. Its `len(snapshot) <= MAX_BODY_LENGTH`
# assertion was therefore vacuous for exactly the invariant that kept failing. That is the
# `strategy could not reach the branch it claims to cover` failure this file's own header
# warns about, and M1-501 and M1-607 before it.
#
# So the invariant is asserted directly, at the maximum of every accepted input at once.

MAXIMAL_LABELS = st.builds(
    # Unique by the index suffix, and padded with a character `ensure_ascii=True` escapes
    # to six bytes -- the worst case the bound was computed against, not the ASCII best
    # case that would make any cap look sufficient.
    lambda count, length: tuple(("é" * (length - 3)) + f"{index:03d}" for index in range(count)),
    count=st.integers(1, _MAX_CATEGORIES),
    length=st.integers(4, _MAX_OPTION_LABEL),
)


@given(
    question_type=st.sampled_from(["binary", "numeric", "multiple_choice"]),
    labels=MAXIMAL_LABELS,
    # Far past anything the platform should send, and past `_MAX_SNAPSHOT_VALUES`. Drawn
    # as a *length* and repeated rather than drawn element-wise: the property is about the
    # rendered size, and generating two thousand floats per example would spend the whole
    # budget on the strategy instead of the assertion.
    observed_length=st.integers(0, 2000),
    # The full finite range, not [0, 1]. Observed values are provider JSON and nothing
    # upstream constrains them to probabilities -- and the longest rendering a float has
    # is 24 characters with its sign (`-2.2250738585072014e-308`), which a non-negative
    # strategy can never draw. Round 4 review, non-blocking observation 1: the envelope
    # must be measured at the real worst case, not a convenient one.
    observed_value=st.floats(allow_nan=False, allow_infinity=False),
    # When true the platform echoes back exactly what was sent, which is the only way to
    # reach `confirmed`. Without it the observed vector is one repeated value, and a valid
    # CDF varies -- so numeric confirmation was unreachable inside this property. Round 4
    # review, non-blocking observation 2; it is this file's own recurring defect (a
    # strategy that cannot reach the branch it claims to cover) one level down.
    echo_expected=st.booleans(),
    reversed_order=st.booleans(),
)
def test_no_accepted_input_can_cost_a_snapshot_its_evidence(
    question_type: str,
    labels: tuple[str, ...],
    observed_length: int,
    observed_value: float,
    echo_expected: bool,
    reversed_order: bool,
) -> None:
    """The reduced `values_omitted` envelope is unreachable for anything this module accepts.

    **This is the property the three rounds should have been.** It says the thing the caps
    exist to guarantee, rather than a consequence of it: whatever the operator's payload
    and whatever the platform reports back, the snapshot fits `MAX_BODY_LENGTH` *and still names
    its evidence*. Delete `_MAX_SNAPSHOT_VALUES`, or raise `_MAX_OPTION_LABEL`, and the
    maximal envelope goes back over the limit and this fails.

    The `confirmed` case carries the stronger claim, because that is the row that makes an
    assertion an auditor may later have to check: a confirmation must always be able to
    show the values it was confirmed by.
    """
    if question_type == "multiple_choice":
        probabilities = tuple(1.0 / len(labels) for _ in labels)
        plan = MultipleChoicePost(
            probability_yes_per_category=tuple(zip(labels, probabilities, strict=True))
        )
        expected = expected_values(plan)
        expected_labels: tuple[str, ...] | None = expected_option_labels(plan)
        platform_order: tuple[str, ...] | None = (
            tuple(reversed(labels)) if reversed_order else labels
        )
    elif question_type == "numeric":
        expected = tuple(index / (CDF_POINTS - 1) for index in range(CDF_POINTS))
        expected_labels = None
        platform_order = None
    else:
        expected = (0.6,)
        expected_labels = None
        platform_order = None

    if not echo_expected:
        vector = (observed_value,) * observed_length
    elif question_type == "binary":
        # `observed_values` reads `values[1]` of exactly two, so an honest binary
        # observation is the complementary pair.
        vector = (1.0 - expected[0], expected[0])
    elif question_type == "numeric":
        vector = expected
    else:
        # The platform reports in its own order, so an honest multiple-choice observation
        # is the expected vector permuted into that order -- which is precisely what
        # `label_order` exists to let an auditor undo.
        by_label = dict(zip(labels, expected, strict=True))
        assert platform_order is not None
        vector = tuple(by_label[label] for label in platform_order)

    true_length = len(vector)
    observed = ForecastHistory((ForecastEntry(200.0, vector),), platform_order)
    result = classify_refetch(
        question_type=question_type,
        expected=expected,
        baseline_latest_start_time=100.0,
        observed=observed,
        expected_labels=expected_labels,
    )
    rendered = build_verification_snapshot(
        question_type=question_type,
        expected=expected,
        baseline_entry_count=1,
        baseline_latest_start_time=100.0,
        result=result,
        expected_labels=expected_labels,
    )
    snapshot = read_verification_snapshot(rendered)

    assert len(rendered) <= MAX_BODY_LENGTH
    # The whole point: the envelope never degrades to the form that names nothing.
    assert "values_omitted" not in snapshot
    assert snapshot["expected_values"] == list(expected)

    seen = snapshot["observed"]
    assert isinstance(seen, dict)
    # The true length, always -- never the truncated one. A row that reported the sample
    # size as the real size would hide the truncation instead of recording it.
    assert seen["latest_value_count"] == true_length
    assert len(seen["latest_values"]) == min(true_length, _MAX_SNAPSHOT_VALUES)

    if result.outcome == "confirmed":
        # A confirmation is an assertion about a paid post, so it must be replayable from
        # the row alone: nothing it was confirmed by may have been sampled away.
        assert true_length <= _MAX_SNAPSHOT_VALUES
        assert seen["latest_values"] == list(vector)
        if question_type == "multiple_choice":
            assert snapshot["expected_labels"] == list(labels)
            assert seen["label_order"] is not None


@given(snapshot=ANY_VALUE)
def test_reading_a_stored_snapshot_never_raises_a_foreign_type(snapshot: Any) -> None:
    try:
        read_verification_snapshot(snapshot)
    except LiveSubmissionError:
        return
    except GatewayError:  # pragma: no cover - the base class, kept explicit
        return


# ── the derived identifier ───────────────────────────────────────────────────


KEYS = st.text(
    st.characters(min_codepoint=48, max_codepoint=122, categories=["Ll", "Lu", "Nd"]),
    min_size=1,
    max_size=32,
)


@given(key=KEYS, other=KEYS)
def test_a_live_attempt_id_is_deterministic_and_never_echoes_its_key(key: str, other: str) -> None:
    first = live_attempt_id(key)
    assert first == live_attempt_id(key)
    assert re.fullmatch(r"wjlive-1-[0-9a-f]{64}", first)
    assert len(first) <= MAX_IDENTIFIER_LENGTH
    # The claim is that an attempt id can never be pasted into a query against
    # `submission_attempts.idempotency_key` and match -- not that the key's characters
    # never appear in a 64-character hex digest, which for a one-character key they
    # sometimes do. A containment assertion here would be the vacuous kind pointed the
    # other way: it would fail on correct code.
    assert first != key
    assume(other != key)
    assert live_attempt_id(other) != first


@given(key=ANY_VALUE)
def test_a_malformed_key_is_refused_as_this_modules_error(key: Any) -> None:
    try:
        live_attempt_id(key)
    except LiveSubmissionError:
        return
    assert isinstance(key, str)
