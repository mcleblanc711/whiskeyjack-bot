"""Packet transforms for replay arms: pure functions over an already-numbered packet (M5-806, AC4).

A transform decides which of the control's documents a replay arm is shown, in what order, and
for one transform with which publisher names. It never adds a document and never edits one's
content; ``forecast.inputs.require_transformed_input`` holds the result to that rule again at the
spending site.

**Source ids are assigned before any transform runs.** ``build_model_input`` numbers the control's
whole freshness-filtered packet (``src-001`` ...); a transform then selects from those numbered
documents, so a document carries the same ``source_id`` in every arm and a citation in one arm
names the same evidence as in another. Dropping a document *before* numbering would renumber the
rest, and the paired comparison of RQ3 (cited clusters against influence) would compare
different documents under one id.

**Randomness is derived from a hash, not a generator.** A draw for one key is
``sha256(canonical([seed, question_id, sample_index, key]))`` read as a fraction of 2**64. It
depends on nothing but those four values: not on wall-clock time, process state, the Python
version's PRNG, or the order documents arrive in. The same spec and record always give the same
subset and the same shuffle.

**Labellers.** The cluster and publisher-class kinds take a labeller by name. Source clusters are
M1-321's and publisher classes M1-369's; neither has merged, so the command line registers no
labeller and refuses those kinds before anything is spent. They are implemented and
property-tested here against synthetic labellers, so wiring a real one is a registration, not a
rewrite.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from whiskeyjack_bot.experiment.errors import ExperimentError
from whiskeyjack_bot.experiment.spec import (
    CollapseSyndicationTransform,
    DropClusterTransform,
    DropProviderTransform,
    DropPublisherClassTransform,
    IdentityTransform,
    MaskPublisherTransform,
    NoResearchTransform,
    RandomSubsetTransform,
    ShuffleOrderTransform,
    Transform,
)
from whiskeyjack_bot.forecast.inputs import ForecastDocumentInput, ModelInput
from whiskeyjack_bot.research.canonical import host_identity
from whiskeyjack_bot.research.dedup import dedup_key
from whiskeyjack_bot.research.model import RetrievalProvider
from whiskeyjack_bot.research.packet import ResearchPacket

# The labeller backlog items, named in the refusal so an operator knows what the kind waits on.
_LABELLER_ITEMS: Final = "M1-321 (source clusters) or M1-369 (publisher classes)"


@dataclass(frozen=True)
class ArmDocument:
    """One numbered document of the control's packet, as a transform sees it."""

    source_id: str
    provider: RetrievalProvider
    retrieval_run_id: str
    canonical_url: str
    content_sha256: str
    rendered: ForecastDocumentInput


ClusterLabeller = Callable[[Sequence[ArmDocument]], Mapping[str, str]]
"""Every document's ``source_id`` to its cluster id, for one packet."""

PublisherClassLabeller = Callable[[ArmDocument], str]
"""One document's publisher class."""


@dataclass(frozen=True)
class Labellers:
    """The labellers a run may use, by the name an arm spec gives."""

    clusters: Mapping[str, ClusterLabeller] = field(default_factory=dict)
    publisher_classes: Mapping[str, PublisherClassLabeller] = field(default_factory=dict)


NO_LABELLERS: Final = Labellers()


@dataclass(frozen=True)
class TransformResult:
    """What a transform keeps for one (record, sample); ``applicable`` False buys nothing."""

    applicable: bool
    documents: tuple[ArmDocument, ...]


def arm_documents(model_input: ModelInput, packet: ResearchPacket) -> tuple[ArmDocument, ...]:
    """The control's numbered documents, in ``source_id`` order, each with its provider.

    ``build_model_input`` numbers documents in ``dedup_key`` order, so sorting the packet the
    same way pairs each rendered document with the stored one it came from; the pairing is then
    checked field by field rather than trusted. The provider is the document's run's provider:
    a document whose run the packet does not carry is refused (``ResearchPacket`` already
    refuses that shape, so this is the second line).
    """
    ordered = sorted(packet.documents, key=dedup_key)
    rendered = model_input.packet.research_documents
    if len(ordered) != len(rendered) or len(rendered) != len(model_input.sources):
        raise ExperimentError("the reasoning packet does not match the research packet")
    providers = {run.retrieval_run_id: run.provider for run in packet.runs}
    documents: list[ArmDocument] = []
    for stored, shown, reference in zip(ordered, rendered, model_input.sources, strict=True):
        if (
            shown.source_id != reference.source_id
            or stored.canonical_url != reference.canonical_url
            or stored.content_sha256 != reference.content_sha256
            or shown.url != stored.canonical_url
        ):
            raise ExperimentError("the reasoning packet does not match the research packet")
        provider = providers.get(stored.retrieval_run_id)
        if provider is None:
            raise ExperimentError("a research document names a run the packet does not carry")
        documents.append(
            ArmDocument(
                source_id=reference.source_id,
                provider=provider,
                retrieval_run_id=stored.retrieval_run_id,
                canonical_url=stored.canonical_url,
                content_sha256=stored.content_sha256,
                rendered=shown,
            )
        )
    return tuple(documents)


