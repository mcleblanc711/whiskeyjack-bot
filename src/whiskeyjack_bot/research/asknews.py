"""AskNews retrieval adapter: normalized news evidence with per-article provenance (M1-302).

AskNews is the primary retrieval provider (decision D17). This module turns its
search results into validated :class:`ResearchDocument` records, one per article,
so the ledger keeps each article's own URL, timestamps and publisher rather than a
flattened digest.

Why not ``forecasting_tools.AskNewsSearcher``: the pinned SDK ships a wrapper, but
``AskNewsSearcher.get_formatted_news`` returns a single pre-formatted markdown
string (``_format_articles``), which discards exactly the article-level provenance
this item exists to preserve. It also reads credentials from the environment
itself, hardcodes a 12-second sleep, and keeps an on-disk cache. We call
``asknews_sdk`` directly instead.

Verified against asknews==0.13.54 on 2026-07-21: constructing ``AskNewsSDK`` with
``api_key=`` performs **no network I/O** — it builds an ``httpx.Client`` and an
``APIKey`` auth object, and the OAuth token round-trip is skipped entirely in
API-key mode. That is what lets :func:`build_asknews_client` be exercised under
the test suite's socket guard, and what makes the missing-credential check
provably pre-network (and therefore pre-billing).

Content-hash source rule (pinned; changing it changes document identity):
``full_text`` if non-empty, else ``summary``, else the title. Hashing always goes
through :func:`whiskeyjack_bot.research.hashing.content_sha256` so no provider can
drift into its own rule for the same article. See docs/M1-302-NOTES.md for the
stability caveat on summary-derived hashes.

Error hygiene: this module handles arbitrary retrieved provider text and an API
key in the same call frame, so no string it produces is built from provider data.
Nothing interpolates an article field, a query, or a credential. Two channels are
easy to miss and are closed deliberately:

- **Exceptions from the provider are discarded, never inspected or re-raised** —
  an SDK error may quote the request, the response body, or an auth header.
- **Pydantic serializer warnings embed the offending value in their text**, so
  every ``model_dump`` of provider data passes ``warnings=False``. This is not
  noise suppression; a warning is an egress path to stderr and to captured logs.

Provider failure itself is reported as data on the returned :class:`AskNewsRetrieval`,
not as a raise (see :func:`retrieve_news`) -- a run that has already billed for some
calls must stay recordable rather than lose that record to an exception. The module
does raise two exceptions, both before any network use and therefore before any
billing: ``MissingCredentialError``, and :class:`AskNewsRetrievalError` for a caller
argument the run record would reject outright (M1-309, matching the Exa adapter's
round-4 preflight -- see :mod:`whiskeyjack_bot.research.preflight`, shared by both
adapters rather than duplicated).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, get_args

from asknews_sdk import AskNewsSDK

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.credentials import MissingCredentialError
from whiskeyjack_bot.research.asknews_cost import NEWS_CALL_ESTIMATE_USD, credits_microusd
from whiskeyjack_bot.research.hashing import content_sha256
from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    validate_document,
    validate_run,
)
from whiskeyjack_bot.research.preflight import require_run_metadata, string_list
from whiskeyjack_bot.research.transport import apply_connection_retries

# The passes AskNews scopes by strategy rather than by endpoint. Typed as the SDK's own
# Literal so a rename in a future asknews release is a type error here, not a
# silently rejected request. Only the current pass is issued; see `_STRATEGIES`.
_Strategy = Literal["latest news", "news knowledge"]

_STRATEGY_CURRENT: _Strategy = "latest news"
_STRATEGY_HISTORICAL: _Strategy = "news knowledge"
# M1-352, owner decision 2026-09-22: the historical pass is no longer issued. It was the
# expensive half -- an estimated $0.125 against $0.025, about 83% of the AskNews credit cost --
# and MiniBench's question rate after the 33125 re-point (42 questions in 29 hours, each
# retrieved exactly once) was exhausting the plan. `_STRATEGY_HISTORICAL` stays defined: it
# still names what a STORED run may have been configured with, and `replay` reads those rows.
#
# What this costs is stated rather than hidden: the historical pass returned URLs the current
# pass never sees (measured 2026-09-11), so some questions now retrieve fewer documents, fall
# through to the Exa fallback, and -- when that finds nothing either -- become M1-349's
# evidence-poor forecast instead of a richer packet.
_STRATEGIES: tuple[_Strategy, ...] = (_STRATEGY_CURRENT,)

_HOURS_PER_DAY = 24

# What a failed AskNews call was, as far as the exception's CLASS can say (M1-332, D44).
#
# Honest names, not hopeful ones. The pinned SDK has no quota class: a spent quota arrives as
# `ForbiddenError`, `RateLimitExceededError` or the base `APIError` (an HTTP status the SDK's
# ErrorMap does not list, such as 402, maps to the base class), and only the numeric `code` --
# which is copied out of the response body -- could tell them apart. M1-332 forbids reading
# anything that could carry response content, and AskNews does not document which code means
# quota, so every member that COULD be a quota says so in its name and none claims it for sure.
AskNewsFailure = Literal[
    "rate_or_quota_limited",
    "forbidden_or_quota",
    "auth_rejected",
    "request_rejected",
    "provider_unavailable",
    "provider_error",
    "transient",
]

# The module whose classes are matched, and the classes, by NAME. Restricted by module so a
# same-named exception from anywhere else is never mistaken for the SDK's; nothing is
# imported to classify, the way `submission_live.classify_error` matches `requests`.
_SDK_ERRORS_MODULE: Final = "asknews_sdk.errors"
_HTTPX_MODULE: Final = "httpx"
_SDK_CLASSES: Final[dict[str, AskNewsFailure]] = {
    "RateLimitExceededError": "rate_or_quota_limited",
    "ConcurrencyLimitExceededError": "rate_or_quota_limited",
    "ForbiddenError": "forbidden_or_quota",
    "UnauthorizedError": "auth_rejected",
    "BadRequestError": "request_rejected",
    "ResourceNotFoundError": "request_rejected",
    "MethodNotAllowed": "request_rejected",
    "ValidationError": "request_rejected",
    "RequestTimeoutError": "provider_unavailable",
    "ServiceUnavailableError": "provider_unavailable",
    # Last in any MRO it appears in: every SDK error subclasses it, so a class the pinned
    # version does not have lands here rather than on the transient default.
    "APIError": "provider_error",
}
_HTTPX_CLASSES: Final[dict[str, AskNewsFailure]] = {
    "TimeoutException": "provider_unavailable",
}
assert set(_SDK_CLASSES.values()) | set(_HTTPX_CLASSES.values()) | {"transient"} == set(
    get_args(AskNewsFailure)
)

# What each failure tells an operator, for the `provider_failed` alert (M1-332). Constants
# only. "Quota" appears only where the class cannot rule it out, and never as a certainty.
FAILURE_ADVICE: Final[dict[AskNewsFailure, str]] = {
    "rate_or_quota_limited": (
        "AskNews refused the call with a rate-limit response (HTTP 429). That is a "
        "per-minute or concurrency limit, or a spent plan quota -- if the first call of "
        "every question keeps failing this way, check the plan's remaining credits on the "
        "AskNews dashboard."
    ),
    "forbidden_or_quota": (
        "AskNews refused the call as forbidden (HTTP 403). That is either a key without "
        "access to this endpoint or a spent plan quota -- check the plan's remaining "
        "credits and the key's scopes on the AskNews dashboard."
    ),
    "auth_rejected": (
        "AskNews rejected the API key (HTTP 401). Check the key the configured variable holds."
    ),
    "request_rejected": (
        "AskNews rejected the request itself (HTTP 400/404/405/422): a problem with what "
        "was asked, not with the account. Check data/logs/ for the retrieval run."
    ),
    "provider_unavailable": (
        "AskNews timed out or reported itself unavailable. Usually transient; the next "
        "poll retries."
    ),
    "provider_error": (
        "AskNews returned an error the pinned SDK does not name (a 5xx, or an unlisted "
        "status such as 402). It may be an outage or a billing refusal -- check the "
        "AskNews dashboard if it repeats."
    ),
    "transient": (
        "The call failed without a provider error response (a dropped connection or an "
        "unrecognized error). The cause is not known beyond that; check data/logs/ if it "
        "repeats."
    ),
}
assert set(FAILURE_ADVICE) == set(get_args(AskNewsFailure))


class AskNewsRetrievalError(Exception):
    """A retrieval call was requested in a way this module refuses to make.

    Covers caller-side mistakes that must never be papered over: a bare string where a
    sequence of queries was expected, and run metadata (``question_id``,
    ``retrieval_run_id``, ``now``) the run record would reject outright. Both fire in
    :func:`retrieve_news` before any network use, and therefore before any billing (M1-309).
    Same hygiene rule as the sibling adapters' own errors (e.g. ``ExaFallbackError``): the
    message is a constant and never echoes the offending value.
    """


@dataclass(frozen=True)
class AskNewsRetrieval:
    """One AskNews retrieval pass over one question's queries.

    ``raw_responses`` is held in memory only. Persisting it — and the file layout
    and replay contract that implies — belongs to M1-306; this adapter writes
    nothing to disk, so ``run.raw_response_path`` and every document's
    ``raw_artifact_path`` stay ``None``.

    ``documents_dropped`` and ``duplicates_collapsed`` are routine bookkeeping,
    not failure: a run that drops an unusable article or collapses the expected
    current/historical overlap is a *successful* run. They live here rather than
    on :class:`ResearchRun` because that model has no counter for them and
    overloading ``error_summary`` would make ordinary runs look failed to the
    fallback (M1-303) and validation (M1-504) logic. M1-306 decides whether they
    become persisted columns.

    ``provider_failed`` is the fallback signal: it is ``True`` when a provider
    call raised, in which case retrieval stopped early and everything already
    retrieved is still returned.

    ``calls_attempted`` counts every billable request this pass made, including
    one that then raised. **It is not derivable from the other fields**, which is
    why it is reported rather than left to the caller: ``raw_responses`` holds
    only the requests that came back, so a caller reconstructing the count from
    it silently loses the failed one -- and this adapter issues
    ``len(queries) x len(_STRATEGIES)`` requests (one per query since M1-352 dropped the
    historical pass, two before it), so the count is the adapter's to report.
    M1-315 round 3 found the paid-run accounting reporting provider *runs* where
    it published a count of *calls*; this is the field that makes the two agree.
    ``forecast/generate.py`` reports the same quantity as ``invocations``.
    """

    run: ResearchRun
    documents: tuple[ResearchDocument, ...]
    raw_responses: tuple[dict[str, Any], ...]
    documents_dropped: int
    duplicates_collapsed: int
    provider_failed: bool
    calls_attempted: int
    # What the failed call was (M1-332), or None when no call failed. Set exactly when
    # `provider_failed` is: see `classify_failure`.
    failure: AskNewsFailure | None


def classify_failure(exc: BaseException) -> AskNewsFailure:
    """Name a provider exception in this module's closed vocabulary, from its class alone.

    **Reads nothing off the exception but its type** (M1-332). Not ``str(exc)``, not
    ``args``, not the SDK's ``detail``, ``code`` or ``response``: an AskNews error may quote
    the request, the response body or an auth header, and ``code`` is copied from the
    response body. ``type(exc).__mro__`` is walked, most specific first, and each class is
    matched on ``(__module__, __name__)`` -- so a subclass the pinned SDK does not have
    resolves to its nearest listed ancestor, and a same-named class from another module
    matches nothing.

    Anything unrecognized is ``transient``, never a quota: a wrong "quota" label sends an
    operator to a vendor dashboard for a dropped socket. Total: it never raises.
    """
    for klass in type(exc).__mro__:
        module = getattr(klass, "__module__", None)
        name = getattr(klass, "__name__", None)
        if type(module) is not str or type(name) is not str:
            continue
        if module == _SDK_ERRORS_MODULE and name in _SDK_CLASSES:
            return _SDK_CLASSES[name]
        if module == _HTTPX_MODULE and name in _HTTPX_CLASSES:
            return _HTTPX_CLASSES[name]
    return "transient"


def build_asknews_client(config: AppConfig) -> AskNewsSDK:
    """Construct the one configured AskNews client.

    Raises :class:`MissingCredentialError` when the configured key variable is
    unset or empty, before any network use and therefore before any billable
    call. An empty string counts as missing.

    Retries cannot be applied via ``retries=`` on the SDK: asknews 0.13.54 stores
    that argument (``client.py:73``) and never reads it — the request path calls
    ``httpx.Client.send()`` directly (``client.py:266``) — so it is a no-op. Nor
    can they be applied by passing ``transport=httpx.HTTPTransport(retries=...)``:
    ``httpx.Client.__init__`` computes ``allow_env_proxies = trust_env and
    transport is None`` (httpx 0.28), so any explicit transport silently drops
    ``HTTP(S)_PROXY`` routing — a proxy-dependent deployment would lose AskNews
    connectivity, surfaced only as an ordinary ``provider_failed`` fallback.

    So we build the SDK normally (env proxies preserved) and set the retry count
    on the resulting connection pool afterwards; see
    :func:`whiskeyjack_bot.research.transport.apply_connection_retries`, which
    holds the full reasoning and is shared with the Exa fallback (M1-303).
    Retries there are connection-failure only (never a re-billed request) and
    direct-connection only (a no-op on a proxied hop) -- accepted M1-302 scope;
    what round 2 had to preserve is env-proxy *routing*, and that is intact.
    """
    provider = config.retrieval.primary
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise MissingCredentialError(provider.api_key_env)
    sdk = AskNewsSDK(
        api_key=api_key,
        scopes={"news"},
        timeout=provider.timeout_seconds,
    )
    apply_connection_retries(sdk.client._client, provider.retries)
    return sdk


def _hash_source(article: Any) -> str:
    """Return the text that defines this article's identity, per the pinned rule."""
    for candidate in (article.full_text, article.summary, article.eng_title, article.title):
        if candidate:
            return str(candidate)
    return ""


