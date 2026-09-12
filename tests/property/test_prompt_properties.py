"""Invariants of the forecaster prompt's declared-range parser and the two relations
built on it (M1-407).

``parse_declared_probability_bounds`` is a pure function over a prompt body, and the
body is operator-editable text pointed at by ``forecast.prompt_path``. Every startup
path runs it before a billable call, so a raise of the wrong type here is an unhandled
exception in the live worker rather than a refusal an operator can act on, and a leaked
line is a prompt's contents in a diagnostic -- the hazard ``prompt.py``'s module
docstring names, since a prompt can carry a mistakenly pasted credential.

The CLAUDE.md pre-review fuzz pass, asserting the four invariants that pass has
historically been worth:

- **totality** -- any text raises only ``PromptError``, never a bare ``ValueError`` from
  ``float()``, ``OverflowError``, or a ``TypeError`` out of a comparison;
- **no value leak** in any message *or rendered traceback*, on every failure path;
- **replay-stability** across the persisted form of the text, which is what a re-parse of
  a stored prompt has to agree with;
- the **relation** each of the two comparison functions claims, asserted as an iff in both
  directions rather than one-sided (the M1-501 lesson: a one-sided conditional property is
  vacuous on the side it never reaches).

Plus the one specific to this item: **the two relations must actually differ.** Splitting
``probability_bounds_disagreement`` (equality, at the load boundary) from
``probability_bounds_violation`` (containment, at the spending site) buys nothing unless a
pair exists that is one and not the other, and the row's own motivating case -- a narrowed
config -- is exactly such a pair.

Every property here was re-run against deliberately broken code and confirmed to fail
first; three of M1-303's ten new properties passed against the pre-fix tree
(docs/LESSONS.md, lesson 5).
"""

from __future__ import annotations

import json
import math
import traceback
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.prompt import (
    _DECLARED_RANGE_RE,
    _PROBABILITY_LINE_RE,
    DeclaredProbabilityBounds,
    PromptError,
    load_prompt,
    parse_declared_probability_bounds,
    probability_bounds_disagreement,
    probability_bounds_violation,
)

# Low-entropy on purpose: gitleaks scans full history on every branch in CI, so a
# realistic-looking planted secret would fail CI on unrelated PRs until it was
# fingerprint-pinned (M1-301).
PLANTED = "privateFAKE123456"

# Decimal spellings the parser may or may not accept, near-misses of one, and the shapes
# that break a naive parse: an overlong digit run (``float()`` yields ``inf``, it does not
# raise), a bare point, a sign, an exponent.
_DECIMALS = st.sampled_from(
    [
        "0.001",
        "0.999",
        "0",
        "1",
        "0.5",
        ".5",
        "1.",
        "-0.1",
        "1e-6",
        "0." + "0" * 400 + "1",
        "9" * 400,
        "00.001",
        "０.００１",  # fullwidth digits: not ASCII
    ]
)

# Sentence frames the prompt body actually uses, plus ones it does not, so the generator
# reaches both the matching and the non-matching branch of the line scan. The
# ``{low}``/``{high}`` slots are filled from _DECIMALS.
_PROBABILITY_FRAMES = st.sampled_from(
    [
        "Use probability values between {low} and {high} for binary outcomes.",
        "`probability_yes` must be between {low} and {high} inclusive.",
        "Probabilities must be between {low} and {high} and sum to 1 within `1e-6`.",
        "Probability: between {low} and {high}.",
        "probability between {low} and {high}",
    ]
)

# Lines with no ``probabilit`` in them. The percentile one is the decoy the line scan
# exists for: it carries a perfectly parseable range that is not a probability range.
_NON_PROBABILITY_FRAMES = st.sampled_from(
    [
        "Percentile values must be between {low} and {high} and non-decreasing.",
        "Values must be between {low} and {high}.",
        "Return every supplied option exactly once.",
        "",
    ]
)


