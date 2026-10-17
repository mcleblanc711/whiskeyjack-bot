"""Invariants of the packet hash's 1.1.0 dispatch and the locale rows it covers (M1-357).

- **No locale rows, no change.** For any packet without locale rows, ``packet_sha256`` is
  byte-identical to the 1.0.0 rule as it stood before 019. The oracle is a hand-written copy of
  that rule *in this file* -- outside the program, so a change to the shared code cannot move
  the oracle with it (the M1-608 lesson: a parity test with no witness outside the program
  cannot see a change to the shared constant).
- **Locale rows always change the version.** A packet with at least one locale row hashes under
  1.1.0, and differs from the same packet without them.
- **Order independence.** Locale rows handed over in any order hash the same.
- **Replay stability.** A locale row's persisted JSON form validates back to a row that
  hashes the same.
- **The constructor refuses only with PacketError**, for a locale that describes no carried
  document or a document described twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hypothesis import given, strategies as st
from strategies import HOSTILE_TEXT, TIMESTAMPS, research_documents, research_runs

from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.dedup import dedup_key
from whiskeyjack_bot.research.model import (
    DocumentLocale,
    ResearchSchemaError,
    validate_document,
    validate_locale,
    validate_run,
)
from whiskeyjack_bot.research.packet import (
    PacketError,
    build_packet,
    canonical_packet_json,
    packet_sha256,
)
from whiskeyjack_bot.research.store import StoreError, load_packet, persist_retrieval


def _oracle_v1_0(packet: object) -> str:
    """The 1.0.0 rule exactly as ``research/packet.py`` wrote it before 019. Do not edit."""
    excluded_run = {"raw_response_path"}
    excluded_doc = {"document_id", "raw_artifact_path"}

    def dump(model: object, excluded: set[str]) -> dict[str, object]:
        dumped = model.model_dump(mode="json", warnings=False)  # type: ignore[attr-defined]
        return {k: v for k, v in dumped.items() if k not in excluded}

    payload = {
        "packet_schema_version": "1.0.0",
        "question_id": packet.question_id,  # type: ignore[attr-defined]
        "runs": [
            dump(run, excluded_run)
            for run in sorted(packet.runs, key=lambda run: run.retrieval_run_id)  # type: ignore[attr-defined]
        ],
        "documents": [
            dump(document, excluded_doc)
            for document in sorted(packet.documents, key=dedup_key)  # type: ignore[attr-defined]
        ],
    }
    rendered = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


@st.composite
def _packets(draw: st.DrawFn) -> tuple[object, list[DocumentLocale]]:
    """A valid packet, plus locale rows for a drawn subset of its documents."""
    run = draw(research_runs())
    documents = draw(st.lists(research_documents(), max_size=4))
    owned = {}
    for document in documents:
        moved = document.model_copy(update={"retrieval_run_id": run.retrieval_run_id})
        owned[dedup_key(moved)] = moved
    packet = build_packet(run.question_id, [run], list(owned.values()))
    locales: list[DocumentLocale] = []
    for key in sorted(owned):
        if draw(st.booleans()):
            native = draw(st.none() | HOSTILE_TEXT)
            translated = draw(st.booleans())
            locales.append(
                validate_locale(
                    {
                        "retrieval_run_id": key[0],
                        "canonical_url": key[1],
                        "content_sha256": key[2],
                        "locale_code": draw(st.sampled_from(["IR", "OM", "CA-QC"])),
                        "publisher_origin": draw(
                            st.sampled_from(["local", "external", "unclassified"])
                        ),
                        "language": draw(st.none() | st.sampled_from(["fa", "ar", "en-US"])),
                        "provider_story_country": draw(st.none() | HOSTILE_TEXT),
                        "native_title": native,
                        "english_title": draw(st.none() | HOSTILE_TEXT) if translated else native,
                        "translation_source": "provider:asknews" if translated else "none",
                        "provider_first_seen_at_utc": draw(st.none() | TIMESTAMPS),
                        "provider_labels": draw(
                            st.none() | st.just({"reporting_voice": "Objective"})
                        ),
                    }
                )
            )
    return packet, locales


@given(_packets())
def test_a_packet_without_locale_rows_hashes_under_the_unchanged_1_0_0_rule(
    drawn: tuple[object, list[DocumentLocale]],
) -> None:
    packet, _ = drawn
    assert packet_sha256(packet) == _oracle_v1_0(packet)  # type: ignore[arg-type]


@given(_packets())
def test_locale_rows_move_the_version_and_the_digest(
    drawn: tuple[object, list[DocumentLocale]],
) -> None:
    packet, locales = drawn
    with_locales = build_packet(packet.question_id, packet.runs, packet.documents, locales)  # type: ignore[attr-defined]
    rendered = json.loads(canonical_packet_json(with_locales))
    if locales:
        assert rendered["packet_schema_version"] == "1.1.0"
        assert len(rendered["document_locales"]) == len(locales)
        assert packet_sha256(with_locales) != packet_sha256(packet)  # type: ignore[arg-type]
    else:
        assert rendered["packet_schema_version"] == "1.0.0"
        assert "document_locales" not in rendered


@given(_packets(), st.randoms(use_true_random=False))
def test_locale_order_does_not_change_the_digest(
    drawn: tuple[object, list[DocumentLocale]], rnd: object
) -> None:
    packet, locales = drawn
    shuffled = list(locales)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    left = build_packet(packet.question_id, packet.runs, packet.documents, locales)  # type: ignore[attr-defined]
    right = build_packet(packet.question_id, packet.runs, packet.documents, shuffled)  # type: ignore[attr-defined]
    assert packet_sha256(left) == packet_sha256(right)


@given(_packets())
def test_a_persisted_locale_row_replays_to_the_same_digest(
    drawn: tuple[object, list[DocumentLocale]],
) -> None:
    packet, locales = drawn
    replayed = [
        validate_locale(json.loads(json.dumps(locale.model_dump(mode="json"), ensure_ascii=True)))
        for locale in locales
    ]
    left = build_packet(packet.question_id, packet.runs, packet.documents, locales)  # type: ignore[attr-defined]
    right = build_packet(packet.question_id, packet.runs, packet.documents, replayed)  # type: ignore[attr-defined]
    assert packet_sha256(left) == packet_sha256(right)


@given(_packets(), st.integers(0, 3))
def test_a_repeated_or_foreign_locale_refuses_with_packet_error(
    drawn: tuple[object, list[DocumentLocale]], which: int
) -> None:
    packet, locales = drawn
    if not locales:
        return
    target = locales[which % len(locales)]
    # A second row for one document, and a row naming a run the packet does not carry: both
    # are invalid for every drawn packet, so each must refuse, and only with PacketError.
    foreign = target.model_copy(update={"retrieval_run_id": "run-not-in-this-packet"})
    for bad in ([*locales, target], [*locales, foreign]):
        try:
            build_packet(packet.question_id, packet.runs, packet.documents, bad)  # type: ignore[attr-defined]
        except PacketError:
            continue
        raise AssertionError("an invalid locale set was accepted")


# --- Round 1, B1: a real SQLite round trip for provider labels -------------------------
#
# The properties above replay a locale row through JSON in memory. That cannot see what the
# ledger does: `store._load_json` refuses a stored lone surrogate, so labels the writer
# committed unchecked could not be loaded back, and the packet could not be replayed. This
# property writes through the real store into a real ledger and reads back.


_WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
_LABEL_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers(-5, 5) | HOSTILE_TEXT,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(HOSTILE_TEXT, inner, max_size=3),
    max_leaves=6,
)
_LABELS = st.none() | st.dictionaries(HOSTILE_TEXT, _LABEL_VALUES, max_size=3)


@pytest.fixture(scope="module")
def _ledger(tmp_path_factory: pytest.TempPathFactory) -> sqlite3.Connection:
    db = Path(tmp_path_factory.mktemp("locale-ledger")) / "ledger.sqlite3"
    initialize_ledger(db)
    return connect(db)


@given(labels=_LABELS)
def test_labels_either_refuse_atomically_or_replay_from_the_ledger(
    _ledger: sqlite3.Connection, labels: object
) -> None:
    run_id = f"run-{uuid.uuid4().hex}"
    run = validate_run(
        {
            "retrieval_run_id": run_id,
            "question_id": 9,
            "provider": "asknews",
            "started_at_utc": _WHEN,
            "completed_at_utc": _WHEN,
        }
    )
    document = validate_document(
        {
            "retrieval_run_id": run_id,
            "original_url": "https://irna.ir/news/1",
            "canonical_url": "https://irna.ir/news/1",
            "retrieved_at_utc": _WHEN,
            "source_type": "news",
            "provenance": "direct_api",
            "content_sha256": "a" * 64,
        }
    )
    try:
        locale = validate_locale(
            {
                "retrieval_run_id": run_id,
                "canonical_url": document.canonical_url,
                "content_sha256": document.content_sha256,
                "locale_code": "IR",
                "publisher_origin": "local",
                "translation_source": "none",
                "provider_labels": labels,
            }
        )
    except ResearchSchemaError:
        return  # the model refused it; nothing reaches the store
    built = build_packet(9, [run], [document], [locale])
    try:
        persist_retrieval(_ledger, run, [document], locales=[locale])
    except StoreError:
        # Refused: then nothing of this run may have been committed.
        assert (
            _ledger.execute(
                "SELECT count(*) FROM research_runs WHERE retrieval_run_id = ?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        return
    loaded = load_packet(_ledger, question_id=9, retrieval_run_ids=[run_id])
    assert packet_sha256(loaded) == packet_sha256(built)


# --- M1-359: what the shadow arm's model is shown ---------------------------------------------


def _persisted(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=True))


def _question_for(packet: object) -> object:
    from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion

    return CanonicalBinaryQuestion(
        question_id=packet.question_id,  # type: ignore[attr-defined]
        post_id=7,
        title="Will X happen?",
    )


@given(_packets())
def test_a_document_carries_a_locale_exactly_when_the_packet_holds_its_row(
    drawn: tuple[object, list[DocumentLocale]],
) -> None:
    """The lens render (``include_locales=True``): every document has a ``locale`` key, and the
    set rendered non-null is the set with a row -- both directions, so neither "every document
    gets one" nor "none does" passes. Each rendered locale is its row's five shown fields."""
    from whiskeyjack_bot.forecast.inputs import build_model_input, render_model_input

    packet, locales = drawn
    with_locales = build_packet(packet.question_id, packet.runs, packet.documents, locales)  # type: ignore[attr-defined]
    # No ``except ForecastInputError: return`` here or below: a valid packet always builds, and
    # an escape for a refusal let a mutant that broke every build pass both properties.
    built = build_model_input(
        question=_question_for(packet),  # type: ignore[arg-type]
        packet=with_locales,
        tournament_id="minibench",
        as_of=_WHEN,
        include_locales=True,
    )
    rendered = render_model_input(built)
    assert rendered.isascii()
    documents = json.loads(rendered)["research_documents"]
    assert all("locale" in document for document in documents)
    by_url_and_source = {
        (reference.canonical_url, reference.content_sha256): reference.source_id
        for reference in built.sources
    }
    expected = {by_url_and_source[(row.canonical_url, row.content_sha256)]: row for row in locales}
    shown = {d["source_id"]: d["locale"] for d in documents if d["locale"] is not None}
    assert set(shown) == set(expected)
    for source_id, locale in shown.items():
        row = expected[source_id]
        # In the persisted form (``json.dumps`` -> ``json.loads``): a surrogate pair held as two
        # code units renders as an escaped pair that reads back as one character (M1-306).
        assert locale == _persisted(
            {
                "locale_code": row.locale_code,
                "publisher_origin": row.publisher_origin,
                "language": row.language,
                "native_title": row.native_title,
                "translation_source": row.translation_source,
            }
        )


@given(_packets())
def test_the_control_render_does_not_depend_on_locale_rows(
    drawn: tuple[object, list[DocumentLocale]],
) -> None:
    """``include_locales`` off -- every control forecast: the bytes sent are the same whether
    or not the packet carries locale rows, and no ``locale`` key appears anywhere. A packet
    with lens rows changes the packet hash (above) and nothing the control's model sees."""
    from whiskeyjack_bot.forecast.inputs import build_model_input, render_model_input

    packet, locales = drawn
    with_locales = build_packet(packet.question_id, packet.runs, packet.documents, locales)  # type: ignore[attr-defined]
    plain, located = (
        render_model_input(
            build_model_input(
                question=_question_for(packet),  # type: ignore[arg-type]
                packet=candidate,  # type: ignore[arg-type]
                tournament_id="minibench",
                as_of=_WHEN,
            )
        )
        for candidate in (packet, with_locales)
    )
    assert plain == located
    assert all("locale" not in d for d in json.loads(plain)["research_documents"])
