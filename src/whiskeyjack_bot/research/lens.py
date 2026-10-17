"""The local-source lens: plan, buy and record retrieval aimed at local publishers (M1-358, D53).

For a question about a place, the lens asks for reporting from **publishers based there** --
Iranian outlets for a Strait of Hormuz question, Omani and Emirati ones beside them -- and
records what it found next to the research the forecast was actually made from. Three rules
shape everything here:

- **Locality is the publisher, not the language** (owner, 2026-10-16). Every lens call is
  aimed at the locale's ``local_domains`` from ``config/locales.yaml``; nothing is filtered by
  language, and nothing is classified by AskNews's ``country``, which the 2026-10-16 probe
  showed tags the story (``docs/LOCAL-LENS-NOTES.md`` § 2).
- **Collect-only.** Lens runs are ordinary ``research_runs`` rows, but they are named only by a
  ``local_lens_checkpoint`` journal event, never by ``research_checkpoint``, so the packet the
  submitted forecast is built from -- and its hash -- is exactly what it would have been without
  the lens. That forecast is the control for M1-359's shadow arm.
- **Every call is budgeted.** AskNews calls go through ``retrieve_news`` and Exa calls through
  ``retrieve_web``, so each reserves and settles through ``research/durable.py`` under the
  worker's budget; AskNews calls count against the series AskNews sub-cap (M1-355). The lens
  runs after the question's own work, so a cap it hits is a cap the baseline already cleared.

**Which provider.** AskNews where it can read the locale's press, Exa otherwise.
:data:`ASKNEWS_LANGUAGES` is the set of languages AskNews has been *observed* to return
(census of the live artifacts, 2026-10-16) -- Persian is not among them, and the probe's
``languages=["fa"]`` was refused with HTTP 422. An AskNews call asks for the locale's local
domains (``domain_url``) with the question's own English query, because AskNews searches its
English translations. An Exa call asks for the same domains (``includeDomains``) with the
locale's **native terms** for the places and institutions the question named -- a curated
local-language query, not a translation of the question -- and falls back to the English title
when the registry has none.

**Locale rows** (019) record, per document, what the provider said about it and where its
publisher sits. AskNews rows carry the article's language, native title, AskNews's English
title, ``crawl_date`` and descriptive labels, read from the run's own raw response by URL; an
Exa row carries the page title as its native title and no translation, which is the honest
state until M1-361. Provider values are carried verbatim or left out -- a value the ledger could
not read back (a lone surrogate) is dropped from the row rather than allowed to make the paid
run unwritable.

Errors: :class:`LensError` for a refusal before any purchase. Once a call is billed, a ledger
failure arrives as ``StoreError`` from the writer, exactly as on the baseline path; the worker's
hook (``tournament.py``) catches everything the lens raises and records it, because nothing the
lens does may stop the question loop.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from whiskeyjack_bot.research.asknews import LensRequest, retrieve_news
from whiskeyjack_bot.research.asknews_cost import credits_microusd
from whiskeyjack_bot.research.exa import retrieve_web
from whiskeyjack_bot.research.locales import LocaleEntry, LocaleRegistry, detect, publisher_origin
from whiskeyjack_bot.research.model import (
    DocumentLocale,
    ResearchDocument,
    ResearchSchemaError,
    validate_locale,
    validate_run,
)
from whiskeyjack_bot.research.orchestrate import derive_queries
from whiskeyjack_bot.research.persist import persist_paid_run
from whiskeyjack_bot.research.store import open_run, with_retrieval_counts

if TYPE_CHECKING:
    from whiskeyjack_bot.config import AppConfig
    from whiskeyjack_bot.questions.model import CanonicalQuestion

_LOGGER = logging.getLogger(__name__)

LensProvider = Literal["asknews", "exa"]
QuerySource = Literal["question", "native_terms", "question_title"]

# Languages AskNews has been observed to return: the census of every stored AskNews article
# on 2026-10-16 (`whiskeyjack research language-census`), `en-US` and `en-us` folded into
# `en`. Not a claim about what AskNews indexes -- a claim about what this project has seen it
# return. Persian is absent, and the probe found `languages=["fa"]` refused (HTTP 422).
ASKNEWS_LANGUAGES: Final = frozenset(
    {
        "ar", "cs", "de", "el", "en", "es", "fr", "hi", "it",
        "ja", "ko", "nl", "pl", "pt", "ru", "tr", "uk", "zh",
    }
)  # fmt: skip

# AskNews article fields copied into a locale row's provider labels: descriptive labels
# AskNews assigns, kept attributed to AskNews and never used to weight evidence.
_ASKNEWS_LABELS: Final = ("reporting_voice", "provocative", "classification")

_RUN_ID_PREFIX: Final = "run-"


class LensError(Exception):
    """The lens refused to plan or start. Never echoes question text or provider content."""


@dataclass(frozen=True)
class LensCall:
    """One planned lens call: which locale, which provider, what query, which publishers."""

    locale_code: str
    provider: LensProvider
    query: str
    query_source: QuerySource
    domains: tuple[str, ...]


@dataclass(frozen=True)
class LensRun:
    """One lens call as it happened. ``skipped`` names why a planned call was not made."""

    locale_code: str
    provider: LensProvider
    retrieval_run_id: str | None
    documents: int
    locales: int
    calls: int
    cost_usd: float | None
    provider_failed: bool
    skipped: str | None = None


@dataclass(frozen=True)
class LensOutcome:
    """Everything the lens did for one question, in plan order."""

    question_id: int
    runs: tuple[LensRun, ...]
    elapsed_seconds: float

    @property
    def retrieval_run_ids(self) -> tuple[str, ...]:
        return tuple(run.retrieval_run_id for run in self.runs if run.retrieval_run_id)


def _bare_hosts(domains: Sequence[str]) -> tuple[str, ...]:
    # A registry entry may carry a path prefix (`bbc.com/persian`); both providers filter by
    # host only, so such an entry is not a publisher the lens can ask for by itself.
    return tuple(domain for domain in domains if "/" not in domain)


def _languages(entry: LocaleEntry) -> frozenset[str]:
    return frozenset(entry.languages)


def _distinct_terms(terms: Iterable[str]) -> list[str]:
    """Native terms in order, dropping one whose words an earlier kept term already says.

    "Strait of Hormuz" and "Hormuz" are two triggers with two native terms, and the second
    is a word of the first; repeating it adds nothing to a keyword query but weight.
    """
    kept: list[str] = []
    said: set[str] = set()
    for term in terms:
        words = set(term.split())
        if words and not words <= said:
            kept.append(term)
            said |= words
    return kept


def plan(
    question: CanonicalQuestion, registry: LocaleRegistry, *, max_calls: int
) -> tuple[LensCall, ...]:
    """The lens calls for ``question``, in detection order, at most ``max_calls``.

    Pure: no I/O, no clock. A locale with no bare-host ``local_domains`` gets no call -- there
    is no publisher to ask for, and a call that asked for none would be ordinary retrieval
    under another name. A question no registry trigger matches gets an empty plan, which is
    the common case: a question about US inflation costs nothing extra.
    """
    if type(max_calls) is not int or max_calls < 1:
        raise LensError("max_calls must be a positive int")
    calls: list[LensCall] = []
    for match in detect(question, registry):
        entry = registry.get(match.code)
        if entry is None:  # unreachable: detect only returns registry codes
            continue
        hosts = _bare_hosts(entry.local_domains)
        if not hosts:
            continue
        if _languages(entry) <= ASKNEWS_LANGUAGES:
            calls.append(
                LensCall(
                    locale_code=entry.code,
                    provider="asknews",
                    query=derive_queries(question)[0],
                    query_source="question",
                    domains=hosts,
                )
            )
        else:
            natives = _distinct_terms(
                entry.native_terms[trigger]
                for trigger in match.triggers
                if trigger in entry.native_terms
            )
            calls.append(
                LensCall(
                    locale_code=entry.code,
                    provider="exa",
                    query=" ".join(natives) if natives else " ".join(question.title.split()),
                    query_source="native_terms" if natives else "question_title",
                    domains=hosts,
                )
            )
        if len(calls) == max_calls:
            break
    return tuple(calls)


def _storable(value: object) -> str | None:
    """``value`` if it is text the ledger can store and read back, else ``None``."""
    if type(value) is not str or not value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _aware(value: object) -> datetime | None:
    text = _storable(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _asknews_articles(raw_responses: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The first article per ``article_url`` across a run's raw responses."""
    by_url: dict[str, dict[str, Any]] = {}
    for body in raw_responses:
        items = body.get("as_dicts") if isinstance(body, dict) else None
        if not isinstance(items, list):
            continue
        for article in items:
            if isinstance(article, dict):
                url = article.get("article_url")
                if type(url) is str and url not in by_url:
                    by_url[url] = article
    return by_url


