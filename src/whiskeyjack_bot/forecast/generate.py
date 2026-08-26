"""One structured call to the configured forecaster model (M1-402, M1-403, M1-501).

**M1-403 added the config-dependent output checks to the parse step** rather than to the
returned result. ``forecast/binary.py`` owns the rules; ``forecast/validate.py``'s
``output_problems`` dispatches to them on the ``question_type`` literal (M1-506 made that
composition public; it was ``generate._output_problems``, then briefly
``parse._output_problems``), and because their problems are the same sanitized
shape the schema produces, the repair loop, the failure classification and the invocation
accounting below are unchanged. The effect is that an out-of-bounds probability costs the
one repair this module already budgets instead of throwing a billed call away.

**M1-501 added the cross-type attribution checks to the same step**, for the same reason
and at the same cost. ``forecast/attribution.py`` owns those rules; they need the
``question_id`` this call was made for and the ``src-NNN`` ids ``forecast.inputs`` minted
over this packet, both of which this module already holds, so the only new work here is
handing them down. A forecast citing a document that was never supplied is the one shape
of bad output that is invisible at every later layer -- it parses, it is in bounds, and
only the mapping this function is holding can tell.

The acceptance criterion is short and the pinned package cannot meet it: *"valid
response returns typed output; malformed output gets at most one bounded repair
attempt."* Everything below follows from that sentence and from what
``forecasting-tools==0.2.92`` actually does, which was read and executed rather than
assumed (CLAUDE.md: if spec and observed package behaviour conflict, stop and ask).

**Why not ``GeneralLlm.invoke_and_return_verified_type``**, the obvious entry point:

- *It is not a repair loop.* On a parse failure it re-sends the **identical** prompt
  (``util/misc.py::try_function_till_tries_run_out``). Nothing in 0.2.92 shows the
  model its own malformed output and asks for a correction, so the criterion's
  "repair" does not exist in the package and has to live here.
- *Its two retry layers multiply.* The constructor's ``allowed_tries`` (tenacity,
  5-60s backoff, retrying **any** exception raised inside the call) composes with
  ``allowed_invoke_tries_for_failed_output``: at the package defaults one logical
  request is up to **four** billable calls. Measured, not inferred.
- *It discards the raw response.* M1-406 must persist the provider response and
  replay from it; a helper that returns only the parsed object throws away the
  artifact the ledger needs.
- *Its failure messages echo everything.* ``outputs_text.py`` raises with the full
  model output **and the full input prompt** in the text, and logs both at WARNING
  before raising. Under this project's error-hygiene rule that is a leak channel, and
  the cheapest way to close it is not to enter that module at all.

So this module calls ``invoke()``, which returns raw text, and owns the parse, the
repair and the accounting. The SDK's ``allowed_tries`` is pinned to **1**: that layer
retries on any exception, including a timeout *after* generation, which re-bills, and
``research/transport.py`` already settled that only connection failures may be
retried -- "a request that reached the server is never re-sent and so cannot be billed
twice". A layer that cannot make that distinction is switched off rather than trusted.

**``model.allowed_tries`` is the total number of model invocations for one forecast**,
bounded at ``config.MAX_MODEL_INVOCATIONS``. ``repairs_allowed = allowed_tries - 1``,
so the committed default of 2 is one call plus at most one repair -- exactly the
criterion -- and 1 means no repair at all.

The bound is **unconditional**, and the first cut of this module got that wrong: it
honoured whatever the field held, so `allowed_tries: 5` bought four repairs and the
criterion held only at the committed default. Review round 1 reproduced it, and it is
worth recording why the looser reading was tempting and still wrong. A bound any
config can lift is not a bound; the criterion exists to cap what a malformed response
can cost, and this field's *name* is the footgun that makes the cap load-bearing --
`GeneralLlm`'s constructor parameter of the same name means transport retries, so an
operator reading "5" as "retry the network five times" would instead buy five billed
model calls. The refusal lives in ``ModelConfig`` so it fails at load and at
``verify-env``, and is repeated here so it also fails for an ``AppConfig`` assembled
some other way. Nothing is silently clamped.

**A provider failure is not repaired.** A repair repairs *output*; re-issuing a call
that raised is the transport retry deliberately disabled above. So an exception from
the provider ends the attempt at one invocation, and this module never re-bills after
one.

**Failure is data, not an exception** -- the M1-302/M1-303 rule. A repair means two
billed calls, and raising would discard the record of both. Caller mistakes *do*
raise, all of them before any billable call, so the two categories stay separable:
:class:`ForecastGenerationError` and ``MissingCredentialError`` always mean nothing was
spent.

**Nothing here writes to the ledger.** ``generate_forecast`` returns a value, the way
M1-203's ``DeferralEvent`` is an in-process value rather than a row. Two reasons: there
is no production caller yet, and ``004_pipeline_failure_events.sql`` refuses a failure
row under an ``attempt_id`` that later produced a successful ``forecast_records`` row --
so a first response that a repair then fixes has nowhere to go as an event. The result
carries a ``failure_code`` already in ``lifecycle.PreForecastFailureCode``'s vocabulary,
so a caller writes ``generation_failed`` without re-deriving it.

Verified against ``forecasting-tools==0.2.92`` on 2026-08-19: ``GeneralLlm.__init__``
names only ``model``, ``responses_api``, ``allowed_tries``, ``temperature``,
``timeout``, ``pass_through_unknown_kwargs`` and ``populate_citations``; everything
else (``max_tokens``, ``api_key``) rides in ``**kwargs`` to litellm. Because
``pass_through_unknown_kwargs`` defaults to ``True`` a misspelled kwarg is accepted
and silently dropped, so it is passed **False** here. ``GeneralLlm.to_dict()`` is
never called: it dumps ``litellm_kwargs`` wholesale, API key included, and the package
contains no redaction anywhere -- the model settings recorded for M1-406 are built
from config instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from math import isfinite
from typing import Any, Protocol

from forecasting_tools.ai_models.general_llm import GeneralLlm
from forecasting_tools.ai_models.resource_managers.monetary_cost_manager import (
    MonetaryCostManager,
)

from whiskeyjack_bot.config import MAX_MODEL_INVOCATIONS, AppConfig, ForecastConfig
from whiskeyjack_bot.forecast.inputs import (
    ForecastInputError,
    ModelInput,
    SourceReference,
    build_model_input,
    render_model_input,
)

# The parse path and the two value objects it produces live in `forecast.parse`, which
# imports no provider SDK, so M1-406's replay can run the *identical* parse without pulling
# a client into the process. Only what this module actually uses is imported back --
# `forecast.parse` is the real home and every other consumer names it directly, rather than
# this module becoming a shim that quietly keeps the old coupling readable.
from whiskeyjack_bot.forecast.parse import (
    ForecastGeneration,
    ModelSettings,
    _classify,
    _parse,
)
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    ForecastSchemaError,
    response_model_for,
)
from whiskeyjack_bot.lifecycle import PreForecastFailureCode
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.prompt import LoadedPrompt
from whiskeyjack_bot.questions.model import CanonicalQuestion, _CanonicalQuestionBase
from whiskeyjack_bot.research.packet import ResearchPacket

_LOGGER = logging.getLogger(__name__)

# What the repair turn tells the model. Constant text plus the *sanitized* problem
# list from forecast.schema -- field paths and validator messages, never a value.
# Sending the model its own previous output back is not a leak (the provider produced
# it), but the same strings must never reach a log, an exception or the ledger
# unsanitized, and those are two different destinations with two different rules.
_REPAIR_PREAMBLE = (
    "Your previous reply could not be used. Return one corrected JSON object and "
    "nothing else: no Markdown fences, no commentary, no trailing text. Keep every "
    "field required by the schema for this question type. The problems were:"
)


class ForecastGenerationError(Exception):
    """A forecast was requested in a way this module refuses to attempt.

    Covers the caller-side mistakes that must never be papered over: a prompt that
    does not match the configured version, a packet belonging to another question, an
    unsupported question type, a client built for a different model, and a call made
    from inside a running event loop. Same hygiene rule as ``ConfigError`` and
    ``ExaFallbackError``: the message is a constant and never echoes the offending
    value.

    Every raise happens before any billable call, so this exception always means
    nothing was spent. Provider and model failures are reported as data on
    :class:`ForecastGeneration` instead.
    """


class Forecaster(Protocol):
    """The slice of ``GeneralLlm`` this module uses.

    Narrow on purpose. A test double implements two members instead of subclassing a
    class whose constructor reaches for litellm, and the ``model`` attribute is part
    of the protocol because :func:`generate_forecast` checks it -- see
    ``_require_forecaster``.
    """

    model: str

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str: ...


def _require_forecaster(client: Forecaster, config: AppConfig) -> None:
    """Refuse a client that would call a different model than config names.

    The ``research/exa.py`` rule, and for the same reason: a client carries no memory
    of which config built it, so the check has to be repeated at the point that
    actually spends money rather than trusted from the builder.
    ``forecast_records.model_name`` and ``model_provider`` are NOT NULL columns; a
    client pointed elsewhere writes an attribution claim the call contradicts.
    """
    model = getattr(client, "model", None)
    if type(model) is not str or model != config.model.name:
        raise ForecastGenerationError(
            "the supplied client does not address the configured model; refusing to "
            "record a forecast against a model it was not produced by "
            "(offending input withheld)"
        )


def build_forecaster_client(config: AppConfig) -> GeneralLlm:
    """Construct the one configured forecaster client.

    Raises ``MissingCredentialError`` when the configured variable is unset or empty,
    before the client exists and therefore before any network use. The key is passed
    explicitly rather than left to litellm's own environment lookup, which reads a
    fixed variable name per provider and would ignore ``model.api_key_env`` entirely.
    """
    _require_provider_matches_model(config)
    api_key = os.environ.get(config.model.api_key_env)
    if not api_key:
        raise MissingCredentialError(config.model.api_key_env)
    return GeneralLlm(
        model=config.model.name,
        temperature=config.model.temperature,
        timeout=config.model.timeout_seconds,
        # Pinned to 1: this module counts invocations, and the SDK's layer would
        # multiply with the repair loop below. See the module docstring.
        allowed_tries=1,
        max_tokens=config.model.max_output_tokens,
        api_key=api_key,
        # False so a misspelled kwarg fails here instead of being forwarded and
        # silently dropped by litellm (docs/LESSONS.md #7: fail closed).
        pass_through_unknown_kwargs=False,
    )


def _require_provider_matches_model(config: AppConfig) -> None:
    """Refuse a config whose provider contradicts its model name.

    ``model.name`` is already the full LiteLLM string ("openrouter/some-model"), so
    ``model.provider`` is recorded rather than composed -- composing would produce
    "openrouter/openrouter/...". That leaves the two free to disagree, and
    ``forecast_records.model_provider`` is a NOT NULL column, so a config claiming
    ``openrouter`` while calling ``anthropic/...`` writes a false attribution.

    Only a self-contradiction is refused. Nothing here enumerates litellm's prefixes:
    a bare model name with no slash carries no claim to check.
    """
    name = config.model.name
    if "/" not in name:
        return
    if name.split("/", 1)[0] != config.model.provider:
        raise ForecastGenerationError(
            "model.provider does not match the prefix of model.name; the ledger "
            "records both and they must agree (offending input withheld)"
        )


def _repair_turn(problems: list[str]) -> str:
    return _REPAIR_PREAMBLE + "".join(f"\n- {problem}" for problem in problems)


async def _invoke_once(client: Forecaster, messages: list[dict[str, str]]) -> tuple[str, float]:
    """One invocation, with its cost read inside the same coroutine.

    ``MonetaryCostManager`` tracks through a ``ContextVar`` and a task copies its
    context at creation, so the manager is entered where the call happens rather than
    in the synchronous wrapper -- that removes any question of propagation across the
    ``asyncio.run`` boundary. ``hard_limit=0`` means *no limit*: wiring
    ``run_limits.max_cost_usd`` in here would be budget enforcement, which is M1-504's
    row, on a branch whose review is about the call seam.
    """
    with MonetaryCostManager() as manager:
        text = await client.invoke(messages)
        usage = manager.current_usage
    return text, usage


def _invoke(client: Forecaster, messages: list[dict[str, str]]) -> tuple[str, float]:
    return asyncio.run(_invoke_once(client, messages))


def _refuse_inside_a_running_loop() -> None:
    """This module's public API is synchronous; say so rather than half-work.

    Nothing else in ``src/`` or ``tests/`` is async, and ``research/exa.py`` cites
    "async/aiohttp in an otherwise synchronous pipeline" as a reason to bypass a
    forecasting-tools helper. Importing ``GeneralLlm`` runs ``nest_asyncio.apply()``,
    which would probably let ``asyncio.run`` succeed inside a running loop anyway --
    but resting correctness on a third-party monkeypatch of the event loop is not a
    thing to do quietly, so the case is refused explicitly instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ForecastGenerationError(
        "generate_forecast is synchronous and cannot be called from inside a running "
        "event loop; call it from a worker thread instead"
    )


