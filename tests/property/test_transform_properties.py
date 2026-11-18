"""Property tests for the packet transforms (M5-806, AC4).

The CLAUDE.md pre-review fuzz pass over ``experiment/transforms.py``:

1. **Nothing added, nothing renumbered, nothing edited.** Every output is a subsequence of the
   input by ``source_id``, without repeats; a kept document is the input document, except that
   ``mask_publisher`` rewrites exactly ``publisher`` and ``url``.
2. **The spending site accepts every output.** ``transformed_model_input`` of any transform's
   result passes ``forecast.inputs.require_transformed_input`` against the base -- the two
   layers cannot disagree about what a transform may do.
3. **Order-invariant and deterministic.** The same documents in any order, with the same spec
   and record, give the same result; the seeded kinds derive their draws from the seed, the
   question and the sample index only.
4. **Never raises outside ``ExperimentError``**, including on labellers that misbehave.

Reach is measured: ``test_the_strategy_reaches_every_kind_and_both_outcomes`` fails if a kind,
a non-applicable result, or a non-trivial subset is starved.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

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
)
from whiskeyjack_bot.experiment.transforms import (
    ArmDocument,
    Labellers,
    apply_transform,
    id_order,
    transformed_model_input,
    unit_draw,
)
from whiskeyjack_bot.forecast.inputs import (
    TRANSFORMABLE_DOCUMENT_FIELDS,
    ForecastDocumentInput,
    ForecastModelInput,
    ModelInput,
    SourceReference,
    require_transformed_input,
)

AS_OF = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
HOSTS = ("a.example", "www.a.example", "b.example", "c.example", "d.example")
PUBLISHERS = ("Wire", "Daily", "Gazette", None)
PROVIDERS = ("asknews", "exa")


@st.composite
def packets(draw: st.DrawFn) -> tuple[ArmDocument, ...]:
    count = draw(st.integers(min_value=0, max_value=12))
    documents = []
    for index in range(1, count + 1):
        source_id = f"src-{index:03d}"
        host = draw(st.sampled_from(HOSTS))
        url = f"https://{host}/story/{index}?id={index}"
        rendered = ForecastDocumentInput(
            source_id=source_id,
            url=url,
            title=f"Title {index}",
            publisher=draw(st.sampled_from(PUBLISHERS)),
            retrieved_at=AS_OF,
            evidence_summary=f"summary {index}",
            source_type="news",
            provenance="direct_api",
        )
        documents.append(
            ArmDocument(
                source_id=source_id,
                provider=draw(st.sampled_from(PROVIDERS)),
                retrieval_run_id=f"run-{index % 2}",
                canonical_url=url,
                content_sha256=f"{index:064x}",
                rendered=rendered,
            )
        )
    return tuple(documents)


seeds = st.integers(min_value=0, max_value=2**63 - 1)
KINDS = (
    "identity",
    "no_research",
    "drop_provider",
    "mask_publisher",
    "shuffle_order",
    "random_document",
    "random_cluster",
    "drop_cluster",
    "drop_publisher_class",
    "collapse_syndication",
)


@st.composite
def transforms(draw: st.DrawFn) -> Any:
    kind = draw(st.sampled_from(KINDS))
    if kind == "identity":
        return IdentityTransform(kind="identity")
    if kind == "no_research":
        return NoResearchTransform(kind="no_research")
    if kind == "drop_provider":
        chosen = sorted(set(draw(st.lists(st.sampled_from(PROVIDERS), min_size=1))))
        return DropProviderTransform(kind="drop_provider", providers=tuple(chosen))
    if kind == "mask_publisher":
        return MaskPublisherTransform(kind="mask_publisher")
    if kind == "shuffle_order":
        return ShuffleOrderTransform(kind="shuffle_order", seed=draw(seeds))
    probability = draw(st.floats(min_value=0.01, max_value=0.99, allow_nan=False))
    if kind == "random_document":
        return RandomSubsetTransform(
            kind="random_subset",
            seed=draw(seeds),
            inclusion_probability=probability,
            unit="document",
        )
    if kind == "random_cluster":
        return RandomSubsetTransform(
            kind="random_subset",
            seed=draw(seeds),
            inclusion_probability=probability,
            unit="cluster",
            labeller="by-host",
        )
    if kind == "drop_cluster":
        return DropClusterTransform(kind="drop_cluster", labeller="by-host")
    if kind == "drop_publisher_class":
        return DropPublisherClassTransform(
            kind="drop_publisher_class", labeller="by-publisher", classes=("Daily",)
        )
    return CollapseSyndicationTransform(kind="collapse_syndication", labeller="by-host")


def _by_host(documents: Any) -> dict[str, str]:
    return {d.source_id: d.canonical_url.split("/")[2].removeprefix("www.") for d in documents}


LABELLERS = Labellers(
    clusters={"by-host": _by_host},
    publisher_classes={"by-publisher": lambda d: d.rendered.publisher or "none"},
)


def _base(documents: tuple[ArmDocument, ...]) -> ModelInput:
    packet = ForecastModelInput(
        as_of_utc=AS_OF,
        question_id=1,
        post_id=2,
        tournament_id="minibench",
        question_type="binary",
        question_text="Will it happen?",
        research_documents=[d.rendered for d in documents],
    )
    sources = tuple(
        SourceReference(d.source_id, f"doc-{d.source_id}", d.canonical_url, d.content_sha256)
        for d in documents
    )
    return ModelInput(packet=packet, sources=sources)


@settings(deadline=None)
@given(packets(), transforms(), st.integers(min_value=0, max_value=5), st.integers(1, 10**6))
def test_a_transform_adds_renumbers_and_edits_nothing(
    documents: tuple[ArmDocument, ...], transform: Any, sample: int, question: int
) -> None:
    result = apply_transform(
        transform, documents, question_id=question, sample_index=sample, labellers=LABELLERS
    )
    by_id = {d.source_id: d for d in documents}
    ids = [d.source_id for d in result.documents]
    assert len(set(ids)) == len(ids)
    assert set(ids) <= set(by_id)
    for kept in result.documents:
        original = by_id[kept.source_id]
        assert kept.provider == original.provider
        assert kept.canonical_url == original.canonical_url
        assert kept.content_sha256 == original.content_sha256
        assert kept.retrieval_run_id == original.retrieval_run_id
        exclude = set(TRANSFORMABLE_DOCUMENT_FIELDS)
        assert kept.rendered.model_dump(exclude=exclude) == original.rendered.model_dump(
            exclude=exclude
        )
        if not isinstance(transform, MaskPublisherTransform):
            assert kept.rendered == original.rendered
    if not isinstance(transform, ShuffleOrderTransform):
        assert ids == sorted(ids, key=id_order), "every kind but shuffle keeps source-id order"


@settings(deadline=None)
@given(packets(), transforms(), st.integers(min_value=0, max_value=5))
def test_the_spending_site_accepts_every_transforms_output(
    documents: tuple[ArmDocument, ...], transform: Any, sample: int
) -> None:
    result = apply_transform(
        transform, documents, question_id=7, sample_index=sample, labellers=LABELLERS
    )
    base = _base(documents)
    accepted = require_transformed_input(base, transformed_model_input(base, result.documents))
    assert [d.source_id for d in accepted.packet.research_documents] == [
        d.source_id for d in result.documents
    ]
    assert [r.source_id for r in accepted.sources] == sorted(
        (d.source_id for d in result.documents), key=id_order
    )


@settings(deadline=None)
@given(packets(), transforms(), st.integers(0, 5), st.randoms(use_true_random=False))
def test_a_transform_is_order_invariant_and_deterministic(
    documents: tuple[ArmDocument, ...], transform: Any, sample: int, rng: Any
) -> None:
    shuffled = list(documents)
    rng.shuffle(shuffled)
    first = apply_transform(
        transform, documents, question_id=11, sample_index=sample, labellers=LABELLERS
    )
    second = apply_transform(
        transform, tuple(shuffled), question_id=11, sample_index=sample, labellers=LABELLERS
    )
    assert first == second


@settings(deadline=None)
@given(packets())
def test_masking_tokens_follow_host_and_publisher_identity(
    documents: tuple[ArmDocument, ...],
) -> None:
    result = apply_transform(
        MaskPublisherTransform(kind="mask_publisher"), documents, question_id=1, sample_index=0
    )
    hosts: dict[str, str] = {}
    publishers: dict[object, object] = {}
    for original, masked in zip(documents, result.documents, strict=True):
        host = original.canonical_url.split("/")[2].removeprefix("www.")
        token_host = masked.rendered.url.split("/")[2]
        assert token_host.endswith(".invalid") and token_host.startswith("source-")
        assert hosts.setdefault(host, token_host) == token_host
        assert masked.rendered.url.split("/", 3)[3] == original.canonical_url.split("/", 3)[3]
        if original.rendered.publisher is None:
            assert masked.rendered.publisher is None
        else:
            assert masked.rendered.publisher != original.rendered.publisher
            assert publishers.setdefault(
                original.rendered.publisher, masked.rendered.publisher
            ) == (masked.rendered.publisher)
    assert len(set(hosts.values())) == len(hosts), "distinct hosts get distinct tokens"
    assert len(set(publishers.values())) == len(publishers)


@settings(deadline=None)
@given(packets(), st.sampled_from(["missing", "partial", "not_text", "raises"]))
def test_a_misbehaving_labeller_is_refused_as_an_experiment_error(
    documents: tuple[ArmDocument, ...], fault: str
) -> None:
    def labeller(docs: Any) -> Any:
        if fault == "raises":
            raise RuntimeError("labeller exploded")
        if fault == "partial":
            return {d.source_id: "c" for d in list(docs)[1:]}
        if fault == "not_text":
            return {d.source_id: 1 for d in docs}
        return {}

    broken = Labellers(clusters={"by-host": labeller})
    transform = CollapseSyndicationTransform(kind="collapse_syndication", labeller="by-host")
    try:
        apply_transform(transform, documents, question_id=1, sample_index=0, labellers=broken)
    except ExperimentError as exc:
        assert "exploded" not in str(exc)
    else:
        # An empty packet is the one case a mapping-shaped fault cannot violate: no document
        # lacks a cluster. A labeller that raises is refused even there.
        assert not documents and fault != "raises"


def test_random_subset_inclusion_follows_the_probability() -> None:
    """Anti-vacuity for the hash draw: over 4,000 keys the kept share tracks p closely."""
    for p in (0.1, 0.5, 0.9):
        kept = sum(unit_draw(3, 99, 0, f"src-{i:04d}") < p for i in range(4000))
        assert abs(kept / 4000 - p) < 0.03, (p, kept)


def test_seeded_kinds_differ_across_samples_and_seeds() -> None:
    keys = [f"src-{i:03d}" for i in range(1, 41)]
    by_sample = {tuple(unit_draw(1, 5, s, k) < 0.5 for k in keys) for s in range(6)}
    by_seed = {tuple(unit_draw(seed, 5, 0, k) < 0.5 for k in keys) for seed in range(6)}
    by_question = {tuple(unit_draw(1, q, 0, k) < 0.5 for k in keys) for q in range(6)}
    assert len(by_sample) == len(by_seed) == len(by_question) == 6


def test_id_order_is_numeric_for_minted_ids() -> None:
    ids = ["src-1000", "src-010", "src-999", "src-001", "src-100"]
    assert sorted(ids, key=id_order) == ["src-001", "src-010", "src-100", "src-999", "src-1000"]


def test_the_strategy_reaches_every_kind_and_both_outcomes() -> None:
    seen: Counter[str] = Counter()

    @settings(max_examples=400, deadline=None, database=None)
    @given(packets(), transforms(), st.integers(0, 5))
    def record(documents: tuple[ArmDocument, ...], transform: Any, sample: int) -> None:
        result = apply_transform(
            transform, documents, question_id=3, sample_index=sample, labellers=LABELLERS
        )
        seen[type(transform).__name__] += 1
        seen["not_applicable" if not result.applicable else "applicable"] += 1
        if 0 < len(result.documents) < len(documents):
            seen["proper_subset"] += 1

    record()
    for name in (
        "IdentityTransform",
        "NoResearchTransform",
        "DropProviderTransform",
        "MaskPublisherTransform",
        "ShuffleOrderTransform",
        "RandomSubsetTransform",
        "DropClusterTransform",
        "DropPublisherClassTransform",
        "CollapseSyndicationTransform",
        "not_applicable",
        "applicable",
        "proper_subset",
    ):
        assert seen[name] > 0, (name, seen)


def _oracle(transform: Any, documents: tuple[ArmDocument, ...], question: int, sample: int) -> Any:
    """What each kind keeps, restated from its spec independently of the implementation.

    Returns the kept source ids as a set (order is asserted by the order property), or None
    for a result that must be "not applicable".
    """
    ids = {d.source_id for d in documents}
    clusters = _by_host(documents)
    groups: dict[str, list[str]] = {}
    for source_id, cluster in clusters.items():
        groups.setdefault(cluster, []).append(source_id)
    ordered_groups = sorted(
        (sorted(group, key=id_order) for group in groups.values()), key=lambda g: id_order(g[0])
    )
    if isinstance(transform, (IdentityTransform, MaskPublisherTransform, ShuffleOrderTransform)):
        return ids
    if isinstance(transform, NoResearchTransform):
        return set()
    if isinstance(transform, DropProviderTransform):
        return {d.source_id for d in documents if d.provider not in transform.providers}
    if isinstance(transform, RandomSubsetTransform):
        p = transform.inclusion_probability
        if transform.unit == "document":
            return {s for s in ids if unit_draw(transform.seed, question, sample, s) < p}
        kept = {c for c in groups if unit_draw(transform.seed, question, sample, c) < p}
        return {s for s, c in clusters.items() if c in kept}
    if isinstance(transform, DropClusterTransform):
        if sample >= len(ordered_groups):
            return None
        return ids - set(ordered_groups[sample])
    if isinstance(transform, DropPublisherClassTransform):
        return {
            d.source_id
            for d in documents
            if (d.rendered.publisher or "none") not in transform.classes
        }
    if all(len(group) < 2 for group in ordered_groups):
        return None
    return {group[0] for group in ordered_groups}


@settings(deadline=None)
@given(packets(), transforms(), st.integers(min_value=0, max_value=5), st.integers(1, 10**6))
def test_each_kind_keeps_exactly_what_its_spec_says(
    documents: tuple[ArmDocument, ...], transform: Any, sample: int, question: int
) -> None:
    result = apply_transform(
        transform, documents, question_id=question, sample_index=sample, labellers=LABELLERS
    )
    expected = _oracle(transform, documents, question, sample)
    if expected is None:
        assert not result.applicable and result.documents == ()
    else:
        assert result.applicable
        assert {d.source_id for d in result.documents} == expected