def locale_rows(
    call: LensCall,
    entry: LocaleEntry,
    documents: Sequence[ResearchDocument],
    raw_responses: Sequence[dict[str, Any]],
) -> tuple[DocumentLocale, ...]:
    """One locale row per document; a row the model refuses is left out, the document kept."""
    articles = _asknews_articles(raw_responses) if call.provider == "asknews" else {}
    rows: list[DocumentLocale] = []
    for document in documents:
        payload: dict[str, Any] = {
            "retrieval_run_id": document.retrieval_run_id,
            "canonical_url": document.canonical_url,
            "content_sha256": document.content_sha256,
            "locale_code": entry.code,
            "publisher_origin": publisher_origin(document.original_url, entry),
            "translation_source": "none",
        }
        if call.provider == "asknews":
            article = articles.get(document.original_url, {})
            english = _storable(article.get("eng_title"))
            labels = {
                key: label
                for key in _ASKNEWS_LABELS
                if (label := _storable(article.get(key))) is not None
            }
            payload.update(
                language=_storable(article.get("language")),
                provider_story_country=_storable(article.get("country")),
                native_title=_storable(article.get("title")),
                english_title=english,
                translation_source="provider:asknews" if english is not None else "none",
                provider_first_seen_at_utc=_aware(article.get("crawl_date")),
                provider_labels=labels or None,
            )
        else:
            payload.update(native_title=_storable(document.title))
        try:
            rows.append(validate_locale(payload))
        except ResearchSchemaError:
            _LOGGER.info("a lens document carries no locale row: its facts did not validate")
    return tuple(rows)


