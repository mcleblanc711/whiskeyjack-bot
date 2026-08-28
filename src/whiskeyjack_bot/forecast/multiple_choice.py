"""The multiple-choice output path (M1-404).

``forecast/schema.py`` accepts any structurally valid option list and says so at the
field: that every supplied option appears exactly once and that the probabilities sum to
1 within ``1e-6`` "is M1-404's acceptance criterion: both need the question's option
list, which this module does not read". This is the module that reads it.

Five rules, and every one of them is multiple-choice-specific by construction. The first
three are the criterion's own words -- *unknown, missing, duplicate* -- and the last two
are the distribution the criterion's "summing to one" describes:

- **unknown**: a label the response names that the question never supplied;
- **missing**: a label the question supplied that the response never names;
- **duplicate**: a label the response names more than once;
- **bounds**: an option probability outside ``forecast.min_probability`` /
  ``forecast.max_probability``, the same two config fields M1-403 gave meaning to;
- **sum**: probabilities that do not total 1 within ``1e-6``.

**Matching is exact string equality, and that is the criterion's word.**
``questions/normalize.py`` hands the SDK's labels through byte-for-byte and
``schema.NonBlankStr`` validates without stripping, so ``" A"`` answered against a
supplied ``"A"`` is one *unknown* plus one *missing* rather than a match. Nothing here
strips, case-folds or Unicode-normalizes: a rule that quietly accepted a near-miss would
make the stored forecast disagree with the option the platform will score, and the
disagreement would be invisible in the ledger.

**Nothing is renormalized.** M1-502's criterion is that "no arbitrary post-hoc
renormalization is hidden" and ``prompts/forecaster.md`` says "do not clamp
mechanically". A vector scaled to sum to 1 is a distribution the model did not produce
and the ledger could not attribute to it, so a bad sum is refused and repaired rather
than fixed up.

**The bounds and the tolerance are rendered; no label, probability or count ever is.**
The asymmetry is M1-403's and M1-501's, applied together. The bounds are operator
configuration and a repair turn that does not state the actual bound is one no model can
satisfy. The labels are the other case entirely: the model is *already holding the option
list* -- ``forecast/inputs.py`` put it in the request under ``options``, which is where
these labels came from -- so naming them back buys nothing and would echo model output.
Each rule contributes **at most one problem**, never one per offending label, because a
per-label list would leak how many were wrong through a channel no leak test that reads
only message text would see (M1-302's rule that a channel is a channel, and
``attribution._citation_problems``' argument for the same shape).

Like ``forecast/binary.py`` this **returns** problems rather than raising, so
``forecast.parse`` can feed them to the existing one-repair loop; and like it, this
module owns :class:`MultipleChoiceOutputError` for the caller mistakes that must never
become a repair turn.

**Imports no provider SDK.** That is the constraint, and it is the one M1-406's replay
path and the persist path (M1-507) rest on: both must reach this module with the provider
client not importable at all.

This used to read "no provider SDK **and no question model**", and took the option list as
bare primitives on that basis. The second half was a stronger claim than the constraint
needs and it was wrong about where the SDK actually enters: ``questions/model.py`` imports
only stdlib, pydantic and ``config``, so it is SDK-free -- it is ``forecast.inputs`` that
reaches a provider, and this module does not import that. M1-405 established the point when
``forecast/numeric.py`` took a ``CanonicalNumericQuestion`` and was added to the
import-graph probe; ``forecast/multiple_choice.py`` is in that probe for the same reason.
Taking the validated question rather than a copy of one of its fields is what retires the
hand-maintained mirror of its invariants -- see :func:`_require_question`.
"""

from __future__ import annotations

import math

from whiskeyjack_bot.config import ForecastConfig
from whiskeyjack_bot.forecast.schema import (
    ForecastSchemaError,
    MultipleChoiceForecastResponse,
)
from whiskeyjack_bot.questions.model import CanonicalMultipleChoiceQuestion

# The field path these problems are reported against, spelled as ``schema._sanitize``
# would spell it. Every rule reports against the list rather than an element: an index
# would name *which* option offended, which is the leak the aggregation exists to avoid.
_OPTIONS_LOC = "final_prediction.options"