def id_order(source_id: str) -> tuple[int, str]:
    """Numeric order for minted ids: ``src-999`` before ``src-1000``, which plain text breaks."""
    return (len(source_id), source_id)


def unit_draw(seed: int, question_id: int, sample_index: int, key: str) -> float:
    """A deterministic draw in [0, 1) for one key: the first 64 bits of a sha256, over 2**64."""
    payload = json.dumps(
        [seed, question_id, sample_index, key], ensure_ascii=True, separators=(",", ":")
    )
    return int(hashlib.sha256(payload.encode("ascii")).hexdigest()[:16], 16) / 2**64


def require_labellers(transform: Transform, labellers: Labellers) -> None:
    """Refuse an arm whose transform names a labeller this run does not have.

    Called before any record is touched, so an arm that cannot run spends nothing.
    """
    name: str | None = None
    table: Mapping[str, object] = {}
    if isinstance(transform, (DropClusterTransform, CollapseSyndicationTransform)):
        name, table = transform.labeller, labellers.clusters
    elif isinstance(transform, DropPublisherClassTransform):
        name, table = transform.labeller, labellers.publisher_classes
    elif isinstance(transform, RandomSubsetTransform) and transform.unit == "cluster":
        if transform.labeller is None:
            raise ExperimentError("a cluster random subset must name a cluster labeller")
        name, table = transform.labeller, labellers.clusters
    elif isinstance(transform, RandomSubsetTransform) and transform.labeller is not None:
        raise ExperimentError("a document random subset takes no labeller")
    if name is not None and name not in table:
        raise ExperimentError(
            f"the arm's labeller is not available in this build; it waits for {_LABELLER_ITEMS}"
        )


def _clusters(
    documents: tuple[ArmDocument, ...], name: str, labellers: Labellers
) -> dict[str, str]:
    labeller = labellers.clusters.get(name)
    if labeller is None:
        raise ExperimentError(
            f"the arm's labeller is not available in this build; it waits for {_LABELLER_ITEMS}"
        )
    try:
        assignment = dict(labeller(documents))
    except Exception:
        raise ExperimentError("the cluster labeller failed (detail withheld)") from None
    ids = {document.source_id for document in documents}
    if set(assignment) != ids or any(type(value) is not str for value in assignment.values()):
        raise ExperimentError("the cluster labeller must give every document exactly one cluster")
    return assignment


def _ordered_clusters(
    documents: tuple[ArmDocument, ...], assignment: Mapping[str, str]
) -> list[tuple[str, ...]]:
    """Clusters as tuples of source ids, ordered by their lowest member's source id."""
    members: dict[str, list[str]] = {}
    for document in documents:
        members.setdefault(assignment[document.source_id], []).append(document.source_id)
    return sorted(
        (tuple(sorted(group, key=id_order)) for group in members.values()),
        key=lambda group: id_order(group[0]),
    )


def _keep(documents: tuple[ArmDocument, ...], kept: set[str]) -> tuple[ArmDocument, ...]:
    return tuple(document for document in documents if document.source_id in kept)


def _mask(documents: tuple[ArmDocument, ...]) -> tuple[ArmDocument, ...]:
    """Replace each publisher name and URL host with a neutral token, numbered per packet.

    Tokens are assigned in ``source_id`` order of first appearance: hosts by
    ``host_identity`` (so ``www.`` and the other spellings of one host share a token) and
    publishers by their exact string. A missing publisher stays missing -- a token there would
    tell the model something it was never told. The URL keeps its path and query, which are
    the document's address rather than its publisher's name; titles and summaries are content
    and are not edited (a stated limit of A3).
    """
    hosts: dict[str, int] = {}
    publishers: dict[str, int] = {}
    masked: list[ArmDocument] = []
    for document in sorted(documents, key=lambda d: id_order(d.source_id)):
        parts = urlsplit(document.rendered.url)
        host = host_identity(parts.hostname or "")
        host_token = hosts.setdefault(host, len(hosts) + 1)
        url = urlunsplit(("https", f"source-{host_token:02d}.invalid", parts.path, parts.query, ""))
        publisher = document.rendered.publisher
        if publisher is not None:
            publisher = f"publisher-{publishers.setdefault(publisher, len(publishers) + 1):02d}"
        rendered = document.rendered.model_copy(update={"url": url, "publisher": publisher})
        masked.append(
            ArmDocument(
                source_id=document.source_id,
                provider=document.provider,
                retrieval_run_id=document.retrieval_run_id,
                canonical_url=document.canonical_url,
                content_sha256=document.content_sha256,
                rendered=rendered,
            )
        )
    by_id = {document.source_id: document for document in masked}
    return tuple(by_id[document.source_id] for document in documents)


