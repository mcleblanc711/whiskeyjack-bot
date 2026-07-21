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
key in the same call frame, so every message raised here is a constant. Nothing
interpolates an article field, a query, or a credential, and SDK failures are
re-raised ``from None`` so the underlying exception cannot reprint a value through
a rendered traceback.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from asknews_sdk import AskNewsSDK

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.research.hashing import content_sha256
from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    validate_document,
    validate_run,
)

# The two passes that together satisfy "current and historical news". AskNews
# scopes each by strategy rather than by endpoint. Typed as the SDK's own
# Literal so a rename in a future asknews release is a type error here, not a
# silently rejected request.
_Strategy = Literal["latest news", "news knowledge"]

_STRATEGY_CURRENT: _Strategy = "latest news"
_STRATEGY_HISTORICAL: _Strategy = "news knowledge"
_STRATEGIES: tuple[_Strategy, ...] = (_STRATEGY_CURRENT, _STRATEGY_HISTORICAL)

_HOURS_PER_DAY = 24


class AskNewsRetrievalError(Exception):
    """AskNews retrieval failed.

    The message is always a constant: the underlying SDK error may quote the
    request, the response body, or an auth header, none of which may reach a log
    or a ledger row.
    """


@dataclass(frozen=True)
class AskNewsRetrieval:
    """One AskNews retrieval pass over one question's queries.

    ``raw_responses`` is held in memory only. Persisting it — and the file layout
    and replay contract that implies — belongs to M1-306; this adapter writes
    nothing to disk, so ``run.raw_response_path`` and every document's
    ``raw_artifact_path`` stay ``None``.
    """

    run: ResearchRun
    documents: tuple[ResearchDocument, ...]
    raw_responses: tuple[dict[str, Any], ...]


def build_asknews_client(config: AppConfig) -> AskNewsSDK:
    """Construct the one configured AskNews client.

    Raises :class:`MissingCredentialError` when the configured key variable is
    unset or empty, before any network use and therefore before any billable
    call. An empty string counts as missing.
    """
    provider = config.retrieval.primary
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise MissingCredentialError(provider.api_key_env)
    return AskNewsSDK(
        api_key=api_key,
        scopes={"news"},
        timeout=provider.timeout_seconds,
        retries=provider.retries,
    )


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
    """Retrieve current and historical news for ``queries`` as normalized documents.

    ``now`` is injected rather than read from the clock so ``started_at_utc`` and
    every ``retrieved_at_utc`` are deterministic under test and under replay.
    Queries are supplied by the caller; deriving them from a question is not this
    item's job.
    """
    retrieval = config.retrieval
    capped_queries = list(queries)[: retrieval.max_queries_per_question]
    hours_back = retrieval.freshness_days_default * _HOURS_PER_DAY

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

    for query in capped_queries:
        for strategy in _STRATEGIES:
            try:
                response = client.news.search_news(
                    query=query,
                    n_articles=retrieval.max_documents_per_query,
                    return_type="dicts",
                    strategy=strategy,
                    historical=strategy == _STRATEGY_HISTORICAL,
                    hours_back=hours_back,
                )
            except Exception:
                # The SDK error may quote the request or an auth header.
                raise AskNewsRetrievalError(
                    "AskNews search failed (provider detail withheld from this message)"
                ) from None

            raw_responses.append(response.model_dump(mode="json"))

            for article in response.as_dicts or []:
                try:
                    payload = _to_document(
                        article,
                        retrieval_run_id=retrieval_run_id,
                        retrieved_at=now,
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
            "started_at_utc": now,
            "completed_at_utc": now,
            "freshness_cutoff_utc": now - timedelta(days=retrieval.freshness_days_default),
            "error_summary": _error_summary(
                dropped=dropped, collapsed=collapsed, retained=len(documents)
            ),
            # AskNews reports usage in credits, not currency, and no credit->USD
            # rate is configured. Recording a converted number would put an
            # unearned figure in the ledger; the credit count survives in
            # raw_responses for M1-306, which owns cost capture.
            "cost_usd": None,
        }
    )

    return AskNewsRetrieval(
        run=run,
        documents=tuple(documents),
        raw_responses=tuple(raw_responses),
    )


def _error_summary(*, dropped: int, collapsed: int, retained: int) -> str | None:
    """Summarize a run's losses without echoing any retrieved value."""
    parts: list[str] = []
    if retained == 0:
        parts.append("no documents retained")
    if dropped:
        parts.append(f"{dropped} article(s) dropped: unusable url or shape")
    if collapsed:
        parts.append(f"{collapsed} intra-run duplicate(s) collapsed")
    return "; ".join(parts) if parts else None