# The prompt's own literal, twice over: ``prompts/forecaster.md`` states "sum to 1 within
# ``1e-6``" in both the general rules and the multiple-choice schema section. It is a
# constant rather than config because the criterion names the number, and because a
# tolerance an operator could widen is a tolerance that could accept a vector Metaculus
# will not.
#
# ``submission_live._CATEGORY_SUM_TOLERANCE`` is the same number for a different reason --
# it bounds what this project will *post* -- and the two are deliberately independent
# constants. This module may not import that one: ``submission_live`` reaches the provider
# SDK, and the no-SDK rule in the header is what M1-406's replay path rests on.
_SUM_TOLERANCE = 1e-6

# One message per rule, in the criterion's own order. Each is a constant: the text does
# not vary with the response, so neither the wording nor the *number* of problems can
# carry information about what the model actually said.
_UNKNOWN = "must name only options the question supplied (offending labels withheld)"
_MISSING = "must name every option the question supplied (offending labels withheld)"
_DUPLICATE = "must name each supplied option at most once (offending labels withheld)"


class MultipleChoiceOutputError(ForecastSchemaError):
    """A multiple-choice forecast cannot be used, or was asked for in a refused way.

    Subclasses :class:`ForecastSchemaError` for ``BinaryOutputError``'s stated reason: a
    caller that handles this package's response failures as one type keeps working
    unchanged, while a caller that wants *this* module's boundary can name it without
    importing ``forecast.schema``.

    Carries the same sanitized ``problems`` list as its parent: a field path, a colon and
    a value-free message. Nothing here echoes model output or a supplied label.
    """


def _format_bound(value: float) -> str:
    """Render one configured bound for the repair turn.

    ``repr`` rather than a fixed precision: it is the shortest string that round-trips,
    so the number the model is shown is exactly the number it is checked against.

    Duplicated from ``binary.py`` rather than shared, along with :func:`_require_config`
    below. Every module in this project owns its own sanitized exception, so a shared
    helper would have to be parameterized by the error type it raises -- more machinery
    than the few lines it saves, and a refactor of a merged and reviewed module inside an
    item that is not about it. ``tests/unit/test_forecast_multiple_choice.py`` pins that
    the two copies agree.
    """
    return repr(value)


def _require_config(forecast_config: ForecastConfig) -> tuple[float, float]:
    """Return the configured bounds, refusing a config that admits no probability.

    ``ForecastConfig`` already refuses ``min >= max`` at load; repeated here for
    ``binary._require_config``'s reason, which applies with more force to this type. A
    config object carries no memory of which validator built it, and an inverted pair
    would fail *every* forecast through the repair loop -- two billed calls per question
    to reject something no model could have supplied.
    """
    if not isinstance(forecast_config, ForecastConfig):
        raise MultipleChoiceOutputError(["forecast_config: must be a ForecastConfig"])
    low = forecast_config.min_probability
    high = forecast_config.max_probability
    if not low < high:
        raise MultipleChoiceOutputError(
            ["forecast_config: min_probability must be strictly below max_probability"]
        )
    return low, high


def _require_question(question: CanonicalMultipleChoiceQuestion) -> None:
    """Refuse a question this checker's rules could not be stated against.

    ``numeric._require_question``'s precedent, and its rule about what to restate.
    That function gates the type and re-checks ``zero_point``, which the canonical model
    does *not* guarantee -- and deliberately does **not** restate
    ``lower_bound < upper_bound``, which it does. The same division applies here, and it
    leaves only the type gate: ``CanonicalMultipleChoiceQuestion`` already enforces every
    property these rules need, at the input contract and for this row's sake --
    ``options: list[str] = Field(min_length=2)`` plus a validator refusing a blank or
    repeated label.

    **That is a change in kind, not a relaxation, and it is worth being explicit about.**
    Until M1-405 merged this module was handed the option list as bare primitives with no
    validator behind them, so it re-checked each of those properties by hand: a bare
    ``str`` (which would silently mean its characters -- the M1-303 round-4 defect), a
    non-``str`` member, an empty list, a list of one, a repeated label, a blank label.
    Hand-mirroring another model's invariants is exactly what M1-404's round-1 review
    found a hole in -- five of six were mirrored and the sixth, ``min_length=2``, was not,
    which made the rule set unsatisfiable for a one-option list. Taking the validated
    question instead dissolves that whole class: there is no second list to disagree with
    the first, and no hand-maintained mirror to fall behind the model.
    """
    if not isinstance(question, CanonicalMultipleChoiceQuestion):
        raise MultipleChoiceOutputError(["question: must be a canonical multiple-choice question"])


