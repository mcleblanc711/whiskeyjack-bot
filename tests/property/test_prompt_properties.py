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
from hypothesis import assume, event, given, settings, strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.prompt import (
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
# reaches both the matching and the non-matching branch of the sentence scan. The
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

# Lines with no ``probabilit`` in them. The percentile one is the decoy the scope
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


# --------------------------------------------------------------------------------------
# M1-409: the verdict against an oracle that never reads the parser's regular expressions.
# --------------------------------------------------------------------------------------

# What each decimal spelling in ``_DECIMALS`` is read as, written out by hand, per slot.
# This is the independent oracle the row asks for: M1-407's version of the property below
# rebuilt its expectation from ``_DECLARED_RANGE_RE`` and ``_PROBABILITY_LINE_RE`` and so
# agreed with the parser by construction -- it could not have seen the wrapping defect,
# because it scanned lines exactly as the parser did. A spelling missing from a table is
# one the parser must not read in that slot. The two tables differ in one entry: ``1.``
# reads as ``1`` where a sentence may end on it (the high slot) and ends the sentence
# before ``and`` where it cannot (the low slot).
_OVERLONG_SMALL = "0." + "0" * 400 + "1"  # underflows to 0.0: not a raise
_OVERLONG_LARGE = "9" * 400  # overflows to inf: not a raise
_READ_AS_LOW: dict[str, float] = {
    "0.001": 0.001,
    "0.999": 0.999,
    "0": 0.0,
    "1": 1.0,
    "0.5": 0.5,
    "00.001": 0.001,
    _OVERLONG_SMALL: 0.0,
    _OVERLONG_LARGE: math.inf,
}
_READ_AS_HIGH: dict[str, float] = {**_READ_AS_LOW, "1.": 1.0}

# Complete sentences, each ending in a terminator so the frames are separate sentences
# whatever the wrapping does. The first three are the shipped prompt's own declarations.
_KNOWN_PROBABILITY_FRAMES = (
    "Use probability values between {low} and {high} for binary outcomes.",
    "`probability_yes` must be between {low} and {high} inclusive.",
    "Probabilities must be between {low} and {high} and sum to 1 within `1e-6`.",
    "Probability: between {low} and {high}.",
)
_KNOWN_OTHER_FRAMES = (
    "Percentile values must be between {low} and {high} and non-decreasing.",
    "Values must be between {low} and {high}.",
    "Return every supplied option exactly once.",
)


# ``_DECIMALS`` again, with the two shipped ends repeated so ``sampled_from`` draws them
# often enough to reach agreement and disagreement between *usable* ranges; the near-miss
# spellings stay in, so the not-read branch of each slot is still drawn.
_KNOWN_DECIMALS = st.sampled_from(
    ["0.001"] * 4
    + ["0.999"] * 4
    + ["0.5", "0", "1", "1.", "00.001", ".5", "-0.1", "1e-6", "０.００１"]
    + [_OVERLONG_SMALL, _OVERLONG_LARGE]
)


def _opens_a_block(token: str) -> bool:
    """Whether a line starting with ``token`` would open a Markdown list item or heading.

    Hand-written from Markdown, for the wrap strategy below: an editor's wrap never puts a
    list marker at the start of a continuation line, so a draw that would is not a wrap.
    """
    return token[:1] in {"-", "*", "+", "#"} or (token[:-1].isdigit() and token[-1:] in ".)")


def _wrapped(draw: Any, sentence: str) -> str:
    """``sentence`` with a drawn subset of its spaces replaced by a line break."""
    words = sentence.split(" ")
    out = [words[0]]
    for word in words[1:]:
        brk = draw(st.sampled_from([" ", " ", "\n", "\n    ", "\r\n"]))
        out.append((" " if _opens_a_block(word) else brk) + word)
    return "".join(out)


@st.composite
def _known_bodies(draw: Any) -> tuple[str, DeclaredProbabilityBounds | None, str]:
    """A body, the verdict the oracle expects for it, and a reach tag.

    Three modes, because two independent draws rarely coincide: every frame states one
    shared pair (the accept branch, over several declarations), every frame but the last
    does and the last states a different one (disagreement, with the odd one out wrapped as
    often as any other), or each frame draws its own.
    """
    mode = draw(st.sampled_from(["shared", "one differs", "independent"]))
    pair = (draw(_KNOWN_DECIMALS), draw(_KNOWN_DECIMALS))
    odd = (draw(_KNOWN_DECIMALS), draw(_KNOWN_DECIMALS))
    count = draw(st.integers(min_value=0, max_value=4))
    sentences: list[str] = []
    read: list[tuple[float, float]] = []
    wraps = 0
    for index in range(count):
        about_probability = draw(st.integers(0, 3)) > 0
        frames = _KNOWN_PROBABILITY_FRAMES if about_probability else _KNOWN_OTHER_FRAMES
        if mode == "independent":
            low, high = draw(_KNOWN_DECIMALS), draw(_KNOWN_DECIMALS)
        elif mode == "one differs" and index == count - 1:
            low, high = odd
        else:
            low, high = pair
        sentence = draw(st.sampled_from(frames)).format(low=low, high=high)
        wrapped = _wrapped(draw, sentence)
        wraps += wrapped != sentence
        sentences.append(wrapped)
        if about_probability and low in _READ_AS_LOW and high in _READ_AS_HIGH:
            read.append((_READ_AS_LOW[low], _READ_AS_HIGH[high]))
    body = "\n".join(sentences)
    # Unrelated text in its own paragraph, and never itself carrying a range: it is here
    # to be ignored, and a hostile string that happened to state one is a different claim.
    noise = draw(st.lists(HOSTILE_TEXT, max_size=2))
    assume(all("between" not in line.lower() for line in noise))
    body = "\n\n".join([body, *noise])

    expected: DeclaredProbabilityBounds | None
    if not read:
        expected, tag = None, "no range"
    elif any(other != read[0] for other in read[1:]):
        expected, tag = None, "disagreement"
    elif not 0.0 <= read[0][0] < read[0][1] <= 1.0:
        expected, tag = None, "unusable range"
    else:
        expected = DeclaredProbabilityBounds(low=read[0][0], high=read[0][1])
        tag = f"accepted from {min(len(read), 2)}{'+' if len(read) >= 2 else ''} declaration(s)"
    return body, expected, f"{tag}; wrapped={wraps > 0}"


@settings(max_examples=400)
@given(_known_bodies())
def test_the_verdict_matches_a_hand_written_oracle_however_the_body_is_wrapped(
    case: tuple[str, DeclaredProbabilityBounds | None, str],
) -> None:
    """The M1-409 criterion as an iff, over bodies whose line breaks fall anywhere.

    Accepted with exactly the stated pair when every probability declaration agrees on a
    usable range; refused otherwise -- including when a disagreeing declaration is wrapped
    so that its range and the word ``probability`` sit on different lines. Both directions,
    because a one-sided property is vacuous on the side it never reaches (M1-501), and
    ``event`` shows both are reached with and without wrapping.
    """
    body, expected, tag = case
    event(tag)
    assert _parse(body) == expected