def generate_forecast(
    *,
    config: AppConfig,
    question: CanonicalQuestion,
    packet: ResearchPacket,
    prompt: LoadedPrompt,
    tournament_id: str,
    now: datetime,
    client: Forecaster | None = None,
) -> ForecastGeneration:
    """Call the configured model once, with at most one bounded repair.

    Never raises on provider or model failure: those are reported on the returned
    :class:`ForecastGeneration`, because a repair means two billed calls and raising
    would discard the record of both. Raises :class:`ForecastGenerationError` or
    ``MissingCredentialError`` for caller mistakes, always before anything is spent.
    """
    _refuse_inside_a_running_loop()
    if not isinstance(question, _CanonicalQuestionBase):
        raise ForecastGenerationError("question must be a canonical question")
    if type(question.question_id) is not int:
        # Exact-type, not isinstance: an IntEnum satisfies isinstance and would break
        # the %d safety claim on the log line below (M1-303 round 5).
        raise ForecastGenerationError("question_id must be an int")
    if question.qtype not in config.forecast.supported_question_types:
        raise ForecastGenerationError(
            "question type is not in forecast.supported_question_types (offending input withheld)"
        )
    if not isinstance(prompt, LoadedPrompt):
        raise ForecastGenerationError("prompt must be a LoadedPrompt")
    if prompt.version != config.forecast.prompt_version:
        # Both versions are provably bare semver by M1-401's parser, but neither is
        # named here: the mismatch is the fact, and the ledger stores the pair.
        raise ForecastGenerationError(
            "the loaded prompt's version does not match forecast.prompt_version; a "
            "LoadedPrompt carries no memory of which config loaded it"
        )
    if not isinstance(packet, ResearchPacket):
        raise ForecastGenerationError("packet must be a ResearchPacket")
    if packet.question_id != question.question_id:
        raise ForecastGenerationError("packet must belong to the question being forecast")
    if config.model.allowed_tries > MAX_MODEL_INVOCATIONS:
        # ModelConfig already refuses this at load, so reaching here means an
        # AppConfig assembled some other way. Repeated at the spending site for the
        # reason research/exa.py repeats its configuration check there: a config
        # object carries no memory of which validator built it, and this is the
        # bound that decides how much a malformed response can cost.
        raise ForecastGenerationError(
            "model.allowed_tries exceeds the one-repair bound; a malformed response "
            "may cost at most one initial call and one repair"
        )
    if not config.forecast.min_probability < config.forecast.max_probability:
        # ForecastConfig refuses this at load, so reaching here means an AppConfig
        # assembled some other way -- the same reason the bound above is repeated.
        # Left unchecked it would fail every binary forecast through the repair loop:
        # two billed calls per question to reject a probability no model could supply.
        raise ForecastGenerationError(
            "forecast.min_probability is not strictly below forecast.max_probability; "
            "no probability could satisfy the configured bounds"
        )

    response_model = _response_model_or_refuse(question.qtype)
    model_input = _model_input_or_refuse(
        question=question, packet=packet, tournament_id=tournament_id, now=now
    )

    if client is None:
        client = build_forecaster_client(config)
    else:
        # Repeated at the spending site even when this module built the client: the
        # supplied-client path is the one that can disagree with config.
        _require_provider_matches_model(config)
    _require_forecaster(client, config)

    settings = ModelSettings(
        provider=config.model.provider,
        name=config.model.name,
        temperature=config.model.temperature,
        max_output_tokens=config.model.max_output_tokens,
        timeout_seconds=config.model.timeout_seconds,
        allowed_tries=config.model.allowed_tries,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )
    request = render_model_input(model_input)
    # The hashed prompt file is sent verbatim as the system message, and the reasoning
    # packet as the user message. That is a stronger attribution claim than splicing
    # the two into one string: prompt_sha256 is then the digest of exactly the
    # instructions the model was given, with no separator this module invented.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": request},
    ]

    # One log line, before the first call. %d is safe only because question_id was
    # exact-type gated above: given a string, logging fails to interpolate and prints
    # the raw argument to stderr in its own error report -- a value leak by another
    # route (M1-303 round 4).
    _LOGGER.info(
        "forecaster invoked for question %d (type: %s)", question.question_id, question.qtype
    )

    return _run_attempts(
        client=client,
        messages=messages,
        response_model=response_model,
        allowed_tries=config.model.allowed_tries,
        forecast_config=config.forecast,
        settings=settings,
        sources=model_input.sources,
        question_id=question.question_id,
        request=request,
    )


