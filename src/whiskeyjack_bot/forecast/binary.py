"""The binary output path (M1-403).

``forecast/schema.py`` accepts any structurally valid probability, and says so at the
field: the *configured* bounds ``forecast.min_probability`` and
``forecast.max_probability`` "are M1-403's criterion and are deliberately not applied
here: this module never reads config". This is the module that reads it.

Two rules, and both are binary-specific by construction:

- ``final_prediction.probability_yes`` must lie inside the configured bounds,
  inclusive. Those two fields have shipped in ``ForecastConfig`` since M0 with **no
  consumer anywhere in src/** -- the same condition M1-402 found for
  ``model.allowed_tries`` -- so this item defines what they mean.
- ``base_rate.prior_probability`` and ``model_prior`` must be **supplied**. The prompt
  states only the converse ("if the question is not binary, they must be null"), which
  ``schema.py`` already enforces on the two non-binary responses. Enforcing one
  direction alone leaves binary the single question type where the prior is optional --
  and the prior is what a binary forecast is built from (prompt, Method step 1). M1-402
  weighed this rule and deferred it; the owner has since settled it here.

**The rule lives on the output path rather than in the schema**, and that placement is
the decision rather than an accident. ``test_a_binary_response_may_carry_priors`` pins
that ``schema.py`` accepts both spellings, and M1-402 recorded rejecting the schema-level
version. Layering it here leaves that decision and that test intact instead of reversing
them, and it makes the rule *repairable*: ``forecast.generate`` applies these checks
inside its parse step, so a violation becomes a sanitized problem the existing
one-repair loop feeds back to the model rather than a wasted billable call.

That is also why :func:`binary_output_problems` **returns** problems instead of raising.
Its strings are the shape ``schema._sanitize`` produces -- a schema-authored field path,
a colon, a value-free message -- so ``generate._repair_turn`` renders them uniformly and
``generate._classify`` reads them as ``schema_invalid`` with no change on either side.

**The bound message names the configured numbers; the model's value is withheld.** The
asymmetry is deliberate. A probability is untrusted model output; the bounds are
operator configuration, which is the M1-401 path carve-out's category, and the argument
for rendering them is stronger here than it was there. ``prompts/forecaster.md`` prints
0.001-0.999 to the model as a literal, while config is free to narrow it -- so a repair
turn that does not state the *actual* bound is one **no model can satisfy**, and an
error nobody can act on is its own failure mode. Nothing else in this module renders a
value.

This module owns :class:`BinaryOutputError`, and it **subclasses**
:class:`ForecastSchemaError`. The first cut raised the parent directly, reasoning that
the condition -- *this model response is not acceptable* -- already belonged to that
type and that a second type for one condition worked against the rule it would be
obeying. Review round 1 was right that this reads the rule too narrowly, and the
subclass is why: it is not a choice between the two readings. A caller handling the
forecast package's response failures still writes ``except ForecastSchemaError`` and
still catches every one, ``generate._parse`` is unchanged, and this module nonetheless
has an error boundary a caller can name without importing ``forecast.schema``. The
tension the first cut accepted was not there to accept.

Imports no provider SDK and no question model: like ``forecast/schema.py``, this has to
stay reachable from a replay path (M1-406) with the provider client not importable at
all.
"""

from __future__ import annotations

from collections.abc import Sequence

from whiskeyjack_bot.config import ForecastConfig
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastSchemaError,
)

# The field paths these problems are reported against, spelled as ``schema._sanitize``
# would spell them so the two are indistinguishable in a repair turn. Every one is a
# name this project's schema authored; none is model output.
_PROBABILITY_LOC = "final_prediction.probability_yes"
_PRIOR_LOC = "base_rate.prior_probability"
_MODEL_PRIOR_LOC = "model_prior"


class BinaryOutputError(ForecastSchemaError):
    """A binary forecast cannot be used, or was asked for in a way this module refuses.

    Subclasses :class:`ForecastSchemaError` deliberately, so the two readings of the
    project's error rule are both satisfied rather than traded off. A caller that
    handles the forecast package's response failures as one type keeps working
    unchanged -- ``generate._parse`` is exactly such a caller -- while a caller that
    wants *this* module's boundary can name it without importing ``forecast.schema``.

    Carries the same sanitized ``problems`` list as its parent: a field path, a colon
    and a value-free message. Nothing here echoes model output.
    """


