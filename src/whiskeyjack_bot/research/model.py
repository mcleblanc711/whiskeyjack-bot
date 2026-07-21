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
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AwareDatetime, Field, ValidationError, model_validator

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
    original_url: str = Field(min_length=1)
    canonical_url: str = Field(min_length=1)

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


class ResearchRun(_StrictModel):
    """One provider invocation for one question, and how it went."""

    retrieval_run_id: str = Field(min_length=1)
    # Integer reference only. Deliberately not the M1-201 CanonicalQuestion: the
    # run needs the question's identity, not its content, and importing the model
    # would couple the retrieval epic to the normalization epic for nothing.
    question_id: int

    provider: RetrievalProvider
    provider_config: dict[str, Any] | None = None
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


def _sanitize(exc: ValidationError) -> ResearchSchemaError:
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
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
        raise _sanitize(exc) from None


def validate_run(data: Any) -> ResearchRun:
    """Validate a run payload; raises ResearchSchemaError on failure."""
    try:
        return ResearchRun.model_validate(data)
    except ValidationError as exc:
        raise _sanitize(exc) from None