@st.composite
def _prompt_bodies(draw: Any) -> str:
    """A prompt body assembled from probability lines, decoy lines and hostile text.

    The three dimensions are drawn independently so the body can carry one range, several
    agreeing ranges, several disagreeing ones, a range only on a non-probability line, or
    none at all. A generator that could not produce all of those would leave the
    agreement and no-range properties asserting over a single branch.
    """
    lines: list[str] = []
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        frame = draw(st.one_of(_PROBABILITY_FRAMES, _NON_PROBABILITY_FRAMES))
        lines.append(frame.format(low=draw(_DECIMALS), high=draw(_DECIMALS)))
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        lines.append(draw(HOSTILE_TEXT))
    return "\n".join(lines)


PROMPT_BODIES = _prompt_bodies()

# Arbitrary text, not just assembled bodies: the parser is also handed whatever an
# operator's own prompt file happens to contain.
ANY_TEXT = st.one_of(PROMPT_BODIES, HOSTILE_TEXT, st.text(max_size=200))

# Bounds arguments a caller could pass, including the shapes ``ForecastConfig`` guarantees
# cannot happen and an ``AppConfig`` assembled some other way can.
BOUND_VALUES = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(min_value=0.0, max_value=1.0),
    st.integers(min_value=-2, max_value=2),
    st.text(max_size=4),
    st.none(),
    st.booleans(),
)

# Well-formed declared ranges, for the relation properties.
DECLARED = st.builds(
    lambda pair: DeclaredProbabilityBounds(low=pair[0], high=pair[1]),
    st.tuples(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    ).filter(lambda pair: pair[0] < pair[1]),
)

FINITE_BOUNDS = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


def _parse(text: str) -> DeclaredProbabilityBounds | None:
    """Parse, or None if the text declares no usable range.

    Any other exception escaping this helper is the finding: every caller of
    ``load_prompt`` handles ``PromptError`` and nothing else.
    """
    try:
        return parse_declared_probability_bounds(text)
    except PromptError:
        return None


# --------------------------------------------------------------------------------------
# Invariant 1: totality -- only PromptError, ever.
# --------------------------------------------------------------------------------------


@given(ANY_TEXT)
def test_the_parse_raises_only_prompt_error(text: str) -> None:
    """``float()`` on an overlong digit run returns ``inf`` rather than raising, but a
    parser that reached ``int()``, ``Decimal`` or an unguarded comparison would not be so
    lucky -- and a raw exception is both a caller-contract break and a leak channel,
    because its text quotes what it choked on."""
    _parse(text)


@given(declared=DECLARED, minimum=BOUND_VALUES, maximum=BOUND_VALUES)
@settings(max_examples=120)
def test_both_relations_raise_only_prompt_error(
    declared: DeclaredProbabilityBounds, minimum: Any, maximum: Any
) -> None:
    """Both are public, and ``forecast.generate`` calls ``..._violation`` directly, so
    neither may escape this module as something a caller does not handle.

    This property is here because a mutation pass found the gap rather than the other way
    round: with the bound guard reachable only from ``load_prompt``, deleting it left the
    whole suite green -- equality is total, so a ``str`` bound simply compared unequal --
    while ``probability_bounds_violation("x")`` raised a bare ``TypeError`` out of its
    ``<=``. A ``NaN`` is the same shape without the crash: no comparison rejects it.
    """
    for relation in (probability_bounds_disagreement, probability_bounds_violation):
        try:
            relation(declared, min_probability=minimum, max_probability=maximum)
        except PromptError:
            continue
        except Exception as exc:  # pragma: no cover - only reached on a real defect
            pytest.fail(f"{relation.__name__} raised {type(exc).__name__}, not PromptError")


