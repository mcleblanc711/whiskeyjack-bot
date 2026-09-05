"""M2-705: is the refetch receipt a sufficient record of what the platform accepted?

**This module is the evidence for a decision, not the test suite for a feature.** Every
claim `docs/M2-NOTES.md`'s M2-705 section makes about what the receipt can and cannot
establish is asserted here by execution, so a reader of that section can check it rather
than take it, and so a future SDK that changes any of those behaviours turns the section
red instead of quietly false.

`D28`'s revisit trigger is the question, in the decision log's own words: *"Refetch cannot
establish submission outcome."* The tests below are that trigger, made falsifiable.

**The depth is the point** (T-902's lesson: fake at the depth that makes the claim a
measurement). Every test drives a real `MetaculusClient` and a real `SingleAttemptPoster`
over `fake_platform.CountingTransport`, into a real ledger, and then reads the
`submission_attempts` row back with SQL. A claim about what the ledger holds is therefore
a claim about bytes in a database, taken from outside the code under test -- not a property
of a hand-written double. `tests/unit/test_submission_live.py` already drives every refetch
outcome against its own `FakePoster`; those tests are cited in the notes rather than
repeated here, and what this module adds is the two things a double structurally cannot
show: what the SDK does with a real HTTP response, and what the ledger row looks like
afterwards.

The eight-cell totality test at the bottom is the exception to that depth, deliberately:
it drives `lifecycle.record_submission_attempt` directly, because the claim it makes is
about the *partition* being total and landing where the writer's docstring says, and
reaching all eight cells through the gateway would mean scripting eight transports to
prove something about a table.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fake_platform import (
    CountingTransport,
    OCCURRED,
    api_response,
    binary_values,
    build_real_poster,
    forecast_entry,
    install_transport,
    post_with_forecast_history,
)

from tests.unit.test_submission_live import BINARY_PAYLOAD, PROBABILITY

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.lifecycle import (
    FailureCode,
    LifecycleEventType,
    RefetchOutcome,
    SubmissionAttempt,
    current_status,
    record_submission_attempt,
)
from whiskeyjack_bot.submission_live import (
    LiveSubmissionError,
    LiveSubmissionRecord,
    post_approved_forecast,
    verify_uncertain_attempt,
)

BASELINE_START = 1_000_000.0
NEW_START = 1_000_100.0

EMPTY_HISTORY = post_with_forecast_history([])
"""The platform before the post: readable, and holding no forecast of ours."""

CONFIRMING_HISTORY = post_with_forecast_history(
    [forecast_entry(NEW_START, binary_values(PROBABILITY))]
)
"""The platform after an honest post: a newer entry whose values are what was sent."""

MARKER = "PLATFORM_RESPONSE_MARKER_M2_705"
"""A string that exists nowhere but in the platform's HTTP response body.

