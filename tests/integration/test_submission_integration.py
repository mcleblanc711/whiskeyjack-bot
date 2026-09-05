"""T-902: the submission path, from an approved record to a ledger row, over real HTTP shapes.

Three of ``CODEX_HANDOFF.md``'s mocked-integration bullets live here -- "approval required
before submission", "429/5xx retry and final failure", and "post success followed by refetch
verification" -- plus the one this item's acceptance criteria single out: *"uncertain timeout
where posting may have succeeded: block retry until refetch resolves state."*

**What this adds over ``tests/unit/test_submission_live.py``, which is thorough.** That suite
drives ``post_approved_forecast`` against a hand-written four-method ``FakePoster``, and says
so deliberately: ``submission_live`` imports nothing from ``forecasting_tools`` or
``requests``, so its "one post" is a property of the double. The consequence is that no test
there can see the SDK's blind POST retry, because the retry lives *below* the protocol -- and
neutralizing that retry is the whole of ``M2-704``. ``tests/unit/test_metaculus_poster.py``
covers the other side, driving the real client with a counted ``requests`` stub, but stops at
``http_details(exc)`` and never reaches a receipt or a ledger row.

Every test below spans that seam: a real ``MetaculusClient``, a real ``SingleAttemptPoster``,
a real ledger, and the counter on ``requests`` itself. "Exactly one POST" is then a
measurement taken outside the code under test rather than a property of a double.

**Deviation from the handoff, stated rather than left to be read as an omission.** The bullet
says "429/5xx **retry** and final failure". There is no write retry to test, deliberately:
the pinned SDK blind-retries every POST four times, and ``SingleAttemptPoster`` exists to
strip that, on the rule *reads may retry, writes must not* (owner decision 2026-08-25) --
because a re-POST that already landed is precisely the duplicate submit this item's
acceptance criterion forbids. So the bullet is tested as the shipped contract: one POST per
429/5xx with the status captured and the terminal outcome recorded, **and** the retry that
does exist, on the refetch, asserted as its counterpart.
"""

from __future__ import annotations

import sqlite3

import pytest
import requests
from fake_platform import (
    CountingTransport,
    api_response,
    binary_values,
    build_real_poster,
    forecast_entry,
    OCCURRED,
    install_transport,
    post_with_forecast_history,
)

from tests.unit.test_submission_live import BINARY_PAYLOAD, PROBABILITY
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.lifecycle import current_status
from whiskeyjack_bot.submission_live import LiveSubmissionError, post_approved_forecast

BASELINE_START = 1_000_000.0
NEW_START = 1_000_100.0

EMPTY_HISTORY = post_with_forecast_history([])
"""The platform before the post: readable, and holding no forecast of ours."""

CONFIRMING_HISTORY = post_with_forecast_history(
    [forecast_entry(NEW_START, binary_values(PROBABILITY))]
)
"""The platform after an honest post: a newer entry whose values are what was sent."""


