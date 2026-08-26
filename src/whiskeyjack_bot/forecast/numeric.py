"""The numeric percentile path (M1-405).

``forecast/schema.py`` accepts any list of at least one structurally valid percentile
point, and says so at the field: that the nine levels "are exactly the declared ones,
non-decreasing and compatible with the question's bounds is M1-405's acceptance criterion;
all three need the question, which this module does not read". This is the module that
reads it.

Four rules, and every one is numeric-specific by construction:

- ``final_prediction.percentiles`` must carry **exactly the nine declared levels, in
  ascending order**. ``prompts/forecaster.md`` prints them and then says "Return exactly
  the nine percentile levels shown above". One tuple comparison covers the count, the
  membership, the duplicates and the order at once, which is why there is one problem
  string rather than four.
- Values must be **non-decreasing**, which is the prompt's own word in both places it
  states the rule, and is also what the pinned SDK enforces: ``NumericDistribution.
  _check_percentiles_increasing`` raises on ``value[i] > value[i + 1]`` and tolerates a
  tie. Strictly-increasing is the stricter reading of the backlog row's "ordered" and it
  is **not** taken -- see the docstring of :func:`_ordering_problem`.
- A **closed** bound must be respected: no value below ``lower_bound`` when
  ``open_lower_bound`` is false, none above ``upper_bound`` when ``open_upper_bound`` is
  false. An **open** bound constrains nothing here; the prompt says the tails "may extend
  beyond the displayed bound only when the question model and application validation
  permit it", and that permission is measured against ``numeric_calibration``, which this
  module cannot see -- M1-503 owns it.
- When the question carries a ``zero_point``, no value may fall below it. That is the
  SDK's ``_check_log_scaled_fields``, and it is here rather than in M1-503 because it is
  one of the two checks ``NumericDistribution`` runs **unconditionally** -- with
  ``strict_validation`` off, with ``standardize_cdf`` off, on every path.

**Where the line between this item and M1-503 falls.** M1-405 enforces what is true of a
percentile set for *any* configuration; M1-503 enforces what depends on
``numeric_calibration`` -- the 25%-wiggle and 2x-range tail rules
(``_check_too_far_from_bounds``), the adjacent-level spacing, the 201-point count and the
PMF step cap. ``NumericCalibrationConfig`` is a sibling of ``ForecastConfig`` on
``AppConfig``, not a member of it, so a rule keyed on it cannot be applied from a checker
that is handed only the latter -- applying it anyway would refuse a forecast a permissive
config accepts. The split is the config boundary, not a guess about scope.

**The rules live on the output path rather than in the schema**, M1-403's placement
decision applied to a third item and for its three reasons: it leaves
``test_numeric_percentile_levels_are_not_checked_here`` and ``schema.py``'s stated scope
intact rather than reversing them, it keeps the schema free of anything a question or a
config supplies, and it makes every rule **repairable** -- ``forecast.parse`` applies these
inside the attempt loop, so a violation becomes a sanitized problem the existing
one-repair loop feeds back to the model rather than a wasted billable call.

That is also why :func:`numeric_output_problems` **returns** problems instead of raising.
Its strings are the shape ``schema._sanitize`` produces -- a schema-authored field path, a
colon, a value-free message -- so ``generate._repair_turn`` renders them uniformly and
``generate._classify`` reads them as ``schema_invalid`` with no change on either side.

**No message renders a value -- not the model's, and not the question's.** The first cut
rendered ``lower_bound``, ``upper_bound`` and ``zero_point``, reasoning from ``binary.py``
that a repair turn which does not name the bound is one no model can aim at. Round 1 was
right to refuse that, and the reason is that ``binary.py``'s argument does not survive the
move. It holds there because ``prompts/forecaster.md`` prints ``0.001``-``0.999`` to the
model as a **literal** while config is free to narrow it, so the model genuinely does not
know the effective bound. Here it does: ``forecast/inputs.py`` puts ``lower_bound``,
``upper_bound``, ``open_lower_bound``, ``open_upper_bound`` and ``zero_point`` into the
model's own request under the numeric fields, so naming them back buys nothing.

``attribution.py`` had already drawn exactly this line -- it declines to render the supplied
``source_ids`` because "the model already holds the whole id list in its own request, under
``research_documents``" -- and this module cited ``binary.py``'s side of it without checking
that one. The rule these messages state is enough to act on; the numbers were never the
part the model was missing.

That matters beyond tidiness, because these strings do not stop at the repair turn. A
response that fails twice puts them in ``ForecastGeneration.failure_problems``, which
``forecast/artifacts.py`` writes into the persisted raw-output envelope. Question fields
come from Metaculus payloads, which CLAUDE.md classes as untrusted, and the carve-out that
lets a *path* be rendered is about operator configuration, not about provider content. So a
rendered bound is provider data entering the ledger's diagnostics.

The nine declared levels **are** still named, and they are a different category: they are
this project's own constant, transcribed from the hashed prompt, and naming them is what
``schema.response_model_for`` does when it prints the supported question types. Nothing
reached through ``forecast`` or ``question`` is interpolated anywhere in this module.

**This module reads the canonical question, and that is a widening of the M1-506 seam.**
``docs/TRACKS.md`` predicted M1-404 and M1-405 would each be one changed line in
``forecast/validate.py``; that holds for the registration and not for the signature, since
three of the four rules above are about the question. ``questions/model.py`` is SDK-free
and is already imported by ``forecast/inputs.py``, which the import-graph probe pins
clean, so the widening does not cost M1-406's replay guarantee. It imports no provider SDK
and no HTTP client, like ``schema``, ``binary``, ``attribution``, ``validate`` and
``parse``.
"""

