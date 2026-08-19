"""M1-306 acceptance: retrieval runs and their documents persist to the ledger and
replay out of it with zero provider calls, as this module's own error type."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.config import AppConfig, load_config
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    validate_document,
    validate_run,
)
from whiskeyjack_bot.research.packet import build_packet, packet_sha256
from whiskeyjack_bot.research.store import (
    StoreError,
    _read_snapshot,
    complete_run,
    list_retrieval_run_ids,
    load_documents,
    load_packet,
    load_run,
    open_run,
    persist_retrieval,
    replay_research,
    with_retrieval_counts,
)

WHEN = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 18, 12, 5, tzinfo=timezone.utc)
QUESTION = 42

# Low-entropy on purpose: a realistic-looking key would trip the repository's
# gitleaks full-history scan on every unrelated PR (docs/LESSONS.md).
PLANTED = "privateFAKE123456"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _run(**overrides: Any) -> ResearchRun:
    payload: dict[str, Any] = {
        "retrieval_run_id": "run-1",
        "question_id": QUESTION,
        "provider": "asknews",
        "provider_config": {"hours_back": 720, "strategy": "news knowledge"},
        "queries": ["inflation", "cpi release"],
        "started_at_utc": WHEN,
        "completed_at_utc": LATER,
        "freshness_cutoff_utc": datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        "cost_usd": None,
    }
    payload.update(overrides)
    return validate_run(payload)


def _document(index: int = 0, **overrides: Any) -> ResearchDocument:
    payload: dict[str, Any] = {
        "retrieval_run_id": "run-1",
        "original_url": f"https://example.org/a{index}",
        "canonical_url": f"https://example.org/a{index}",
        "title": f"Article {index}",
        "retrieved_at_utc": WHEN,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": f"{index:064x}",
    }
    payload.update(overrides)
    return validate_document(payload)


def _config(tmp_path: Path, **retrieval: object) -> AppConfig:
    data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openai/gpt-4o"
    data["retrieval"]["social"]["agent_model"] = "grok-3"
    data["retrieval"].update(retrieval)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


# --- the acceptance criterion ------------------------------------------------


def test_replay_reproduces_the_packet_hash_with_zero_provider_calls(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    """The item's acceptance criterion, in one assertion.

    The hash is taken of the in-memory packet *before* anything is written, and
    again of the packet assembled from rows read back out. Zero provider calls is
    structural (see ``test_the_store_imports_no_provider_client``) and is also
    enforced by the suite-wide socket guard in ``tests/unit/conftest.py``.
    """
    run = with_retrieval_counts(_run(), documents_dropped=2, duplicates_collapsed=1)
    documents = [_document(i) for i in range(3)]
    before = packet_sha256(build_packet(QUESTION, [run], documents))

    persist_retrieval(ledger, run, documents, raw_response_path="research/42/run-1.json")

    config = _config(tmp_path, replay_saved_research=True)
    replayed = replay_research(
        ledger,
        config,
        question_id=QUESTION,
        retrieval_run_ids=list_retrieval_run_ids(ledger, question_id=QUESTION),
    )
    assert packet_sha256(replayed) == before


def test_the_store_imports_no_provider_client() -> None:
    """Zero provider calls is a property of the import graph, not of a mock count.

    Run in a clean interpreter, because in-process ``sys.modules`` is polluted by
    every other test that imported an adapter -- checking it there would assert
    nothing. If importing the replay path cannot reach an SDK or an HTTP client,
    there is no call for a replay to make, and none can be added without this
    failing. The mechanical form of the check M1-308 used to keep
    ``forecasting_tools`` out of ``env_verify``.
    """
    program = (
        "import sys;"
        "before=set(sys.modules);"
        "import whiskeyjack_bot.research.store;"
        "import whiskeyjack_bot.research.packet;"
        "print(','.join(sorted(set(sys.modules)-before)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    # The *delta*, and by full dotted name. Top-level names are too coarse:
    # `research.model` legitimately imports `urllib.parse` for `urlsplit`, which is
    # string handling, while `urllib.request` is the one that opens a connection.
    # A check that could not tell those apart would have to be weakened until it
    # asserted nothing.
    #
    # ``socket`` is deliberately absent from the list, and for a reason worth
    # recording rather than discovering twice: it *is* loaded, transitively, by
    # ``email.utils`` (which pydantic reaches through its own imports). That is a
    # fact about pydantic's internals, not evidence that this module can open a
    # connection, so asserting on it would fail for an unrelated reason and end up
    # weakened. What actually forbids a call at runtime is the suite-wide socket
    # guard in ``tests/unit/conftest.py``; what this test forbids is the *client*
    # ever being reachable from the replay path.
    added = {name for name in result.stdout.strip().split(",") if name}
    forbidden = {
        "asknews_sdk",
        "httpx",
        "forecasting_tools",
        "requests",
        "urllib.request",
        "http.client",
        "ssl",
    }
    assert not (added & forbidden), f"replay path imported: {sorted(added & forbidden)}"


# --- the two-phase write -----------------------------------------------------


def test_open_run_records_the_spend_before_the_run_completes(
    ledger: sqlite3.Connection,
) -> None:
    open_run(ledger, _run(completed_at_utc=None))
    stored = load_run(ledger, "run-1")
    assert stored.completed_at_utc is None
    assert stored.started_at_utc == WHEN
    # Nothing was retrieved yet, so nothing was dropped -- and "unmeasured" is not
    # the same claim as "nothing".
    assert stored.documents_dropped is None
    assert stored.duplicates_collapsed is None


def test_complete_run_fills_in_what_the_run_learned(ledger: sqlite3.Connection) -> None:
    open_run(ledger, _run(completed_at_utc=None))
    finished = with_retrieval_counts(
        _run(cost_usd=0.0125, error_summary="provider call failed"),
        documents_dropped=0,
        duplicates_collapsed=3,
    )
    ids = complete_run(ledger, finished, [_document(0)], raw_response_path="research/42/run-1.json")

    stored = load_run(ledger, "run-1")
    assert len(ids) == 1
    assert stored.completed_at_utc == LATER
    assert stored.cost_usd == 0.0125
    assert stored.raw_response_path == "research/42/run-1.json"
    assert stored.documents_dropped == 0
    assert stored.duplicates_collapsed == 3


def test_open_run_refuses_a_run_that_already_completed(ledger: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="has not completed"):
        open_run(ledger, _run())


def test_complete_run_refuses_a_run_that_was_never_opened(ledger: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="no open run matches this one"):
        complete_run(ledger, _run(), [])


def test_complete_run_refuses_a_run_with_no_completion_time(
    ledger: sqlite3.Connection,
) -> None:
    open_run(ledger, _run(completed_at_utc=None))
    with pytest.raises(StoreError, match="completed_at_utc"):
        complete_run(ledger, _run(completed_at_utc=None), [])


def test_a_failed_document_write_leaves_no_run_row(ledger: sqlite3.Connection) -> None:
    """Atomicity: the run and its documents are one unit or neither.

    Exercised through a real failure rather than a mock -- a document carrying a
    lone surrogate cannot be encoded as UTF-8 by ``sqlite3`` -- so this tests the
    transaction, not a patched one.
    """
    with pytest.raises(StoreError):
        persist_retrieval(ledger, _run(), [_document(0), _document(1, title="a\ud800b")])
    assert ledger.execute("SELECT count(*) FROM research_runs").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM research_documents").fetchone()[0] == 0


# --- deduplication, ownership and the counters -------------------------------


def test_duplicates_collapse_before_the_unique_constraint_is_reached(
    ledger: sqlite3.Connection,
) -> None:
    """The AskNews current/historical passes overlap by design.

    Without collapsing, an ordinary successful run would fail on
    ``UNIQUE (retrieval_run_id, canonical_url, content_sha256)`` *after* its calls
    were paid for. The assertion is that one row lands and no error is raised.
    """
    duplicate = _document(0, title="a different title for the same article")
    ids = persist_retrieval(ledger, _run(), [_document(0), duplicate, _document(1)])
    assert len(ids) == 2
    assert len(load_documents(ledger, "run-1")) == 2


def test_a_document_from_another_run_is_refused(ledger: sqlite3.Connection) -> None:
    """Caught here rather than left to the foreign key.

    The FK would happily accept a document belonging to a *different existing* run,
    filing evidence under the wrong retrieval without any error at all.
    """
    with pytest.raises(StoreError, match="this run's retrieval_run_id"):
        persist_retrieval(ledger, _run(), [_document(0, retrieval_run_id="run-9")])


def test_an_unmeasured_count_stays_distinct_from_zero(ledger: sqlite3.Connection) -> None:
    """004 keeps NULL and 0 apart on purpose; the round trip must too."""
    persist_retrieval(
        ledger, with_retrieval_counts(_run(), documents_dropped=0, duplicates_collapsed=0), []
    )
    persist_retrieval(
        ledger,
        with_retrieval_counts(
            _run(retrieval_run_id="run-2"), documents_dropped=None, duplicates_collapsed=None
        ),
        [],
    )
    assert load_run(ledger, "run-1").documents_dropped == 0
    assert load_run(ledger, "run-2").documents_dropped is None


def test_with_retrieval_counts_refuses_a_negative_count() -> None:
    """Before any I/O, as this module's error -- not at 004's CHECK after the spend."""
    with pytest.raises(StoreError, match="non-negative"):
        with_retrieval_counts(_run(), documents_dropped=-1, duplicates_collapsed=0)


def test_an_unknown_cost_round_trips_as_unknown(ledger: sqlite3.Connection) -> None:
    """``cost_usd is None`` means unknown, never free (M1-303 round 3).

    A run that spent real money the adapter could not vouch for must not read back
    as a free one, which is what summing NULL as zero would make it.
    """
    persist_retrieval(ledger, _run(cost_usd=None), [])
    assert load_run(ledger, "run-1").cost_usd is None


# --- replay refusals ---------------------------------------------------------


def test_replay_refuses_while_the_config_flag_is_off(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    persist_retrieval(ledger, _run(), [_document(0)])
    with pytest.raises(StoreError, match="replay_saved_research is disabled"):
        replay_research(
            ledger, _config(tmp_path), question_id=QUESTION, retrieval_run_ids=("run-1",)
        )


def test_replay_refuses_an_empty_packet_rather_than_returning_one(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    """An empty packet cannot be told apart from research that found nothing.

    Returning one would let a caller forecast as though research had happened,
    which is the failure the ledger exists to make impossible.
    """
    config = _config(tmp_path, replay_saved_research=True)
    with pytest.raises(StoreError, match="refusing to replay an empty packet"):
        replay_research(ledger, config, question_id=QUESTION, retrieval_run_ids=())


def test_a_question_with_several_runs_replays_all_of_them(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    persist_retrieval(ledger, _run(), [_document(0)])
    persist_retrieval(
        ledger,
        _run(retrieval_run_id="run-2", provider="exa"),
        [_document(1, retrieval_run_id="run-2")],
    )
    packet = replay_research(
        ledger,
        _config(tmp_path, replay_saved_research=True),
        question_id=QUESTION,
        retrieval_run_ids=list_retrieval_run_ids(ledger, question_id=QUESTION),
    )
    assert {run.provider for run in packet.runs} == {"asknews", "exa"}
    assert len(packet.documents) == 2


# --- error hygiene -----------------------------------------------------------


def test_a_lone_surrogate_is_refused_as_a_store_error_without_echoing_it(
    ledger: sqlite3.Connection,
) -> None:
    """``sqlite3`` raises ``UnicodeEncodeError`` quoting the offending character.

    Two defects in one: an exception escaping a module that contracts to raise only
    its own type, and a message printing untrusted provider text. Lone surrogates
    reach a document field from any provider body, because ``json.loads`` accepts
    ``"\\ud800"`` and hands back a string containing it.
    """
    with pytest.raises(StoreError) as caught:
        persist_retrieval(ledger, _run(), [_document(0, title=f"{PLANTED}\ud800")])
    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert "\ud800" not in rendered
    assert PLANTED not in rendered


def test_a_duplicate_run_id_is_refused_as_a_store_error(ledger: sqlite3.Connection) -> None:
    """A database constraint failure arrives as this module's error type."""
    persist_retrieval(ledger, _run(), [])
    with pytest.raises(StoreError, match="ledger refused a research write"):
        persist_retrieval(ledger, _run(), [])


