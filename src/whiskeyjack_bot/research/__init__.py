"""Research retrieval: the normalized evidence schema and its primitives (M1-301).

Adapters (M1-302 AskNews, M1-303 Exa, M1-304 structured router, M1-307 X agent)
import from here so every provider produces one comparable evidence record.
"""

from whiskeyjack_bot.research.asknews import (
    AskNewsRetrieval,
    build_asknews_client,
    retrieve_news,
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
    "AskNewsRetrieval",
    "Provenance",
    "ReliabilityTag",
    "ResearchDocument",
    "ResearchRun",
    "ResearchSchemaError",
    "RetrievalProvider",
    "SourceType",
    "build_asknews_client",
    "content_sha256",
    "normalize_content",
    "retrieve_news",
    "validate_document",
    "validate_run",
]