from __future__ import annotations

from itertools import pairwise

from whiskeyjack_bot.config import ForecastConfig
from whiskeyjack_bot.forecast.schema import (
    ForecastSchemaError,
    NumericForecastResponse,
)
from whiskeyjack_bot.questions.model import CanonicalNumericQuestion

# The nine levels ``prompts/forecaster.md`` prints under "## Numeric schema", in its
# order. Transcribed rather than parsed at import: the prompt is a hashed artifact and
# reading it here would put a file read on the replay path. ``tests/unit/
# test_forecast_numeric.py::test_the_declared_levels_are_the_prompts_own`` parses the
# prompt's fenced JSON and asserts the two agree, so a prompt edit that changes the
# levels without changing this tuple fails CI rather than drifting.
DECLARED_PERCENTILE_LEVELS: tuple[float, ...] = (
    0.01,
    0.05,
    0.10,
    0.25,
    0.50,
    0.75,
    0.90,
    0.95,
    0.99,
)

# The one field path these problems are reported against, spelled as ``schema._sanitize``
# would spell it. Every problem names the list rather than an index: the rules are about
# the sequence as a whole, and an index is a fact about the model's output.
_PERCENTILES_LOC = "final_prediction.percentiles"

# The three bound rules, stated as constants so no call site can interpolate a question
# field by accident. Each names the field of the question it is about -- ``lower_bound``,
# ``upper_bound``, ``zero_point`` are names this project's canonical model authored, and
# the model was sent all three under the numeric fields of its own request -- and none
# names a number. "offending input withheld" is said of the model's value, which is the
# one thing a reader might otherwise expect to see here.
_BELOW_CLOSED_LOWER = (
    "every value must be at or above the question's lower_bound, which is closed "
    "for this question (offending input withheld)"
)
_ABOVE_CLOSED_UPPER = (
    "every value must be at or below the question's upper_bound, which is closed "
    "for this question (offending input withheld)"
)
_BELOW_ZERO_POINT = (
    "every value must be at or above the question's zero_point (offending input withheld)"
)


class NumericOutputError(ForecastSchemaError):
    """A numeric forecast cannot be used, or was asked for in a way this module refuses.

    Subclasses :class:`ForecastSchemaError` for the reason ``BinaryOutputError`` argues at
    length and M1-403 round 1 settled: a caller that handles the forecast package's
    response failures as one type keeps working unchanged, while a caller that wants
    *this* module's boundary can name it without importing ``forecast.schema``.

    Carries the same sanitized ``problems`` list as its parent: a field path, a colon and
    a value-free message. Nothing here echoes model output.
    """


# The declared levels, rendered once. ``repr`` is ``binary._format_bound``'s rule and its
# reason survives here even though the bounds no longer use it: it is the shortest string
# that round-trips, so a level the model is asked for is exactly a level it is checked
# against. A truncated ``0.010`` would be a level the model could aim at and still miss.
# ``test_the_rendered_levels_round_trip_back_to_the_levels_exactly`` pins that by parsing
# the tokens back rather than by matching ``repr``'s output, which would agree with any
# renderer that happened to be ``repr`` -- including a wrong one.
_DECLARED_LEVELS_TEXT = ", ".join(repr(level) for level in DECLARED_PERCENTILE_LEVELS)