def test_a_corrupt_stored_timestamp_is_refused_without_echoing_it(
    ledger: sqlite3.Connection,
) -> None:
    """Values read back out of the ledger are untrusted (CLAUDE.md's threat boundary).

    Written around the writers on purpose: this is the shape a hand-edited or
    foreign-tool-written database presents, and ``fromisoformat`` raises a
    ``ValueError`` that quotes the offending string.
    """
    persist_retrieval(ledger, _run(), [])
    ledger.execute(
        "UPDATE research_runs SET freshness_cutoff_utc = ? WHERE retrieval_run_id = ?",
        (PLANTED, "run-1"),
    )
    with pytest.raises(StoreError) as caught:
        load_run(ledger, "run-1")
    assert PLANTED not in f"{caught.value}{caught.value!r}{caught.value.args}"


def test_reading_a_run_that_does_not_exist_is_a_store_error(
    ledger: sqlite3.Connection,
) -> None:
    with pytest.raises(StoreError, match="no research run"):
        load_run(ledger, "run-missing")


def test_a_connection_not_opened_by_ledger_connect_is_refused(tmp_path: Path) -> None:
    """``lifecycle.transaction`` refuses it; the refusal must arrive as a StoreError.

    ``transaction`` is a generator-based context manager, so its refusal fires on
    ``__enter__`` -- a ``try`` around the call would catch nothing and a
    ``LifecycleError`` would escape.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    raw = sqlite3.connect(db)
    try:
        with pytest.raises(StoreError, match="explicit-transaction mode"):
            persist_retrieval(raw, _run(), [])
    finally:
        raw.close()


def test_malformed_arguments_arrive_as_store_errors(ledger: sqlite3.Connection) -> None:
    for call in (
        lambda: persist_retrieval(ledger, "not a run", []),  # type: ignore[arg-type]
        lambda: persist_retrieval(ledger, _run(), ["not a document"]),  # type: ignore[list-item]
        lambda: load_run(ledger, 7),  # type: ignore[arg-type]
        lambda: load_documents(ledger, ""),
        lambda: load_packet(ledger, question_id="42", retrieval_run_ids=()),  # type: ignore[arg-type]
        lambda: load_packet(ledger, question_id=1, retrieval_run_ids="run-1"),
        lambda: load_packet(ledger, question_id=1, retrieval_run_ids=("a", "a")),
    ):
        with pytest.raises(StoreError):
            call()


# --- round-1 review regressions ----------------------------------------------
#
# One per blocking finding, each reproduced by execution against 837f333 before any
# fix was written and confirmed to fail there.


def test_a_negative_zero_cost_replays_to_the_same_hash(ledger: sqlite3.Connection) -> None:
    """Finding 1. ``-0.0`` satisfies `ge=0`, renders as `-0.0`, returns as `0.0`.

    An ordinary accepted input that falsified the acceptance criterion: the packet
    hashed one way in memory and another after a round trip through a REAL column.
    The fix canonicalizes at model validation so both sides agree; the assertion is
    the SQLite round trip, not the model, because the model was never the half that
    disagreed.
    """
    run = _run(cost_usd=-0.0)
    before = packet_sha256(build_packet(QUESTION, [run], []))
    persist_retrieval(ledger, run, [])
    after = load_packet(
        ledger,
        question_id=QUESTION,
        retrieval_run_ids=list_retrieval_run_ids(ledger, question_id=QUESTION),
    )
    assert packet_sha256(after) == before


def test_a_named_packet_is_unchanged_by_a_later_unrelated_run(
    ledger: sqlite3.Connection,
) -> None:
    """Finding 2. A packet keyed on "every row sharing a question" has no identity.

    Persisting a second run silently changed what the *first* packet was, and the
    earlier one became unreplayable. Naming the runs makes a stored hash a claim that
    can still be checked a year later.
    """
    persist_retrieval(ledger, _run(), [_document(0)])
    named = list_retrieval_run_ids(ledger, question_id=QUESTION)
    before = packet_sha256(load_packet(ledger, question_id=QUESTION, retrieval_run_ids=named))

    persist_retrieval(
        ledger,
        _run(retrieval_run_id="run-2", provider="exa"),
        [_document(1, retrieval_run_id="run-2")],
    )
    after = packet_sha256(load_packet(ledger, question_id=QUESTION, retrieval_run_ids=named))
    assert after == before


def test_an_open_run_is_not_replayable(ledger: sqlite3.Connection, tmp_path: Path) -> None:
    """Finding 2, second half. An open run is a spend record, not evidence.

    It has no documents yet and its own columns are still to be written, so replaying
    it reproduces a hash the finished run will not match.
    """
    open_run(ledger, _run(completed_at_utc=None))
    assert list_retrieval_run_ids(ledger, question_id=QUESTION) == ()
    config = _config(tmp_path, replay_saved_research=True)
    with pytest.raises(StoreError, match="has not completed"):
        replay_research(ledger, config, question_id=QUESTION, retrieval_run_ids=("run-1",))


def test_a_packet_is_read_from_one_snapshot(tmp_path: Path) -> None:
    """Finding 3. Three unsynchronized reads produced a state the ledger never held.

    A concurrent ``complete_run`` landing between the run read and the document read
    returned an *unfinished* run together with the documents it only has once
    finished. Two real connections and a real commit; no mocking, because the race is
    ordinary and reachable.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    reader, writer = connect(db), connect(db)
    try:
        opened = _run(completed_at_utc=None)
        open_run(reader, opened)
        finished = _run(cost_usd=0.5)
        with _read_snapshot(reader):
            runs = [load_run(reader, "run-1")]
            complete_run(writer, finished, [_document(0)])
            documents = list(load_documents(reader, "run-1"))
        # Either both halves are the before-state or both are the after-state; the
        # torn combination (unfinished run + its completed-run documents) is what
        # this forbids.
        assert (runs[0].completed_at_utc is None) == (documents == [])
    finally:
        reader.close()
        writer.close()


