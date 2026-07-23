"""Research retrieval: the normalized evidence schema and its primitives (M1-301).

Adapters (M1-302 AskNews, M1-303 Exa, M1-304 structured router, M1-307 X agent)
import from here so every provider produces one comparable evidence record.
Deduplication, freshness-tagging and URL canonicalization are M1-305.
"""

from whiskeyjack_bot.research.canonical import CanonicalizationError, canonicalize_url
from whiskeyjack_bot.research.dedup import DedupResult, dedup_key, deduplicate
from whiskeyjack_bot.research.freshness import (
    FreshnessReason,
    FreshnessState,
    FreshnessVerdict,
    assess_document,
    assess_freshness,
    freshness_cutoff,
)
from whiskeyjack_bot.research.hashing import content_sha256, normalize_content
from whiskeyjack_bot.research.model import (
    Provenance,
    ReliabilityTag,
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    RetrievalProvider,
    SourceType,
    validate_document,
    validate_run,
)

__all__ = [
    "CanonicalizationError",
    "DedupResult",
    "FreshnessReason",
    "FreshnessState",
    "FreshnessVerdict",
    "Provenance",
    "ReliabilityTag",
    "ResearchDocument",
    "ResearchRun",
    "ResearchSchemaError",
    "RetrievalProvider",
    "SourceType",
    "assess_document",
    "assess_freshness",
    "canonicalize_url",
    "content_sha256",
    "dedup_key",
    "deduplicate",
    "freshness_cutoff",
    "normalize_content",
    "validate_document",
    "validate_run",
]
