"""Central redaction of the tournament journal (M1-613).

`tournament_state.append` stores `canonical(journal_form(data))`, where every string leaf --
keys included -- is redacted of the process's configured secrets. The registry is filled by
`config.load_config`; each test here gives it a private, empty set so no other test's
profile leaks names in.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from whiskeyjack_bot import redaction
from whiskeyjack_bot.config import load_config
from whiskeyjack_bot.export import export_ledger
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.tournament_state import (
    StorageFailure,
    append,
    check_storage,
    events,
    witness,
)

ROOT = Path(__file__).resolve().parents[2]

# Low-entropy on purpose: CI runs gitleaks over full history (see T-901).
FAKE_SECRET = "privateFAKE123456"
VARIABLE = "FAKE_WJ_JOURNAL_TOKEN"


@pytest.fixture(autouse=True)
def private_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redaction, "_REGISTERED", set())


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn, db
    finally:
        conn.close()


def _stored(conn: sqlite3.Connection, identifier: str) -> str:
    return str(
        conn.execute(
            "SELECT data FROM tournament_events WHERE event_id = ?", (identifier,)
        ).fetchone()[0]
    )


def test_a_secret_planted_through_append_is_absent_from_the_row_and_the_export(
    ledger: tuple[sqlite3.Connection, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion, both halves. The secret sits in a value, a nested list and
    a *key*, and is confirmed present in the input before it is asserted absent."""
    conn, db = ledger
    monkeypatch.setenv(VARIABLE, FAKE_SECRET)
    redaction.register_secret_env_var_names([VARIABLE])
    data = {
        "text": f"comment echoing Token {FAKE_SECRET}",
        "nested": [{"deep": FAKE_SECRET}],
        FAKE_SECRET: 1,
    }
    assert FAKE_SECRET in json.dumps(data)
    identifier = append(conn, "comment_intent", "rec-1", data)

    stored = _stored(conn, identifier)
    assert FAKE_SECRET not in stored
    marker = f"<redacted:{VARIABLE}>"
    parsed = json.loads(stored)
    assert parsed["text"] == f"comment echoing Token {marker}"
    assert parsed["nested"] == [{"deep": marker}]
    assert parsed[marker] == 1

    export_ledger(db, tmp_path / "out", export_format="jsonl")
    exported = b"".join(p.read_bytes() for p in sorted((tmp_path / "out").iterdir()))
    assert FAKE_SECRET.encode() not in exported
    assert marker.encode() in exported


def test_a_secret_that_is_also_a_number_does_not_break_the_json(
    ledger: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the redaction walks leaves rather than the rendered text.

    An all-digit secret that also occurs in a number -- here the account ID -- substituted
    into the rendered JSON would put `<redacted:...>` in the middle of a number, the row
    would fail `CHECK(json_valid(data))`, and the worker would stop on a storage failure.
    """
    conn, _db = ledger
    monkeypatch.setenv(VARIABLE, "305299")
    redaction.register_secret_env_var_names([VARIABLE])
    identifier = append(conn, "activation", "account", {"account_id": 305299, "note": "id 305299"})
    parsed = json.loads(_stored(conn, identifier))
    assert parsed == {"account_id": 305299, "note": f"id <redacted:{VARIABLE}>"}


def test_a_witness_and_its_row_stay_equal_so_no_restore_is_detected(
    ledger: tuple[sqlite3.Connection, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check_storage` requires the witness file's `data` to equal the row's. Redacting one
    and not the other would read as a restored ledger and stop the worker."""
    conn, _db = ledger
    monkeypatch.setenv(VARIABLE, FAKE_SECRET)
    redaction.register_secret_env_var_names([VARIABLE])
    root = tmp_path / "artifacts"
    identifier = witness(conn, root, "42:33122", {"request": {"auth": f"Bearer {FAKE_SECRET}"}})
    check_storage(conn, root)
    for path in (root / "operations").glob("*.json"):
        assert FAKE_SECRET not in path.read_text()
    assert FAKE_SECRET not in _stored(conn, identifier)


def test_a_restored_ledger_is_still_detected(
    ledger: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    """The control for the test above: the equality check still has teeth."""
    conn, _db = ledger
    root = tmp_path / "artifacts"
    identifier = witness(conn, root, "42:33122", {"request": "original"})
    path = root / "operations" / f"{identifier}.json"
    envelope = json.loads(path.read_text())
    envelope["data"] = {"request": "tampered"}
    path.write_text(json.dumps(envelope))
    with pytest.raises(StorageFailure):
        check_storage(conn, root)


def test_loading_a_profile_registers_its_secret_names() -> None:
    config = load_config(ROOT / "config" / "tournament.yaml")
    registered = set(redaction.registered_secret_env_var_names())
    assert set(config.secret_env_var_names()) <= registered
    assert config.metaculus.token_env in registered


def test_with_nothing_registered_append_stores_the_data_unchanged(
    ledger: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that loaded no profile has no configured secrets; that is not an error."""
    conn, _db = ledger
    monkeypatch.setenv(VARIABLE, FAKE_SECRET)
    identifier = append(conn, "heartbeat", "worker", {"text": FAKE_SECRET})
    assert events(conn, "heartbeat", "worker")[-1] == {"text": FAKE_SECRET}
    assert identifier


def test_deeply_nested_data_the_base_accepted_is_still_accepted(
    ledger: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1's blocker. `append`'s contract is `dict[str, Any]` with no nesting limit, and
    the base persisted this payload: the depth it accepts is set by `json.dumps`' C encoder.

    The first draft of `redact_leaves` recursed, spending two interpreter frames per level,
    so it exhausted Python's recursion limit at roughly *half* that depth and turned payloads
    the base had stored into a raw `RecursionError`. Measured on both trees, the first
    failing depth is now identical (993 at the default recursion limit), so this asserts well
    inside that with a secret at the bottom to prove the walk still reaches it.
    """
    conn, _db = ledger
    monkeypatch.setenv(VARIABLE, FAKE_SECRET)
    redaction.register_secret_env_var_names([VARIABLE])

    data: dict[str, object] = {"text": f"Bearer {FAKE_SECRET}"}
    for _ in range(900):
        data = {"nested": data}

    identifier = append(conn, "model_response", "deep", data)
    stored = _stored(conn, identifier)
    assert FAKE_SECRET not in stored

    walked = json.loads(stored)
    for _ in range(900):
        walked = walked["nested"]
    assert walked == {"text": f"Bearer <redacted:{VARIABLE}>"}


def test_a_value_repeated_as_two_siblings_is_not_mistaken_for_a_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The iterative walk tracks only the containers on the *current path*. Tracking every
    container it had ever seen would refuse this, which is ordinary data, not a cycle."""
    monkeypatch.setenv(VARIABLE, FAKE_SECRET)
    shared = {"token": FAKE_SECRET}
    out = redaction.redact_leaves({"a": shared, "b": shared}, [VARIABLE])
    marker = f"<redacted:{VARIABLE}>"
    assert out == {"a": {"token": marker}, "b": {"token": marker}}


def test_a_true_cycle_fails_exactly_as_json_dumps_already_did() -> None:
    """Not a new refusal: `canonical` raised this before the item existed, and `append` did
    not sanitize it then either. Pinned so the iterative walk cannot spin forever instead."""
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValueError, match="Circular reference detected"):
        redaction.redact_leaves(cyclic, [VARIABLE])
    with pytest.raises(ValueError, match="Circular reference detected"):
        json.dumps(cyclic)