def test_completing_a_run_twice_is_refused(ledger: sqlite3.Connection) -> None:
    """Finding 4. The second completion rewrote a stored run's queries and cost.

    That moved an already-computed packet hash on a table whose whole point is that
    evidence is not re-identified after the fact.
    """
    open_run(ledger, _run(completed_at_utc=None))
    complete_run(ledger, _run(queries=["first"], cost_usd=1.0), [])
    with pytest.raises(StoreError, match="no open run matches this one"):
        complete_run(ledger, _run(queries=["second"], cost_usd=2.0), [])
    assert load_run(ledger, "run-1").queries == ["first"]


def test_completing_with_a_mismatched_identity_is_refused(
    ledger: sqlite3.Connection,
) -> None:
    """Finding 4. A caller id mix-up silently fused two runs.

    Completing `run-1` with a model carrying a different question, provider and start
    time kept the opened identity and took the other model's payload — a row that
    describes neither retrieval.
    """
    open_run(ledger, _run(completed_at_utc=None))
    other = _run(
        question_id=99,
        provider="exa",
        started_at_utc=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
        completed_at_utc=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
        queries=["third"],
    )
    with pytest.raises(StoreError, match="no open run matches this one"):
        complete_run(ledger, other, [])
    stored = load_run(ledger, "run-1")
    assert stored.question_id == QUESTION
    assert stored.completed_at_utc is None


