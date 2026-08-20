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

import itertools
import json
import sqlite3
from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.store import (
    StoreError,
    load_packet,
    load_run,
    persist_retrieval,
)

from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun, validate_run
from whiskeyjack_bot.research.packet import (
    PacketError,
    ResearchPacket,
    build_packet,
    canonical_packet_json,
    packet_sha256,
)
from strategies import (
    ENCODABLE_TEXT,
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
def packets(draw: st.DrawFn, text: st.SearchStrategy[str] = HOSTILE_TEXT) -> ResearchPacket:
    """A schema-valid packet: one question, its runs, and documents that belong to them.

    Built by *construction* rather than by generating freely and filtering, because a
    packet's constructor refuses most free combinations (a document must belong to a
    run the packet carries, runs must be distinct) and a filtered strategy would
    spend its draws on rejections.
    """
    question_id = draw(st.sampled_from([1, 42]))
    runs = draw(st.lists(research_runs(text=text), min_size=1, max_size=3))
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

    documents = draw(st.lists(research_documents(text=text), max_size=4))
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


@given(research_runs(), research_documents())
def test_mutating_caller_owned_inputs_does_not_move_the_hash(
    run: ResearchRun, document: ResearchDocument
) -> None:
    """M1-313: the packet is a copy, not a view onto the caller's objects.

    ``ResearchRun``/``ResearchDocument`` are plain (unfrozen) pydantic models with
    mutable list/dict fields (``queries``, ``provider_config``), so a tuple of them
    is immutable only at the container level unless ``build_packet`` detaches its
    own copies. Every caller-owned mutable surface the acceptance criterion names is
    exercised here: the run object itself, a document object, the run's ``queries``
    list, and the run's ``provider_config`` dict.
    """
    run = run.model_copy(update={"question_id": 1, "provider_config": {"k": "v"}})
    document = document.model_copy(update={"retrieval_run_id": run.retrieval_run_id})
    runs = [run]
    documents = [document]

    packet = build_packet(1, runs, documents)
    before = packet_sha256(packet)

    # Mutate the caller's own lists -- already covered by build_packet's `tuple(...)`
    # even pre-fix, kept here so the test exercises every input the criterion names.
    runs.append(run)
    documents.append(document)

    # Mutate the caller-retained model objects and their nested mutable fields.
    run.queries.append("a query appended after build_packet returned")
    assert run.provider_config is not None
    run.provider_config["injected"] = "a value added after build_packet returned"
    run.error_summary = "mutated after construction"
    document.title = "mutated after construction"

    assert packet_sha256(packet) == before


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
    # Equality in the **persisted form**, not in memory. This guard originally compared
    # the Python strings and the property failed at the full 200-example profile (it
    # passed at 25, which is exactly what `fast` is warned about): the packet held the
    # astral scalar "\U0001f600" and the drawn query was its surrogate-pair spelling
    # "\ud83d\ude00". Those are two distinct Python strings that `ensure_ascii` renders
    # identically, so the digest correctly does not move -- the hash keys on persisted
    # equivalence, and a test that asks it to distinguish them is asking for the M1-305
    # round-4 bug back.
    if _persisted(payload["queries"]) == _persisted([query]):
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


# --- the round trip that goes through SQLite ---------------------------------


@pytest.fixture(scope="module")
def sqlite_ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """One ledger for the whole property, not one per example.

    ``@given`` with a function-scoped ``tmp_path`` is a hypothesis health-check
    failure, and building a schema per example would dominate the runtime. Each
    example writes its own run id instead.
    """
    db = tmp_path_factory.mktemp("packet-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _persisted(value: object) -> str:
    """The canonical rendering the packet digest keys on. See `packet.canonical_packet_json`."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _is_storable(value: str | None) -> bool:
    """True if SQLite can hold this text; see store._require_storable_text."""
    if value is None:
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _json_text(value: object) -> list[str | None]:
    """Every string inside a provider config, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str | None] = []
        for key, nested in value.items():
            out.append(key)
            out.extend(_json_text(nested))
        return out
    if isinstance(value, list):
        return [s for item in value for s in _json_text(item)]
    return []


def _text_of(packet: ResearchPacket) -> list[str | None]:
    """Every caller-supplied text value the writer binds into a TEXT column."""
    values: list[str | None] = []
    for run in packet.runs:
        values.extend([run.retrieval_run_id, run.error_summary, run.agent_model])
        # The JSON columns are checked too: a surrogate *pair* is not UTF-8
        # encodable and json round-trips it into a different string, so the store
        # refuses it (see store._require_storable_json).
        values.extend(run.queries)
        values.extend(_json_text(run.provider_config))
    for document in packet.documents:
        values.extend(
            [
                document.title,
                document.publisher,
                document.author,
                document.snippet,
                document.summary,
                document.raw_artifact_path,
                document.original_url,
                document.canonical_url,
            ]
        )
    return values


# Monotonic, not drawn: a drawn nonce repeats across examples and collides on
# research_runs' primary key, which fails the test for a reason that has nothing to
# do with what it asserts.
_RUN_SEQUENCE = itertools.count()


@given(packets(text=ENCODABLE_TEXT))
@settings(max_examples=60, deadline=None)
def test_the_hash_survives_a_real_sqlite_round_trip(
    sqlite_ledger: sqlite3.Connection, packet: ResearchPacket
) -> None:
    """The persisted-form round trip, through the actual ledger rather than JSON.

    ``round_trip_run``/``round_trip`` simulate storage with ``json.dumps`` ->
    ``json.loads``, which is the right model of *most* of what storage does and was
    wrong about the case that mattered: JSON preserves the sign of ``-0.0`` and a
    SQLite REAL column does not, so a schema-valid cost hashed differently before and
    after persistence and no property could see it (round 1, finding 1). A simulated
    boundary tests the simulation.

    Re-keyed onto a per-example run id so one ledger serves the whole property; the
    ids are not part of what is being asserted, since both sides carry the same ones.

    Scoped to packets the ledger accepts, which is the claim being made: *every
    schema-valid run that persists successfully replays to its original hash.* The
    shared strategies deliberately generate lone surrogates and surrogate pairs, and
    the store deliberately refuses both -- neither is UTF-8 encodable, and escaping
    or recombining them would store something other than what the caller supplied.
    Those are a refusal to assert about, not a hash failure, and
    ``test_research_store.py`` asserts the refusal directly.

    Drawn from ``ENCODABLE_TEXT`` rather than filtered with ``assume``: filtering was
    the first cut and hypothesis rejected it as a failed health check, because
    surrogates are common enough in ``HOSTILE_TEXT`` that most packets were
    discarded. The assertion below is not redundant with the draw -- it is what would
    fail loudly if ``ENCODABLE_TEXT`` ever stopped excluding them, rather than the
    property quietly asserting nothing.
    """
    assert all(_is_storable(value) for value in _text_of(packet))
    runs, documents = [], []
    for index, run in enumerate(packet.runs):
        run_id = f"p{next(_RUN_SEQUENCE)}-{index}"
        runs.append(
            run.model_copy(
                update={"retrieval_run_id": run_id, "completed_at_utc": run.started_at_utc}
            )
        )
        for document in packet.documents:
            if document.retrieval_run_id == run.retrieval_run_id:
                documents.append(document.model_copy(update={"retrieval_run_id": run_id}))

    question_id = packet.question_id
    before = packet_sha256(build_packet(question_id, runs, documents))
    for run in runs:
        persist_retrieval(
            sqlite_ledger,
            run,
            [d for d in documents if d.retrieval_run_id == run.retrieval_run_id],
        )
    stored = load_packet(
        sqlite_ledger,
        question_id=question_id,
        retrieval_run_ids=[run.retrieval_run_id for run in runs],
    )
    if packet_sha256(stored) != before:
        import difflib

        a = canonical_packet_json(build_packet(question_id, runs, documents))
        b = canonical_packet_json(stored)
        raise AssertionError(
            "\n".join(difflib.unified_diff(a.split(","), b.split(","), lineterm="", n=1))
        )


@given(HOSTILE_TEXT)
@settings(max_examples=100, deadline=None)
def test_everything_the_reader_returns_is_something_the_writer_could_write(
    sqlite_ledger: sqlite3.Connection, text: str
) -> None:
    """Whatever comes out of the ledger must be a value the writer would accept.

    The contract round 2 settled for the artifact envelope and round 3 found broken
    for the JSON columns, stated as a property instead of one example at a time:
    `queries_json = '["\\ud800"]'` is schema-accepted, **pure ASCII** on disk, and
    `json.loads` hands back a lone surrogate the writer refuses.

    The stored value is written **around** the writer, straight into the column,
    because that is the state the reader exists to be safe against: a schema-valid
    row from a foreign tool or from corruption. Going through the writer would only
    produce rows the writer already accepts, which was never the case in doubt.

    The invariant is deliberately about **what the reader returns**, not about the
    text that was encoded. The first cut asserted "if the writer refused this text,
    the reader must refuse it too" and was wrong on its second example: a surrogate
    *pair* is refused by the writer, but `json.loads` recombines it into an astral
    scalar, and that scalar is a perfectly ordinary value the writer would happily
    store. The reader returning it is correct. What must never happen is the reader
    returning something unwritable.
    """
    run_id = f"rw{next(_RUN_SEQUENCE)}"
    persist_retrieval(
        sqlite_ledger,
        validate_run(
            {
                "retrieval_run_id": run_id,
                "question_id": 7,
                "provider": "asknews",
                "started_at_utc": "2026-08-18T12:00:00+00:00",
                "completed_at_utc": "2026-08-18T12:00:00+00:00",
            }
        ),
        [],
    )
    sqlite_ledger.execute(
        "UPDATE research_runs SET queries_json = ? WHERE retrieval_run_id = ?",
        (json.dumps([text], ensure_ascii=True), run_id),
    )

    try:
        queries = load_run(sqlite_ledger, run_id).queries
    except StoreError:
        return  # A refusal is always an acceptable answer.
    for query in queries:
        assert _is_storable(query), "the reader returned a value its own writer refuses"
    # And what it returns must survive being written back: read-then-write is the
    # replay path, so a value that cannot make the return trip is not replayable.
    persist_retrieval(
        sqlite_ledger,
        validate_run(
            {
                "retrieval_run_id": f"{run_id}-rt",
                "question_id": 7,
                "provider": "asknews",
                "started_at_utc": "2026-08-18T12:00:00+00:00",
                "completed_at_utc": "2026-08-18T12:00:00+00:00",
                "queries": queries,
            }
        ),
        [],
    )
    assert load_run(sqlite_ledger, f"{run_id}-rt").queries == queries