def _first_author_name(article: Any) -> str | None:
    """Return the first author's name, or None.

    Only the name is taken. ``asknews_sdk.dto.base.Author`` also carries an
    ``email``, which is personal data with no forecasting value and must not
    enter the ledger.
    """
    authors = article.authors or []
    for author in authors:
        name = getattr(author, "name", None)
        if name:
            return str(name)
    return None


def _to_document(article: Any, *, retrieval_run_id: str, retrieved_at: datetime) -> dict[str, Any]:
    """Build the document payload for one article (unvalidated)."""
    url = str(article.article_url)
    return {
        "retrieval_run_id": retrieval_run_id,
        # M1-305 derives the real canonical form; until then the two are equal.
        "original_url": url,
        "canonical_url": url,
        "title": article.eng_title or article.title,
        "publisher": article.source_id,
        "author": _first_author_name(article),
        "published_at_utc": article.pub_date,
        "updated_at_utc": article.crawl_date,
        "retrieved_at_utc": retrieved_at,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": content_sha256(_hash_source(article)),
        "snippet": article.summary,
        # `summary` is reserved for our own summarization; AskNews's is provider
        # text and belongs in `snippet`.
        "summary": None,
        # The reliability vocabulary is social-source oriented; tagging news
        # publishers is M1-305/M1-308's call, not this adapter's.
        "reliability_tag": None,
    }