def _levels_problem(forecast: NumericForecastResponse) -> str | None:
    """The declared levels, compared as one sequence.

    Exact float equality, deliberately. Every level is a decimal literal in the prompt and
    arrives through ``json.loads``, which maps a literal to the nearest double; ``0.10``,
    ``0.1`` and ``1e-1`` are therefore the *same* double, and a tolerance would only widen
    the rule to levels the prompt does not print. A model that emits ``0.11`` is asked to
    fix it, which is the whole point of a repair turn.

    Comparing the tuple rather than the set also settles ordering, count and duplication in
    one step -- and it is what makes :func:`_ordering_problem` well defined, since "values
    are non-decreasing" is only a rule once the levels they belong to are known to ascend.
    """
    levels = tuple(point.percentile for point in forecast.final_prediction.percentiles)
    if levels == DECLARED_PERCENTILE_LEVELS:
        return None
    return (
        f"{_PERCENTILES_LOC}: must be exactly the "
        f"{len(DECLARED_PERCENTILE_LEVELS)} declared levels "
        f"{_DECLARED_LEVELS_TEXT} in ascending order, each once (offending input withheld)"
    )


def _ordering_problem(forecast: NumericForecastResponse) -> str | None:
    """Non-decreasing, not strictly increasing -- and the difference is a decision.

    ``prompts/forecaster.md`` states the rule twice and says "non-decreasing" both times,
    so a reply with two equal values followed the prompt exactly. The backlog row says
    "ordered", and CLAUDE.md's stricter-reading rule would ordinarily resolve that
    ambiguity upward; it is not applied here because the stricter reading contradicts the
    prompt rather than sharpening it, and refusing a compliant reply spends the one budgeted
    repair call on the project's own inconsistency. ``schema.py`` makes the same argument
    about ``source_disagreements``.

    The pinned SDK agrees: ``NumericDistribution._check_percentiles_increasing`` raises only
    on ``value[i] > value[i + 1]``, whatever its message says. What it then does with a tie
    -- ``_check_and_update_repeating_values`` nudges repeated values by 1e-6 in place -- sits
    against this project's "nothing is clamped" rule and is filed for M1-503, which owns the
    conversion where it happens.
    """
    values = [point.value for point in forecast.final_prediction.percentiles]
    if all(first <= second for first, second in pairwise(values)):
        return None
    return f"{_PERCENTILES_LOC}: values must be non-decreasing (offending input withheld)"


def _bound_problems(
    forecast: NumericForecastResponse, question: CanonicalNumericQuestion
) -> list[str]:
    """Closed bounds and the zero point; an open bound constrains nothing here.

    Both bound comparisons are inclusive, which is the SDK's own reading of a closed bound:
    ``get_cdf``'s docstring puts ``cdf[0] = P(outcome < lower_bound)`` and
    ``cdf[-1] = P(outcome <= upper_bound)``, so a value sitting exactly on either bound is
    representable and is not an error. The zero-point comparison is inclusive for the same
    reason -- ``_check_log_scaled_fields`` raises on ``value < zero_point``.
    """
    values = [point.value for point in forecast.final_prediction.percentiles]
    problems: list[str] = []
    if not question.open_lower_bound and any(value < question.lower_bound for value in values):
        problems.append(f"{_PERCENTILES_LOC}: {_BELOW_CLOSED_LOWER}")
    if not question.open_upper_bound and any(value > question.upper_bound for value in values):
        problems.append(f"{_PERCENTILES_LOC}: {_ABOVE_CLOSED_UPPER}")
    zero_point = question.zero_point
    if zero_point is not None and any(value < zero_point for value in values):
        problems.append(f"{_PERCENTILES_LOC}: {_BELOW_ZERO_POINT}")
    return problems


