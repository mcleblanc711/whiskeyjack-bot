"""Canonical research-run and research-document schema (M1-301).

Every retrieval provider normalizes into these two models: AskNews (M1-302),
Exa (M1-303), the structured-source router (M1-304) and the xAI X Search agent
(M1-307). Fixing the shape here is what lets those adapters be swapped, and
what lets the ledger store one comparable evidence record regardless of where a
document came from.

The two models mirror the two ledger tables (``research_runs`` and
``research_documents``, M1-601). Two fields have no column in the initial
migration and are added by ``002_research_document_fields.sql``:

- ``provenance`` -- introduced by the brief's X-adapter amendment after M1-601
  shipped, and explicitly assigned to M1-301 to backfill across adapters. It
  separates a document the pipeline retrieved itself (``direct_api``) from one a
  research agent *told* us about (``llm_reported``). The forecaster prompt caps
  how load-bearing the latter may be, so the distinction has to survive storage.
- ``original_url`` -- the URL exactly as the provider returned it. M1-305 will
  rewrite ``canonical_url`` for dedup; without this field the as-retrieved URL
  would be unrecoverable, which is an attribution loss.

The mirroring is not field-for-field, and the two departures are deliberate:

- ``created_at_utc`` (NOT NULL in both tables) is **writer-owned metadata**, not
  adapter data. It records when the ledger stored the row, so only the write
  path (M1-602) may set it; letting an adapter supply it would let a caller
  backdate its own audit trail. The same reasoning as ``document_id``.
- Two fields are stored serialized: ``provider_config`` maps to the
  ``provider_config_json`` TEXT column and ``queries`` to ``queries_json``.
  Hence ``provider_config`` is typed ``dict[str, JsonValue]`` -- a value that
  cannot round-trip through JSON cannot be persisted, so it is not valid here.

Vocabularies are closed ``Literal`` sets. The handoff does not enumerate
``source_type``, so this module enumerates it (ambiguity rule 4: implement the
stricter reading) -- an unrecognized source type is a normalization bug and must
fail loudly rather than land in the ledger as a free-text label.

Models are strict (``extra="forbid"``, reusing ``config._StrictModel``). Use
:func:`validate_document` / :func:`validate_run` rather than bare
``model_validate``: pydantic's own error rendering echoes the offending input,
and a research document can hold arbitrary retrieved text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from whiskeyjack_bot.config import _StrictModel

# Where a document came from. ``structured`` is the M1-304 router's official
# dataset path (FRED and friends); ``official`` is a primary-source web document
# reached through ordinary retrieval; ``social`` is the X adapter (M1-307).
SourceType = Literal["news", "web", "official", "structured", "social"]

# How we came to hold the document. ``direct_api`` means the pipeline fetched it;
# ``llm_reported`` means a research agent reported it and its content and
# timestamps are claims, not verified facts (brief § B, citation hygiene).
Provenance = Literal["direct_api", "llm_reported"]

# Source-trust tags. This is the canonical set referenced by the header comment
# in config/x_accounts.yaml ("must match schema"); M1-308's allowlist loader
# imports this alias rather than restating the values.
ReliabilityTag = Literal["official_primary", "verified_org", "journalist", "unverified_social"]

# Retrieval providers, matching the config vocabularies in
# ``RetrievalProviderConfig`` and ``SocialRetrievalConfig`` plus the structured
# router, which has no provider credential of its own.
RetrievalProvider = Literal["asknews", "exa", "structured", "xai_x_search"]

_SHA256_HEX = r"^[0-9a-f]{64}$"


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


# Timezone-aware only, normalized to UTC. A naive timestamp is not valid
# provenance (the rule metaculus/snapshots.py already applies to snapshot
# metadata): "published 09:00" is unusable evidence without an offset, and
# freshness windows (M1-305) compare these across providers in different zones.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


# Every rejection from _require_http_url uses this one constant string. The
# message must not vary with the input: a URL is row content, and a "helpful"
# diagnostic naming what was wrong with it is a channel for echoing it.
_BAD_URL = "must be an absolute http(s) URL with a hostname (offending input withheld)"

# C0/C1 control characters. urlsplit *silently deletes* tab, LF and CR (WHATWG
# rule), so "https://exa\nmple.org/a" parses as a clean host while the string we
# would store still carries the newline -- a stored URL that no longer matches
# what any parser sees. Rejected outright rather than stripped: this validator
# does not rewrite, and a control character in a URL is a normalization bug.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


def _require_http_url(value: str) -> str:
    """Check absolute http(s) syntax without rewriting the URL.

    Deliberately *not* canonicalization -- that is M1-305, and this returns the
    string byte-for-byte as the provider sent it. What it rejects is input that
    is not a URL at all: whitespace, a bare title, a relative path, a scheme we
    never retrieve over, a netloc that is only userinfo or a port. Those are
    normalization bugs, and a document whose URL does not resolve is an
    attribution the reader cannot check.
    """
    # urlsplit tolerates surrounding whitespace and strips it, so a value that is
    # only usable after stripping must be caught before parsing: the stored URL
    # is the one we were given, and " https://x/y " is not that URL.
    if value != value.strip():
        raise ValueError(_BAD_URL)
    if _CONTROL_CHARS.intersection(value):
        raise ValueError(_BAD_URL)
    try:
        parts = urlsplit(value)
        # .port parses lazily and raises for an out-of-range or non-numeric port;
        # .hostname is netloc minus userinfo and port, so it is empty for both
        # "https://user@/a" and "https://:443/a", which netloc alone accepts.
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        # from None, and a constant message: urlsplit embeds the offending netloc
        # in some of its own ValueErrors (the NFKC-normalization check does), so
        # letting either the message or the __cause__ through re-leaks the input
        # that this validator exists to withhold.
        raise ValueError(_BAD_URL) from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(_BAD_URL)
    if not hostname:
        raise ValueError(_BAD_URL)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(_BAD_URL)
    return value


# An absolute http(s) URL, preserved exactly. See _require_http_url.
HttpUrlString = Annotated[str, Field(min_length=1), AfterValidator(_require_http_url)]


def _reject_non_finite(value: JsonValue) -> JsonValue:
    """Reject NaN and +/-Inf anywhere inside a provider config.

    ``JsonValue`` admits them because Python floats carry them, but they have no
    JSON representation: ``model_dump_json()`` renders all three as ``null``, so
    a config that validated as ``{"threshold": nan}`` persists as
    ``{"threshold": null}``. Replay would then reconstruct a run against a
    *different* configuration than the one that was validated, silently -- the
    exact class of drift the ledger exists to make impossible.

    Recursive because a provider config is arbitrarily nested; the top-level
    ``dict[str, JsonValue]`` annotation only constrains the outermost layer.
    """
    if isinstance(value, float) and not isfinite(value):
        # No value in the message: config may hold provider-supplied material.
        raise ValueError("must not contain NaN or Infinity: they cannot round-trip through JSON")
    if isinstance(value, dict):
        for nested in value.values():
            _reject_non_finite(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)
    return value


# A JSON value that survives a round trip through the TEXT column it is stored in.
PersistableJson = Annotated[JsonValue, AfterValidator(_reject_non_finite)]


class ResearchSchemaError(Exception):
    """A research run or document failed validation, with inputs withheld.

    Same hygiene rule as ``ConfigError``/``SnapshotError``: pydantic renders the
    offending input in its message, and a research document carries arbitrary
    provider text, so consumers print this exception and never the raw
    ``ValidationError``.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid research record:\n" + "\n".join(f"  - {p}" for p in problems))