# Said of both priors, so the two problems differ only in their location. The prompt's
# shared-fields example populates both for a binary question, so a model that followed
# the prompt exactly already satisfies this -- the test M1-402 applied to
# ``source_disagreements`` and the reason this is not a rule that fails a compliant reply.
_PRIOR_REQUIRED = "must be supplied for a binary question"


def _format_bound(value: float) -> str:
    """Render one configured bound for the repair turn.

    ``repr`` rather than a fixed precision: it is the shortest string that round-trips,
    so the number the model is shown is exactly the number it is checked against. A
    truncated ``0.0010`` would be a bound the model could aim at and still miss.
    """
    return repr(value)


def _require_config(forecast_config: ForecastConfig) -> tuple[float, float]:
    """Return the configured bounds, refusing a config that admits no probability.

    ``ForecastConfig`` already refuses ``min >= max`` at load. Repeated here for the
    reason ``research/exa.py`` and ``forecast/generate.py`` repeat their own checks: a
    config object carries no memory of which validator built it, and an inverted pair
    would fail *every* forecast through the repair loop -- two billed calls per question
    to reject something no model could have supplied.
    """
    if not isinstance(forecast_config, ForecastConfig):
        raise BinaryOutputError(["forecast_config: must be a ForecastConfig"])
    low = forecast_config.min_probability
    high = forecast_config.max_probability
    if not low < high:
        # The two values are operator configuration and are rendered elsewhere in this
        # module; here they buy nothing the message does not already say.
        raise BinaryOutputError(
            ["forecast_config: min_probability must be strictly below max_probability"]
        )
    return low, high


def binary_output_problems(
    forecast: BinaryForecastResponse,
    forecast_config: ForecastConfig,
    *,
    options: Sequence[str] | None = None,
) -> list[str]:
    """Every configured-bounds and binary-prior problem with one binary response.

    An empty list means the response is usable as a binary forecast. Each string is a
    schema-authored field path, a colon, and a value-free message -- safe to log, to
    store, and to send back to the model as a repair turn.

    ``options`` is **accepted and never read**. It exists so this function satisfies
    ``validate._TypeChecker``, the uniform signature M1-404 gave the type-specific
    dispatch table when the multiple-choice rule turned out to need the question's option
    list. A binary question has no option list, and ``validate.output_problems`` pairs the
    argument with the question type in both directions, so the only value that reaches
    this parameter through the entry point is ``None``. It is not validated here for that
    reason: a rule about an argument this function has no opinion on belongs to the layer
    that does.

    Raises :class:`BinaryOutputError` only for a caller mistake (a response or a config
    of the wrong type, or a config admitting no probability at all). Those are not
    problems with the model's output and must never become a repair turn.
    """
    if not isinstance(forecast, BinaryForecastResponse):
        # Exact category, not a duck-typed read: a response of another question type has
        # no probability_yes, and a non-response has no fields at all. Both are caller
        # mistakes, and neither is something to ask the model to fix.
        raise BinaryOutputError(["forecast: must be a binary forecast response"])
    low, high = _require_config(forecast_config)

    problems: list[str] = []
    probability = forecast.final_prediction.probability_yes
    # Inclusive on both ends, the prompt's own wording ("must be between 0.001 and 0.999
    # inclusive"). ``Probability`` already guarantees a finite value in [0, 1], so these
    # comparisons cannot meet a NaN.
    if not low <= probability <= high:
        problems.append(
            f"{_PROBABILITY_LOC}: must be between {_format_bound(low)} and "
            f"{_format_bound(high)} inclusive (offending input withheld)"
        )
    if forecast.base_rate.prior_probability is None:
        problems.append(f"{_PRIOR_LOC}: {_PRIOR_REQUIRED}")
    if forecast.model_prior is None:
        problems.append(f"{_MODEL_PRIOR_LOC}: {_PRIOR_REQUIRED}")
    return problems


def validate_binary_output(
    forecast: BinaryForecastResponse, forecast_config: ForecastConfig
) -> BinaryForecastResponse:
    """Return the response unchanged, or raise with the sanitized problems.

    The entry point for a caller holding a response it cannot repair -- a replay, or a
    validation pass over a stored record. ``forecast.generate`` uses
    :func:`binary_output_problems` instead, because inside the attempt loop a problem is
    a repair turn rather than an error.

    Nothing is clamped. ``prompts/forecaster.md`` says "do not clamp mechanically" and
    M1-502's criterion is that "no arbitrary post-hoc renormalization is hidden"; a
    coerced probability is a number the ledger cannot attribute to the model.
    """
    problems = binary_output_problems(forecast, forecast_config)
    if problems:
        raise BinaryOutputError(problems)
    return forecast