def apply_transform(
    transform: Transform,
    documents: tuple[ArmDocument, ...],
    *,
    question_id: int,
    sample_index: int,
    labellers: Labellers = NO_LABELLERS,
) -> TransformResult:
    """The documents one (record, sample) of an arm is shown, and in what order.

    Pure. The input is the control's numbered documents in ``source_id`` order; the output is
    a subsequence of them (``shuffle_order`` reorders, ``mask_publisher`` rewrites two fields),
    never a document the input lacks.
    """
    if type(question_id) is not int or type(sample_index) is not int or sample_index < 0:
        raise ExperimentError("a transform needs an int question id and a non-negative sample")
    ordered = tuple(sorted(documents, key=lambda d: id_order(d.source_id)))
    if len({document.source_id for document in ordered}) != len(ordered):
        raise ExperimentError("a packet's documents must have distinct source ids")

    if isinstance(transform, IdentityTransform):
        return TransformResult(True, ordered)
    if isinstance(transform, NoResearchTransform):
        return TransformResult(True, ())
    if isinstance(transform, DropProviderTransform):
        dropped = set(transform.providers)
        return TransformResult(True, tuple(d for d in ordered if d.provider not in dropped))
    if isinstance(transform, MaskPublisherTransform):
        return TransformResult(True, _mask(ordered))
    if isinstance(transform, ShuffleOrderTransform):
        seed = transform.seed
        return TransformResult(
            True,
            tuple(
                sorted(
                    ordered,
                    key=lambda d: (
                        unit_draw(seed, question_id, sample_index, d.source_id),
                        id_order(d.source_id),
                    ),
                )
            ),
        )
    if isinstance(transform, RandomSubsetTransform):
        p = transform.inclusion_probability
        if transform.unit == "document":
            kept = {
                d.source_id
                for d in ordered
                if unit_draw(transform.seed, question_id, sample_index, d.source_id) < p
            }
            return TransformResult(True, _keep(ordered, kept))
        if transform.labeller is None:
            raise ExperimentError("a cluster random subset must name a cluster labeller")
        assignment = _clusters(ordered, transform.labeller, labellers)
        kept_clusters = {
            cluster
            for cluster in set(assignment.values())
            if unit_draw(transform.seed, question_id, sample_index, cluster) < p
        }
        return TransformResult(
            True, _keep(ordered, {s for s, c in assignment.items() if c in kept_clusters})
        )
    if isinstance(transform, DropClusterTransform):
        clusters = _ordered_clusters(ordered, _clusters(ordered, transform.labeller, labellers))
        if sample_index >= len(clusters):
            return TransformResult(False, ())
        dropped_ids = set(clusters[sample_index])
        return TransformResult(True, tuple(d for d in ordered if d.source_id not in dropped_ids))
    if isinstance(transform, CollapseSyndicationTransform):
        clusters = _ordered_clusters(ordered, _clusters(ordered, transform.labeller, labellers))
        if all(len(group) < 2 for group in clusters):
            return TransformResult(False, ())
        return TransformResult(True, _keep(ordered, {group[0] for group in clusters}))
    if isinstance(transform, DropPublisherClassTransform):
        labeller = labellers.publisher_classes.get(transform.labeller)
        if labeller is None:
            raise ExperimentError(
                f"the arm's labeller is not available in this build; it waits for {_LABELLER_ITEMS}"
            )
        classes = set(transform.classes)
        kept_ids: set[str] = set()
        for document in ordered:
            try:
                label = labeller(document)
            except Exception:
                raise ExperimentError(
                    "the publisher-class labeller failed (detail withheld)"
                ) from None
            if type(label) is not str:
                raise ExperimentError("the publisher-class labeller must return a string")
            if label not in classes:
                kept_ids.add(document.source_id)
        return TransformResult(True, _keep(ordered, kept_ids))
    raise ExperimentError("the arm's transform kind is not one this build implements")


def transformed_model_input(base: ModelInput, documents: tuple[ArmDocument, ...]) -> ModelInput:
    """The reasoning packet showing ``documents``: base fields, base references, in base order.

    ``sources`` keeps base ``source_id`` order whatever order the documents render in, because it
    is the citation map, not the presentation; ``generate_forecast`` re-validates the result.
    """
    kept = {document.source_id for document in documents}
    packet = base.packet.model_copy(
        update={"research_documents": [document.rendered for document in documents]}
    )
    sources = tuple(reference for reference in base.sources if reference.source_id in kept)
    return ModelInput(packet=packet, sources=sources)


def kept_packet(packet: ResearchPacket, documents: tuple[ArmDocument, ...]) -> ResearchPacket:
    """The research packet restricted to ``documents``: its hash is the kept evidence's hash.

    Runs are kept whole and locale rows follow their documents, as ``research.quality`` filters.
    ``packet_sha256`` ignores order and rendering, so a shuffle or a mask hashes as the control;
    the rendered request's own sha256 is stored beside it for that reason.
    """
    kept = {(d.retrieval_run_id, d.canonical_url, d.content_sha256) for d in documents}
    return ResearchPacket(
        question_id=packet.question_id,
        runs=packet.runs,
        documents=tuple(d for d in packet.documents if dedup_key(d) in kept),
        locales=tuple(locale for locale in packet.locales if locale.key() in kept),
    )