def _post(
    conn: sqlite3.Connection,
    record_id: str,
    config: AppConfig,
    transport: CountingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
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


def _attempt_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return list(
        conn.execute(
            "SELECT success, verified_by_refetch, refetch_outcome, http_status, error_type "
            "FROM submission_attempts ORDER BY created_at_utc"
        )
    )


# ── approval required before submission ──────────────────────────────────────


def test_an_unapproved_record_sends_no_request_at_all(
    validated_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate holds with the real adapter and the real transport in the chain.

    ``tests/unit/test_submission_live.py`` asserts its ``FakePoster`` saw no call. The
    witness here is one layer further out and cannot be satisfied by a double that simply
    was not asked: **zero HTTP requests of either verb**. A gate that ran after the baseline
    fetch would pass the unit assertion and fail this one.
    """
    conn, record_id = validated_record
    transport = CountingTransport(get_outcomes=[api_response(200, EMPTY_HISTORY)])

    with pytest.raises(LiveSubmissionError):
        _post(conn, record_id, live_config, transport, monkeypatch)

    assert transport.posts == 0
    assert transport.gets == 0
    assert _attempt_rows(conn) == []


# ── 429 / 5xx, and the final failure ─────────────────────────────────────────


def test_a_429_is_recorded_as_http_error_and_sends_exactly_one_request(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handoff's 429 case, as the shipped design answers it.

    Two claims, and the second is the one no existing test can make. **One POST**: the pinned
    SDK would send four, so this fails loudly if ``SingleAttemptPoster`` ever stops being
    reached. And ``error_type == "http_error"``: ``classify_error`` matches on
    ``__module__ == "requests.exceptions"``, so the synthetic ``Exception`` that
    ``test_submission_live.py`` uses for its 429 receipt classifies as ``internal_error`` and
    can never exercise this branch. Reaching it needs the real exception, which needs the
    real client raising it.
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[
            api_response(
                429, b'{"detail": "slow down"}', {"Retry-After": "30", "Set-Cookie": "s=x"}
            )
        ],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, EMPTY_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert transport.posts == 1, "the SDK's blind retry would make this four"
    receipt = recorded.receipt  # type: ignore[attr-defined]
    assert receipt.success is False
    assert receipt.error_type == "http_error"
    assert receipt.http_status == 429
    assert receipt.response_headers is not None
    assert "retry-after" in receipt.response_headers.lower()
    assert "set-cookie" not in receipt.response_headers.lower()


def test_a_5xx_the_platform_never_shows_is_a_terminal_failure(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handoff's "final failure": the post raised and the refetch establishes absence.

    ``(success=False, refetch_outcome="absent")`` is the one cell the ledger calls terminal.
    Every other combination stays ``approved`` and resolvable, so asserting the status is
    what separates "this failed" from "this might have landed".
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[api_response(503, b'{"detail": "unavailable"}')],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, EMPTY_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert transport.posts == 1
    assert recorded.event.event_type == "submission_failed"  # type: ignore[attr-defined]
    assert recorded.receipt.http_status == 503  # type: ignore[attr-defined]
    assert recorded.receipt.error_type == "http_error"  # type: ignore[attr-defined]
    assert recorded.receipt.success is False  # type: ignore[attr-defined]
    assert recorded.receipt.verified_by_refetch is False  # type: ignore[attr-defined]
    assert current_status(conn, record_id) == "failed"


# ── post success, then refetch verification ──────────────────────────────────


def test_a_post_confirmed_by_an_sdk_parsed_refetch_reaches_submitted(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path, with the refetch parsed by the SDK from a real API post body.

    ``T-901``'s notes close with exactly this gap: its golden fixtures are structurally valid
    but *"nothing offline proves a real MiniBench post normalizes to a record of this
    shape"*. Here the question the verification reads is built by
    ``DataOrganizer``/``MetaculusClient`` out of the committed API post, so
    ``read_my_forecasts`` walks the real ``api_json`` layout rather than a dict written to
    match it.
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[api_response(200, b"{}")],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, CONFIRMING_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert transport.posts == 1
    assert recorded.event.event_type == "submitted"  # type: ignore[attr-defined]
    assert recorded.receipt.verified_by_refetch is True  # type: ignore[attr-defined]
    assert recorded.receipt.refetch_outcome == "confirmed"  # type: ignore[attr-defined]
    assert current_status(conn, record_id) == "submitted"
    assert transport.post_urls == ["https://www.metaculus.com/api/questions/forecast/"]


def test_a_post_the_platform_does_not_show_is_never_recorded_as_verified(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discriminating half of refetch verification.

    The POST returns 200 and raises nothing -- so a gateway that trusted the call would call
    this a success. The refetch shows the same empty history it showed before, which
    establishes only that nothing of ours is there. ``CODEX_HANDOFF.md:376`` forbids the
    other answer: *"Do not say a live API call succeeded without a recorded receipt and
    refetch confirmation."*

    The preceding test alone passes against an implementation that hard-codes
    ``verified_by_refetch=True``; this one is what makes the pair mean something.
    """
    conn, record_id = approved_record
    transport = CountingTransport(
        post_outcomes=[api_response(200, b"{}")],
        get_outcomes=[api_response(200, EMPTY_HISTORY), api_response(200, EMPTY_HISTORY)],
    )

    recorded = _post(conn, record_id, live_config, transport, monkeypatch)

    assert recorded.receipt.success is True, "the call itself did not fail"  # type: ignore[attr-defined]
    assert recorded.receipt.verified_by_refetch is False  # type: ignore[attr-defined]
    assert recorded.event.event_type == "submission_uncertain"  # type: ignore[attr-defined]
    assert current_status(conn, record_id) == "approved", "still resolvable, not terminal"


# ── the retry that does exist, and the one that must not ─────────────────────


def test_the_refetch_retries_where_the_post_does_not(
    approved_record: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest reading of the handoff's "429/5xx **retry**" half.

    *Reads may retry, writes must not.* The pair is the assertion: one POST against twelve
    GETs for the same underlying failure. Measured rather than assumed --

    * ``1`` POST, because ``SingleAttemptPoster`` posts through the undecorated function;
    * ``1 + 3 x 4 = 13`` GETs: one baseline, then the gateway's three refetch attempts, each
      of which is itself retried four times by the SDK decorator the refetch path
      deliberately keeps;
    * two gateway pauses of ``2.0`` seconds, between three attempts.

    The injected ``sleep`` is what makes this free. The SDK's own backoff is neutralized in
    ``install_transport``; left alone it asks for 10.9s + 59.7s + 75.0s on the first refetch
    attempt alone, which is why that patch is load-bearing rather than tidiness.
    """
    conn, record_id = approved_record
    pauses: list[float] = []
    transport = CountingTransport(
        post_outcomes=[api_response(200, b"{}")],
        get_outcomes=[
            api_response(200, EMPTY_HISTORY),
            requests.exceptions.ConnectionError("the refetch could not be performed"),
        ],
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
    assert transport.gets == 13
    assert pauses == [2.0, 2.0]
    assert recorded.receipt.refetch_outcome == "unreadable"
    assert recorded.event.event_type == "submission_uncertain"
    assert current_status(conn, record_id) == "approved", "unreadable is not absent"
