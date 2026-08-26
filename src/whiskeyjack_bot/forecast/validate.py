"""One composed output-validation entry point for a forecast response (M1-506).

The checks a valid response must pass are split across modules **on purpose**, and that
split is not what this item changes. ``forecast/attribution.py`` owns the cross-type
rules (M1-501); ``forecast/binary.py`` owns the binary-specific ones (M1-403); M1-404 and
M1-405 will own the multiple-choice option set and the numeric percentile levels. Each
rule lives with the type it is a rule about, and each module states why.

What was wrong was the **seam**. ``parse._output_problems`` composed them, but it was
private with a single caller, so every other caller -- ``forecast/store.py`` validating a
record before it is persisted, a replay verifying a stored response -- had to know the
full list and call each member itself, with nothing to tell it when the list grew.

That is not a hypothetical cost. **M1-501's round-1 cross-model review filed a blocking
finding** -- *"binary forecasts are accepted without the required prior"* -- against
``attribution.py``. The rule was in ``binary.py`` and the composed path did refuse the
response; the finding was reproduced by execution and rebutted in round 2, at the price of
a round. A reviewer reading one checker in isolation could not see the rule, and a seam
that misleads a careful reader is a seam a caller will get wrong.

So there is now one public name, :func:`output_problems`, and ``parse`` is **defined in
terms of it** rather than beside it -- the private function is gone, not delegating, so
the two cannot diverge by construction.

**The type-specific layer is a table keyed on the ``question_type`` literal**, mirroring
``schema._RESPONSE_MODELS``. Never ``isinstance``: ``DiscreteQuestion`` subclasses
``NumericQuestion`` in the pinned SDK, so dispatching that way silently validates an
unsupported type as numeric -- a wrong forecast rather than an error, which is the project
gotcha ``questions/normalize.py`` carries the regression test for. Every supported type
holds an **explicit** entry, ``None`` where the checker is not written yet, so
"decided, and there is nothing" is distinguishable from "forgotten" and M1-404/M1-405 each
become one changed line.

Imports no provider SDK and no question model, like ``schema``, ``binary``, ``attribution``
and ``parse``: a replay path (M1-406) and the persist path (M1-507) must both reach this
with the provider client not importable at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from whiskeyjack_bot.config import ForecastConfig
from whiskeyjack_bot.forecast.attribution import attribution_problems
from whiskeyjack_bot.forecast.binary import binary_output_problems
from whiskeyjack_bot.forecast.schema import (
    SUPPORTED_RESPONSE_TYPES,
    ForecastResponse,
    ForecastSchemaError,
)

# Bound to the *union*, not to ``schema.ForecastResponseT``'s broader
# ``_ForecastResponseBase``: ``validate_output`` returns the response it was handed, and a
# caller that passed a ``BinaryForecastResponse`` should get that type back rather than the
# union. The narrower bound is also what lets this file hand the value straight to
# :func:`output_problems` without a cast.
_ResponseT = TypeVar("_ResponseT", bound=ForecastResponse)

# One type-specific checker: a response and the config, returning sanitized problems.
#
# ``Any`` for the response, deliberately and narrowly. ``binary_output_problems`` takes a
# ``BinaryForecastResponse``, so it is not assignable to a ``Callable`` declared over the
# ``ForecastResponse`` union -- parameters are contravariant, and mypy --strict is right to
# say so. The narrowing this table relies on is a *runtime* fact it cannot express: the
# lookup is keyed on ``question_type``, and every checker exact-type gates its own argument
# anyway (``binary.py`` raises ``BinaryOutputError`` for a response of another type), so a
# mis-keyed entry surfaces as that module's own error rather than as a wrong answer. The
# alternative -- a per-type adapter with a narrowing ``isinstance`` -- would put a second
# place per type for M1-404 and M1-405 to edit, which is the cost this shape exists to
# avoid.
_TypeChecker = Callable[[Any, ForecastConfig], list[str]]

# Keyed on the question-type literal, never on isinstance (see the module docstring).
# ``None`` is a decision, not a gap: the type is supported, and it has no type-specific
# checks *yet*. ``tests/unit/test_forecast_validate.py`` fails if a supported type has no
# entry at all, and fails again if a checker exists in this package that no entry reaches.
_TYPE_CHECKERS: dict[str, _TypeChecker | None] = {
    "binary": binary_output_problems,
    # M1-404 registers the option-set checker here; the criterion is exact multiple-choice
    # normalization, and nothing approximates it in the meantime.
    "multiple_choice": None,
    # M1-405 registers the percentile-level checker here.
    "numeric": None,
}

# The vocabulary the message below names is ``schema``'s, reused rather than recomputed
# from ``get_args(SupportedQuestionType)`` a second time. ``schema.response_model_for``
# has exactly this shape -- membership tested against its own table, message drawn from
# the config-derived set -- and sharing the set means the two modules cannot come to
# disagree about what "supported" means while each stays internally consistent.
# ``tests/unit/test_forecast_validate.py`` pins the table's keys against config directly.


class ForecastOutputError(ForecastSchemaError):
    """A forecast response failed the composed output validation.

    Subclasses :class:`ForecastSchemaError` for the reason ``BinaryOutputError`` argues at
    length: a caller that handles this package's response failures as one type keeps
    working unchanged, while a caller that wants *this* boundary can name it without
    importing ``forecast.schema``.

    Carries the same sanitized ``problems`` list its members produce -- a schema-authored
    field path, a colon and a value-free message. Nothing here echoes model output.
    """


def output_problems(
    forecast: ForecastResponse,
    forecast_config: ForecastConfig,
    *,
    question_id: int,
    source_ids: Sequence[str],
) -> list[str]:
    """Every output problem with one response, of any supported question type.

    An empty list means the response passed every output check this project applies: the
    cross-type attribution rules **and** the rules specific to its question type. That is
    the whole point of the name -- a caller gets the complete answer without knowing which
    modules the rules live in, and gains new checkers as they are registered.

    Two layers, in order. **M1-501's cross-type attribution rules run first** and apply to
    every question type: the fields that row names, plus every citation resolved against
    the ids ``forecast.inputs`` actually minted for this packet. Then the type-specific
    layer, looked up on the ``question_type`` literal.

    Each string is a schema-authored field path, a colon, and a value-free message -- safe
    to log, to store, and to send back to the model as a repair turn. The order is stable:
    attribution problems, then type-specific ones.

    Raises only for a caller mistake, never for a problem with the model's output: the
    member checkers raise their own ``ForecastSchemaError`` subclasses for a response or a
    config of the wrong type, and this function raises :class:`ForecastOutputError` for a
    response whose ``question_type`` no entry covers. Those must never become a repair
    turn.
    """
    problems = attribution_problems(forecast, question_id=question_id, source_ids=source_ids)

    # Exact-type gate before the lookup, ``schema.response_model_for``'s rule: a str
    # subclass is unvetted and an unhashable value makes ``dict.get`` raise a raw
    # TypeError. ``question_type`` is a validated Literal on every response model, so this
    # is unreachable through the schema -- and every malformed shape still has to arrive as
    # this module's own error, which is the defect this project has taken as a finding
    # twice.
    question_type = getattr(forecast, "question_type", None)
    if type(question_type) is not str or question_type not in _TYPE_CHECKERS:
        # Membership is tested rather than a ``.get()`` default, because a default would
        # conflate "no entry for this type" with "an entry that is deliberately ``None``"
        # and let an unsupported question type pass every output check in silence -- the
        # exact failure this table exists to prevent. The vocabulary is ours to name; the
        # offending value is not.
        raise ForecastOutputError(
            [
                "question_type: must be one of "
                + ", ".join(sorted(SUPPORTED_RESPONSE_TYPES))
                + " (offending input withheld)"
            ]
        )
    checker = _TYPE_CHECKERS[question_type]
    if checker is not None:
        problems.extend(checker(forecast, forecast_config))
    return problems


def validate_output(
    forecast: _ResponseT,
    forecast_config: ForecastConfig,
    *,
    question_id: int,
    source_ids: Sequence[str],
) -> _ResponseT:
    """Return the response unchanged, or raise with every sanitized problem.

    The entry point for a caller holding a response it cannot repair -- a replay, or a
    validation pass over a stored record before it is persisted (M1-507).
    ``forecast.parse`` uses :func:`output_problems` instead, because inside the attempt
    loop a problem is a repair turn rather than an error.

    **One error carrying the whole list**, rather than letting whichever member checker
    fired first raise its own type. The complete list is the entire account of why the
    response was refused, and it is what an operator needs -- the same argument
    ``store.py::_require_attributable`` makes for carrying ``AttributionFieldError``'s text
    through instead of replacing it with a constant. A caller that wants to distinguish the
    members still can: every problem string names the field it is about.

    Nothing is clamped, repaired, dropped or renumbered. A forecast whose evidence list was
    quietly edited, or whose probability was silently pulled inside the configured bounds,
    is precisely the record the ledger could not stand behind.
    """
    problems = output_problems(
        forecast, forecast_config, question_id=question_id, source_ids=source_ids
    )
    if problems:
        raise ForecastOutputError(problems)
    return forecast
