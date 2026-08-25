"""Exa fallback retrieval adapter: web/official evidence, and why it was reached (M1-303).

Exa is the *fallback* provider (decision D18): it runs only when AskNews fails or
when official-source/web retrieval is required -- never as an unannounced
substitute, and never merely because AskNews succeeded with nothing to show for
it (a provider that answers with zero documents has not *failed*). That last
clause is the whole point of this module's shape, so it is enforced by the API
rather than by convention:

- :func:`decide_fallback` is the pure policy, gated on exactly those two
  conditions. It returns **every** trigger that holds when it does run, not a
  winner, because each one is a distinct fact about the run -- including
  ``primary_returned_no_documents`` as an additional fact alongside a real
  trigger, though it cannot authorize a run on its own.
- :func:`retrieve_web` **requires** a non-empty ``fallback_reasons`` argument,
  and requires at least one of those reasons to be an authorizing one
  (``primary_provider_failed`` or ``official_source_required``):
  ``primary_returned_no_documents`` alone is rejected, matching
  :func:`decide_fallback`'s own policy that it cannot authorize a run by
  itself. There is no way to spell an Exa call that does not say why it
  happened, nor one authorized only by a non-authorizing fact.
- The reasons are persisted on the run (``provider_config["fallback_reasons"]``,
  stored in ``research_runs.provider_config_json``) and logged once, so the
  switch is auditable from the ledger alone.
- :func:`build_exa_client` refuses to build a client when the configured
  fallback provider is not ``exa``. Running Exa while config names something
  else *is* the silent switch. :func:`retrieve_web` repeats the same check
  independently, so a caller holding any ``httpx.Client`` -- not only one
  built by :func:`build_exa_client` -- cannot use it to run Exa against a
  config that never named Exa at all.
- Config alone was not enough, though: it proves what was *configured*, not
  where the ``client`` argument sends a request. :func:`retrieve_web` also
  requires the URL this module *builds* from the client to be exactly Exa's
  search endpoint (:func:`_require_exa_client`), because a client pointed at
  another host while the run records ``provider="exa"`` is the same silent
  switch reached from the other side. Redirects are refused rather than
  followed, for the same reason and one worse: ``httpx`` forwards every header
  but ``Authorization`` across origins, so a followed redirect hands
  ``x-api-key`` to whatever host the response named. That claim is about the
  request URL, and nothing stronger -- a caller-supplied transport or
  ``event_hooks`` can still send the bytes elsewhere, and this module does not
  attempt to close that boundary (see :func:`_require_exa_client`).
- ``include_domains`` accepts only bare hostnames with something registrable
  beyond the public-suffix boundary, canonicalized through the same code that
  canonicalizes result URLs. Exa also documents path and wildcard filters; this
  module refuses them rather than forward a restriction it cannot then verify
  per result. A bare public suffix -- single-label (``com``) or multi-label
  (``co.uk``, M1-311) -- is refused because the subdomain rule below would then
  label every host beneath it ``official``. See :func:`_validated_domains`.

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
from urllib.parse import urlsplit

import httpx
from publicsuffixlist import PublicSuffixList

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.research.canonical import CanonicalizationError, canonicalize_url
from whiskeyjack_bot.research.dedup import deduplicate
from whiskeyjack_bot.research.hashing import content_sha256
from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    SourceType,
    validate_document,
    validate_run,
)
from whiskeyjack_bot.research.preflight import require_run_metadata as _shared_run_metadata
from whiskeyjack_bot.research.preflight import string_list as _shared_string_list
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

# The subset of _FALLBACK_REASONS that can put should_run at True in
# decide_fallback. primary_returned_no_documents is a true fact worth
# persisting alongside one of these, but per the module docstring it cannot
# authorize a fallback call on its own -- retrieve_web enforces that directly,
# since nothing upstream of it does yet.
_AUTHORIZING_REASONS: Final[frozenset[FallbackReason]] = frozenset(
    {"primary_provider_failed", "official_source_required"}
)

_BASE_URL: Final = "https://api.exa.ai"
_SEARCH_PATH: Final = "/search"

# The one URL a request from this module may carry. Compared against the URL the
# *client* builds for _SEARCH_PATH, not against its base_url -- see
# _require_exa_client for the spellings that difference rejects.
_EXPECTED_REQUEST_URL: Final = httpx.URL(f"{_BASE_URL}{_SEARCH_PATH}")

# Characters that mean an allowlist entry is not a bare host: a path or wildcard
# filter, a scheme, a port, userinfo, or an escape. See _validated_domains for
# why those forms are refused rather than forwarded unverifiable.
_DISALLOWED_IN_DOMAIN: Final[frozenset[str]] = frozenset("/:@*?#%[]\\ \t\r\n\v\f")

# Built once from the bundled offline snapshot (M1-311): no network fetch, so this
# is safe to construct at import time under the socket-blocked test suite. See
# _validated_domains for what it's used to refuse.
_PUBLIC_SUFFIXES: Final = PublicSuffixList()

# One constant for every allowlist refusal: the entry is caller content, and a
# message that named which rule it broke would narrow it. The public-suffix
# rule is named here rather than given its own message for exactly that reason.
_BAD_DOMAIN: Final = (
    "include_domains entries must be bare hostnames of at least two labels -- no "
    "scheme, path, port, userinfo or wildcard, and no bare public suffix "
    "(offending input withheld)"
)

# The container refusal for fallback_reasons. Separate from the vocabulary
# message below, which names the vocabulary itself: this one is about the shape
# of the argument, and says nothing about what was in it.
_BAD_REASONS: Final = (
    "fallback_reasons must be a sequence of reason strings (offending input withheld)"
)

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

# Exa's documented ceiling for ``numResults``. ``RetrievalConfig.max_documents_per_query``
# only enforces ``ge=1`` -- it is shared with the AskNews adapter's own, differently
# bounded, ``n_articles`` -- so a configured value above this is capped here rather than
# sent as-is: an oversized request is rejected by Exa outright, which would otherwise turn
# a configuration choice into a full run failure.
_MAX_NUM_RESULTS: Final = 100


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
    """Whether the fallback should run, and every relevant fact about why.

    ``reasons`` is a tuple in :data:`_FALLBACK_REASONS` order, so two runs with
    the same facts persist byte-identical reason lists, and it is empty whenever
    ``should_run`` is ``False``. It holds *all* the facts that apply when the
    fallback does run, not the highest-priority one: "AskNews raised" and
    "AskNews returned nothing" are different facts about a run, and collapsing
    them to a single winner discards attribution for no benefit -- even though,
    per :func:`decide_fallback`, only the first of those two (or an explicit
    official-source requirement) can put ``should_run`` at ``True`` in the first
    place.
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

    ``should_run`` is exactly the backlog's two conditions ("use Exa only when
    AskNews fails or when official-source/web retrieval is required") -- a
    provider that answered with zero documents has not *failed*, so an
    all-success run with nothing retained and no official-source requirement
    does not, by itself, authorize a paid call. When the fallback *does* run for
    one of the two real reasons, ``primary_returned_no_documents`` is still
    reported alongside it if it also holds: it is a true fact worth persisting,
    just not an independent trigger.

    ``primary_documents`` is the count of documents the primary provider
    *retained*, not the count it returned.

    All three arguments are gated on their exact types. The two flags are not
    tested for truthiness: ``primary_failed="false"`` is truthy, and would
    otherwise authorize a paid call and persist ``primary_provider_failed`` as
    the reason it happened -- a fabricated attribution, from a caller mistake
    the annotation cannot catch at runtime (cross-model review round 4,
    finding 3).
    """
    if not isinstance(primary_failed, bool):
        # Constant message: the value could be anything a caller computed.
        raise ExaFallbackError("primary_failed must be a bool (offending input withheld)")
    if not isinstance(official_source_required, bool):
        raise ExaFallbackError("official_source_required must be a bool (offending input withheld)")
    if isinstance(primary_documents, bool) or not isinstance(primary_documents, int):
        raise ExaFallbackError("primary_documents must be an int (offending input withheld)")
    if primary_documents < 0:
        raise ExaFallbackError("primary_documents must not be negative")

    should_run = primary_failed or official_source_required
    if not should_run:
        return FallbackDecision(should_run=False, reasons=())

    reasons: list[FallbackReason] = []
    if primary_failed:
        reasons.append("primary_provider_failed")
    if primary_documents == 0:
        reasons.append("primary_returned_no_documents")
    if official_source_required:
        reasons.append("official_source_required")
    return FallbackDecision(should_run=True, reasons=_canonical_reasons(reasons))


def _canonical_reasons(reasons: Sequence[str]) -> tuple[FallbackReason, ...]:
    """Validate reasons and return them deduplicated, in vocabulary order.

    Order and duplicates are normalized rather than preserved because this tuple
    is persisted: two runs triggered by the same facts must produce the same
    stored list regardless of the order a caller assembled them in, or replay
    comparisons turn on caller bookkeeping.

    The container goes through :func:`_string_list` for the reason every other
    caller argument does: ``fallback_reasons=None`` used to raise a raw
    ``TypeError: 'NoneType' object is not iterable``, and an ``__iter__`` that
    raised escaped as whatever it threw -- the hardening round 4 applied to
    ``queries`` and ``include_domains`` had simply skipped this one argument
    (cross-model review round 5, finding 6). :func:`decide_fallback` passes a
    list it built itself, so it is unaffected.
    """
    entries = _string_list(reasons, _BAD_REASONS)
    unknown = [reason for reason in entries if reason not in _FALLBACK_REASONS]
    if unknown:
        # The offending value is withheld; the vocabulary itself is ours to name.
        raise ExaFallbackError(
            "fallback reason is not in the vocabulary "
            f"({', '.join(_FALLBACK_REASONS)}); offending input withheld"
        )
    present = set(entries)
    return tuple(reason for reason in _FALLBACK_REASONS if reason in present)


def _ensure_exa_is_configured_fallback(config: AppConfig) -> None:
    """Refuse to proceed unless ``config`` actually names Exa as the fallback.

    Shared by :func:`build_exa_client` and :func:`retrieve_web` so the check
    cannot be bypassed by calling ``retrieve_web`` with a client built some
    other way -- the client carries no memory of which config built it, so
    the check has to be repeated at the point that actually spends money.

    Two refusals:

    - the configured fallback provider is not ``exa`` -- calling Exa anyway
      would be precisely the silent provider switch this item forbids;
    - the configured *primary* provider is not ``asknews`` -- this adapter
      implements the AskNews-to-Exa fallback specifically, and a config that
      names Exa as its own primary would let Exa "fall back" to itself, which
      is the same silent switch under a different config shape.
    """
    if config.retrieval.fallback.provider != "exa":
        raise ExaFallbackError(
            "retrieval.fallback.provider is not 'exa'; refusing to run the Exa "
            "adapter against a differently configured fallback (no silent provider switching)"
        )
    if config.retrieval.primary.provider != "asknews":
        raise ExaFallbackError(
            "retrieval.primary.provider is not 'asknews'; refusing to run the Exa fallback "
            "adapter against a configuration where Exa is not a fallback at all "
            "(no silent provider switching)"
        )


def _require_exa_client(client: httpx.Client) -> None:
    """Refuse a client that does not address the Exa API.

    The config checks above prove that *configuration* names Exa; they say
    nothing about where the ``client`` argument actually sends a request. A
    client built with ``base_url="https://other-provider.example"`` posts to
    that host while this module writes ``provider="exa"`` on the run -- the
    silent provider switch, arrived at from the other side (cross-model review
    round 4, finding 1).

    The check is the **actual merged request URL**, not a decomposition of
    ``base_url``. Round 4 compared ``(scheme, host, port, path.rstrip("/"),
    userinfo)``, which reads like a stricter test than a string comparison but
    is a looser one: it never looks at the query or the fragment, and
    ``rstrip("/")`` collapses repeated slashes. Four client shapes passed it and
    then addressed something other than ``/search`` (cross-model review round 5,
    finding 2) -- note that the last is not a ``base_url`` at all, which is why
    the fix is not "also compare the query and the fragment"::

        base_url="https://api.exa.ai?x=1"  ->  https://api.exa.ai/?x=1/search
        base_url="https://api.exa.ai//"    ->  https://api.exa.ai//search
        base_url="https://api.exa.ai#f"    ->  https://api.exa.ai/search#f
        params={"k": "v"}                  ->  https://api.exa.ai/search?k=v

    Asking ``httpx`` to build the request instead removes the guesswork: it is
    the same merge ``.post()`` performs, so what is compared is what would be
    sent, and one ``httpx.URL`` equality covers scheme, host, port, path, query,
    fragment and userinfo at once. Two spellings still pass, as they must --
    ``https://api.exa.ai`` and ``https://api.exa.ai/`` merge to the same URL.

    What this does **not** do, deliberately, is constrain the transport: every
    test injects a ``MockTransport`` client, and the ledger's claim is about
    which service was asked, not which socket layer carried it. A marker type
    only :func:`build_exa_client` could produce was considered and rejected in
    round 2 for that same reason. So the claim is bounded to exactly this: **the
    request URL this module builds is Exa's search endpoint**. A caller-supplied
    ``transport`` or ``event_hooks`` -- a request hook may rewrite ``request.url``
    after it is built -- can still direct the bytes elsewhere, and that remains a
    trusted boundary this module does not close (round 5, non-blocking
    observation 2). An absolute URL passed to ``.post()`` would bypass
    ``base_url`` entirely, but no caller can reach that: the path is the module
    constant ``_SEARCH_PATH``.
    """
    try:
        # Anything the client merges in reaches the URL here: base_url, and also
        # client-level `params`. A client is an arbitrary caller object, so a
        # build that raises must arrive as this module's error like every other
        # malformed shape rather than as whatever it happened to throw.
        request = client.build_request("POST", _SEARCH_PATH)
    except Exception:
        raise ExaFallbackError(
            "client base_url does not address the Exa API; refusing to run the Exa adapter "
            "against another destination (no silent provider switching; "
            "offending input withheld)"
        ) from None
    if request.url != _EXPECTED_REQUEST_URL:
        raise ExaFallbackError(
            "client base_url does not address the Exa API; refusing to run the Exa adapter "
            "against another destination (no silent provider switching; "
            "offending input withheld)"
        )


def _string_list(values: Sequence[str], message: str) -> list[str]:
    """Return ``values`` as a list of non-blank strings, or raise ``ExaFallbackError``.

    A thin binding of :func:`whiskeyjack_bot.research.preflight.string_list` to this
    module's own error type -- the guard is shared with the AskNews adapter (M1-309)
    rather than duplicated; see that function's docstring for the full rationale
    (cross-model review round 4, finding 2; round 5, finding 6).
    """
    return _shared_string_list(values, message, error=ExaFallbackError)


def _require_run_metadata(*, question_id: int, retrieval_run_id: str, now: datetime) -> datetime:
    """Refuse caller metadata the run record would reject, and return ``now`` in UTC.

    A thin binding of :func:`whiskeyjack_bot.research.preflight.require_run_metadata` to
    this module's own error type -- the guard is shared with the AskNews adapter (M1-309)
    rather than duplicated; see that function's docstring for the full rationale
    (cross-model review round 4, finding 4; round 5, finding 3 and non-blocking
    observation 1).
    """
    return _shared_run_metadata(
        question_id=question_id,
        retrieval_run_id=retrieval_run_id,
        now=now,
        error=ExaFallbackError,
    )


def build_exa_client(config: AppConfig) -> httpx.Client:
    """Construct the one configured Exa client.

    Three refusals, all before any network use and therefore before any billable
    call: the two config checks in :func:`_ensure_exa_is_configured_fallback`,
    plus:

    - the configured key variable is unset or empty (an empty string counts as
      missing), which raises ``MissingCredentialError``.

    Retries are applied to the connection pool after construction rather than
    through ``transport=``; see
    :func:`whiskeyjack_bot.research.transport.apply_connection_retries` for why
    an explicit transport would silently drop ``HTTP(S)_PROXY`` routing, and for
    the scope of what a retry covers (connection failures only, so a request
    that reached Exa is never re-sent and cannot be billed twice).
    """
    _ensure_exa_is_configured_fallback(config)
    provider = config.retrieval.fallback
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise MissingCredentialError(provider.api_key_env)
    client = httpx.Client(
        base_url=_BASE_URL,
        timeout=provider.timeout_seconds,
        # httpx's default, stated rather than inherited: a redirect must never be
        # followed with the API key attached. `retrieve_web` pins it at the call
        # site too, since it accepts clients this function did not build.
        follow_redirects=False,
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

    Refuses, before any network use, to run against a ``config`` that does not
    name Exa as its fallback (see :func:`_ensure_exa_is_configured_fallback`) --
    repeated here rather than trusted from :func:`build_exa_client`, since the
    ``client`` argument carries no memory of which config built it.

    ``fallback_reasons`` is required and must be non-empty: this adapter cannot
    be invoked without recording why the pipeline left its primary provider.
    Pass :attr:`FallbackDecision.reasons` from :func:`decide_fallback`.

    ``include_domains`` restricts the search to a caller-supplied allowlist and
    is sent to Exa as ``includeDomains``, but Exa's own enforcement of that
    restriction is not this module's to trust: each *result* is promoted from
    ``web`` to ``official`` only if its own URL's host actually matches (or is a
    subdomain of) an allowlisted domain -- see :func:`_matches_official_domain`.
    A result whose host falls outside the allowlist stays ``web`` even though
    the run requested official domains and Exa returned it anyway. Tagging on
    the strength of the *reason* instead would let ``official_source_required``
    label whatever the open web returned, which is an unearned attribution
    claim.

    ``now`` is injected rather than read from the clock so ``started_at_utc``,
    every ``retrieved_at_utc`` and the published-date bound are deterministic
    under test and under replay. It is **converted to UTC once, in preflight**
    (:func:`_require_run_metadata`), and that value is what the run, the
    documents and ``startPublishedDate`` all carry: the same instant a caller
    passed, spelled independently of the timezone they spelled it in.

    **Never raises on provider failure.** A run makes up to
    ``max_queries_per_question`` billable calls; raising partway through would
    discard the record of every call already paid for. On failure this stops
    early, sets ``provider_failed``, records it in ``run.error_summary``, and
    returns everything retrieved so far.
    """
    _ensure_exa_is_configured_fallback(config)
    _require_exa_client(client)
    reasons = _canonical_reasons(fallback_reasons)
    if not reasons:
        raise ExaFallbackError(
            "fallback_reasons must be non-empty: an Exa call has to record why "
            "it ran (no silent provider switching)"
        )
    if not any(reason in _AUTHORIZING_REASONS for reason in reasons):
        raise ExaFallbackError(
            "fallback_reasons must include primary_provider_failed or "
            "official_source_required; primary_returned_no_documents cannot "
            "authorize the fallback on its own"
        )
    # Normalized once, here, and used everywhere below: converting at the end
    # instead let an upper-bound datetime bill a call and then raise (round 5,
    # finding 3). It also makes the persisted `start_published_date` independent
    # of the caller's timezone spelling, agreeing with the run's own UTC columns.
    now_utc = _require_run_metadata(
        question_id=question_id, retrieval_run_id=retrieval_run_id, now=now
    )
    validated_queries = _string_list(
        queries, "queries entries must be non-blank strings (offending input withheld)"
    )
    domains = _validated_domains(include_domains)

    retrieval = config.retrieval
    capped_queries = validated_queries[: retrieval.max_queries_per_question]
    # Exa's own contract, not a general retrieval invariant: capped here rather than in
    # the shared config schema. See _MAX_NUM_RESULTS.
    num_results = min(retrieval.max_documents_per_query, _MAX_NUM_RESULTS)
    try:
        published_after = now_utc - timedelta(days=retrieval.freshness_days_default)
    except OverflowError:
        # An aware datetime near datetime.min: the freshness bound falls outside
        # the representable range. A caller mistake like any other here, and it
        # must arrive as this module's error rather than a raw OverflowError.
        raise ExaFallbackError(
            "now is too early to compute a freshness bound (offending input withheld)"
        ) from None

    # Logged once, before the first call, so an engagement is on the record even
    # if every call then fails. Constants and an integer id only -- no query
    # text, no URLs, nothing provider-derived. `%d` is safe *only* because
    # _require_run_metadata has already proved question_id is an int: given a
    # string, logging fails to interpolate and prints the raw argument to
    # stderr in its own error report, which is a value leak by another route.
    _LOGGER.info(
        "exa fallback engaged for question %d (reasons: %s)",
        question_id,
        ", ".join(reasons),
    )

    raw_responses: list[dict[str, Any]] = []
    documents: list[ResearchDocument] = []
    dropped = 0
    provider_failed = False
    cost_total = 0.0
    # A published cost_usd has to be the whole run's spend, not whichever calls
    # happened to report a usable figure. calls_attempted counts every billable
    # attempt (including one that then raises, or returns a malformed body);
    # calls_with_cost counts only those that yielded a usable costDollars.total.
    # cost_reported is derived below, only once every attempted call matched.
    calls_attempted = 0
    calls_with_cost = 0

    for query in capped_queries:
        payload: dict[str, Any] = {
            "query": query,
            "type": _SEARCH_TYPE,
            "numResults": num_results,
            "startPublishedDate": published_after.isoformat(),
            "contents": {
                "text": {"maxCharacters": _TEXT_MAX_CHARACTERS},
                "maxAgeHours": _MAX_AGE_HOURS,
            },
        }
        if domains:
            payload["includeDomains"] = domains

        calls_attempted += 1
        try:
            # follow_redirects is pinned at the call site, not left to the
            # client's default: httpx strips `Authorization` when a redirect
            # leaves the origin but forwards every other header, so following one
            # would hand `x-api-key` to whatever host the response named -- while
            # the run still recorded provider="exa". The same silent switch
            # _require_exa_client refuses, arrived at from a third side
            # (cross-model review round 5, finding 1).
            response = client.post(_SEARCH_PATH, json=payload, follow_redirects=False)
            if response.is_redirect:
                # Refused on its own terms rather than left to raise_for_status.
                # The pinned httpx does treat a 3xx as an error status, so this
                # branch is belt and braces today -- but relying on that would
                # mean a redirect carrying a JSON body parsing as a real answer
                # from a host that never sent one, the day it stopped. `None` is
                # not a dict, so the contract-breach branch below stops the run
                # without a second code path.
                body: Any = None
            else:
                response.raise_for_status()
                body = response.json()
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
            calls_with_cost += 1

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
        for result in results[:num_results]:
            try:
                payload_document = _to_document(
                    result,
                    retrieval_run_id=retrieval_run_id,
                    retrieved_at=now_utc,
                    domains=domains,
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

            documents.append(document)

    # Two queries can surface the same page, and research_documents carries
    # UNIQUE (retrieval_run_id, canonical_url, content_sha256). Every document
    # here shares one retrieval_run_id, so this collapses on the same
    # (canonical_url, content_sha256) identity a local set would -- but picks
    # the survivor via M1-305's deterministic total order instead of
    # first-seen, so the retained author/URL does not depend on provider
    # result order (cross-model review round 2, finding 2).
    dedup_result = deduplicate(documents)

    # Complete-or-nothing: a total is only trustworthy when every attempted
    # call -- including one that raised or returned a malformed body -- also
    # yielded a usable cost. Anything less is a subtotal, and publishing a
    # subtotal as cost_usd would make an incomplete figure look complete
    # (cross-model review round 3, finding 3).
    cost_reported = calls_attempted > 0 and calls_with_cost == calls_attempted
    if cost_reported and not isfinite(cost_total):
        # Each call's own cost was finite (_call_cost_usd already checked
        # isfinite); only the sum overflowed. Drop it the same way an
        # unusable per-call cost is dropped, rather than let validate_run's
        # finite-check turn two billed, successful calls into a run failure
        # (cross-model review round 2, finding 3).
        cost_reported = False

    run = validate_run(
        {
            "retrieval_run_id": retrieval_run_id,
            "question_id": question_id,
            "provider": "exa",
            "provider_config": {
                "endpoint": _SEARCH_PATH,
                "search_type": _SEARCH_TYPE,
                "num_results": num_results,
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
            "started_at_utc": now_utc,
            "completed_at_utc": now_utc,
            "freshness_cutoff_utc": published_after,
            "error_summary": _error_summary(
                provider_failed=provider_failed, retained=len(dedup_result.documents)
            ),
            # Exa reports a per-request dollar estimate, unlike AskNews's
            # unconvertible credits, so this is a real figure -- but the API
            # documents it as an estimate and not an invoice record.
            "cost_usd": cost_total if cost_reported else None,
        }
    )

    return ExaRetrieval(
        run=run,
        documents=dedup_result.documents,
        raw_responses=tuple(raw_responses),
        documents_dropped=dropped,
        duplicates_collapsed=dedup_result.collapsed_count,
        provider_failed=provider_failed,
        fallback_reasons=reasons,
    )


def _validated_domains(include_domains: Sequence[str]) -> list[str]:
    """Return the allowlist as canonical bare hosts, or refuse it.

    Validated rather than trusted because it is persisted into
    ``provider_config``, sent to the provider, and matched against every result:
    a non-string entry would fail at ``model_dump_json`` time inside a later
    ledger write, long after the billable calls happened.

    Two rules, both from cross-model review round 4, finding 5.

    **Only bare hosts are accepted.** Exa's ``includeDomains`` also documents
    path prefixes (``exa.ai/blog``) and subdomain wildcards (``*.substack.com``).
    This module forwarded both and could never match either, so every result they
    selected stayed ``web`` -- the run asked for official sources, Exa honoured
    it, and the ledger under-attributed the answer. Since per-result
    verification is the whole design (Exa's enforcement is not ours to trust), a
    filter shape this module cannot verify is one it must not accept: refused
    here, before the network, rather than silently under-labelled afterwards.
    Widening to those forms means pinning down semantics Exa does not document
    at the edges (does ``*.substack.com`` match bare ``substack.com``?), which
    is a deliberate future change, not an adaptation to make in passing.

    **Accepted entries are canonicalized**, through the same public
    :func:`canonicalize_url` that produced the ``canonical_url`` they are
    compared against -- that shared code path *is* the fix. Previously an entry
    was merely lowercased, so the IDN ``bücher.de`` never matched a result at
    ``https://bücher.de/``, whose canonical host is the A-label
    ``xn--bcher-kva.de``. The canonical form is what gets sent to Exa and what
    reaches ``provider_config["include_domains"]``: the ledger records the
    filter that was actually applied and matched, not the caller's spelling of
    it.

    The character screen is not redundant with canonicalization.
    ``canonicalize_url`` *silently drops* a port and userinfo (``bls.gov:443``
    and ``user@bls.gov`` both reduce to ``bls.gov``), so an entry meaning
    something other than a bare host has to be refused before it is normalized
    into one that looks fine.

    Two further rules, both from cross-model review round 5.

    **One terminal DNS root dot is normalized away** (finding 4), now by
    ``canonicalize_url`` itself. ``bls.gov.`` and ``bls.gov`` -- two valid
    spellings of one DNS host -- canonicalized to two different strings and never
    matched each other in either direction. Round 5 stripped the dot locally here,
    deliberately leaving canonical form alone on a review branch and filing the
    identity question as M1-310; **M1-310 settled it in the canonicalizer** (D32),
    so the local strip is gone and both sides of every comparison below get the
    rule from the one function that owns it.

    **Single-label entries are refused** (finding 5). ``include_domains=("com",)``
    was accepted, and :func:`_matches_official_domain`'s subdomain rule then
    labelled ``https://attacker.com/report`` ``official`` -- a false attribution
    claim in the one place this project says it will not make one. Round 4
    deferred this as allowlist policy beyond the finding; round 5 demonstrated it
    is not policy but a defect, and the deferral is reversed.

    **Multi-label public suffixes are refused too** (M1-311). The single-label
    rule above left ``include_domains=("co.uk",)`` accepted -- two labels, so it
    passed -- and every host beneath ``co.uk`` was still labelled ``official``.
    Round 5 named this residual rather than half-fixing it on a review branch,
    because closing it needs a real public-suffix list (a new dependency, and
    therefore a wave-level decision, not a review fix). ``_PUBLIC_SUFFIXES``
    is that list: an entry is refused unless it has something registrable
    *beyond* the public-suffix boundary, which subsumes the single-label rule
    (a lone label is never more than a suffix) and closes the multi-label gap.
    """
    domains = _string_list(
        include_domains,
        "include_domains entries must be non-blank strings (offending input withheld)",
    )
    canonical: list[str] = []
    for domain in domains:
        if _DISALLOWED_IN_DOMAIN.intersection(domain) or domain.strip() != domain:
            raise ExaFallbackError(_BAD_DOMAIN)
        try:
            host = urlsplit(canonicalize_url(f"https://{domain}")).hostname
        except CanonicalizationError:
            raise ExaFallbackError(_BAD_DOMAIN) from None
        if host is None:
            raise ExaFallbackError(_BAD_DOMAIN)
        # A host with nothing registrable beyond the public-suffix boundary would
        # let _matches_official_domain's subdomain rule label every host beneath
        # it `official` -- true of a bare single label ("com", "gov": M1-303
        # round 5) and equally true of a multi-label public suffix ("co.uk",
        # "com.au": M1-311). Canonicalization has already removed the root dot
        # (M1-310), so `gov.` is refused here for the same reason `gov` is.
        if _PUBLIC_SUFFIXES.privatesuffix(host) is None:
            raise ExaFallbackError(_BAD_DOMAIN)
        canonical.append(host)
    return canonical


def _matches_official_domain(canonical_url: str, domains: Sequence[str]) -> bool:
    """Return whether ``canonical_url``'s host earns the ``official`` label.

    Exact match or subdomain match against ``domains`` -- ``data.bls.gov``
    counts as a match for ``bls.gov``, since a federal agency's data desk is
    still the agency. Exa's own enforcement of ``includeDomains`` is not this
    module's to trust (Exa can return a result outside it), so each result's
    host is checked here independently rather than assuming the run-level
    allowlist applies to every result it returned.

    Both sides are already canonical: ``domains`` comes from
    :func:`_validated_domains` and the URL from :func:`canonicalize_url`, so
    the comparison is exact. It deliberately does **not** lowercase or IDNA-fold
    here as well -- normalizing at the comparison would mask an un-normalized
    allowlist reaching this function, and only one of the two forms would be
    fixed by it (a U-label entry needs IDNA, not ``str.lower``).

    The terminal DNS root dot used to need handling here as well: the URL is
    provider-derived, ``canonicalize_url`` preserved whichever spelling arrived,
    and ``https://bls.gov./report`` was therefore labelled ``web`` against a
    ``bls.gov`` allowlist (cross-model review round 5, finding 4). **M1-310 moved
    that rule into ``canonicalize_url``** (D32), so both sides now arrive without
    the dot and the local strip that round 5 added has been removed rather than
    kept as a second copy of one rule.

    Total: any string pair is a valid question with a ``bool`` answer.
    """
    host = urlsplit(canonical_url).hostname
    if host is None:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _to_document(
    result: Any,
    *,
    retrieval_run_id: str,
    retrieved_at: datetime,
    domains: Sequence[str],
) -> dict[str, Any]:
    """Build the document payload for one Exa result (unvalidated).

    Raises a constant-message ``TypeError`` for a result that is not a mapping
    or carries no URL string; :func:`retrieve_web` counts those as drops.

    ``source_type`` is decided per result, from its own URL, via
    :func:`_matches_official_domain` -- not inherited from whether the run
    requested an allowlist at all. See that function and the module docstring
    for why the run-level allowlist alone is not proof for every result.

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

    canonical_url = canonicalize_url(url)
    source_type: SourceType = (
        "official" if _matches_official_domain(canonical_url, domains) else "web"
    )
    text = _optional_text(result.get("text"))
    return {
        "retrieval_run_id": retrieval_run_id,
        "original_url": url,
        "canonical_url": canonical_url,
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
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        # A syntactically valid boundary timestamp (e.g. 0001-01-01T00:00:00+14:00)
        # whose UTC conversion falls outside datetime's representable range. Same
        # rule as an unparseable date: unusable, not a reason to lose the citation.
        return None


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
    try:
        value = float(total)
    except OverflowError:
        # A JSON integer too large for a float (e.g. 10**400): unusable, dropped
        # the same as any other malformed cost rather than crashing a paid run.
        return None
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