class ResearchDocument(_StrictModel):
    """One normalized piece of evidence, from any provider."""

    # Minted by the first writer (M1-602), consistent with how M1-601 deferred
    # forecast_records.record_id: adapters construct documents before the ledger
    # transaction that assigns identity.
    document_id: str | None = None
    retrieval_run_id: str = Field(min_length=1)

    # As returned by the provider; never rewritten. M1-305 derives canonical_url
    # from it, and until then adapters may set the two to the same value.
    original_url: HttpUrlString
    canonical_url: HttpUrlString

    title: str | None = None
    publisher: str | None = None
    author: str | None = None

    published_at_utc: UtcDatetime | None = None
    updated_at_utc: UtcDatetime | None = None
    # Required: a document with no retrieval time cannot be placed in a run's
    # timeline or checked against a freshness window.
    retrieved_at_utc: UtcDatetime

    source_type: SourceType
    provenance: Provenance
    # Lowercase hex; see research.hashing.content_sha256 for the pinned input rule.
    content_sha256: str = Field(pattern=_SHA256_HEX)

    snippet: str | None = None
    summary: str | None = None
    raw_artifact_path: str | None = None

    # Absent for providers with no trust model of their own; the X adapter always
    # assigns one, defaulting to unverified_social.
    reliability_tag: ReliabilityTag | None = None

    @model_validator(mode="after")
    def _social_documents_are_agent_reported_and_tagged(self) -> ResearchDocument:
        """Bind the trust fields the forecaster prompt's evidence caps depend on.

        Social evidence reaches the pipeline only through the xAI research agent
        (brief § B): Grok reports posts, so their content and timestamps are
        claims (``llm_reported``), and every one carries a trust tag assigned
        from the M1-308 allowlist, defaulting to ``unverified_social``. The
        prompt caps how load-bearing such a document may be *by reading these two
        fields*, so a social document missing either silently escapes the cap.

        Ambiguity rule 4 (stricter reading): the brief describes no other route
        to a social document. A direct X API adapter would produce
        ``social``/``direct_api`` and require revisiting this -- deliberately a
        schema change rather than a hole left open in advance.
        """
        if self.source_type == "social":
            if self.provenance != "llm_reported":
                raise ValueError(
                    "source_type 'social' requires provenance 'llm_reported': "
                    "social evidence is reported by the research agent, not retrieved"
                )
            if self.reliability_tag is None:
                raise ValueError(
                    "source_type 'social' requires a reliability_tag "
                    "(use 'unverified_social' when the handle is not on the allowlist)"
                )
        return self