@given(text=ANY_TEXT, minimum=BOUND_VALUES, maximum=BOUND_VALUES)
@settings(max_examples=120)
def test_load_prompt_raises_only_prompt_error(
    tmp_path_factory: Any, text: str, minimum: Any, maximum: Any
) -> None:
    """The whole load, over arbitrary body and arbitrary bounds arguments. A NaN bound is
    the interesting one: every comparison against it is False, so an unguarded check
    passes it silently and the refusal never happens."""
    path = tmp_path_factory.mktemp("prompt") / "forecaster.md"
    path.write_text(f"# p — v1.1.0\n\n{text}\n", encoding="utf-8", errors="surrogatepass")
    try:
        load_prompt(path, "1.1.0", min_probability=minimum, max_probability=maximum)
    except PromptError:
        return
    except Exception as exc:  # pragma: no cover - only reached on a real defect
        pytest.fail(f"load_prompt raised {type(exc).__name__}, not PromptError")


# --------------------------------------------------------------------------------------
# Invariant 2: no value leak, in the message or the rendered traceback.
# --------------------------------------------------------------------------------------


@given(PROMPT_BODIES)
def test_a_refusal_never_echoes_the_body(body: str) -> None:
    """A prompt line is file content and must never reach a diagnostic. The rendered
    traceback is checked too: that is the realistic leak path, because a frame-capturing
    logger or a failed assertion renders locals and source, not just the message."""
    text = f"{PLANTED}\n{body}\n{PLANTED}"
    try:
        parse_declared_probability_bounds(text)
    except PromptError as error:
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        assert PLANTED not in str(error)
        assert PLANTED not in rendered


@given(
    declared=DECLARED,
    first=st.tuples(FINITE_BOUNDS, FINITE_BOUNDS),
    second=st.tuples(FINITE_BOUNDS, FINITE_BOUNDS),
)
def test_neither_relation_echoes_a_configured_bound(
    declared: DeclaredProbabilityBounds,
    first: tuple[float, float],
    second: tuple[float, float],
) -> None:
    """M1-509 is open -- whether a *configured* bound may be rendered at all is not
    settled -- so neither message states one.

    Asserted as *independence* rather than as "no substring of the configured pair appears
    in the message", and the substring form is not merely weaker here, it is wrong: the
    declared pair **is** rendered, deliberately, so a configured ``0.2`` against a declared
    ``0.25`` fails a substring check with no leak having occurred. That is the false alarm
    ``test_canonical_properties.test_rejection_never_echoes_the_input`` describes.

    Two different configured pairs that the same relation rejects against the same declared
    range must produce the *same* string. A message carrying any configured value could not.
    """
    for relation in (probability_bounds_disagreement, probability_bounds_violation):
        problems = [
            relation(declared, min_probability=low, max_probability=high)
            for low, high in (first, second)
        ]
        if any(problem is None for problem in problems):
            continue
        assert problems[0] == problems[1]


# --------------------------------------------------------------------------------------
# Invariant 3: replay-stability across the persisted form of the text.
# --------------------------------------------------------------------------------------


@given(ANY_TEXT)
def test_the_parse_is_stable_across_the_persisted_form(text: str) -> None:
    """A prompt reaches the ledger as a digest and reaches a re-parse as text that has
    been through storage. The M1-305 rule for that round trip is
    ``json.dumps(..., ensure_ascii=True, sort_keys=True)`` -- the one form that survives a
    lone surrogate -- and the parse over the reloaded string has to be the same answer, or
    a replay could read a different range than the run it replays."""
    reloaded = json.loads(json.dumps(text, ensure_ascii=True, sort_keys=True))
    first = _parse(text)
    second = _parse(reloaded)
    assert first == second


@given(ANY_TEXT)
def test_the_parse_is_a_fixed_point_on_repetition(text: str) -> None:
    """Nothing in the parse mutates or normalizes its input, so two parses of one string
    are one answer. Without this the range could depend on how many times a prompt had
    been read."""
    assert _parse(text) == _parse(text)


# --------------------------------------------------------------------------------------
# Invariant 4: the relation each function claims, in both directions.
# --------------------------------------------------------------------------------------