# The keys :func:`lens_record` adds to a lens run's ``provider_config``, flat beside the
# adapter's own. Disjoint from every key either adapter writes (asserted by a test), so a lens
# run carries the provider's full account of the call and the lens's account of why.
LENS_RECORD_KEYS: Final = frozenset(
    {"lens", "locales", "languages", "domains", "registry_sha256", "query_source",
     "baseline_run_ids"}
)  # fmt: skip


def lens_record(
    call: LensCall,
    entry: LocaleEntry,
    registry: LocaleRegistry,
    baseline_run_ids: Sequence[str],
) -> dict[str, Any]:
    """The lens's account of one call, stored on its run's ``provider_config``.

    ``lens: "local"`` marks the run as a local-source lens run -- the ledger alone answers
    which runs were collect-only -- and the rest says which locale, which of its languages,
    which publishers, under which registry, from which query source, beside which baseline.
    """
    return {
        "lens": "local",
        "locales": [entry.code],
        "languages": list(entry.languages),
        "domains": list(call.domains),
        "registry_sha256": registry.sha256,
        "query_source": call.query_source,
        "baseline_run_ids": list(baseline_run_ids),
    }


def asknews_cost_usd(raw_responses: Sequence[object], attempted: int) -> float | None:
    """What a lens AskNews call billed, from each response's own ``usage.credits``.

    The same figure ``complete_call`` settles the reservation from (M1-336); the run row's
    ``cost_usd`` stays ``None`` for AskNews, as on the baseline path, so the lens records it on
    its checkpoint instead. Unknown is never free: ``None`` unless every attempted request
    returned a response whose credits convert. No request attempted (a refused reservation)
    is ``0.0``.
    """
    if attempted == 0:
        return 0.0
    if len(raw_responses) != attempted:
        return None
    total = 0
    for response in raw_responses:
        microusd = credits_microusd(response)
        if microusd is None:
            return None
        total += microusd
    return total / 1_000_000