class ResearchRun(_StrictModel):
    """One provider invocation for one question, and how it went."""

    retrieval_run_id: str = Field(min_length=1)
    # Integer reference only. Deliberately not the M1-201 CanonicalQuestion: the
    # run needs the question's identity, not its content, and importing the model
    # would couple the retrieval epic to the normalization epic for nothing.
    question_id: int

    provider: RetrievalProvider
    # JsonValue, not Any: this column is provider_config_json TEXT, so a config
    # holding a non-serializable value is not storable. Typing it Any would let
    # validation "pass" and fail later at model_dump_json(), inside the ledger
    # write, where the run has already happened and cannot be re-run for free.
    provider_config: dict[str, PersistableJson] | None = None
    queries: list[str] = Field(default_factory=list)

    started_at_utc: UtcDatetime
    completed_at_utc: UtcDatetime | None = None
    freshness_cutoff_utc: UtcDatetime | None = None

    raw_response_path: str | None = None
    # Set when the run failed or returned nothing. Social retrieval is additive
    # evidence: its failure is recorded here and must not fail a research run in
    # which AskNews or Exa succeeded (brief § B, failure mode).
    error_summary: str | None = None
    cost_usd: float | None = Field(default=None, ge=0)

    # A second model participating in evidence gathering must be identified by
    # name and version; attribution requires it (brief § B).
    agent_model: str | None = None
    # Citation-hygiene counter: agent-reported posts dropped for lacking a
    # resolvable status URL (M1-307). None for providers where it has no meaning.
    posts_dropped_no_url: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _completion_not_before_start(self) -> ResearchRun:
        if self.completed_at_utc is not None and self.completed_at_utc < self.started_at_utc:
            # No values in the message: a run's timestamps are row content and
            # this class contracts not to echo it.
            raise ValueError("completed_at_utc must not precede started_at_utc")
        return self

    @model_validator(mode="after")
    def _agent_runs_account_for_themselves(self) -> ResearchRun:
        """Require model identity and drop accounting from the agent provider.

        Both are attribution, not telemetry. D27 forbids a silent model default,
        so a second model that participated in gathering evidence must be named
        in the row that records its output -- and ``agent_model`` is
        config-supplied, so it is known even for a run that failed outright.

        ``posts_dropped_no_url`` is required for the same reason ``0`` and
        ``None`` must not be the same row: unset means nobody counted, while
        zero is the auditable claim that no citation was discarded. A run that
        gathered nothing dropped nothing, and says so.
        """
        if self.provider == "xai_x_search":
            if self.agent_model is None or not self.agent_model.strip():
                raise ValueError(
                    "provider 'xai_x_search' requires a non-blank agent_model: "
                    "an agent's output is attributed to the model that produced it (D27)"
                )
            if self.posts_dropped_no_url is None:
                raise ValueError(
                    "provider 'xai_x_search' requires posts_dropped_no_url "
                    "(use 0 for a run that dropped no citations; None means unmeasured)"
                )
        return self


