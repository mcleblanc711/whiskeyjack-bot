"""Research retrieval: the normalized evidence schema and its primitives (M1-301).

Adapters (M1-302 AskNews, M1-303 Exa, M1-304 structured router, M1-307 X agent)
import from here so every provider produces one comparable evidence record.
"""

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
    "Provenance",
    "ReliabilityTag",
    "ResearchDocument",
    "ResearchRun",
    "ResearchSchemaError",
    "RetrievalProvider",
    "SourceType",
    "content_sha256",
    "normalize_content",
    "validate_document",
    "validate_run",
]