def test_an_integer_too_wide_for_sqlite_is_refused(ledger: sqlite3.Connection) -> None:
    """Finding 5. `question_id` is schema-valid at any Python width.

    Binding one wider than 64 bits raised a raw `OverflowError` out of a module that
    contracts to raise only its own type.
    """
    with pytest.raises(StoreError, match="outside the range SQLite can store"):
        persist_retrieval(ledger, _run(question_id=2**63), [])


def test_an_undecodable_stored_column_is_refused_without_echoing_it(
    ledger: sqlite3.Connection,
) -> None:
    """Finding 5, and the leak in it.

    Decoding happens at **fetch**, so ending the protection at `conn.execute` left a
    raw `sqlite3.OperationalError` escaping — and its message quotes the stored
    bytes, so it printed the planted value verbatim. Written around the writers on
    purpose: this is ordinary foreign-tool-written or corrupted state.
    """
    persist_retrieval(ledger, _run(), [])
    ledger.execute(
        "UPDATE research_runs SET error_summary = CAST(? AS TEXT) WHERE retrieval_run_id = ?",
        (PLANTED.encode() + b"\xff\xfe", "run-1"),
    )
    with pytest.raises(StoreError) as caught:
        load_run(ledger, "run-1")
    assert PLANTED not in f"{caught.value}{caught.value!r}{caught.value.args}"