# Substituted for any error-location part that did not come from the schema. See
# _sanitize: matches the "offending input withheld" wording config.py uses.
_WITHHELD = "<withheld>"


def _sanitize(exc: ValidationError, model: type[BaseModel]) -> ResearchSchemaError:
    """Render a ValidationError with every input-controlled fragment removed.

    ``include_input=False`` withholds the offending *value*, but an error's
    ``loc`` can itself be input: under ``extra="forbid"`` the location of an
    unexpected key **is** that key, and inside ``provider_config`` it is a
    caller-supplied dict key. A payload assembled from provider text (or from a
    misplaced credential) would otherwise print verbatim.

    So a location part survives only if the schema authored it: an ``int`` list
    index, or a field name declared on ``model``. Everything else is withheld.
    That is stricter than needed for today's two flat models and stays correct
    if a later error type puts something new in ``loc``.

    **The message itself cannot be filtered here**, because a ``ValueError`` from
    any validator becomes ``err["msg"]`` verbatim. So the companion invariant is
    on the validators: *every* raise in this module uses a constant, value-free
    message. That is easy to satisfy by hand and easy to breach by accident --
    ``_require_http_url`` originally let ``urlsplit``'s own ValueError propagate,
    and that exception embeds the offending netloc, which leaked a URL through a
    sanitizer that was otherwise airtight (review round 2, finding 1). Any raise
    added here must either use a literal string or be caught and replaced with
    one. ``test_no_field_leaks_a_planted_secret_through_any_message`` is the net.
    """
    known = set(model.model_fields)
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        parts = [
            str(part) if isinstance(part, int) or part in known else _WITHHELD
            for part in err["loc"]
        ]
        location = ".".join(parts) or "<root>"
        problems.append(f"{location}: {err['msg']}")
    return ResearchSchemaError(problems)


def validate_document(data: Any) -> ResearchDocument:
    """Validate a document payload; raises ResearchSchemaError on failure.

    The sanctioned entry point: unlike a bare ``model_validate``, its errors
    never echo the retrieved content.
    """
    try:
        return ResearchDocument.model_validate(data)
    except ValidationError as exc:
        # from None: a chained __cause__ re-exposes the raw ValidationError (which
        # echoes inputs) whenever this error reaches a traceback renderer.
        raise _sanitize(exc, ResearchDocument) from None


def validate_run(data: Any) -> ResearchRun:
    """Validate a run payload; raises ResearchSchemaError on failure."""
    try:
        return ResearchRun.model_validate(data)
    except ValidationError as exc:
        raise _sanitize(exc, ResearchRun) from None