def _require_question(question: CanonicalNumericQuestion) -> None:
    """Refuse a question no percentile set could satisfy, before it becomes a repair turn.

    ``binary._require_config``'s precedent, for its reason: a value object carries no memory
    of which validator built it, and an unsatisfiable one would fail *every* forecast through
    the repair loop -- two billed calls per question to reject something no model could have
    supplied. ``forecast.generate`` repeats this check before anything is spent.

    ``lower_bound < upper_bound`` is guaranteed by ``CanonicalNumericQuestion`` and is not
    restated; a ``zero_point`` at or above ``lower_bound`` is **not** guaranteed, and the
    pinned SDK refuses it outright (``_check_log_scaled_fields``: "Lower bound must be
    greater than the zero point"), so a question carrying one cannot produce a submittable
    distribution however the percentiles are drawn.

    **This raise is reachable from a stored record, and round 1 found where.** The canonical
    model accepts ``zero_point == lower_bound``, so does ``ForecastRecordDraft``, and so did
    every writer before this item existed -- numeric had no checker at the pinned base. A
    replay of such a record reaches here through ``parse._parse``, which is why
    ``forecast/replay.py`` now translates a ``ForecastSchemaError`` out of that call into
    its own ``ForecastRecordError``, exactly as it already did for ``response_model_for``.
    The raise stays a raise: it is not a problem the model can repair, and turning it into
    one would ask a model to fix a question.
    """
    if not isinstance(question, CanonicalNumericQuestion):
        raise NumericOutputError(["question: must be a canonical numeric question"])
    if question.zero_point is not None and question.lower_bound <= question.zero_point:
        # Both numbers are question data and are rendered elsewhere in this module; here
        # they buy nothing the message does not already say.
        raise NumericOutputError(
            ["question: zero_point must be strictly below lower_bound for a log-scaled question"]
        )


def numeric_output_problems(
    forecast: NumericForecastResponse,
    forecast_config: ForecastConfig,
    question: CanonicalNumericQuestion,
) -> list[str]:
    """Every declared-level, ordering and bound problem with one numeric response.

    An empty list means the response is usable as a numeric forecast at every level of the
    stack that does not depend on ``numeric_calibration``; M1-503 applies the rest when it
    builds the CDF. Each string is a schema-authored field path, a colon, and a value-free
    message -- safe to log, to store, and to send back to the model as a repair turn.

    ``forecast_config`` is accepted and not read, and that is stated rather than hidden.
    Every registered checker takes the same three arguments so that ``validate._TYPE_CHECKERS``
    stays one flat table with no per-type adapter; this type simply has no rule that a
    ``ForecastConfig`` decides -- ``min_probability``/``max_probability`` are about a
    probability, and a percentile value is not one. It is still exact-type gated, because a
    caller passing the wrong object here is a caller mistake wherever it is noticed.

    The order is stable and is the order the rules are stated in the module docstring:
    levels, then ordering, then bounds. A response with the wrong levels still gets its
    ordering and bound problems reported -- the complete account in one repair turn is worth
    more than the second billed call a staged check would cost.

    Raises :class:`NumericOutputError` only for a caller mistake (a response, a config or a
    question of the wrong type, or a question no percentile set could satisfy). Those are not
    problems with the model's output and must never become a repair turn.
    """
    if not isinstance(forecast, NumericForecastResponse):
        # Exact category, not a duck-typed read: a response of another question type has no
        # percentiles, and a non-response has no fields at all. Both are caller mistakes,
        # and neither is something to ask the model to fix.
        raise NumericOutputError(["forecast: must be a numeric forecast response"])
    if not isinstance(forecast_config, ForecastConfig):
        raise NumericOutputError(["forecast_config: must be a ForecastConfig"])
    _require_question(question)

    problems: list[str] = []
    levels = _levels_problem(forecast)
    if levels is not None:
        problems.append(levels)
    ordering = _ordering_problem(forecast)
    if ordering is not None:
        problems.append(ordering)
    problems.extend(_bound_problems(forecast, question))
    return problems


def validate_numeric_output(
    forecast: NumericForecastResponse,
    forecast_config: ForecastConfig,
    question: CanonicalNumericQuestion,
) -> NumericForecastResponse:
    """Return the response unchanged, or raise with the sanitized problems.

    The entry point for a caller holding a response it cannot repair -- a replay, or a
    validation pass over a stored record. ``forecast.parse`` uses
    :func:`numeric_output_problems` instead, because inside the attempt loop a problem is a
    repair turn rather than an error.

    Nothing is clamped, sorted, truncated or padded. ``prompts/forecaster.md`` says "do not
    clamp mechanically" and M1-502's criterion is that "no arbitrary post-hoc
    renormalization is hidden"; a percentile list this project reordered or pulled inside a
    bound is a distribution the ledger cannot attribute to the model.
    """
    problems = numeric_output_problems(forecast, forecast_config, question)
    if problems:
        raise NumericOutputError(problems)
    return forecast
