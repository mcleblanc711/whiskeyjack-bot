"""The research packet and the hash replay reproduces (M1-306).

M1-306's acceptance criterion is "replay produces zero provider calls and the same
research packet hash", and until this module there was no research packet.
``hashing.py`` promises "the research-packet hash that replay reproduces",
``CLAUDE_CODE_PROMPT.md`` § B requires the X adapter to reproduce it, and the
handoff's canonical record lists "retrieval run and normalized source references"
-- but nothing defined the thing or hashed it. This module defines it.

A **research packet** is the complete evidence one question was forecast from: its
retrieval runs and their deduplicated documents. It is a *derived* value object over
rows that already exist, deliberately **not** a table. A stored hash can disagree
with the rows it summarizes, and a hash the evidence contradicts is worse than no
hash at all; recomputing it makes that disagreement unrepresentable rather than
merely unlikely. (M1-602 stamps the hash onto a forecast record, which is the other
half of the same audit: the forecast records the packet it *saw*, and this module
recomputes what the evidence *is*.)

What the digest covers is the whole design decision, and it covers **how the
evidence was gathered, not only what was found**: provider, provider config,
queries, freshness window, timestamps, cost, error summary and the discarded-evidence
counters, alongside every document. Two runs that surfaced the same twelve articles
under different queries are not the same research, and a packet that could not tell
them apart would degrade from an attribution record into a reading list. The
consequence is deliberate: a *fresh* retrieval over identical evidence hashes
differently, because it is a different gathering event. Replay reproduces the hash
because it reads the same rows.

Three fields are excluded, each for its own reason:

- ``document_id`` -- a writer-minted UUID. A document's identity here is its dedup
  key, which ``UNIQUE (retrieval_run_id, canonical_url, content_sha256)`` already
  states; including the UUID would make the digest a fact about when rows were
  written rather than about the evidence.
- ``raw_response_path`` / ``raw_artifact_path`` -- where bytes were filed is not
  what was retrieved. Both are stored relative to the operator-configured
  ``storage.artifact_root``, so including them would make the packet hash
  machine-dependent: reorganizing an artifact directory must not be able to
  invalidate an attribution record.

``created_at_utc`` needs no rule -- it is writer-owned metadata the two models
deliberately do not carry (see ``model.py``), so it never reaches the dump.

**The digest keys on the persisted form, and changing that breaks replay.** This is
M1-305's lesson applied verbatim (``dedup._sort_key``, five review rounds):
``model_dump(mode="json")`` renders exactly what SQLite stores, so before == after.
``model_dump_json()`` raises on a lone surrogate, which is reachable from provider
JSON; ``repr``/plain ``model_dump()`` carry distinctions JSON drops -- ``datetime.fold``,
and the astral-scalar vs surrogate-pair spelling of one character -- so a digest over
them is stable in memory and *changes across a store->load round-trip*, which is
exactly the hash that would pass every test that never went through the ledger and
then fail the acceptance criterion.

The rule is versioned for the same reason ``hashing.content_sha256`` is: every packet
already hashed keeps its old digest, so if the rule must change it changes as a new
versioned function alongside this one, never as an edit to this one.

Error hygiene follows the rest of the package: :class:`PacketError` never echoes a
run's or a document's content, every raise here uses a constant message, and wrapped
raises use ``from None``.

Purely computational: no I/O, no network, no SQL.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from whiskeyjack_bot.research.dedup import dedup_key
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun

# Bumping this changes every digest, which is the point: a rule change must be
# visible as a different hash rather than silently reinterpreting stored packets.
# It is part of the hashed payload, so the version and the rule cannot drift apart.
PACKET_SCHEMA_VERSION = "1.0.0"

# Excluded from the digest. See the module docstring for why each one is here; the
# names are checked against the models at import time by _assert_fields_exist, so a
# field renamed upstream fails loudly instead of silently re-entering the hash.
_EXCLUDED_RUN_FIELDS = frozenset({"raw_response_path"})
_EXCLUDED_DOCUMENT_FIELDS = frozenset({"document_id", "raw_artifact_path"})


class PacketError(Exception):
    """A research packet is malformed, or cannot be rendered into its stored form.

    Same hygiene rule as ``ResearchSchemaError``/``LedgerError``: the message never
    echoes a document's text, a query, a provider config or any other row content,
    and sanitizing raises use ``from None`` so an underlying exception cannot
    reprint a value through its text or a rendered traceback.
    """


def _assert_fields_exist() -> None:
    """Fail at import if an excluded field name no longer names a model field.

    The exclusions are the security-relevant half of the hash rule: drop the guard
    and a rename upstream turns ``raw_artifact_path`` back into hashed content,
    which would make the digest machine-dependent again -- silently, and only
    observably on a second machine.
    """
    missing = (_EXCLUDED_RUN_FIELDS - set(ResearchRun.model_fields)) | (
        _EXCLUDED_DOCUMENT_FIELDS - set(ResearchDocument.model_fields)
    )
    if missing:
        # Sorted for a deterministic message; these are this module's own literals,
        # not row content, so naming them is safe.
        raise PacketError(
            "packet hash exclusion list names fields that do not exist: "
            + ", ".join(sorted(missing))
        )


_assert_fields_exist()


@dataclass(frozen=True)
class ResearchPacket:
    """One question's retrieval runs and their deduplicated documents.

    Validated on construction: a packet is about exactly one question, its runs are
    distinct, and every document belongs to a run the packet carries. The last of
    those is not bookkeeping -- a document whose ``retrieval_run_id`` names no run
    here is evidence attributed to a run the packet cannot show, which is the shape
    of attribution loss this whole item exists to prevent.

    Order is not part of packet identity: :func:`packet_sha256` sorts both
    sequences. The tuples are kept in the order they were supplied so a caller can
    present evidence in retrieval order.
    """

    question_id: int
    runs: tuple[ResearchRun, ...]
    documents: tuple[ResearchDocument, ...]

    def __post_init__(self) -> None:
        # type() is str/int-style exact gates rather than isinstance: bool is an int
        # subclass, so `isinstance(True, int)` would accept True as a question id.
        if type(self.question_id) is not int:
            raise PacketError("question_id must be an int")
        if not isinstance(self.runs, tuple) or not isinstance(self.documents, tuple):
            raise PacketError("runs and documents must be tuples")
        run_ids: set[str] = set()
        for run in self.runs:
            if not isinstance(run, ResearchRun):
                raise PacketError("every run must be a ResearchRun")
            if run.question_id != self.question_id:
                # No ids in the message: a question id is row content, and the
                # no-echo rule is unconditional (M1-202's duplicate-id precedent).
                raise PacketError("every run must belong to the packet's question")
            if run.retrieval_run_id in run_ids:
                raise PacketError("runs must be distinct: a retrieval_run_id appears twice")
            run_ids.add(run.retrieval_run_id)
        keys: set[tuple[str, str, str]] = set()
        for document in self.documents:
            if not isinstance(document, ResearchDocument):
                raise PacketError("every document must be a ResearchDocument")
            if document.retrieval_run_id not in run_ids:
                raise PacketError(
                    "every document must belong to a run the packet carries: "
                    "evidence cannot be attributed to a run that is not present"
                )
            key = dedup_key(document)
            if key in keys:
                # Exactly the ledger's UNIQUE (retrieval_run_id, canonical_url,
                # content_sha256): two such documents are one row, so a packet
                # holding both could not have come out of the ledger and would
                # hash differently from the packet that goes back in.
                raise PacketError(
                    "documents must be deduplicated: two share the ledger's dedup key"
                )
            keys.add(key)


def _dump(model: ResearchRun | ResearchDocument, excluded: frozenset[str]) -> dict[str, object]:
    """Render one model into its stored form, minus the excluded fields.

    ``warnings=False`` for the reason M1-302 round 1 established: a pydantic
    serializer warning **embeds the offending value in its text** and reaches
    stderr and captured logs, so it is an egress channel, not noise. A research
    document carries arbitrary provider text.
    """
    try:
        dumped = model.model_dump(mode="json", warnings=False)
    except Exception:
        # Deliberately broad, and scoped to this one call. M1-308's round 7 is the
        # precedent: when a third-party serializer can fail, it fails as a *class*
        # of exceptions rather than the one type you predicted, and several of them
        # quote the offending value. from None keeps the cause chain from
        # reprinting it.
        raise PacketError(
            "a run or document could not be rendered into its stored form "
            "(detail withheld: it can echo row content)"
        ) from None
    return {name: value for name, value in dumped.items() if name not in excluded}


def canonical_packet_json(packet: ResearchPacket) -> str:
    """Return the exact string :func:`packet_sha256` digests.

    Exposed for tests and diagnostics the way ``hashing.normalize_content`` is: a
    hash that cannot be inspected is a hash whose disagreements cannot be explained.
    """
    if not isinstance(packet, ResearchPacket):
        raise PacketError("packet must be a ResearchPacket")
    payload = {
        "packet_schema_version": PACKET_SCHEMA_VERSION,
        "question_id": packet.question_id,
        # Sorted so packet identity does not depend on the order runs and documents
        # were handed over. Both keys are total orders over values the constructor
        # has already proved distinct, and both compare strings only, so neither
        # sort can raise.
        "runs": [
            _dump(run, _EXCLUDED_RUN_FIELDS)
            for run in sorted(packet.runs, key=lambda run: run.retrieval_run_id)
        ],
        "documents": [
            _dump(document, _EXCLUDED_DOCUMENT_FIELDS)
            for document in sorted(packet.documents, key=dedup_key)
        ],
    }
    try:
        # ensure_ascii escapes lone surrogates rather than failing to encode them
        # (M1-305 round 2); sort_keys and the compact separators make the rendering
        # canonical; allow_nan=False refuses NaN/Infinity, which json.dumps would
        # otherwise emit as bare `NaN`/`Infinity` -- not valid JSON, and a value
        # SQLite reads back as NULL, so a packet containing one could never replay
        # to its own hash.
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        # from None and a constant message: json.dumps names the offending value in
        # both the circular-reference and the out-of-range-float cases.
        raise PacketError(
            "packet could not be rendered as canonical JSON "
            "(detail withheld: it can echo row content)"
        ) from None


def packet_sha256(packet: ResearchPacket) -> str:
    """Return the lowercase hex SHA-256 of ``packet`` under the pinned rule."""
    return hashlib.sha256(canonical_packet_json(packet).encode("utf-8")).hexdigest()


def build_packet(
    question_id: int,
    runs: Iterable[ResearchRun],
    documents: Iterable[ResearchDocument],
) -> ResearchPacket:
    """Build a validated packet from any iterables; raises :class:`PacketError`.

    The sanctioned entry point for callers holding lists: constructing the frozen
    dataclass directly with a list raises, deliberately, so that "it happened to
    work" and "it is a tuple" cannot come apart.
    """
    try:
        run_tuple = tuple(runs)
        document_tuple = tuple(documents)
    except TypeError:
        raise PacketError("runs and documents must be iterable") from None
    return ResearchPacket(question_id=question_id, runs=run_tuple, documents=document_tuple)