Every assertion about "the response reached the ledger" or "it did not" is made by looking
for this, across **every** column of the row rather than the three the receipt names. A
test that checked only ``response_body IS NULL`` would pass against an implementation that
stashed the body in ``error_message``, which is exactly the kind of near-miss this module
exists to rule out.
"""

MARKER_BODY = f'{{"detail": "{MARKER}", "id": 8675309}}'.encode()
MARKER_HEADERS = {"Retry-After": "30", "X-Marker": MARKER}


def _post(
    conn: sqlite3.Connection,
    record_id: str,
    config: AppConfig,
    transport: CountingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> LiveSubmissionRecord:
    """One live post through the whole real chain. Mirrors ``test_submission_integration``."""
    install_transport(monkeypatch, transport)
    return post_approved_forecast(
        conn,
        record_id=record_id,
        payload=BINARY_PAYLOAD,
        poster=build_real_poster(transport),
        config=config,
        occurred_at=OCCURRED,
        clock=lambda: OCCURRED,
        sleep=lambda _seconds: None,
    )


def _attempt_row(conn: sqlite3.Connection) -> dict[str, Any]:
    """The whole of the single ``submission_attempts`` row, by column name.

    Every column, not a chosen subset: the marker assertions below are about the row and
    not about the three fields the receipt happens to name.
    """
    cursor = conn.execute("SELECT * FROM submission_attempts")
    rows = cursor.fetchall()
    assert len(rows) == 1, "these tests each make exactly one attempt"
    names = [column[0] for column in cursor.description]
    return dict(zip(names, rows[0], strict=True))


# ── what a successful post leaves behind, and what it does not ───────────────


def test_a_successful_post_leaves_no_platform_response_in_the_ledger(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The gap M2-705 exists to price, measured rather than argued.**

    The platform answers the POST with a real body and real headers, and none of it
    survives anywhere. `MetaculusClient._post_question_prediction` is annotated `-> None`
    and binds the `requests.Response` to a local it never reads past
    `raise_for_status_with_additional_info`, so the receipt's `http_status`,
    `response_body` and `response_headers` are the three fields `CODEX_HANDOFF.md`'s
    submission seam names that a *successful* post can never fill.

    This is a claim about the pinned SDK, so it is written to fail if the SDK stops being
    that way: a future version that returned the response, or a gateway that started
    capturing it, turns this red and sends someone back to this section of the notes.

    Its counterpart immediately below is the same marker on a failure, and the pair is the
    asymmetry the whole spike turns on -- evidence on failure, none on success.
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[api_response(200, MARKER_BODY, MARKER_HEADERS)],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, CONFIRMING_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert transport.posts == 1
    assert recorded.event.event_type == "submitted", "an honest, refetch-confirmed post"
    assert recorded.receipt.http_status is None
    assert recorded.receipt.response_body is None
    assert recorded.receipt.response_headers is None

    row = _attempt_row(conn)
    assert row["http_status"] is None
    assert row["response_body"] is None
    assert row["response_headers"] is None
    # The whole row, not the three columns above: the body did not land somewhere else.
    assert not any(MARKER in str(value) for value in row.values()), (
        "no column of the attempt row carries anything the platform said"
    )


def test_a_failed_post_does_leave_the_platform_response_in_the_ledger(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the asymmetry, with the identical marker.

    On failure the SDK's `raise_for_status_with_additional_info` re-raises a *new*
    `HTTPError` chained `from` the original, and the original still carries the
    `requests.Response` -- so `submission_live.http_details` recovers the status, an
    allowlisted header subset and a truncated body through public attributes only.

    So the ledger is not uniformly blind to the platform: it is blind on exactly the path
    where a post succeeded. Naming which half is missing is what makes the decision in the
    notes a decision rather than a preference.
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[api_response(429, MARKER_BODY, MARKER_HEADERS)],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, EMPTY_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert transport.posts == 1, "a failed POST is still posted exactly once"
    assert recorded.receipt.success is False
    assert recorded.receipt.error_type == "http_error"

    row = _attempt_row(conn)
    assert row["http_status"] == 429
    assert row["response_body"] is not None and MARKER in row["response_body"]
    assert row["response_headers"] is not None and "retry-after" in row["response_headers"]
    # The allowlist is still the allowlist: a header nobody vetted does not ride in.
    assert "x-marker" not in row["response_headers"].lower()


# ── what a refetch can get wrong, rather than merely miss ────────────────────


def test_a_forecast_this_run_did_not_make_is_recorded_as_confirmed(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The receipt's false positive, and the sharpest thing an exact response would buy.**

    The POST is answered `200` with an empty body -- nothing about it says a forecast was
    created -- and the refetch then shows an entry newer than the baseline whose values
    are what we sent. `classify_refetch` confirms, and the ledger records `submitted`.

    Nothing here distinguishes "our post landed" from "something else wrote that entry
    between the baseline and the refetch". A refetch is an observation of *state*, and two
    causes produce the same state. An exact response body would carry the platform's own
    identifier for the forecast it created, which is an observation of *this action*.

    The test asserts the current behaviour deliberately, and it is not a bug report: under
    this project's threat model the operator's machine and account are non-malicious and
    single-bot, so the second writer has to be the operator's own second process -- which
    `M2-708`'s key reservation already refuses. What it is, is the boundary of what the
    receipt means, written down where the notes can cite it.

    The residue is bounded rather than absent: `build_verification_snapshot` stores the
    baseline's and the observation's `start_time`, so an auditor holding the row can see
    *when* the confirming entry appeared even though they cannot see who wrote it.
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[api_response(200, b"{}")],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, CONFIRMING_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert recorded.receipt.refetch_outcome == "confirmed"
    assert recorded.event.event_type == "submitted"
    assert current_status(conn, record_id) == "submitted"

    snapshot = _attempt_row(conn)["refetched_forecast_snapshot"]
    assert snapshot is not None
    # The bounded residue: the row says when, even though it cannot say who.
    assert str(NEW_START) in snapshot and str(BASELINE_START) not in snapshot


@pytest.mark.parametrize(
    "post_body",
    [
        pytest.param(b"{}", id="says-nothing"),
        pytest.param(b'{"id": 111, "question": 91001}', id="describes-a-forecast"),
        pytest.param(b'{"id": 222, "question": 999999}', id="describes-a-different-question"),
    ],
)
def test_the_platform_response_cannot_move_the_verdict_whatever_it_says(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    post_body: bytes,
) -> None:
    """**The discriminating half of the test above**, and the reason it is not circular.

    Three POST responses that say wildly different things -- nothing at all, a forecast on
    this question, a forecast on a question that is not ours -- and the ledger row is
    byte-identical in every column that carries a verdict. The response is not weighed
    lightly; it is not weighed at all.

    That is what makes "the refetch is the whole of the receipt" a measurement rather than
    a reading of the source. It also states the cost of the decision precisely: the third
    case is a response that would let the ledger *catch* a post landing on the wrong
    question, and today nothing looks at it.
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[api_response(200, post_body)],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, CONFIRMING_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    row = _attempt_row(conn)
    assert (row["success"], row["refetch_outcome"], row["verified_by_refetch"]) == (
        1,
        "confirmed",
        1,
    )
    assert (row["http_status"], row["response_body"], row["error_type"]) == (None, None, None)
    assert recorded.event.event_type == "submitted"


