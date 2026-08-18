"""Properties of the research-packet hash (M1-306).

``packet_sha256`` is a hash and a canonicalizer, which is the exact shape CLAUDE.md
requires a property pass for before the first review: M1-305 spent five review rounds
on one function for three properties a single local run finds. The four invariants
below are that list, applied here:

1. it never raises anything but :class:`PacketError`;
2. packet identity is independent of the order runs and documents arrive in;
3. **the digest survives the persisted round trip** -- dump to JSON, load it back,
   hash again, get the same value. This is the acceptance criterion's "same research
   packet hash" reduced to its smallest testable form, and it is the property that
   fails if the hash ever keys on the in-memory object (``datetime.fold``, the
   surrogate-pair spelling of an astral scalar) rather than on what the ledger holds;
4. no message echoes a value.

Two more are here because a hash rule is defined as much by what it *excludes*: the
excluded fields must not move the digest, and a field that is supposed to be in it
must. A property that only asserts stability passes on a function that returns a
constant.

Every property here was re-run against a deliberately broken ``packet.py`` to confirm
it fails -- see ``docs/M1-NOTES.md``. Three of M1-303's ten new properties passed
against broken code, which is the reason that step is written down rather than assumed.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun, validate_run
from whiskeyjack_bot.research.packet import (
    PacketError,
    ResearchPacket,
    build_packet,
    canonical_packet_json,
    packet_sha256,
)
from strategies import (
    HOSTILE_TEXT,
    research_documents,
    research_runs,
    round_trip,
    round_trip_run,
)

# A value that must never appear in any message this module produces. Low-entropy on
# purpose: a realistic-looking key would trip the repository's gitleaks history scan
# on every unrelated PR (docs/LESSONS.md).
PLANTED = "privateFAKE123456"


@st.composite
def packets(draw: st.DrawFn) -> ResearchPacket:
    """A schema-valid packet: one question, its runs, and documents that belong to them.

    Built by *construction* rather than by generating freely and filtering, because a
    packet's constructor refuses most free combinations (a document must belong to a
    run the packet carries, runs must be distinct) and a filtered strategy would
    spend its draws on rejections.
    """
    question_id = draw(st.sampled_from([1, 42]))
    runs = draw(st.lists(research_runs(), min_size=1, max_size=3))
    # Re-key the drawn runs onto this packet's question and onto distinct ids, which
    # is what the constructor requires and what the generator cannot know.
    #
    # ``model_copy``, deliberately, and not ``model_dump(mode="json")`` ->
    # ``validate_run``. The dump-and-revalidate spelling was the first cut, and it
    # silently defeated the whole suite: it round-trips every timestamp through
    # ISO-8601, which **drops datetime.fold**, so no packet reaching any property
    # below could carry the distinction the replay-stability property exists to
    # catch. Confirmed by mutation -- a fold-sensitive hash passed
    # ``test_the_hash_survives_the_persisted_round_trip`` under the old strategy and
    # fails under this one. The strategies generate fold; the strategy that consumes
    # them must not launder it away. (M1-305 round 3; docs/LESSONS.md lesson 5.)
    unique: list[ResearchRun] = [
        run.model_copy(update={"question_id": question_id, "retrieval_run_id": f"run-{index}"})
        for index, run in enumerate(runs)
    ]

    documents = draw(st.lists(research_documents(), max_size=4))
    attached: list[ResearchDocument] = []
    seen: set[tuple[str, str, str]] = set()
    for document in documents:
        run = unique[draw(st.integers(min_value=0, max_value=len(unique) - 1))]
        rekeyed = document.model_copy(update={"retrieval_run_id": run.retrieval_run_id})
        key = (rekeyed.retrieval_run_id, rekeyed.canonical_url, rekeyed.content_sha256)
        # Duplicates on the ledger's key are one row, so a packet may not hold two.
        if key in seen:
            continue
        seen.add(key)
        attached.append(rekeyed)
    return build_packet(question_id, unique, attached)


@given(packets())
def test_the_hash_is_hex_and_deterministic(packet: ResearchPacket) -> None:
    digest = packet_sha256(packet)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest == packet_sha256(packet)


@given(packets(), st.randoms(use_true_random=False))
def test_the_hash_is_independent_of_input_order(packet: ResearchPacket, random: object) -> None:
    """Reordering the same evidence is not different evidence.

    The ledger returns rows in whatever order a query produced, so a packet built
    from a live retrieval and one read back out must hash the same even though
    neither controls the sequence.
    """
    runs = list(packet.runs)
    documents = list(packet.documents)
    random.shuffle(runs)  # type: ignore[attr-defined]
    random.shuffle(documents)  # type: ignore[attr-defined]
    assert packet_sha256(build_packet(packet.question_id, runs, documents)) == packet_sha256(packet)


@given(packets())
def test_the_hash_survives_the_persisted_round_trip(packet: ResearchPacket) -> None:
    """Store it, load it, hash it again: the same digest.

    The acceptance criterion in one assertion. It is also the guard against the
    M1-305 round-3 defect class: an in-memory keying would be stable here on every
    input *except* the ones that carry a distinction JSON drops, which is why the
    strategies deliberately generate ``datetime.fold`` and surrogate-pair spellings.
    """
    replayed = build_packet(
        packet.question_id,
        [round_trip_run(run) for run in packet.runs],
        [round_trip(document) for document in packet.documents],
    )
    assert packet_sha256(replayed) == packet_sha256(packet)


@given(packets())
def test_the_canonical_form_is_ascii_and_parses(packet: ResearchPacket) -> None:
    """Whatever text a provider sent, the hashed string is plain ASCII JSON.

    ``ensure_ascii`` is what keeps a lone surrogate from making the digest
    uncomputable -- the failure ``model_dump_json()`` produces, and the reason the
    rule is spelled out rather than left to a convenience method.
    """
    rendered = canonical_packet_json(packet)
    assert rendered.isascii()
    assert json.loads(rendered)["question_id"] == packet.question_id


@given(
    packets(),
    st.none() | st.sampled_from(["research/9/z.json", "elsewhere/z.json"]),
    st.none() | st.sampled_from(["a/b.json", "c/d.json"]),
)
def test_storage_paths_do_not_move_the_hash(
    packet: ResearchPacket, run_path: str | None, document_path: str | None
) -> None:
    """Where the bytes were filed is not part of what was retrieved.

    These paths are relative to the operator-configured ``storage.artifact_root``, so
    a digest that included them would be machine-dependent: the same evidence would
    fail to verify on a second checkout. This property is what makes that a checked
    claim rather than a comment.
    """
    runs = []
    for run in packet.runs:
        payload = run.model_dump(mode="json")
        payload["raw_response_path"] = run_path
        runs.append(validate_run(payload))
    documents = []
    for document in packet.documents:
        payload = document.model_dump(mode="json")
        payload["raw_artifact_path"] = document_path
        documents.append(ResearchDocument.model_validate(payload))
    assert packet_sha256(build_packet(packet.question_id, runs, documents)) == packet_sha256(packet)


@given(packets(), HOSTILE_TEXT)
def test_a_changed_query_moves_the_hash(packet: ResearchPacket, query: str) -> None:
    """The discriminating half: the packet records *how* evidence was gathered.

    Without this, every stability property above is satisfied by a function that
    returns a constant. Two runs that surfaced the same articles under different
    queries are not the same research.
    """
    payload = packet.runs[0].model_dump(mode="json")
    if payload["queries"] == [query]:
        return  # Not a change; nothing is claimed about hashing equal inputs differently.
    payload["queries"] = [query]
    changed = [validate_run(payload), *packet.runs[1:]]
    assert packet_sha256(
        build_packet(packet.question_id, changed, packet.documents)
    ) != packet_sha256(packet)


@given(
    st.one_of(
        st.none(),
        st.integers(),
        st.text(max_size=8),
        st.lists(st.integers(), max_size=2),
        st.booleans(),
    )
)
def test_malformed_input_raises_only_packet_error(value: object) -> None:
    """Every malformed shape arrives as this module's own error type.

    A raw ``AttributeError``/``TypeError`` escaping is a review finding in this
    repository, twice over.
    """
    for call in (
        lambda: build_packet(value, [], []),  # type: ignore[arg-type]
        lambda: build_packet(1, value, []),  # type: ignore[arg-type]
        lambda: build_packet(1, [], value),  # type: ignore[arg-type]
        lambda: packet_sha256(value),  # type: ignore[arg-type]
        lambda: canonical_packet_json(value),  # type: ignore[arg-type]
    ):
        try:
            call()
        except PacketError:
            pass
        except Exception as exc:  # noqa: BLE001 - the assertion is that this is unreachable
            pytest.fail(f"{type(exc).__name__} escaped instead of PacketError")


@given(research_runs(), research_documents())
def test_no_message_echoes_a_planted_secret(run: ResearchRun, document: ResearchDocument) -> None:
    """A rejection never reprints the row that caused it.

    The planted value goes into every free-text field a caller controls, and every
    rejection path is then forced. ``str(exc)`` alone is not the whole channel --
    ``repr`` and the args tuple reach a traceback renderer too.
    """
    run_payload = run.model_dump(mode="json")
    run_payload["queries"] = [PLANTED]
    run_payload["error_summary"] = PLANTED
    run_payload["question_id"] = 1
    planted_run = validate_run(run_payload)

    document_payload = document.model_dump(mode="json")
    document_payload["title"] = PLANTED
    document_payload["retrieval_run_id"] = "not-a-run-in-this-packet"
    planted_document = ResearchDocument.model_validate(document_payload)

    with pytest.raises(PacketError) as caught:
        build_packet(1, [planted_run], [planted_document])
    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert PLANTED not in rendered

    # And the success path must not smuggle it out either: the hash is a digest, not
    # a rendering, so the planted value must not survive into anything a caller prints.
    digest = packet_sha256(build_packet(1, [planted_run], []))
    assert PLANTED not in digest
