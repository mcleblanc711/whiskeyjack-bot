"""Exa fallback retrieval adapter: web/official evidence, and why it was reached (M1-303).

Exa is the *fallback* provider (decision D18): it runs when AskNews fails, when
AskNews returned nothing, or when official-source/web retrieval is required --
never as an unannounced substitute. That last clause is the whole point of this
module's shape, so it is enforced by the API rather than by convention:

- :func:`decide_fallback` is the pure policy. It returns **every** trigger that
  holds, not a winner, because each one is a distinct fact about the run.
- :func:`retrieve_web` **requires** a non-empty ``fallback_reasons`` argument.
  There is no way to spell an Exa call that does not say why it happened.
- The reasons are persisted on the run (``provider_config["fallback_reasons"]``,
  stored in ``research_runs.provider_config_json``) and logged once, so the
  switch is auditable from the ledger alone.
- :func:`build_exa_client` refuses to build a client when the configured
  fallback provider is not ``exa``. Running Exa while config names something
  else *is* the silent switch.

Transport: the Exa HTTP API directly, over ``httpx``.

Why not ``forecasting_tools.ExaSearcher`` (D03 says use the package where it
supports the workflow; here it does not, for the same reasons M1-302 bypassed
``AskNewsSearcher``): it reads ``EXA_API_KEY`` from the environment itself via
``os.getenv`` + ``assert`` rather than from our configured ``api_key_env``; it is
async/aiohttp in an otherwise synchronous pipeline; it discards the raw response
body (which M1-306 must persist to replay) and the ``costDollars`` block (the
only cost figure this pipeline can honestly record); and it parses
``publishedDate`` with ``datetime.fromisoformat(value.strip("Z"))``, producing a
**naive** datetime that this project's schema rejects outright. Why not
``exa-py``: it would add ``openai``, ``requests`` and ``python-dotenv`` as
transitive dependencies for a single POST, and its typed results likewise do not
expose the raw body. ``httpx`` is already a declared direct dependency, so this
adapter adds none.

API contract, verified against ``https://api.exa.ai/openapi.json`` on 2026-07-27:

- ``POST https://api.exa.ai/search``, credential in the ``x-api-key`` header.
- Request: ``query`` (required), ``numResults`` (1-100), ``type`` (``auto`` is
  the default mode), ``startPublishedDate``/``endPublishedDate`` (ISO 8601),
  ``includeDomains``/``excludeDomains``, and ``contents`` with
  ``text.maxCharacters`` (<= 10000) and ``maxAgeHours`` (0-720). ``livecrawl``
  is deprecated in favour of ``maxAgeHours`` and is not sent.
- Response: ``requestId``, ``results[]`` (``title`` and ``url`` required;
  ``publishedDate`` documented as ``YYYY-MM-DD``; ``author``, ``id``, ``text``,
  ``highlights``, ``highlightScores``, ``summary``), and ``costDollars.total``
  -- which Exa documents as an **estimate**, "not an invoice record". It is
  recorded as ``cost_usd`` on that understanding; the raw bodies keep the
  provider's own figures for M1-306.

Content-hash source rule (pinned; changing it changes document identity):
``text`` if non-empty, else the title, else the empty string, always through
:func:`whiskeyjack_bot.research.hashing.content_sha256`. Deliberately **not**
highlights: highlights are generated per query, so one page retrieved by two
queries would hash differently and defeat both the intra-run collapse below and
M1-305's dedup. For the same reason this asks for ``text`` rather than
``highlights``, and pins ``maxCharacters`` so the hashed prefix is bounded.

Error hygiene: as in the AskNews adapter, no string this module produces is
built from provider data, a query, a URL or a credential. Provider exceptions
are **discarded, never inspected or re-raised** -- an ``httpx`` error can quote
the request, and the request carries the API key in a header. Every raise here
uses a constant message.

Failure is data, not an exception: provider failure is reported on the returned
:class:`ExaRetrieval`. The exceptions this module *does* raise --
:class:`ExaFallbackError` and ``MissingCredentialError`` -- are caller mistakes
(an unattributed switch, a misconfigured provider, an absent credential) and all
of them fire before any network use, and therefore before any billing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Final, Literal, get_args

import httpx

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.research.canonical import CanonicalizationError, canonicalize_url
from whiskeyjack_bot.research.hashing import content_sha256
from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    SourceType,
    validate_document,
    validate_run,
)
from whiskeyjack_bot.research.transport import apply_connection_retries

_LOGGER = logging.getLogger(__name__)

# Why a fallback run happened. Closed vocabulary, and the order of the members is
# the canonical order reasons are recorded in -- see _canonical_reasons.
FallbackReason = Literal[
    "primary_provider_failed",
    "primary_returned_no_documents",
    "official_source_required",
]

_FALLBACK_REASONS: Final[tuple[FallbackReason, ...]] = get_args(FallbackReason)

_BASE_URL: Final = "https://api.exa.ai"
_SEARCH_PATH: Final = "/search"

# ``auto`` is Exa's default mode and the only one whose cost is a flat per-search
# charge; the deep/agentic modes bill per reasoning step. A retrieval fallback
# has no business choosing an open-ended budget, so the mode is pinned here
# rather than exposed as configuration.
_SEARCH_TYPE: Final = "auto"

# The hashed prefix of a page. Bounded because ``content_sha256`` is document
# identity: an unbounded body would make the digest depend on how much of a page
# Exa happened to extract on the day. 4000 <= the API's 10000 ceiling.
_TEXT_MAX_CHARACTERS: Final = 4000

# Accept cached content up to a day old rather than forcing a live crawl on every
# result. Live crawling costs more and re-fetches the page at retrieval time,
# which makes the stored text (and so the content hash) depend on the minute the
# run happened -- the opposite of what a replayable ledger wants.
_MAX_AGE_HOURS: Final = 24

# Stored ``snippet`` length. The full text stays in the raw response for M1-306;
# the document row keeps a readable excerpt, not a copy of the page.
_SNIPPET_CHARACTERS: Final = 500


class ExaFallbackError(Exception):
    """A fallback call was requested in a way this module refuses to make.

    Covers the caller-side mistakes that must never be papered over: an Exa call
    with no recorded reason, a reason outside the vocabulary, a malformed domain
    allowlist, and a configuration whose fallback provider is not Exa. Same
    hygiene rule as ``ConfigError``/``ResearchSchemaError``: the message is a
    constant and never echoes the offending value.
    """


@dataclass(frozen=True)
class FallbackDecision:
    """Whether the fallback should run, and every trigger that says so.

    ``reasons`` is a tuple in :data:`_FALLBACK_REASONS` order, so two runs with
    the same triggers persist byte-identical reason lists. It holds *all* the
    triggers rather than the highest-priority one: "AskNews raised" and "AskNews
    returned nothing" are different facts about a run, and collapsing them to a
    single winner discards attribution for no benefit.
    """

    should_run: bool
    reasons: tuple[FallbackReason, ...]


@dataclass(frozen=True)
class ExaRetrieval:
    """One Exa fallback pass over one question's queries.

    Deliberately parallel to :class:`whiskeyjack_bot.research.asknews.AskNewsRetrieval`
    so the two adapters read as one family: ``raw_responses`` is held in memory
    only (persisting it, and the replay contract that implies, is M1-306, so
    ``run.raw_response_path`` and every ``raw_artifact_path`` stay ``None``),
    ``documents_dropped``/``duplicates_collapsed`` are routine bookkeeping rather
    than failure, and ``provider_failed`` is set when a call raised, in which
    case retrieval stopped early and everything already retrieved is still
    returned.

    ``fallback_reasons`` is the same tuple recorded on the run, repeated here so
    a caller holding only this object can still say why the switch happened.
    """

    run: ResearchRun
    documents: tuple[ResearchDocument, ...]
    raw_responses: tuple[dict[str, Any], ...]
    documents_dropped: int
    duplicates_collapsed: int
    provider_failed: bool
    fallback_reasons: tuple[FallbackReason, ...]


def decide_fallback(
    *,
    primary_failed: bool,
    primary_documents: int,
    official_source_required: bool,
) -> FallbackDecision:
    """Decide whether the Exa fallback runs, and name every trigger.

    Pure and total: no I/O, no clock, and the only exception it can raise is
    :class:`ExaFallbackError` for a document count that is not a count.

    The three triggers are exactly the backlog's ("use Exa only when AskNews
    fails or when official-source/web retrieval is required"), with the
    zero-document case separated from the raised-exception case because the
    ledger reads them differently: a provider that answered with nothing has not
    failed, and ``ResearchRun.error_summary`` distinguishes the two.

    ``primary_documents`` is the count of documents the primary provider
    *retained*, not the count it returned.
    """
    if isinstance(primary_documents, bool) or not isinstance(primary_documents, int):
        # Constant message: the value could be anything a caller computed.
        raise ExaFallbackError("primary_documents must be an int (offending input withheld)")
    if primary_documents < 0:
        raise ExaFallbackError("primary_documents must not be negative")

    reasons: list[FallbackReason] = []
    if primary_failed:
        reasons.append("primary_provider_failed")
    if primary_documents == 0:
        reasons.append("primary_returned_no_documents")
    if official_source_required:
        reasons.append("official_source_required")
    ordered = _canonical_reasons(reasons)
    return FallbackDecision(should_run=bool(ordered), reasons=ordered)


def _canonical_reasons(reasons: Sequence[str]) -> tuple[FallbackReason, ...]:
    """Validate reasons and return them deduplicated, in vocabulary order.

    Order and duplicates are normalized rather than preserved because this tuple
    is persisted: two runs triggered by the same facts must produce the same
    stored list regardless of the order a caller assembled them in, or replay
    comparisons turn on caller bookkeeping.
    """
    unknown = [reason for reason in reasons if reason not in _FALLBACK_REASONS]
    if unknown:
        # The offending value is withheld; the vocabulary itself is ours to name.
        raise ExaFallbackError(
            "fallback reason is not in the vocabulary "
            f"({', '.join(_FALLBACK_REASONS)}); offending input withheld"
        )
    present = set(reasons)
    return tuple(reason for reason in _FALLBACK_REASONS if reason in present)


def build_exa_client(config: AppConfig) -> httpx.Client:
    """Construct the one configured Exa client.

    Two refusals, both before any network use and therefore before any billable
    call:

    - the configured fallback provider is not ``exa`` -- calling Exa anyway
      would be precisely the silent provider switch this item forbids;
    - the configured key variable is unset or empty (an empty string counts as
      missing), which raises ``MissingCredentialError``.

    Retries are applied to the connection pool after construction rather than
    through ``transport=``; see
    :func:`whiskeyjack_bot.research.transport.apply_connection_retries` for why
    an explicit transport would silently drop ``HTTP(S)_PROXY`` routing, and for
    the scope of what a retry covers (connection failures only, so a request
    that reached Exa is never re-sent and cannot be billed twice).
    """
    provider = config.retrieval.fallback
    if provider.provider != "exa":
        raise ExaFallbackError(
            "retrieval.fallback.provider is not 'exa'; refusing to run the Exa "
            "adapter against a differently configured fallback (no silent provider switching)"
        )
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise MissingCredentialError(provider.api_key_env)
    client = httpx.Client(
        base_url=_BASE_URL,
        timeout=provider.timeout_seconds,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "x-api-key": api_key,
        },
    )
    apply_connection_retries(client, provider.retries)
    return client


def retrieve_web(
    client: httpx.Client,
    config: AppConfig,
    *,
    question_id: int,
    queries: Sequence[str],
    retrieval_run_id: str,
    now: datetime,
    fallback_reasons: Sequence[FallbackReason],
    include_domains: Sequence[str] = (),
) -> ExaRetrieval:
    """Retrieve web evidence for ``queries`` as normalized documents.

    ``fallback_reasons`` is required and must be non-empty: this adapter cannot
    be invoked without recording why the pipeline left its primary provider.
    Pass :attr:`FallbackDecision.reasons` from :func:`decide_fallback`.

    ``include_domains`` restricts the search to a caller-supplied allowlist. It
    is also what promotes the resulting documents from ``web`` to ``official``:
    a document is only called an official source when retrieval was actually
    constrained to official domains. Tagging on the strength of the *reason*
    instead would let ``official_source_required`` label whatever the open web
    returned, which is an unearned attribution claim.

    ``now`` is injected rather than read from the clock so ``started_at_utc``,
    every ``retrieved_at_utc`` and the published-date bound are deterministic
    under test and under replay.

    **Never raises on provider failure.** A run makes up to
    ``max_queries_per_question`` billable calls; raising partway through would
    discard the record of every call already paid for. On failure this stops
    early, sets ``provider_failed``, records it in ``run.error_summary``, and
    returns everything retrieved so far.
    """
    reasons = _canonical_reasons(fallback_reasons)
    if not reasons:
        raise ExaFallbackError(
            "fallback_reasons must be non-empty: an Exa call has to record why "
            "it ran (no silent provider switching)"
        )
    domains = _validated_domains(include_domains)

    retrieval = config.retrieval
    capped_queries = list(queries)[: retrieval.max_queries_per_question]
    published_after = now - timedelta(days=retrieval.freshness_days_default)
    # See the docstring: the allowlist, not the reason, is what makes a document
    # an official source.
    source_type: SourceType = "official" if domains else "web"

    # Logged once, before the first call, so an engagement is on the record even
    # if every call then fails. Constants and an integer id only -- no query
    # text, no URLs, nothing provider-derived.
    _LOGGER.info(
        "exa fallback engaged for question %d (reasons: %s)",
        question_id,
        ", ".join(reasons),
    )

    raw_responses: list[dict[str, Any]] = []
    documents: list[ResearchDocument] = []
    # Constraint safety, not M1-305's cross-run deduplication: two queries can
    # surface the same page, and research_documents carries
    # UNIQUE (retrieval_run_id, canonical_url, content_sha256).
    seen: set[tuple[str, str]] = set()
    dropped = 0
    collapsed = 0
    provider_failed = False
    cost_total = 0.0
    cost_reported = False

    for query in capped_queries:
        payload: dict[str, Any] = {
            "query": query,
            "type": _SEARCH_TYPE,
            "numResults": retrieval.max_documents_per_query,
            "startPublishedDate": published_after.isoformat(),
            "contents": {
                "text": {"maxCharacters": _TEXT_MAX_CHARACTERS},
                "maxAgeHours": _MAX_AGE_HOURS,
            },
        }
        if domains:
            payload["includeDomains"] = domains

        try:
            response = client.post(_SEARCH_PATH, json=payload)
            response.raise_for_status()
            body: Any = response.json()
        except Exception:
            # Stop, but do not raise: calls already made were billed, and their
            # responses are the only record of that spend. The exception is
            # discarded entirely rather than inspected -- httpx errors quote the
            # request, and the request carries the API key in a header.
            provider_failed = True
            break

        if not isinstance(body, dict):
            provider_failed = True
            break
        raw_responses.append(body)

        call_cost = _call_cost_usd(body)
        if call_cost is not None:
            cost_total += call_cost
            cost_reported = True

        results = body.get("results")
        if not isinstance(results, list):
            # A 200 with no result list is a contract breach, not an empty
            # answer. Stopping is the cheaper honest option: continuing would
            # pay for more calls against a provider that is not answering in the
            # documented shape. The body is already recorded above.
            provider_failed = True
            break

        # Sliced as well as capped in the request: `numResults` is the provider's
        # promise, and the ledger's cost is ours.
        for result in results[: retrieval.max_documents_per_query]:
            try:
                payload_document = _to_document(
                    result,
                    retrieval_run_id=retrieval_run_id,
                    retrieved_at=now,
                    source_type=source_type,
                )
                document = validate_document(payload_document)
            except (
                ResearchSchemaError,
                CanonicalizationError,
                AttributeError,
                TypeError,
                ValueError,
                KeyError,
            ):
                # One unusable result must not fail a run that otherwise
                # retrieved good evidence. Counted, never echoed. ValueError
                # also covers the known `content_sha256` lone-surrogate raise
                # (UnicodeEncodeError is a ValueError) -- see the CLAUDE.md
                # gotcha; here it degrades to a drop rather than a crash.
                dropped += 1
                continue

            key = (document.canonical_url, document.content_sha256)
            if key in seen:
                collapsed += 1
                continue
            seen.add(key)
            documents.append(document)

    run = validate_run(
        {
            "retrieval_run_id": retrieval_run_id,
            "question_id": question_id,
            "provider": "exa",
            "provider_config": {
                "endpoint": _SEARCH_PATH,
                "search_type": _SEARCH_TYPE,
                "num_results": retrieval.max_documents_per_query,
                "text_max_characters": _TEXT_MAX_CHARACTERS,
                "max_age_hours": _MAX_AGE_HOURS,
                "start_published_date": published_after.isoformat(),
                "include_domains": domains,
                # The audit trail the acceptance criterion asks for: it reaches
                # research_runs.provider_config_json, so the ledger alone answers
                # why a second provider was engaged.
                "fallback_reasons": list(reasons),
            },
            "queries": capped_queries,
            "started_at_utc": now,
            "completed_at_utc": now,
            "freshness_cutoff_utc": published_after,
            "error_summary": _error_summary(
                provider_failed=provider_failed, retained=len(documents)
            ),
            # Exa reports a per-request dollar estimate, unlike AskNews's
            # unconvertible credits, so this is a real figure -- but the API
            # documents it as an estimate and not an invoice record.
            "cost_usd": cost_total if cost_reported else None,
        }
    )

    return ExaRetrieval(
        run=run,
        documents=tuple(documents),
        raw_responses=tuple(raw_responses),
        documents_dropped=dropped,
        duplicates_collapsed=collapsed,
        provider_failed=provider_failed,
        fallback_reasons=reasons,
    )


def _validated_domains(include_domains: Sequence[str]) -> list[str]:
    """Return the domain allowlist as a list of non-blank strings.

    Validated rather than trusted because it is persisted into
    ``provider_config`` and sent to the provider: a non-string entry would fail
    at ``model_dump_json`` time inside a later ledger write, long after the
    billable calls happened.
    """
    domains = list(include_domains)
    for domain in domains:
        if not isinstance(domain, str) or not domain.strip():
            raise ExaFallbackError(
                "include_domains entries must be non-blank strings (offending input withheld)"
            )
    return domains


def _to_document(
    result: Any,
    *,
    retrieval_run_id: str,
    retrieved_at: datetime,
    source_type: SourceType,
) -> dict[str, Any]:
    """Build the document payload for one Exa result (unvalidated).

    Raises a constant-message ``TypeError`` for a result that is not a mapping
    or carries no URL string; :func:`retrieve_web` counts those as drops.

    ``publisher`` stays ``None``: Exa returns no publisher field, and deriving
    one from the hostname would put a value in the ledger that no provider
    asserted. ``summary`` likewise stays ``None`` -- it is reserved for the
    pipeline's own summarization, and Exa's text is provider material, so it
    goes in ``snippet``. ``reliability_tag`` is left to M1-308's allowlist.
    """
    if not isinstance(result, dict):
        raise TypeError("exa result must be a mapping (offending input withheld)")
    url = result.get("url")
    if not isinstance(url, str):
        raise TypeError("exa result url must be a string (offending input withheld)")

    text = _optional_text(result.get("text"))
    return {
        "retrieval_run_id": retrieval_run_id,
        "original_url": url,
        "canonical_url": canonicalize_url(url),
        "title": _optional_text(result.get("title")),
        "publisher": None,
        "author": _optional_text(result.get("author")),
        "published_at_utc": _published_at_utc(result.get("publishedDate")),
        "updated_at_utc": None,
        "retrieved_at_utc": retrieved_at,
        "source_type": source_type,
        "provenance": "direct_api",
        "content_sha256": content_sha256(_hash_source(result)),
        "snippet": text[:_SNIPPET_CHARACTERS] if text is not None else None,
        "summary": None,
        "reliability_tag": None,
    }


def _optional_text(value: Any) -> str | None:
    """Return a non-empty string value, or None for anything else."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _hash_source(result: dict[str, Any]) -> str:
    """Return the text that defines this result's identity, per the pinned rule."""
    for candidate in (result.get("text"), result.get("title")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _published_at_utc(value: Any) -> datetime | None:
    """Parse Exa's ``publishedDate`` into an aware UTC timestamp, or None.

    One uniform rule, because the field's precision varies: the API documents it
    as ``YYYY-MM-DD`` while live responses also carry full ISO timestamps.

    - An offset-aware value converts to UTC.
    - A date-only or offset-less value is pinned to **midnight UTC of the stated
      date**. The true instant is unknown within the day and within the source's
      zone, so this carries up to roughly a day of error either way; that is
      recorded here rather than hidden, and it is preferable to discarding the
      only date the provider gave.
    - Anything unparseable yields ``None``. An undated document is treated as
      stale by M1-305, which is the safe direction: it can only make evidence
      look weaker than it is, never fresher.

    Never raises: a bad date is not a reason to lose an otherwise usable
    citation.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # 3.11's fromisoformat accepts a trailing uppercase "Z" but not "z".
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _call_cost_usd(body: dict[str, Any]) -> float | None:
    """Return one call's reported dollar cost, or None when it is not usable.

    ``bool`` is excluded explicitly: it is an ``int`` subclass in Python, so a
    ``costDollars.total`` of ``true`` would otherwise be recorded as a cost of
    one dollar. Non-finite and negative values are dropped rather than stored --
    ``ResearchRun.cost_usd`` requires a finite non-negative number, and the
    ledger's own trigger rejects the rest, so a run would validate here and fail
    at write time.
    """
    cost = body.get("costDollars")
    if not isinstance(cost, dict):
        return None
    total = cost.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return None
    value = float(total)
    if not isfinite(value) or value < 0:
        return None
    return value


def _error_summary(*, provider_failed: bool, retained: int) -> str | None:
    """Describe an actual failure, or return None for a successful run.

    Scoped to the schema's own meaning for this field -- "set when the run failed
    or returned nothing". Routine drops and intra-run duplicate collapsing are
    *not* failures and ride on :class:`ExaRetrieval` instead, so that the
    validation gate (M1-504) cannot mistake an ordinary run for a failed one.
    Identical wording to the AskNews adapter's, deliberately: the two providers'
    failures should be indistinguishable to anything reading this field.

    Built from constants and integers only; no retrieved value reaches it.
    """
    parts: list[str] = []
    if provider_failed:
        parts.append("provider call failed; retrieval stopped early")
    if retained == 0:
        parts.append("no documents retained")
    return "; ".join(parts) if parts else None