def test_a_platform_rounded_value_is_recorded_as_a_mismatch(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A refetch that is wrong, not merely absent** -- the second failure mode.

    `values_match` compares against `_VALUE_TOLERANCE = 1e-9`. A platform that stored what
    we sent but reported it rounded to six decimal places -- or a value that lost precision
    on a JSON round trip somewhere between the post and the read -- differs by more than
    that, and the outcome is `mismatched`, whose meaning in
    `verify_uncertain_attempt` is *"the platform holds a forecast that is not the one this
    attempt sent"*. That is a stronger statement than the observation supports.

    The consequence is asserted rather than described: the record stays `approved` and
    stuck, and the one command that could resolve it **refuses**, telling the operator to
    resolve it by hand. So this cell costs a human.

    Whether the platform actually rounds is unknown and is not knowable offline -- it is
    written up under this item's standing risk. What is knowable, and is what this test
    pins, is what *would* happen if it did.
    """
    conn, record_id = approved_record
    rounded = binary_values(PROBABILITY)
    rounded[1] = round(rounded[1] + 1e-6, 6)
    transport = CountingTransport(
        post_outcomes=[api_response(200, b"{}")],
        get_outcomes=[
            api_response(200, EMPTY_HISTORY),
            api_response(200, post_with_forecast_history([forecast_entry(NEW_START, rounded)])),
        ],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert recorded.receipt.success is True, "the post itself did not fail"
    assert recorded.receipt.refetch_outcome == "mismatched"
    assert recorded.event.event_type == "submission_uncertain"
    assert current_status(conn, record_id) == "approved"

    # And the way out is closed: a mismatch is the one outcome verification will not judge.
    later = CountingTransport(
        get_outcomes=[
            api_response(200, post_with_forecast_history([forecast_entry(NEW_START, rounded)]))
        ]
    )
    install_transport(monkeypatch, later)
    with pytest.raises(LiveSubmissionError) as excinfo:
        verify_uncertain_attempt(
            conn,
            record_id=record_id,
            attempt_id=recorded.receipt.attempt_id,
            poster=build_real_poster(later),
            occurred_at=OCCURRED,
            sleep=lambda _seconds: None,
        )
    assert "resolve it by hand" in str(excinfo.value)


def test_a_refetch_that_lags_the_post_is_recorded_as_absent(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The third failure mode: the platform accepted it and had not shown it yet.**

    Every GET returns the pre-post history, so the outcome is `absent` -- which
    `classify_refetch` reaches through *"nothing newer than the baseline"*, the same branch
    an honest rejection would reach. The ledger records `submission_uncertain`, which is
    the conservative answer and the right one; the cost is that it is also the answer for a
    post that genuinely never landed, and the row cannot tell them apart.

    **The count is the finding, and it was not what this test was written expecting.**
    `_REFETCH_ATTEMPTS` is 3, so the budget looks like three looks at the platform. It is
    not: `_observe_with_detail` returns on the first *readable* history, and retries only
    when the read itself could not be performed or parsed. A history that reads fine and
    shows nothing is readable. So the measured shape is **two GETs and zero pauses** -- one
    baseline, one refetch -- and the retry budget contributes nothing at all to this cell.

    That is the honest boundary on replication lag: the gateway looks exactly once, with no
    delay beyond the round trip. Compare `test_submission_integration.py::
    test_the_refetch_retries_where_the_post_does_not`, which measures 13 GETs and two 2.0s
    pauses for the same `_REFETCH_ATTEMPTS` -- because there the read *fails*. The retries
    are for an unreachable platform, not a slow one.

    Whether Metaculus's read path ever lags its write path is not knowable offline and is
    written up under this item's standing risk. What is knowable, and is what this pins, is
    that if it does, nothing in the current design waits for it.
    """
    conn, record_id = approved_record
    pauses: list[float] = []
    transport = CountingTransport(
        post_outcomes=[api_response(200, b"{}")],
        get_outcomes=[api_response(200, EMPTY_HISTORY)],
    )
    install_transport(monkeypatch, transport)

    recorded = post_approved_forecast(
        conn,
        record_id=record_id,
        payload=BINARY_PAYLOAD,
        poster=build_real_poster(transport),
        config=live_config,
        occurred_at=OCCURRED,
        clock=lambda: OCCURRED,
        sleep=pauses.append,
    )

    assert transport.posts == 1
    # One baseline, one refetch. Not four: a readable history ends the loop, so the three
    # attempts `_REFETCH_ATTEMPTS` names are never spent on a platform that answers.
    assert transport.gets == 2
    assert pauses == [], "no pause, because there was no second attempt to pause before"
    assert recorded.receipt.success is True
    assert recorded.receipt.refetch_outcome == "absent"
    assert recorded.event.event_type == "submission_uncertain"
    assert current_status(conn, record_id) == "approved"


# ── the partition itself ─────────────────────────────────────────────────────

# What each cell needs alongside its outcome. `detail_code` is required for every
# non-verified cell and refused for the verified one, so the mapping is part of the claim
# rather than scaffolding: a cell whose code the writer would reject is a cell this table
# gets wrong.
_CELLS: tuple[tuple[bool, RefetchOutcome, FailureCode | None, LifecycleEventType, str], ...] = (
    (True, "confirmed", None, "submitted", "submitted"),
    (True, "absent", "refetch_missing", "submission_uncertain", "approved"),
    (True, "mismatched", "refetch_mismatch", "submission_uncertain", "approved"),
    (True, "unreadable", "malformed_response", "submission_uncertain", "approved"),
    (False, "confirmed", "timeout", "submission_uncertain", "approved"),
    (False, "absent", "refetch_missing", "submission_failed", "failed"),
    (False, "mismatched", "refetch_mismatch", "submission_uncertain", "approved"),
    (False, "unreadable", "malformed_response", "submission_uncertain", "approved"),
)


@pytest.mark.parametrize(
    ("success", "outcome", "detail_code", "event_type", "status"),
    _CELLS,
    ids=[f"{'ok' if success else 'raised'}-{outcome}" for success, outcome, *_ in _CELLS],
)
def test_every_cell_of_the_success_by_refetch_table_lands_where_it_says(
    approved_record: tuple[sqlite3.Connection, str],
    success: bool,
    outcome: RefetchOutcome,
    detail_code: FailureCode | None,
    event_type: LifecycleEventType,
    status: str,
) -> None:
    """All eight cells of `record_submission_attempt`'s partition, driven and read back.

    **This is the enumeration the decision rests on.** The notes argue about where the
    ledger ends up with less than the truth, and that argument is only as good as the claim
    that these eight are all the places it can end up. `(success, refetch_outcome)` is the
    whole of what the writer derives its event from, so eight rows is exhaustive by
    construction -- and asserting each one against the docstring's own table is what makes
    the docstring checkable.

    Two cells are the ones worth reading twice. `(False, confirmed)` is *uncertain*, not
    failed: the call errored and the forecast is on the platform anyway, and recording that
    as a failure would leave the ledger permanently disagreeing with the world.
    `(False, absent)` is the only terminal failure in the table, and it is terminal because
    it is the only cell where something was actually *observed* to be missing.

    Driven at the writer rather than through the gateway on purpose -- see the module
    docstring. `tests/unit/test_submission_live.py` reaches most of these cells through
    `post_approved_forecast` against its own double; this asserts the partition.
    """
    ledger, record_id = approved_record
    attempt = SubmissionAttempt(
        attempt_id=f"attempt-{outcome}-{int(success)}",
        idempotency_key=f"wjlive-1-{outcome}-{int(success)}",
        requested_at_utc=OCCURRED,
        completed_at_utc=OCCURRED,
        request_payload_sha256="0" * 64,
        success=success,
        refetch_outcome=outcome,
    )
    event = record_submission_attempt(
        ledger,
        record_id=record_id,
        attempt=attempt,
        occurred_at=OCCURRED,
        secret_env_var_names=(),
        detail_code=detail_code,
    )

    assert event.event_type == event_type
    assert current_status(ledger, record_id) == status
    # `verified_by_refetch` is derived, never supplied: exactly one member is a confirmation.
    assert attempt.verified_by_refetch is (outcome == "confirmed")


def test_the_eight_cells_are_the_whole_partition() -> None:
    """The table above is exhaustive, and stays exhaustive if the vocabulary grows.

    Without this the parametrization is a list someone wrote once. `RefetchOutcome` is a
    closed `Literal`, so a fifth member added by a future migration would leave two cells
    untested and nothing would say so -- the vacuous-coverage shape `docs/LESSONS.md` #9 is
    about, one level up.
    """
    from typing import get_args

    outcomes = set(get_args(RefetchOutcome))
    assert {(success, outcome) for success, outcome, *_ in _CELLS} == {
        (success, outcome) for success in (True, False) for outcome in outcomes
    }
    assert len(_CELLS) == 2 * len(outcomes)