def _response_model_or_refuse(question_type: str) -> type[ForecastResponse]:
    try:
        return response_model_for(question_type)
    except ForecastSchemaError:
        # from None: the schema error is already sanitized, but this module owns the
        # caller-mistake vocabulary and a caller should handle one error type.
        raise ForecastGenerationError(
            "no response model for this question type (offending input withheld)"
        ) from None


def _model_input_or_refuse(
    *, question: CanonicalQuestion, packet: ResearchPacket, tournament_id: str, now: datetime
) -> ModelInput:
    try:
        return build_model_input(
            question=question, packet=packet, tournament_id=tournament_id, as_of=now
        )
    except ForecastInputError:
        # The preflight above covers what these two share; this is the net for the
        # arguments only inputs.py checks (tournament_id, an aware ``now``).
        raise ForecastGenerationError(
            "the reasoning packet could not be built from these arguments "
            "(offending input withheld)"
        ) from None


def _run_attempts(
    *,
    client: Forecaster,
    messages: list[dict[str, str]],
    response_model: type[ForecastResponse],
    allowed_tries: int,
    forecast_config: ForecastConfig,
    settings: ModelSettings,
    sources: tuple[SourceReference, ...],
    question_id: int,
    request: str,
) -> ForecastGeneration:
    """Invoke, parse, and repair once per remaining try. Never raises."""
    # The minted citation ids, read once. ``SourceReference.source_id`` is the ``src-NNN``
    # ``forecast.inputs`` assigned over this packet's documents in ``dedup_key`` order,
    # and it is exactly what the model was shown under ``research_documents``. Mapped
    # here rather than inside ``forecast.attribution``, which must stay free of
    # ``forecast.inputs`` (M1-406's replay path). That module no longer reaches a provider
    # SDK -- see its header -- but the independence is a property of attribution.py's own
    # signature rather than of another package's __init__.py, which is the point.
    source_ids = tuple(reference.source_id for reference in sources)
    raw_responses: list[str] = []
    # The exa.py accounting shape: attempts are counted the moment a call is about to
    # be issued, so one that then raises still counts, and a total is published only
    # when every attempted call reported a usable figure. Anything less is a subtotal,
    # and a subtotal stored in cost_usd looks exactly like a complete one.
    calls_attempted = 0
    calls_with_cost = 0
    cost_total = 0.0
    problems: list[str] = []
    failure_code: PreForecastFailureCode | None = None

    for attempt in range(1, allowed_tries + 1):
        calls_attempted += 1
        try:
            text, usage = _invoke(client, messages)
        except Exception as exc:
            # The exception is never inspected beyond its type: a provider error can
            # quote the request, and the request carries the API key in a header.
            # A provider failure is not repaired -- see the module docstring.
            failure_code = "timeout" if isinstance(exc, TimeoutError) else "provider_error"
            problems = ["the provider call did not complete (detail withheld)"]
            break
        raw_responses.append(text)
        if usage > 0.0 and isfinite(usage):
            calls_with_cost += 1
            cost_total += usage
        forecast, problems = _parse(
            text,
            response_model,
            forecast_config,
            question_id=question_id,
            source_ids=source_ids,
        )
        if forecast is not None:
            return _result(
                forecast=forecast,
                settings=settings,
                sources=sources,
                request=request,
                raw_responses=raw_responses,
                calls_attempted=calls_attempted,
                calls_with_cost=calls_with_cost,
                cost_total=cost_total,
                failure_code=None,
                problems=[],
            )
        failure_code = _classify(problems)
        if attempt < allowed_tries:
            messages = [
                *messages,
                {"role": "assistant", "content": text},
                {"role": "user", "content": _repair_turn(problems)},
            ]

    return _result(
        forecast=None,
        settings=settings,
        sources=sources,
        request=request,
        raw_responses=raw_responses,
        calls_attempted=calls_attempted,
        calls_with_cost=calls_with_cost,
        cost_total=cost_total,
        failure_code=failure_code or "internal_error",
        problems=problems,
    )


def _result(
    *,
    forecast: ForecastResponse | None,
    settings: ModelSettings,
    sources: tuple[SourceReference, ...],
    request: str,
    raw_responses: list[str],
    calls_attempted: int,
    calls_with_cost: int,
    cost_total: float,
    failure_code: PreForecastFailureCode | None,
    problems: list[str],
) -> ForecastGeneration:
    # cost_usd is None for "unknown", never 0.0 for "free". The package coerces an
    # untrackable cost to 0.0, so the two are indistinguishable at the source; the
    # settled rule (M1-303 round 3) is that None means unknown and summing it as zero
    # undercounts run_limits.max_cost_usd on exactly the runs most likely to be
    # retried.
    complete = calls_attempted > 0 and calls_with_cost == calls_attempted
    cost_usd = cost_total if complete and isfinite(cost_total) else None
    return ForecastGeneration(
        forecast=forecast,
        settings=settings,
        sources=sources,
        request=request,
        raw_responses=tuple(raw_responses),
        invocations=calls_attempted,
        repair_attempted=calls_attempted > 1,
        cost_usd=cost_usd,
        failure_code=failure_code,
        failure_problems=tuple(problems),
    )
