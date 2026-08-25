"""The SDK-free half of one forecast call: the response contract and the parse (M1-406).

Every name here was :mod:`whiskeyjack_bot.forecast.generate`'s and still behaves exactly as
it did; this module is a **move**, not a rewrite. What changed is what importing it costs.

``generate.py`` imports ``GeneralLlm`` and, through it, litellm. M1-406's acceptance
criterion is *"model replay makes zero API calls and reproduces the parsed forecast hash"*,
and M1-306 settled that **zero-calls is a property of the import graph**, not of a mock
count -- ``tests/unit/test_forecast_generate.py::test_the_response_schema_reaches_no_provider_client``
is that property, asserted. A replay path has to run the *identical* parse the generating
call ran, or it verifies a different function than the one that produced the record; but
reaching that parse inside ``generate.py`` would pull a provider client into the replay
process. Splitting the file is what lets both be true at once.

So the rule for this module is one line: **nothing here may import a provider SDK, an HTTP
client, or anything that reaches one.** ``forecast.schema``, ``forecast.binary``,
``forecast.attribution`` and ``forecast.inputs`` are all clean today and the import-graph
test pins this module alongside them.

:class:`ModelSettings` and :class:`ForecastGeneration` moved for the same reason: replay
reconstructs a ``ForecastGeneration`` from a stored artifact and hands it to
``forecast.record.build_forecast_record_draft``, and a value object that cannot be built
without the SDK is not a value object a replay can use.

**This is adjacent to M1-506 but is not it.** ``_output_problems`` stays private and
uncomposed; M1-506's row is a *public* composed output-validation entry point that
``generate`` defines itself in terms of. Moving a private function does not close that.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from whiskeyjack_bot.config import ForecastConfig
from whiskeyjack_bot.forecast.attribution import attribution_problems
from whiskeyjack_bot.forecast.binary import binary_output_problems
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    ForecastSchemaError,
    validate_forecast_response,
)
from whiskeyjack_bot.lifecycle import PreForecastFailureCode


# Used when the previous reply was not one JSON object at all. Deliberately a
# constant: a JSONDecodeError's text is positional rather than quoting content today,
# but that is a property of the stdlib's current wording and not a contract.
_NOT_JSON = "the reply was not a single JSON object"

# Markdown fences the model may wrap its JSON in despite being told not to. The
# package's own strip_code_block_markdown only fires when the string both starts and
# ends with a fence; this is the same idea without entering that module.
_FENCES = ("```json", "```JSON", "```")


@dataclass(frozen=True)
class ModelSettings:
    """What the call was actually made with, for M1-406 to persist.

    Built from config and the loaded prompt, never from ``GeneralLlm.to_dict()``,
    which dumps the API key verbatim.
    """

    provider: str
    name: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: float
    allowed_tries: int
    prompt_version: str
    prompt_sha256: str


@dataclass(frozen=True)
class ForecastGeneration:
    """The outcome of one forecast attempt, successful or not.

    ``forecast`` is ``None`` exactly when ``failure_code`` is set. Both the request
    and every response are carried so M1-406 can persist and replay them, and both are
    ``repr=False`` for the reason ``LoadedPrompt.text`` is: the default dataclass repr
    would print a whole research packet and a whole model response through any log
    line or frame-capturing traceback.
    """

    forecast: ForecastResponse | None
    settings: ModelSettings
    sources: tuple[SourceReference, ...]
    request: str = field(repr=False)
    raw_responses: tuple[str, ...] = field(repr=False)
    invocations: int
    repair_attempted: bool
    cost_usd: float | None
    failure_code: PreForecastFailureCode | None
    # The sanitized problem list from forecast.schema: field paths and validator
    # messages, safe to log and to store. Empty on success.
    failure_problems: tuple[str, ...]


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    for fence in _FENCES:
        if stripped.startswith(fence) and stripped.endswith("```") and len(stripped) > len(fence):
            return stripped[len(fence) :].removesuffix("```").strip()
    return stripped


def _output_problems(
    forecast: ForecastResponse,
    forecast_config: ForecastConfig,
    *,
    question_id: int,
    source_ids: Sequence[str],
) -> list[str]:
    """The config-, question- and input-dependent checks ``forecast.schema`` omits.

    Two layers. **M1-501's cross-type attribution rules run first**, and they apply to
    every question type: the fields that row names, plus every citation resolved against
    the ids ``forecast.inputs`` actually minted for this packet.

    Then the type-specific layer, keyed on the ``question_type`` literal and never
    ``isinstance``: ``DiscreteQuestion`` subclasses ``NumericQuestion`` in the pinned
    SDK, and this project has the rule because dispatching the other way silently
    forecasts an unsupported type as numeric.

    Only ``binary`` has type-specific checks today. The multiple-choice option set is
    **M1-404's** stated criterion and the numeric percentile levels are **M1-405's**;
    each adds its branch below, and neither is approximated in the meantime.
    """
    problems = attribution_problems(forecast, question_id=question_id, source_ids=source_ids)
    if forecast.question_type == "binary":
        problems.extend(binary_output_problems(forecast, forecast_config))
    return problems


def _parse(
    text: str,
    model: type[ForecastResponse],
    forecast_config: ForecastConfig,
    *,
    question_id: int,
    source_ids: Sequence[str],
) -> tuple[ForecastResponse | None, list[str]]:
    """Parse and validate one response; returns the forecast or the problems.

    An empty problem list with a ``None`` forecast is impossible: every failure path
    supplies at least one sanitized problem string.

    The configured-bounds and attribution checks run *here*, inside the attempt loop,
    rather than on the returned result. That is what makes an out-of-bounds probability
    or an unresolvable citation repairable at the cost of the second call this module
    already budgets, instead of a billed call thrown away -- and their problems are the
    same sanitized shape as the schema's, so the repair turn and the failure
    classification below need no special case for them.
    """
    try:
        payload = json.loads(_strip_fences(text))
    except Exception:
        # Broad and scoped to the one call, the M1-308 round-7 rule: json.loads
        # raises more than JSONDecodeError once the input is not a str.
        return None, [_NOT_JSON]
    if not isinstance(payload, dict):
        return None, [_NOT_JSON]
    try:
        forecast = validate_forecast_response(payload, model)
    except ForecastSchemaError as exc:
        return None, list(exc.problems)
    # Cannot raise: the response is provably the model this dispatch selected,
    # ``generate_forecast`` refuses an inverted bounds pair before anything is spent,
    # and it exact-type gates ``question_id`` there too -- so neither checker can meet
    # the caller mistakes each of them refuses.
    problems = _output_problems(
        forecast, forecast_config, question_id=question_id, source_ids=source_ids
    )
    if problems:
        return None, problems
    return forecast, []


def _classify(problems: list[str]) -> PreForecastFailureCode:
    """``malformed_response`` when the reply was not JSON, else ``schema_invalid``."""
    return "malformed_response" if problems == [_NOT_JSON] else "schema_invalid"