def test_a_malformed_queries_column_is_refused_not_rewritten(
    ledger: sqlite3.Connection,
) -> None:
    """Finding 6. `_load_json(...) or []` turned four malformed shapes into `[]`.

    A corrupt row was silently rewritten into a schema-valid run with no queries,
    which then hashed as a perfectly good packet. Reading is not the place to repair
    the ledger. Only SQL NULL may take the legacy default, and that case is asserted
    too so the fix is not a blanket refusal.

    The fix is two halves — dropping ``or []`` and adding ``_load_json(expect=...)``
    — and **each masks the other**, measured by reverting them one at a time: with
    only ``or []`` restored the shape gate refuses first, and with only the gate
    removed pydantic refuses the non-list. Reverting both together is what returns
    ``[]`` for all four inputs. Recorded because either half read on its own looks
    like dead defence and is not.
    """
    persist_retrieval(ledger, _run(queries=["real"]), [])
    for malformed in ("false", "0", '""', "{}", "[1]"):
        ledger.execute(
            "UPDATE research_runs SET queries_json = ? WHERE retrieval_run_id = ?",
            (malformed, "run-1"),
        )
        with pytest.raises(StoreError):
            load_run(ledger, "run-1")
    ledger.execute(
        "UPDATE research_runs SET queries_json = NULL WHERE retrieval_run_id = ?", ("run-1",)
    )
    assert load_run(ledger, "run-1").queries == []