def multiple_choice_output_problems(
    forecast: MultipleChoiceForecastResponse,
    forecast_config: ForecastConfig,
    question: CanonicalMultipleChoiceQuestion,
) -> list[str]:
    """Every option-set and distribution problem with one multiple-choice response.

        An empty list means the response is usable as a multiple-choice forecast: it names
        every supplied option exactly once, names nothing else, and its probabilities are a
        distribution inside the configured bounds. Each string is a schema-authored field
        path, a colon, and a value-free message -- safe to log, to store, and to send back to
        the model as a repair turn.

    ``question.options`` is the option list, in the question's own order. Order is
        irrelevant to every rule here -- the response may answer in any order, and
        ``test_the_option_verdict_does_not_depend_on_the_order_answered`` states that as a
        property -- so it is read as a set below.

        Raises :class:`MultipleChoiceOutputError` only for a caller mistake -- a response, a
        config or a question of the wrong type, or a config admitting no probability. Those
        are not problems with the model's output and must never become a repair turn.
    """
    if not isinstance(forecast, MultipleChoiceForecastResponse):
        # Exact category, not a duck-typed read: a response of another question type has
        # no option list at all, and a non-response has no fields. Both are caller
        # mistakes, and neither is something to ask the model to fix.
        raise MultipleChoiceOutputError(["forecast: must be a multiple-choice forecast response"])
    _require_question(question)
    low, high = _require_config(forecast_config)
    supplied = frozenset(question.options)

    entries = forecast.final_prediction.options
    answered = [entry.option for entry in entries]
    distinct = frozenset(answered)

    problems: list[str] = []
    if not distinct <= supplied:
        problems.append(f"{_OPTIONS_LOC}: {_UNKNOWN}")
    if not supplied <= distinct:
        problems.append(f"{_OPTIONS_LOC}: {_MISSING}")
    if len(distinct) != len(answered):
        problems.append(f"{_OPTIONS_LOC}: {_DUPLICATE}")
    # Inclusive on both ends, ``binary.py``'s convention and the prompt's own wording.
    # ``Probability`` already guarantees a finite value in [0, 1], so no comparison here
    # can meet a NaN.
    if any(not low <= entry.probability <= high for entry in entries):
        problems.append(
            f"{_OPTIONS_LOC}: each probability must be between {_format_bound(low)} and "
            f"{_format_bound(high)} inclusive (offending input withheld)"
        )
    # ``math.fsum`` rather than ``sum``: it is exact and therefore independent of the
    # order the model happened to answer in. With a naive sum, re-ordering one response's
    # options could move the total across the tolerance, and a verdict that depends on
    # answer order is not one a replay could reproduce.
    total = math.fsum(entry.probability for entry in entries)
    if abs(total - 1.0) > _SUM_TOLERANCE:
        problems.append(
            f"{_OPTIONS_LOC}: probabilities must sum to 1 within {_SUM_TOLERANCE} "
            "(observed sum withheld)"
        )
    return problems


def validate_multiple_choice_output(
    forecast: MultipleChoiceForecastResponse,
    forecast_config: ForecastConfig,
    question: CanonicalMultipleChoiceQuestion,
) -> MultipleChoiceForecastResponse:
    """Return the response unchanged, or raise with the sanitized problems.

    The entry point for a caller holding a response it cannot repair -- a replay, or a
    validation pass over a stored record. ``forecast.parse`` reaches
    :func:`multiple_choice_output_problems` through ``forecast.validate`` instead,
    because inside the attempt loop a problem is a repair turn rather than an error.

    Nothing is clamped, dropped, re-ordered or renormalized. See the module header: a
    rescaled vector is a distribution the model did not produce.
    """
    problems = multiple_choice_output_problems(forecast, forecast_config, question)
    if problems:
        raise MultipleChoiceOutputError(problems)
    return forecast