def _opening(run_id: str, question_id: int, provider: LensProvider, now: datetime) -> Any:
    return validate_run(
        {
            "retrieval_run_id": run_id,
            "question_id": question_id,
            "provider": provider,
            "started_at_utc": now,
        }
    )


def run_lens(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question: CanonicalQuestion,
    now: datetime,
    registry: LocaleRegistry,
    baseline_run_ids: Sequence[str],
    news_client: Any | None,
    web_client: Any | None,
) -> LensOutcome:
    """Buy and record every planned lens call for ``question``.

    A planned call whose client is unavailable (no Exa credential) is recorded as skipped,
    not raised. A provider failure is the adapter's to record on its run, as on the baseline
    path. Each call's run row is opened before its billable request, so a process that dies
    mid-call still leaves the spend on the ledger.
    """
    started = time.monotonic()
    calls = plan(question, registry, max_calls=config.retrieval.local_lens.max_calls_per_question)
    results: list[LensRun] = []
    for call in calls:
        entry = registry.get(call.locale_code)
        if entry is None:  # unreachable: plan() only names registry locales
            continue
        client = news_client if call.provider == "asknews" else web_client
        if client is None:
            results.append(
                LensRun(call.locale_code, call.provider, None, 0, 0, 0, None, False, "no_client")
            )
            continue
        run_id = _RUN_ID_PREFIX + uuid.uuid4().hex
        record = lens_record(call, entry, registry, baseline_run_ids)
        open_run(conn, _opening(run_id, question.question_id, call.provider, now))
        if call.provider == "asknews":
            news = retrieve_news(
                client,
                config,
                question_id=question.question_id,
                queries=[call.query],
                retrieval_run_id=run_id,
                now=now,
                lens=LensRequest(domains=call.domains, record=record),
            )
            run, documents, raw = news.run, news.documents, news.raw_responses
            dropped, collapsed = news.documents_dropped, news.duplicates_collapsed
            failed, attempted = news.provider_failed, news.calls_attempted
            cost = asknews_cost_usd(raw, attempted)
        else:
            web = retrieve_web(
                client,
                config,
                question_id=question.question_id,
                queries=[call.query],
                retrieval_run_id=run_id,
                now=now,
                fallback_reasons=["local_source_lens"],
                include_domains=call.domains,
                lens_record=record,
            )
            run, documents, raw = web.run, web.documents, web.raw_responses
            dropped, collapsed = web.documents_dropped, web.duplicates_collapsed
            failed, attempted = web.provider_failed, web.calls_attempted
            cost = run.cost_usd
        rows = locale_rows(call, entry, documents, raw)
        persist_paid_run(
            conn,
            config,
            with_retrieval_counts(run, documents_dropped=dropped, duplicates_collapsed=collapsed),
            documents,
            raw_responses=raw,
            written_at_utc=now,
            run_opened=True,
            locales=rows,
        )
        results.append(
            LensRun(
                locale_code=call.locale_code,
                provider=call.provider,
                retrieval_run_id=run_id,
                documents=len(documents),
                locales=len(rows),
                calls=attempted,
                cost_usd=cost,
                provider_failed=failed,
            )
        )
    return LensOutcome(
        question_id=question.question_id,
        runs=tuple(results),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
