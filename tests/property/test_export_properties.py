"""Properties of the ledger export's value encoder and canonical form (M1-604).

The four invariants CLAUDE.md asks of any hash, tiebreak, canonicalizer or validator
before its first review. The unit under test is the seam between a stored SQLite value and
the bytes that leave the machine, so two of the four are checked twice: once over the
encoder directly, and once over a **real SQLite fetch**, because a simulated boundary
tests the simulation (M1-306, round 1).
"""

from __future__ import annotations

import itertools
import json
import math
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.export import (
    EXPORTED_TABLES,
    Column,
    ExportError,
    _decode_value,
    canonical_json,
    read_table,
    render_jsonl,
)
from whiskeyjack_bot.ledger import connect, connect_readonly, initialize_ledger

from strategies import (  # type: ignore[import-not-found]
    ENCODABLE_TEXT,
    HOSTILE_TEXT,
    SURROGATE_TEXT,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

TS = "2026-09-04T12:00:00.000000+00:00"
SHA = "a" * 64

DOCUMENTS = next(spec for spec in EXPORTED_TABLES if spec.name == "research_documents")

# A per-example serial rather than a hash of the drawn value. `research_documents` is
# append-only -- `INSERT OR REPLACE` is refused by the migration-003 trigger, correctly --
# so each example needs an identifier no earlier example used, and two examples drawing
# the same text must still get two rows. Same idiom as `test_packet_properties.py`.
_SERIAL = itertools.count()

# Every storage class `typeof()` can return, as the bytes a `text_factory = bytes`
# connection hands back. Deliberately includes 'blob', which the export refuses: a
# strategy that could only draw acceptable classes would make the refusal branch
# unreachable, which is this project's most-repeated defect.
STORAGE_CLASSES = st.sampled_from([b"null", b"integer", b"real", b"text", b"blob"])

DECLARED_TYPES = st.sampled_from(["TEXT", "INTEGER", "REAL"])

# The Python objects sqlite3 actually produces on a bytes-factory connection, plus the
# ones it never does -- both matter, because the encoder's job is to refuse rather than
# trust what it is handed.
STORED_VALUES = st.one_of(
    st.none(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=24),
    HOSTILE_TEXT,
)


# ---------------------------------------------------------------------------------------
# 1. Never raises outside the module's own error type.
# ---------------------------------------------------------------------------------------


@given(
    declared=DECLARED_TYPES,
    storage_class=STORAGE_CLASSES,
    value=STORED_VALUES,
    column_name=st.sampled_from(["title", "cost_usd", "question_id"]),
)
def test_the_encoder_never_raises_outside_its_own_error_type(
    declared: Any, storage_class: bytes, value: object, column_name: str
) -> None:
    """Values read back out of the ledger are untrusted, and every malformed shape must
    arrive as `ExportError`.

    A raw `UnicodeDecodeError`/`AttributeError`/`TypeError` escaping is a review finding in
    this project -- it has been, twice. The storage class and the value are drawn
    independently on purpose: the pairs SQLite would never produce together are exactly the
    ones a hand-written encoder gets wrong.
    """
    column = Column(column_name, declared)
    try:
        decoded = _decode_value("research_documents", column, storage_class, value)
    except ExportError:
        return
    assert decoded is None or isinstance(decoded, (str, int, float))
    if isinstance(decoded, float):
        assert math.isfinite(decoded)


@given(
    payload=st.recursive(
        st.none() | st.booleans() | st.integers() | HOSTILE_TEXT,
        lambda children: (
            st.lists(children, max_size=3)
            | st.dictionaries(st.text(max_size=8), children, max_size=3)
        ),
        max_leaves=8,
    )
)
def test_canonical_json_never_raises_outside_its_own_error_type(payload: object) -> None:
    """Including on the lone surrogates that reach the schema from provider JSON.

    `json.dumps(ensure_ascii=True)` escapes a lone surrogate rather than refusing it, so
    this does not exercise the open `content_sha256` defect -- see the module docstring in
    `tests/property/test_canonical_properties.py`. It does pin that the encoder cannot
    raise something a caller is not handling.
    """
    try:
        rendered = canonical_json(payload, "a test value")
    except ExportError:
        return
    assert isinstance(rendered, str)


# ---------------------------------------------------------------------------------------
# 2. A total order, where one is claimed.
# ---------------------------------------------------------------------------------------


@given(identifiers=st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=12, unique=True))
def test_the_exported_row_order_is_a_total_order_on_the_identifier(
    identifiers: list[str],
) -> None:
    """Byte-determinism rests on the ORDER BY, so the ordering must be total.

    SQLite's BINARY collation orders on the UTF-8 bytes, which is what `sorted()` on the
    encoded form reproduces; a Python-side `sorted()` on the `str` disagrees for anything
    above the BMP. Getting that backwards would make the export stable only for ASCII
    identifiers, which every fixture in the suite happens to use.
    """
    db_ordered = _sqlite_sorted(identifiers)
    assert db_ordered == sorted(identifiers, key=lambda value: value.encode("utf-8"))
    assert len(db_ordered) == len(identifiers)
    assert set(db_ordered) == set(identifiers)