def test_a_blob_in_a_text_column_is_refused_not_coerced(
    ledger: sqlite3.Connection,
) -> None:
    """Finding 6. A BLOB in a TEXT-affinity column came back as `bytes`.

    Pydantic then coerced it into a `str`, so corrupt state was returned as valid
    evidence rather than refused.
    """
    persist_retrieval(ledger, _run(), [])
    ledger.execute(
        "UPDATE research_runs SET error_summary = ? WHERE retrieval_run_id = ?",
        (b"\x01\x02\x03", "run-1"),
    )
    with pytest.raises(StoreError, match="not text"):
        load_run(ledger, "run-1")


def test_a_surrogate_pair_in_a_json_column_is_refused(ledger: sqlite3.Connection) -> None:
    """Found by the SQLite round-trip property, not by the review.

    ``queries`` and ``provider_config`` are stored as JSON with ``ensure_ascii=True``,
    and this module previously claimed that made them immune to the surrogate problem
    guarded on the TEXT columns. True of *lone* surrogates, false of **pairs**, which
    is the more dangerous half:

    - ``chr(0xD800) + chr(0xDC00)`` is two Python code points and is not UTF-8
      encodable;
    - ``json.dumps(ensure_ascii=True)`` writes it and ``json.loads`` **recombines it**
      into the single scalar ``U+10000``, so what comes back is a different string;
    - pydantic's ``model_dump(mode="json")`` renders the original as six ``U+FFFD``.

    So the in-memory packet hashed over replacement characters and the stored packet
    over the clean scalar. Refused rather than normalized, for the reason
    ``_require_storable_text`` gives.
    """
    pair = chr(0xD800) + chr(0xDC00)
    with pytest.raises(StoreError, match="lone surrogate"):
        persist_retrieval(ledger, _run(queries=[pair]), [])
    with pytest.raises(StoreError, match="lone surrogate"):
        persist_retrieval(ledger, _run(provider_config={pair: 1}), [])
    with pytest.raises(StoreError, match="lone surrogate"):
        persist_retrieval(ledger, _run(provider_config={"k": [{"nested": pair}]}), [])
    assert ledger.execute("SELECT count(*) FROM research_runs").fetchone()[0] == 0
