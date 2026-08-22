"""Properties of the paid-run composition (M1-312).

Two invariants, both of them the acceptance criterion rather than a restatement of the
primitives' own properties (those live in ``test_packet_properties.py``):

1. **The run is recorded whatever becomes of its artifact.** Over generated runs and
   documents, crossed with the three things that can happen to the artifact -- written,
   lost to a real I/O failure, or never attempted because retention is off -- the row and
   its documents are always in the ledger afterwards, ``raw_response_path`` is NULL in
   exactly the two cases where no file was written, and nothing raises.

2. **An audit loss does not move evidence identity.** The packet hash of the run and its
   documents is the same whether the artifact was written or lost. ``packet.py`` excludes
   ``raw_response_path`` from the digest on purpose (``_EXCLUDED_RUN_FIELDS``); this is the
   composition-level consequence, and it is what makes a lost artifact an *audit* loss and
   not a change to what was retrieved.

The artifact failure is produced by pre-creating the destination file -- artifacts are
never overwritten -- so the failure arm exercises the same code an unlucky operator would,
with no patching.

Every property here was re-run against a deliberately broken ``persist.py`` to confirm it
fails; see ``docs/M1-NOTES.md``. Three of M1-303's ten new properties passed against
broken code, which is why that step is written down rather than assumed.
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime, timezone
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.config import AppConfig, load_config
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.artifacts import artifact_relative_path
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
from whiskeyjack_bot.research.packet import ResearchPacket, build_packet, packet_sha256
from whiskeyjack_bot.research.persist import persist_paid_run
from whiskeyjack_bot.research.store import load_packet

from strategies import ENCODABLE_TEXT, research_documents, research_runs

QUESTION = 42
WHEN = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
BODIES: list[dict[str, Any]] = [{"articles": [{"title": "one"}]}]

# Each example writes its own run id into one shared ledger: `@given` with a
# function-scoped `tmp_path` is a hypothesis health-check failure, and building a schema
# per example would dominate the runtime (test_packet_properties.py makes the same call).
_IDS = itertools.count()


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("persist-properties")


@pytest.fixture(scope="module")
def ledger(workspace: Path) -> Iterator[sqlite3.Connection]:
    db = workspace / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _config(workspace: Path, *, retain: bool) -> AppConfig:
    data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openai/gpt-4o"
    data["retrieval"]["social"]["agent_model"] = "grok-3"
    data["storage"]["artifact_root"] = str(workspace / "artifacts")
    data["retrieval"]["retain_raw_responses"] = retain
    path = workspace / f"config-{retain}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


@pytest.fixture(scope="module")
def configs(workspace: Path) -> dict[bool, AppConfig]:
    return {retain: _config(workspace, retain=retain) for retain in (True, False)}


def _rekey(
    run: ResearchRun, documents: list[ResearchDocument], run_id: str
) -> tuple[ResearchRun, list[ResearchDocument]]:
    """Put the drawn run and documents on this example's own identity.

    ``model_copy``, deliberately, and never ``model_dump(mode="json")`` ->
    ``validate_run``: the dump-and-revalidate spelling round-trips every timestamp through
    ISO-8601 and **drops ``datetime.fold``**, which would launder away exactly the
    distinction the persisted-form rules exist to catch (M1-305 round 3, and the same note
    in ``test_packet_properties.py``).

    ``completed_at_utc`` and ``raw_response_path`` are set here rather than filtered for:
    the strategy generates both freely because the *packet* properties need them to vary,
    and this composition refuses a run that has not completed or that already claims a
    path. Filtering would throw most draws away.
    """
    fixed = run.model_copy(
        update={
            "retrieval_run_id": run_id,
            "question_id": QUESTION,
            "completed_at_utc": run.started_at_utc,
            "raw_response_path": None,
        }
    )
    attached: list[ResearchDocument] = []
    seen: set[tuple[str, str, str]] = set()
    for document in documents:
        rekeyed = document.model_copy(update={"retrieval_run_id": run_id})
        # Duplicates on the ledger's UNIQUE key collapse to one row, so a set that holds
        # two would make the stored packet differ from the in-memory one for a reason
        # that has nothing to do with the artifact.
        key = (rekeyed.retrieval_run_id, rekeyed.canonical_url, rekeyed.content_sha256)
        if key in seen:
            continue
        seen.add(key)
        attached.append(rekeyed)
    return fixed, attached


def _block(root: Path, run_id: str) -> Path:
    """Make the artifact write fail the way a re-used run id really does."""
    destination = root / artifact_relative_path(question_id=QUESTION, retrieval_run_id=run_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("{}", encoding="utf-8")
    return destination


@given(
    run=research_runs(text=ENCODABLE_TEXT),
    documents=st.lists(research_documents(text=ENCODABLE_TEXT), max_size=3),
    retain=st.booleans(),
    blocked=st.booleans(),
)
@settings(max_examples=60, deadline=None)
def test_the_paid_run_is_recorded_whatever_becomes_of_its_artifact(
    ledger: sqlite3.Connection,
    workspace: Path,
    configs: dict[bool, AppConfig],
    run: ResearchRun,
    documents: list[ResearchDocument],
    retain: bool,
    blocked: bool,
) -> None:
    run_id = f"prop-{next(_IDS)}"
    fixed, attached = _rekey(run, documents, run_id)
    if blocked and retain:
        _block(workspace / "artifacts", run_id)

    result = persist_paid_run(
        ledger, configs[retain], fixed, attached, raw_responses=BODIES, written_at_utc=WHEN
    )

    stored = ledger.execute(
        "SELECT raw_response_path FROM research_runs WHERE retrieval_run_id = ?", (run_id,)
    ).fetchone()
    assert stored is not None, "a paid run went unrecorded"
    assert stored["raw_response_path"] == result.raw_response_path
    # The path is recorded in exactly the case where a file was written, and in no other.
    assert (result.raw_response_path is not None) == (result.artifact_outcome == "written")
    assert (result.artifact_error is not None) == (result.artifact_outcome == "failed")
    if not retain:
        assert result.artifact_outcome == "retention_disabled"
    elif blocked:
        assert result.artifact_outcome == "failed"
    else:
        assert result.artifact_outcome == "written"
    documents_stored = ledger.execute(
        "SELECT COUNT(*) AS n FROM research_documents WHERE retrieval_run_id = ?", (run_id,)
    ).fetchone()
    assert documents_stored["n"] == len(attached)
    assert len(result.document_ids) == len(attached)


@given(
    run=research_runs(text=ENCODABLE_TEXT),
    documents=st.lists(research_documents(text=ENCODABLE_TEXT), max_size=3),
)
@settings(max_examples=60, deadline=None)
def test_losing_the_artifact_does_not_move_the_packet_hash(
    ledger: sqlite3.Connection,
    workspace: Path,
    configs: dict[bool, AppConfig],
    run: ResearchRun,
    documents: list[ResearchDocument],
) -> None:
    """The same evidence hashes the same whether its raw copy survived or not."""
    written_id = f"prop-{next(_IDS)}"
    lost_id = f"prop-{next(_IDS)}"
    kept, kept_documents = _rekey(run, documents, written_id)
    lost, lost_documents = _rekey(run, documents, lost_id)
    _block(workspace / "artifacts", lost_id)

    kept_result = persist_paid_run(
        ledger, configs[True], kept, kept_documents, raw_responses=BODIES, written_at_utc=WHEN
    )
    lost_result = persist_paid_run(
        ledger, configs[True], lost, lost_documents, raw_responses=BODIES, written_at_utc=WHEN
    )
    assert kept_result.artifact_outcome == "written"
    assert lost_result.artifact_outcome == "failed"

    # Hashed off the stored rows, not the in-memory models: the run id is the one field
    # that must differ between the two, so each packet is re-keyed onto a shared id
    # before hashing -- otherwise the property would only be asserting that two different
    # runs hash differently.
    kept_packet = load_packet(ledger, question_id=QUESTION, retrieval_run_ids=[written_id])
    lost_packet = load_packet(ledger, question_id=QUESTION, retrieval_run_ids=[lost_id])
    assert packet_sha256(_normalized(kept_packet)) == packet_sha256(_normalized(lost_packet))


def _normalized(packet: ResearchPacket) -> ResearchPacket:
    """Re-key a stored packet onto one shared run id, so only storage facts differ.

    Two runs cannot share a run id in the ledger, and the id *is* hashed, so without this
    the property would be asserting nothing more than that two different runs differ.
    ``document_id`` is left as stored: the packet hash already excludes it.
    """
    runs = [run.model_copy(update={"retrieval_run_id": "shared"}) for run in packet.runs]
    documents = [
        document.model_copy(update={"retrieval_run_id": "shared"}) for document in packet.documents
    ]
    return build_packet(QUESTION, runs, documents)