def _sqlite_sorted(identifiers: list[str]) -> list[str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO t VALUES (?)", [(value,) for value in identifiers])
        return [row[0] for row in conn.execute('SELECT id FROM t ORDER BY "id"')]
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# 3. Replay stability across the persisted form.
# ---------------------------------------------------------------------------------------


@given(
    rows=st.lists(
        st.fixed_dictionaries(
            {
                "document_id": st.text(min_size=1, max_size=8),
                "title": st.none() | HOSTILE_TEXT,
                "content_sha256": st.sampled_from(["a" * 64, "b" * 64]),
            }
        ),
        max_size=6,
    )
)
def test_jsonl_replays_to_itself(rows: list[dict[str, object]]) -> None:
    """Parsing an export and re-rendering it reproduces the bytes exactly.

    This is what makes the export a record rather than a report: a consumer that reads it
    and writes it back has not changed it. It is also what catches an encoder that is
    stable only because every fixture happened to be ASCII -- `ensure_ascii=True` plus
    `sort_keys=True` is the settled form (M1-305), and both halves are load-bearing here.
    """
    first = render_jsonl(rows, "research_documents")
    replayed = [json.loads(line) for line in first.decode("utf-8").splitlines() if line]
    assert render_jsonl(replayed, "research_documents") == first


@given(title=ENCODABLE_TEXT)
@settings(max_examples=60, deadline=None)
def test_a_value_survives_a_real_sqlite_round_trip(
    ledger: sqlite3.Connection, ledger_path: Path, title: str
) -> None:
    """The same claim, over a real database rather than a dict.

    A pure-function property proves the encoder is consistent with itself; only the real
    fetch proves it is consistent with `sqlite3`, which decodes TEXT at fetch rather than
    at execute and applies column affinity on the way in.

    `ENCODABLE_TEXT` rather than `HOSTILE_TEXT`, and the difference is the point rather
    than a convenience: a lone surrogate cannot be *bound* as TEXT at all, so drawing one
    here filters the example out instead of testing anything.
    `test_a_lone_surrogate_cannot_be_stored_as_text_at_all` covers that boundary directly,
    which keeps the two claims separable instead of hiding one inside an `assume`.
    """
    document_id = f"doc-{next(_SERIAL)}"
    ledger.execute(
        "INSERT INTO research_documents (document_id, retrieval_run_id, "
        "canonical_url, retrieved_at_utc, content_sha256, original_url, provenance, "
        "source_type, title) VALUES (?, 'run-1', ?, ?, ?, ?, 'direct_api', 'news', ?)",
        (
            document_id,
            f"https://example.org/{document_id}",
            TS,
            SHA,
            f"https://example.org/{document_id}?utm=x",
            title,
        ),
    )

    reader = connect_readonly(ledger_path)
    try:
        reader.text_factory = bytes
        rows = {row["document_id"]: row for row in read_table(reader, DOCUMENTS)}
    finally:
        reader.close()

    assert rows[document_id]["title"] == title
    rendered = render_jsonl([rows[document_id]], "research_documents")
    assert json.loads(rendered.decode("utf-8"))["title"] == title


@given(title=SURROGATE_TEXT)
@settings(max_examples=25, deadline=None)
def test_a_lone_surrogate_cannot_be_stored_as_text_at_all(
    ledger: sqlite3.Connection, title: str
) -> None:
    """The write boundary refuses what the export would otherwise have to represent.

    Worth pinning rather than assuming, because it is what makes the encoder's
    invalid-UTF-8 branch a defence against a hand-edited database rather than against the
    pipeline's own writers -- and because the open `content_sha256` lone-surrogate defect
    (CLAUDE.md, awaiting an owner decision) is about exactly these strings. This item does
    not close that; it declines to add a second way for one to reach a file.
    """
    document_id = f"doc-{next(_SERIAL)}"
    with pytest.raises((sqlite3.Error, UnicodeEncodeError, ValueError)):
        ledger.execute(
            "INSERT INTO research_documents (document_id, retrieval_run_id, "
            "canonical_url, retrieved_at_utc, content_sha256, original_url, provenance, "
            "source_type, title) VALUES (?, 'run-1', ?, ?, ?, ?, 'direct_api', 'news', ?)",
            (
                document_id,
                f"https://example.org/{document_id}",
                TS,
                SHA,
                f"https://example.org/{document_id}?utm=x",
                title,
            ),
        )


# ---------------------------------------------------------------------------------------
# 4. No value leak in any message.
# ---------------------------------------------------------------------------------------


# A marker no fixed message contains. Searching the message for the drawn value itself
# gives false positives on short draws -- b"_" is inside "research_documents", measured --
# and the first version filtered those out with a length floor on `str` draws only, so a
# one-byte `bytes` draw failed the property under the randomized profile while the
# derandomized gate never drew it. Every text or bytes value carries this marker instead,
# and the marker is what is searched for.
LEAK_CANARY = "zQ9canary9Qz"

CANARY_TEXT = st.builds(lambda a, b: f"{a}{LEAK_CANARY}{b}", HOSTILE_TEXT, HOSTILE_TEXT)
CANARY_BYTES = st.builds(
    lambda a, b: a + LEAK_CANARY.encode() + b, st.binary(max_size=12), st.binary(max_size=12)
)

LEAK_VALUES = st.one_of(
    st.none(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=True, allow_infinity=True),
    CANARY_BYTES,
    CANARY_TEXT,
)


@given(
    declared=DECLARED_TYPES,
    storage_class=STORAGE_CLASSES,
    value=LEAK_VALUES,
)
# Pinned rather than left to the draw, because each refusal carrying a value is reached by
# only a few percent of random draws: invalid UTF-8 (the one arm that raises *inside* an
# `except`, so it is the one `from None` is about), text that did not arrive as bytes, a
# declared/stored mismatch, and a non-finite REAL.
@example(declared="TEXT", storage_class=b"text", value=b"\xff" + LEAK_CANARY.encode())
@example(declared="TEXT", storage_class=b"text", value=LEAK_CANARY)
@example(declared="TEXT", storage_class=b"integer", value=LEAK_CANARY.encode())
@example(declared="INTEGER", storage_class=b"integer", value=LEAK_CANARY)
@example(declared="REAL", storage_class=b"real", value=float("inf"))
def test_a_refusal_never_echoes_the_value_it_refused(
    declared: Any, storage_class: bytes, value: object
) -> None:
    """The unconditional no-echo rule, over every shape that can be refused.

    An export is a file that leaves the machine and its errors reach the operator's
    terminal, so a stored value that turned out to be a pasted credential must not travel
    through either. The column name is schema and is named deliberately -- without it a
    refusal is unactionable.
    """
    column = Column("title", declared)
    try:
        _decode_value("research_documents", column, storage_class, value)
    except ExportError as exc:
        rendered = f"{exc}{exc.__cause__}{exc.__context__}"
        assert "research_documents" in rendered
        assert "title" in rendered
        # from None everywhere, so a rendered traceback cannot reprint the value. Checking
        # `__cause__` alone is not enough: an implicitly chained raise also leaves it None,
        # and only `__suppress_context__` tells the two apart.
        assert exc.__cause__ is None
        assert exc.__context__ is None or exc.__suppress_context__
        if isinstance(value, (str, bytes)):
            assert LEAK_CANARY not in rendered
        if isinstance(value, float) and not math.isfinite(value):
            assert "inf" not in rendered.lower().replace("finite", "")
            assert "nan" not in rendered.lower()


# ---------------------------------------------------------------------------------------
# Fixtures. One ledger per module, because building one is the expensive part.
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ledger_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A module-scoped ledger, rewritten per example rather than rebuilt.

    Never a function-scoped `tmp_path` under `@given`: hypothesis reuses the same fixture
    across every example of one test, so a per-function path would be a health-check
    failure and a lie about isolation at once (the idiom in
    `tests/property/test_allowlist_properties.py`).
    """
    db = tmp_path_factory.mktemp("export-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES ('run-1', 'asknews', 100, ?, ?)",
            (TS, TS),
        )
    finally:
        conn.close()
    return db


@pytest.fixture(scope="module")
def ledger(ledger_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(ledger_path)
    try:
        yield conn
    finally:
        conn.close()
