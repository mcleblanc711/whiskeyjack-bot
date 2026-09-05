"""T-902's headline criterion: *the uncertain-outcome path blocks a duplicate submit.*

``CODEX_HANDOFF.md:328`` -- "uncertain timeout where posting may have succeeded: block retry
until refetch resolves state" -- and acceptance test 4 at ``:335``: *"Re-running the same
submission command with the same idempotency key makes at most one live post."*

**Why this is not a second copy of an existing test.**
``tests/unit/test_submission_live.py:630`` covers the gate and covers it well, but it hands
the two calls **two different ``FakePoster`` instances** and asserts the second one's counter
is zero. That is "the second poster was not asked", which is a weaker claim than the
criterion makes: the criterion is about *one platform* seeing *one post* across a whole
operator session. ``tests/unit/test_cli_submit.py`` drives ``submit`` and then
``verify-submission``, but never a second ``submit`` -- so nothing in the repository
re-issues the command that the acceptance test is literally about.

Here one ``CountingTransport`` spans every command in the session, and the number asserted at
the end is the number of POSTs the platform received. The commands are driven through
``main(argv)``, so the chain is the operator's: ``submit`` -> ``build_poster`` ->
``build_client`` -> a real ``SingleAttemptPoster`` -> ``requests.post``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests
from fake_platform import (
    CONFIRMING_HISTORY_ENTRIES,
    CountingTransport,
    api_response,
    install_transport,
    post_with_forecast_history,
)

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.ledger import connect
from whiskeyjack_bot.lifecycle import current_status

EMPTY_HISTORY = post_with_forecast_history([])
CONFIRMING_HISTORY = post_with_forecast_history(CONFIRMING_HISTORY_ENTRIES)


def _submit(config_file: Path, record_id: str) -> int:
    """The operator's command. No ``--payload-file``: the payload derives from the record."""
    return main(["submit", "--config", str(config_file), "--record-id", record_id])


def _verify(config_file: Path, record_id: str, attempt_id: str) -> int:
    return main(
        [
            "verify-submission",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--attempt-id",
            attempt_id,
        ]
    )


def _attempt_ids(database: Path) -> list[str]:
    conn = connect(database)
    try:
        return [row[0] for row in conn.execute("SELECT attempt_id FROM submission_attempts")]
    finally:
        conn.close()


def _status(database: Path, record_id: str) -> str:
    conn = connect(database)
    try:
        return str(current_status(conn, record_id))
    finally:
        conn.close()


@pytest.fixture()
def session(
    approved_record: tuple[sqlite3.Connection, str], live_config_file: Path
) -> tuple[Path, Path, str]:
    """An approved record on disk, with this test's own connection closed.

    ``submit`` opens the ledger itself, and ``post_approved_forecast`` refuses to run inside
    a caller's open transaction. Closing here keeps the test out of the command's way.
    """
    conn, record_id = approved_record
    database = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    conn.close()
    return live_config_file, database, record_id


def test_re_running_submit_after_an_uncertain_outcome_makes_no_second_post(
    session: tuple[Path, Path, str],
    metaculus_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The criterion, with one counter across both commands.

    The first ``submit`` times out on the POST and cannot read the platform back, which is
    exactly the state the criterion names -- *posting may have succeeded*. The second
    ``submit`` is the blind retry the ledger exists to prevent, and the number that decides
    whether it was prevented is the **transport's**, not the gateway's.

    ``EXIT_REFUSED`` matters as much as the count: a command that silently did nothing and
    reported success would also leave ``posts == 1``.
    """
    config_file, database, record_id = session
    transport = CountingTransport(
        post_outcomes=[requests.exceptions.ReadTimeout("the post timed out")],
        get_outcomes=[
            api_response(200, EMPTY_HISTORY),
            requests.exceptions.ConnectionError("the refetch could not be performed"),
        ],
    )
    install_transport(monkeypatch, transport)

    assert _submit(config_file, record_id) == 0
    posts_after_first = transport.posts
    assert posts_after_first == 1, "the SDK's blind retry would make this four"
    assert _status(database, record_id) == "approved", "uncertain, not terminal"

    assert _submit(config_file, record_id) == EXIT_REFUSED
    assert transport.posts == 1, "the second command posted again"
    assert len(_attempt_ids(database)) == 1, "and it wrote no second attempt"


def test_the_block_is_the_uncertainty_and_it_names_itself(
    session: tuple[Path, Path, str],
    metaculus_token: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Which gate refused, not merely that one did.

    Six gates sit in front of the post and every one of them returns ``EXIT_REFUSED``. A test
    that asserted only the exit code would pass if the record were refused for a bad payload,
    a spent key, or a closed question -- none of which is the criterion. The refusal message
    is the only thing that distinguishes them, and it is the operator's evidence too.
    """
    config_file, _database, record_id = session
    transport = CountingTransport(
        post_outcomes=[requests.exceptions.ReadTimeout("the post timed out")],
        get_outcomes=[
            api_response(200, EMPTY_HISTORY),
            requests.exceptions.ConnectionError("the refetch could not be performed"),
        ],
    )
    install_transport(monkeypatch, transport)

    assert _submit(config_file, record_id) == 0
    capsys.readouterr()

    assert _submit(config_file, record_id) == EXIT_REFUSED
    refusal = capsys.readouterr().out
    assert "blind retry" in refusal
    assert "verify-submission" in refusal, "a dead end is not an acceptable state"


def test_a_resolved_uncertainty_is_refused_for_status_not_for_uncertainty(
    session: tuple[Path, Path, str],
    metaculus_token: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What "block retry **until** refetch resolves state" actually resolves to.

    Worth stating plainly, because the obvious reading is wrong and this suite was drafted
    against the wrong one. "Until" suggests the block lifts and a retry becomes permissible.
    It never does: ``verify_uncertain_attempt`` records ``confirmed -> submitted`` or
    ``absent -> failed``, and **both are terminal**, so the next ``submit`` is refused by
    ``submission_key_for_approved_record`` because the record is no longer ``approved``. A
    genuine retry is a new forecast version behind a fresh human approval.

    That is why this test is here rather than a fourth ``posts == 1`` assertion: it is what
    keeps the two above from being vacuous. A gateway that simply refused *every* second
    submit forever would pass both of them; only the changed refusal reason shows the
    uncertainty gate opened and a different gate closed.
    """
    config_file, database, record_id = session
    transport = CountingTransport(
        post_outcomes=[requests.exceptions.ReadTimeout("the post timed out")],
        get_outcomes=[
            api_response(200, EMPTY_HISTORY),
            api_response(200, CONFIRMING_HISTORY),
        ],
    )
    install_transport(monkeypatch, transport)

    assert _submit(config_file, record_id) == 0
    assert _status(database, record_id) == "approved"
    capsys.readouterr()

    (attempt_id,) = _attempt_ids(database)
    assert _verify(config_file, record_id, attempt_id) == 0
    assert _status(database, record_id) == "submitted"
    posts_after_verification = transport.posts
    assert posts_after_verification == 1, "verify-submission must never post"

    assert _submit(config_file, record_id) == EXIT_REFUSED
    refusal = capsys.readouterr().out
    assert transport.posts == 1
    assert "blind retry" not in refusal, "the uncertainty gate is open; a different one shut"
    assert "no longer awaiting submission" in refusal, (
        "the status gate is the one that must refuse now; a bare exit code cannot say which "
        "of the six did, and 'approved' alone appears in the uncertainty message too"
    )