@given(declared=DECLARED, minimum=FINITE_BOUNDS, maximum=FINITE_BOUNDS)
def test_disagreement_is_none_exactly_when_the_pairs_are_equal(
    declared: DeclaredProbabilityBounds, minimum: float, maximum: float
) -> None:
    """An iff, not one direction. Asserted only as "a different pair is reported", this
    would pass for a function that reported every pair including the equal one."""
    accepted = (
        probability_bounds_disagreement(declared, min_probability=minimum, max_probability=maximum)
        is None
    )
    assert accepted == ((declared.low, declared.high) == (minimum, maximum))


@given(declared=DECLARED, minimum=FINITE_BOUNDS, maximum=FINITE_BOUNDS)
def test_violation_is_none_exactly_when_the_pair_is_contained(
    declared: DeclaredProbabilityBounds, minimum: float, maximum: float
) -> None:
    accepted = (
        probability_bounds_violation(declared, min_probability=minimum, max_probability=maximum)
        is None
    )
    assert accepted == (declared.low <= minimum and maximum <= declared.high)


@given(declared=DECLARED, minimum=FINITE_BOUNDS, maximum=FINITE_BOUNDS)
def test_equality_is_strictly_stronger_than_containment(
    declared: DeclaredProbabilityBounds, minimum: float, maximum: float
) -> None:
    """The ordering the split rests on: every pair the load boundary accepts, the
    spending site accepts too. If this ever inverted, ``load_prompt`` would be admitting
    configurations ``generate_forecast`` then refuses -- a startup check that green-lights
    a run which cannot make a single call."""
    agrees = (
        probability_bounds_disagreement(declared, min_probability=minimum, max_probability=maximum)
        is None
    )
    contained = (
        probability_bounds_violation(declared, min_probability=minimum, max_probability=maximum)
        is None
    )
    assert not agrees or contained


def test_the_two_relations_are_not_the_same_function() -> None:
    """The anti-vacuity check for the property above: a pair that is a disagreement and
    not a violation has to exist, or ``not agrees or contained`` holds for the trivial
    reason that the two functions answer identically. This pair is the row's own
    motivating case -- a narrowed config, which containment passes silently."""
    declared = DeclaredProbabilityBounds(low=0.001, high=0.999)
    assert (
        probability_bounds_disagreement(declared, min_probability=0.05, max_probability=0.95)
        is not None
    )
    assert (
        probability_bounds_violation(declared, min_probability=0.05, max_probability=0.95) is None
    )


# --------------------------------------------------------------------------------------
# Item-specific: what a successful parse is allowed to return.
# --------------------------------------------------------------------------------------


@given(ANY_TEXT)
def test_an_accepted_range_can_always_bound_a_probability(text: str) -> None:
    """Whatever comes back is usable as a bound, so no caller has to re-check it. The
    overlong digit run in the strategy is why this is not obvious: ``float()`` turns it
    into ``inf``, which compares fine and would escape as a range if only ``low < high``
    were checked."""
    bounds = _parse(text)
    if bounds is None:
        return
    assert math.isfinite(bounds.low) and math.isfinite(bounds.high)
    assert 0.0 <= bounds.low < bounds.high <= 1.0


@given(PROMPT_BODIES)
def test_an_accepted_body_states_that_range_everywhere_it_states_one(body: str) -> None:
    """The agreement rule, from the other side: if a body parses, then every range it
    states on a probability line is the range returned. A parse that stopped at the first
    match satisfies the no-raise properties above and fails this one."""
    bounds = _parse(body)
    if bounds is None:
        return
    found = [
        (float(m.group(1)), float(m.group(2)))
        for line in body.splitlines()
        if _PROBABILITY_LINE_RE.search(line) is not None
        for m in _DECLARED_RANGE_RE.finditer(line)
    ]
    assert found, "an accepted body must state the range at least once"
    assert set(found) == {(bounds.low, bounds.high)}