def retrieve_news(
    client: AskNewsSDK,
    config: AppConfig,
    *,
    question_id: int,
    queries: Sequence[str],
    retrieval_run_id: str,
    now: datetime,
) -> AskNewsRetrieval:
    """Retrieve current news for ``queries`` as normalized documents.

    **Current only since M1-352.** The historical ("news knowledge") pass is no longer
    issued; see ``_STRATEGIES``. Stored runs from before that change still record both.

    Refuses, before any network use and therefore before any billing, a caller mistake
    the run record would otherwise only catch after every call had already been paid
    for: a bare string (or other non-sequence) in place of ``queries``, and malformed
    run metadata (``question_id``, ``retrieval_run_id``, ``now`` -- see
    :func:`whiskeyjack_bot.research.preflight.require_run_metadata`). Both raise
    :class:`AskNewsRetrievalError` (M1-309, matching the Exa adapter's round-4
    preflight).

    ``now`` is injected rather than read from the clock so ``started_at_utc`` and
    every ``retrieved_at_utc`` are deterministic under test and under replay. It is
    **converted to UTC once, in preflight**, and that value -- not the caller's raw
    ``now`` -- is what the run and every document carry: the same instant a caller
    passed, spelled independently of the timezone they spelled it in. Queries are
    supplied by the caller; deriving them from a question is not this item's job.

    ``freshness_cutoff_utc`` is likewise computed once, before the query loop, and
    reused when the run is built. An aware ``now`` near ``datetime.min`` passes the
    tz-awareness preflight but overflows this subtraction; computing it only when the
    run is assembled at the end let two calls bill first and then raise a raw
    ``OverflowError`` with no recordable run (cross-model review round 1).

    **Never raises on provider failure.** A run makes up to
    ``max_queries_per_question * len(_STRATEGIES)`` billable calls; raising partway through would
    discard the record of every call already paid for, which is precisely the kind
    of shortcut that weakens the ledger. On failure this stops early, sets
    ``provider_failed``, records the failure in ``run.error_summary``, and returns
    everything retrieved so far so M1-306 can still persist and replay it.
    """
    now_utc = require_run_metadata(
        question_id=question_id,
        retrieval_run_id=retrieval_run_id,
        now=now,
        error=AskNewsRetrievalError,
    )
    validated_queries = string_list(
        queries,
        "queries entries must be non-blank strings (offending input withheld)",
        error=AskNewsRetrievalError,
    )

    retrieval = config.retrieval
    capped_queries = validated_queries[: retrieval.max_queries_per_question]
    hours_back = retrieval.freshness_days_default * _HOURS_PER_DAY
    try:
        # Computed here, before the first billable call, and reused below rather
        # than re-derived when the run is built: an aware `now` near datetime.min
        # (a contract-accepted value, not hostile state) passes require_run_metadata's
        # tz-awareness check but overflows this subtraction, and computing it only
        # inside the final validate_run() dict let two calls bill first and then
        # raise a raw OverflowError with no recordable run (cross-model review round
        # 1, matching Exa's round-5 finding 3).
        freshness_cutoff_utc = now_utc - timedelta(days=retrieval.freshness_days_default)
    except OverflowError:
        raise AskNewsRetrievalError(
            "now is too early to compute a freshness bound (offending input withheld)"
        ) from None

    raw_responses: list[dict[str, Any]] = []
    documents: list[ResearchDocument] = []
    # Constraint safety, not M1-305's cross-run deduplication: the current and
    # historical passes overlap by design, and research_documents carries
    # UNIQUE (retrieval_run_id, canonical_url, content_sha256). Collapsing exact
    # repeats within this run keeps the writer from hitting that constraint.
    # Cross-run dedup, canonicalization and provenance merging remain M1-305's.
    seen: set[tuple[str, str]] = set()
    dropped = 0
    collapsed = 0
    provider_failed = False
    failure: AskNewsFailure | None = None
    # Counted at the point of the request, so the one that raises is included: it
    # reached the provider and may well have been billed. Same rule as
    # `research/exa.py`'s `calls_attempted` and `forecast/generate.py`'s.
    calls_attempted = 0

    for query in capped_queries:
        if provider_failed:
            break
        for strategy in _STRATEGIES:
            from whiskeyjack_bot.research.durable import begin_call, complete_call

            request: dict[str, Any] = {
                "query": query,
                "strategy": strategy,
                "n_articles": retrieval.max_documents_per_query,
                "return_type": "dicts",
                "hours_back": hours_back,
                "historical": strategy == _STRATEGY_HISTORICAL,
            }
            call_scope, cached = begin_call(
                "asknews",
                # M1-337: credits x rate, never a literal. Only the news pass is issued
                # (M1-352), so there is one estimate.
                NEWS_CALL_ESTIMATE_USD,
                request,
                question_id,
                now_utc.isoformat(),
            )
            try:
                if cached is not None:
                    from asknews_sdk.dto.news import SearchResponse

                    response = SearchResponse.model_validate(cached)
                else:
                    calls_attempted += 1
                    response = client.news.search_news(**request)

            except Exception as exc:
                # Stop, but do not raise: calls already made were billed, and
                # their responses are the only record of that spend. The SDK
                # error is discarded rather than inspected -- it may quote the
                # request, the response body, or an auth header. Only its CLASS
                # is read, to name it (M1-332); `del` so no later code can reach it.
                failure = classify_failure(exc)
                del exc
                provider_failed = True
                break

            # warnings=False is a secret-egress control, not cosmetic noise
            # suppression: pydantic's serializer warnings embed the offending
            # *value* in their text, and this dict is built from untrusted
            # provider data. Do not remove. (GPT review round 1, finding 1.)
            raw = response.model_dump(mode="json", warnings=False)
            # M1-336: settle from the response's own `usage.credits`. None (no usage
            # block, a malformed count) leaves the reservation held at its estimate --
            # unknown is never free. A recovered call reaches here with the cached
            # response and settles from the same figure.
            complete_call(
                call_scope,
                raw,
                actual_microusd=credits_microusd(raw),
                basis="asknews_credits",
            )
            raw_responses.append(raw)

            for article in response.as_dicts or []:
                try:
                    payload = _to_document(
                        article,
                        retrieval_run_id=retrieval_run_id,
                        retrieved_at=now_utc,
                    )
                    document = validate_document(payload)
                except (ResearchSchemaError, AttributeError, TypeError, ValueError):
                    # One unusable article must not fail a run that otherwise
                    # retrieved good evidence. Counted, never echoed.
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
            "provider": "asknews",
            "provider_config": {
                "strategies": list(_STRATEGIES),
                "n_articles": retrieval.max_documents_per_query,
                "hours_back": hours_back,
                "return_type": "dicts",
            },
            "queries": capped_queries,
            "started_at_utc": now_utc,
            "completed_at_utc": now_utc,
            "freshness_cutoff_utc": freshness_cutoff_utc,
            "error_summary": _error_summary(failure=failure, retained=len(documents)),
            # Still None when calls were made. Since M1-336 each call's reservation
            # settles in the tournament journal from its own `usage.credits`; putting
            # the sum on the run row as well is deferred (see the M1-336 notes), and
            # the credit counts survive in raw_responses either way.
            "cost_usd": None if calls_attempted else 0.0,
        }
    )

    return AskNewsRetrieval(
        run=run,
        documents=tuple(documents),
        raw_responses=tuple(raw_responses),
        documents_dropped=dropped,
        duplicates_collapsed=collapsed,
        provider_failed=provider_failed,
        calls_attempted=calls_attempted,
        failure=failure,
    )


def _error_summary(*, failure: AskNewsFailure | None, retained: int) -> str | None:
    """Describe an actual failure, or return None for a successful run.

    Scoped to the schema's own meaning for this field — "set when the run failed
    or returned nothing" (`research/model.py`). Routine drops and intra-run
    duplicate collapsing are *not* failures and deliberately do not appear here;
    they ride on :class:`AskNewsRetrieval` instead. Putting them here made
    ordinary runs indistinguishable from failed ones for the fallback (M1-303)
    and validation (M1-504) logic that reads this field. (GPT review round 1,
    finding 3.)

    ``failure`` (M1-332) is named in the text only once it is known to be one of this
    module's own literals.

    Built from constants and integers only; no retrieved value reaches it.
    """
    parts: list[str] = []
    if failure is not None:
        named = failure if failure in get_args(AskNewsFailure) else "transient"
        parts.append(f"provider call failed ({named}); retrieval stopped early")
    if retained == 0:
        parts.append("no documents retained")
    return "; ".join(parts) if parts else None
