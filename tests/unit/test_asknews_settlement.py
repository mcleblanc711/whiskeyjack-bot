"""AskNews credit settlement, the derived estimate, and the failure classifier (M1-336/337/332).

Written from the acceptance criteria first:

- M1-336: a per-credit rate (a module constant under D41) converts ``usage.credits`` to
  dollars and settles the reservation; a planted credit count appends ``cost_settled`` at
  the converted amount and ``spending()`` reports it as actual rather than held; a response
  with no usage block keeps the hold, not a crash. ``correct-costs`` back-fills the
  reservations held before this change (owner decision 2026-09-24).
- M1-337: the one estimate equals ``credits_per_news_call x rate``, not a literal.
- M1-332: a provider exception is named in a closed vocabulary from its class NAMES over
  its MRO, never from its text; an unrecognized class is ``transient``, never a quota.

Every provider here is fake; the suite's socket guards make "no network" enforced.
"""

from __future__ import annotations

import ast
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from asknews_sdk import errors as sdk_errors
from asknews_sdk.dto.news import SearchResponse, SearchResponseDictItem, Usage

from test_asknews import _article as _full_article  # type: ignore[import-not-found]
from test_tournament import case as tournament_case  # type: ignore[import-not-found]
from whiskeyjack_bot.cli import main
from whiskeyjack_bot.research import asknews
from whiskeyjack_bot.research.asknews import (
    FAILURE_ADVICE,
    AskNewsFailure,
    classify_failure,
    retrieve_news,
)
from whiskeyjack_bot.research.asknews_cost import (
    CREDITS_PER_NEWS_CALL,
    MAX_CREDITS,
    MICROUSD_PER_CREDIT,
    credits_microusd,
)
from whiskeyjack_bot.research.durable import begin_call
from whiskeyjack_bot.tournament_state import (
    Budget,
    append,
    budget_context,
    correct_costs,
    events,
    spending,
)

SCOPE = "42:32977"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
SENTINEL = "SENTINEL-asknews-0001"
ABSENT = object()


@pytest.fixture
def case(tmp_path: Any) -> Any:
    yield from tournament_case.__wrapped__(tmp_path)


def _article() -> SearchResponseDictItem:
    """A complete article: the recovery path re-validates the stored response."""
    return _full_article()


class _News:
    """Answers every search with one article and ``usage`` (ABSENT: the DTO default)."""

    def __init__(self, usage: Any = ABSENT, raises: BaseException | None = None) -> None:
        self.news = self
        self.usage = usage
        self.raises = raises
        self.calls = 0

    def search_news(self, **kwargs: Any) -> SearchResponse:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        extra = {} if self.usage is ABSENT else {"usage": self.usage}
        return SearchResponse.model_construct(as_dicts=[_article()], **extra)


def _retrieve(conn: Any, config: Any, news: _News, *, query: str = "q") -> Any:
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    with budget_context(budget):
        return retrieve_news(
            news,  # type: ignore[arg-type]
            config,
            question_id=7,
            queries=[query],
            retrieval_run_id=uuid.uuid4().hex,
            now=NOW,
        )


def _held_one_call(conn: Any) -> tuple[str, dict[str, Any]]:
    (reserved,) = events(conn, "cost_reserved", SCOPE)
    return reserved["reservation_id"], reserved


# --- M1-336: settle from usage.credits ---------------------------------------------------


@pytest.mark.parametrize(("credits", "microusd"), [(1, 25_000), (3, 75_000), (0, 0), (5, 125_000)])
def test_a_planted_credit_count_settles_at_the_converted_amount(
    case: Any, credits: int, microusd: int
) -> None:
    """The AC test. 3 credits is the case a float rate gets wrong (ceil gives 75001)."""
    conn, config, *_ = case
    result = _retrieve(conn, config, _News(Usage(credits=credits)))
    assert result.provider_failed is False and result.failure is None
    reservation, _ = _held_one_call(conn)
    assert events(conn, "cost_settled", SCOPE) == [
        {"reservation_id": reservation, "actual_microusd": microusd, "basis": "asknews_credits"}
    ]
    assert spending(conn, SCOPE) == (microusd, 0), "actual, not held"


@pytest.mark.parametrize(
    "usage",
    [
        ABSENT,
        None,
        Usage.model_construct(credits=1.5),
        Usage.model_construct(credits=1.0),
        Usage.model_construct(credits=-1),
        Usage.model_construct(credits="3"),
        Usage.model_construct(credits=MAX_CREDITS + 1),
        Usage.model_construct(),
    ],
    ids=[
        "no-usage-block",
        "usage-null",
        "float",
        "integral-float",
        "negative",
        "string",
        "too-large",
        "credits-missing",
    ],
)
def test_an_unknown_credit_count_keeps_the_hold_and_never_raises(case: Any, usage: Any) -> None:
    """Unknown is never free: the reservation stays held at its full estimate."""
    conn, config, *_ = case
    result = _retrieve(conn, config, _News(usage))
    assert result.provider_failed is False
    assert len(result.documents) == 1
    assert events(conn, "cost_settled", SCOPE) == []
    assert spending(conn, SCOPE) == (0, 25_000)


@pytest.mark.parametrize(
    ("wire", "settled"),
    [(True, 25_000), ("3", 75_000), (1.0, 25_000), (-1, None), (10**30, None)],
    ids=["bool", "numeric-string", "integral-float", "negative", "huge"],
)
def test_the_adapter_settles_what_the_sdk_parsed_not_the_wire(
    case: Any, wire: object, settled: int | None
) -> None:
    """The pinned SDK validates the body in lax mode before the adapter sees it.

    `true`, `"3"` and `1.0` are coerced to ints by `SearchResponse` itself, so the adapter
    settles the coerced count; refusing them would need the raw HTTP body, which the SDK
    does not expose. A negative or absurd count survives the SDK and is refused here. (A
    `1.5` or NaN fails the SDK's own validation, so the call raises and stays held.)
    """
    conn, config, *_ = case

    class _Wire(_News):
        def search_news(self, **kwargs: Any) -> SearchResponse:
            self.calls += 1
            return SearchResponse.model_validate({"as_dicts": [], "usage": {"credits": wire}})

    _retrieve(conn, config, _Wire())
    held = 25_000 if settled is None else 0
    assert spending(conn, SCOPE) == (settled or 0, held)
    assert len(events(conn, "cost_settled", SCOPE)) == (settled is not None)


def test_a_recovered_call_settles_once_from_the_stored_response(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between completion and settlement: the next attempt settles, and never rebuys."""
    conn, config, *_ = case
    news = _News(Usage(credits=3))
    with monkeypatch.context() as patch:
        patch.setattr(Budget, "settle_microusd", lambda *args, **kwargs: None)
        _retrieve(conn, config, news)
    assert news.calls == 1
    assert events(conn, "cost_settled", SCOPE) == []
    assert (
        len(
            events(
                conn,
                "retrieval_completed",
                conn.execute(
                    "SELECT scope FROM tournament_events WHERE kind='retrieval_completed'"
                ).fetchone()[0],
            )
        )
        == 1
    )

    _retrieve(conn, config, news)
    assert news.calls == 1, "the cached response is reused; nothing is bought twice"
    reservation, _ = _held_one_call(conn)
    assert events(conn, "cost_settled", SCOPE) == [
        {"reservation_id": reservation, "actual_microusd": 75_000, "basis": "asknews_credits"}
    ]
    _retrieve(conn, config, news)
    assert len(events(conn, "cost_settled", SCOPE)) == 1
    assert spending(conn, SCOPE) == (75_000, 0)


@pytest.mark.parametrize("amount", [-1, True, 25_000.0, None, "25000"])
def test_settle_microusd_refuses_anything_but_an_exact_non_negative_int(
    case: Any, amount: Any
) -> None:
    """Its own guard, not only its callers': a refused figure leaves the reservation held."""
    conn, config, *_ = case
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    reservation = budget.reserve("asknews", 0.025, {})
    budget.settle_microusd(reservation, amount, basis="asknews_credits")
    assert events(conn, "cost_settled", SCOPE) == []
    assert spending(conn, SCOPE) == (0, 25_000)
    budget.settle_microusd(reservation, 0, basis="asknews_credits")
    assert spending(conn, SCOPE) == (0, 0), "a real zero settles"


# --- M1-337: the estimate is credits x rate ----------------------------------------------


def test_the_one_estimate_is_credits_per_news_call_times_the_rate(case: Any) -> None:
    conn, config, *_ = case
    _retrieve(conn, config, _News(None))
    _, reserved = _held_one_call(conn)
    assert reserved["provider"] == "asknews"
    assert reserved["estimate_microusd"] == CREDITS_PER_NEWS_CALL * MICROUSD_PER_CREDIT
    # The owner's figures (2026-09-23), written out rather than read back from the module:
    # $0.025 per credit, and a news search is one credit.
    assert (MICROUSD_PER_CREDIT, CREDITS_PER_NEWS_CALL) == (25_000, 1)


def test_the_adapter_carries_no_price_literal() -> None:
    """The dead `0.125 if historical else 0.025` is gone, and no float price came back."""
    source = Path(asknews.__file__).read_text(encoding="utf-8")
    floats = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and type(node.value) is float
    ]
    # The one float left is the run row's `cost_usd` when no call was made: zero, not a price.
    assert floats == [0.0]


# --- credits_microusd, directly ----------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"usage": {"credits": 1}}, 25_000),
        ({"usage": {"credits": 3}}, 75_000),
        ({"usage": {"credits": 0}}, 0),
        ({"usage": {"credits": MAX_CREDITS}}, MAX_CREDITS * 25_000),
        ({"usage": {"credits": MAX_CREDITS + 1}}, None),
        ({"usage": {"credits": -1}}, None),
        ({"usage": {"credits": True}}, None),
        ({"usage": {"credits": 1.0}}, None),
        ({"usage": {"credits": float("nan")}}, None),
        ({"usage": {"credits": "1"}}, None),
        ({"usage": {"credits": None}}, None),
        ({"usage": {}}, None),
        ({"usage": None}, None),
        ({"usage": [1]}, None),
        ({}, None),
        (None, None),
        ([{"usage": {"credits": 1}}], None),
    ],
)
def test_credits_microusd_converts_exactly_or_refuses(response: object, expected: object) -> None:
    assert credits_microusd(response) == expected


def test_the_conversion_is_exact_for_every_credit_count() -> None:
    """The float rate misrounds 3, 6, 7, 12 ... ; integer micro-USD has no such count."""
    for count in range(0, 20_001):
        assert credits_microusd({"usage": {"credits": count}}) == count * 25_000


# --- M1-332: the failure classifier ------------------------------------------------------

REAL_CLASSES: list[tuple[type[BaseException], AskNewsFailure]] = [
    (sdk_errors.RateLimitExceededError, "rate_or_quota_limited"),
    (sdk_errors.ConcurrencyLimitExceededError, "rate_or_quota_limited"),
    (sdk_errors.ForbiddenError, "forbidden_or_quota"),
    (sdk_errors.UnauthorizedError, "auth_rejected"),
    (sdk_errors.BadRequestError, "request_rejected"),
    (sdk_errors.ResourceNotFoundError, "request_rejected"),
    (sdk_errors.MethodNotAllowed, "request_rejected"),
    (sdk_errors.ValidationError, "request_rejected"),
    (sdk_errors.RequestTimeoutError, "provider_unavailable"),
    (sdk_errors.ServiceUnavailableError, "provider_unavailable"),
    (sdk_errors.APIError, "provider_error"),
]


@pytest.mark.parametrize(
    ("klass", "expected"), REAL_CLASSES, ids=lambda v: getattr(v, "__name__", v)
)
def test_the_vocabulary_is_pinned_against_the_real_sdk_classes(
    klass: type[BaseException], expected: AskNewsFailure
) -> None:
    """A rename in a future asknews release is a red build, not a silent `transient`."""
    assert klass.__module__ == "asknews_sdk.errors"
    assert classify_failure(klass(SENTINEL, SENTINEL, 1)) == expected  # type: ignore[call-arg]


def test_every_class_the_sdk_raises_from_a_response_is_named() -> None:
    """`raise_from_response` raises only ErrorMap's classes or the base: each has a name."""
    raised = {*sdk_errors.ErrorMap.values(), sdk_errors.APIError}
    assert raised <= {klass for klass, _ in REAL_CLASSES}


def test_a_status_the_sdk_does_not_list_is_a_provider_error_not_transient() -> None:
    """A 402 has no ErrorMap entry and arrives as the base class; the SDK decides that."""

    class _Response:
        content = {"status_code": 402, "detail": SENTINEL}

    with pytest.raises(sdk_errors.APIError) as raised:
        sdk_errors.raise_from_response(_Response())  # type: ignore[arg-type]
    assert type(raised.value) is sdk_errors.APIError
    assert classify_failure(raised.value) == "provider_error"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ReadTimeout(SENTINEL), "provider_unavailable"),
        (httpx.ConnectTimeout(SENTINEL), "provider_unavailable"),
        (httpx.ConnectError(SENTINEL), "transient"),
        (httpx.RemoteProtocolError(SENTINEL), "transient"),
        (RuntimeError(SENTINEL), "transient"),
        (ValueError(SENTINEL), "transient"),
        (Exception(SENTINEL), "transient"),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else v,
)
def test_transport_and_foreign_exceptions(exc: BaseException, expected: AskNewsFailure) -> None:
    assert classify_failure(exc) == expected


def test_an_unrecognized_class_is_transient_never_a_quota() -> None:
    """The AC's case: a wrong 'quota' alert sends an operator to a dashboard for a socket."""
    lookalike = type("ForbiddenError", (Exception,), {"__module__": "somewhere.else"})
    limited = type("RateLimitExceededError", (Exception,), {"__module__": "asknews_sdk.other"})
    assert classify_failure(lookalike(SENTINEL)) == "transient"
    assert classify_failure(limited(SENTINEL)) == "transient"


def test_a_subclass_resolves_to_its_nearest_named_ancestor() -> None:
    class QuotaExhaustedError(sdk_errors.ForbiddenError):
        pass

    class Newer(sdk_errors.APIError):
        pass

    assert classify_failure(QuotaExhaustedError(SENTINEL)) == "forbidden_or_quota"
    assert classify_failure(Newer(SENTINEL)) == "provider_error"


def test_the_classifier_reads_nothing_off_the_exception_but_its_type() -> None:
    """Every content-bearing attribute raises if touched; the class alone still names it."""

    def touched(self: Any) -> Any:
        raise AssertionError("classify_failure read exception content")

    class Trap(sdk_errors.ForbiddenError):
        detail = property(touched)  # type: ignore[assignment]
        code = property(touched)  # type: ignore[assignment]
        response = property(touched)  # type: ignore[assignment]
        args = property(touched)  # type: ignore[assignment]
        __str__ = touched
        __repr__ = touched

        def __init__(self) -> None:
            pass

    assert classify_failure(Trap()) == "forbidden_or_quota"


@pytest.mark.parametrize(
    ("klass", "expected"), REAL_CLASSES, ids=lambda v: getattr(v, "__name__", v)
)
def test_a_failed_call_names_its_class_in_the_run_and_nothing_of_its_text(
    case: Any, klass: type[BaseException], expected: AskNewsFailure
) -> None:
    conn, config, *_ = case
    exc = klass(SENTINEL, SENTINEL, 1)  # type: ignore[call-arg]
    result = _retrieve(conn, config, _News(raises=exc))
    assert result.provider_failed is True and result.failure == expected
    assert result.calls_attempted == 1, "the request that raised is still counted"
    summary = result.run.error_summary
    assert summary is not None and f"({expected})" in summary
    assert SENTINEL not in json.dumps(result.run.model_dump(mode="json"))
    assert SENTINEL not in "\n".join(conn.iterdump())
    # The call that raised has no stored response: its reservation stays held.
    assert spending(conn, SCOPE) == (0, 25_000)


def test_a_successful_run_has_no_failure(case: Any) -> None:
    conn, config, *_ = case
    result = _retrieve(conn, config, _News(Usage(credits=1)))
    assert (result.provider_failed, result.failure, result.run.error_summary) == (False, None, None)


def test_the_advice_never_claims_a_quota_for_certain() -> None:
    """Only the classes that cannot rule a quota out mention one, and all hedge."""
    for failure in get_args(AskNewsFailure):
        text = FAILURE_ADVICE[failure].lower()
        if failure in ("rate_or_quota_limited", "forbidden_or_quota", "provider_error"):
            assert "quota" in text or "billing" in text
        else:
            assert "quota" not in text
        assert "quota exhausted" not in text and "quota is exhausted" not in text


# --- The correct-costs back-fill (owner decision 2026-09-24) ------------------------------


def _pre_m1_336_call(
    conn: Any,
    config: Any,
    response: Any,
    *,
    scope: str = SCOPE,
    provider: str = "asknews",
    completions: int = 1,
    query: str | None = None,
) -> str:
    """A call as the journal recorded it before M1-336: reserved, completed, never settled."""
    budget = Budget(conn, config.storage.artifact_root, scope, 100_000_000)
    with budget_context(budget):
        call_scope, cached = begin_call(
            provider, 0.025, {"query": query or uuid.uuid4().hex}, 7, "cutoff"
        )
    assert call_scope is not None and cached is None
    for _ in range(completions):
        append(conn, "retrieval_completed", call_scope, {"response": response, "actual_cost": None})
    (started,) = events(conn, "retrieval_started", call_scope)
    return str(started["reservation_id"])


def _settled_rows(conn: Any) -> int:
    return int(
        conn.execute("SELECT count(*) FROM tournament_events WHERE kind='cost_settled'").fetchone()[
            0
        ]
    )


def test_the_back_fill_is_a_dry_run_until_applied_then_idempotent(case: Any) -> None:
    conn, config, *_ = case
    one = _pre_m1_336_call(conn, config, {"usage": {"credits": 1}})
    five = _pre_m1_336_call(conn, config, {"usage": {"credits": 5}})
    _pre_m1_336_call(conn, config, {"usage": None})  # no credit count: refused
    _pre_m1_336_call(conn, config, {"usage": {"credits": 1}}, completions=0)  # outcome unknown
    _pre_m1_336_call(conn, config, {"usage": {"credits": 1}}, completions=2)  # ambiguous
    _pre_m1_336_call(conn, config, {"usage": {"credits": 1}}, provider="exa")  # not AskNews
    assert spending(conn, SCOPE) == (0, 6 * 25_000)
    rows = conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0]

    dry = correct_costs(conn).as_dict(applied=False)
    assert (dry["asknews_settlements"], dry["asknews_total_usd"]) == (2, 0.15)
    assert (dry["asknews_refused_no_credits"], dry["asknews_written"]) == (3, 0)
    assert conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0] == rows

    applied = correct_costs(conn, apply=True)
    assert applied.asknews_written == 2
    assert events(conn, "cost_settled", SCOPE) == [
        {
            "reservation_id": one,
            "actual_microusd": 25_000,
            "basis": "asknews_credits",
            "backfilled": True,
        },
        {
            "reservation_id": five,
            "actual_microusd": 125_000,
            "basis": "asknews_credits",
            "backfilled": True,
        },
    ]
    assert spending(conn, SCOPE) == (150_000, 4 * 25_000)

    again = correct_costs(conn, apply=True)
    assert (again.asknews, again.asknews_written, again.asknews_refused) == ((), 0, 3)
    assert _settled_rows(conn) == 2
    assert spending(conn, SCOPE) == (150_000, 4 * 25_000)


def test_the_back_fill_settles_in_each_reservation_s_own_scope(case: Any) -> None:
    conn, config, *_ = case
    old = _pre_m1_336_call(conn, config, {"usage": {"credits": 5}}, scope="42:33122")
    new = _pre_m1_336_call(conn, config, {"usage": {"credits": 1}}, scope="42:33125")
    correct_costs(conn, apply=True)
    assert [row["reservation_id"] for row in events(conn, "cost_settled", "42:33122")] == [old]
    assert [row["reservation_id"] for row in events(conn, "cost_settled", "42:33125")] == [new]
    assert spending(conn, "42:33122") == (125_000, 0)
    assert spending(conn, "42:33125") == (25_000, 0)


def test_the_back_fill_never_resettles_a_live_settlement(case: Any) -> None:
    """A call settled by the adapter already carries the guard; the back-fill skips it."""
    conn, config, *_ = case
    _retrieve(conn, config, _News(Usage(credits=3)))
    report = correct_costs(conn, apply=True)
    assert (report.asknews, report.asknews_refused, report.asknews_written) == ((), 0, 0)
    assert _settled_rows(conn) == 1


def test_the_back_fill_rechecks_the_guard_inside_its_transaction(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run landing between this run's plan and its write writes nothing twice."""
    from whiskeyjack_bot import tournament_state
    from whiskeyjack_bot.ledger import connect

    conn, config, *_ = case
    _pre_m1_336_call(conn, config, {"usage": {"credits": 1}})
    real = tournament_state._asknews_settlements
    raced: list[int] = []

    def plan_then_race(conn: Any) -> Any:
        found = real(conn)
        if not raced:
            monkeypatch.setattr(tournament_state, "_asknews_settlements", real)
            with connect(config.storage.sqlite_path) as other:
                raced.append(correct_costs(other, apply=True).asknews_written)
        return found

    monkeypatch.setattr(tournament_state, "_asknews_settlements", plan_then_race)
    report = correct_costs(conn, apply=True)
    assert raced == [1]
    assert len(report.asknews) == 1 and report.asknews_written == 0
    assert _settled_rows(conn) == 1


def test_the_command_reports_and_applies_the_back_fill(
    case: Any, tmp_path: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    monkeypatch.setattr("whiskeyjack_bot.logging_setup.configure_logging", lambda config: None)
    conn, config, *_ = case
    _pre_m1_336_call(conn, config, {"usage": {"credits": 5}})
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = ["tournament", "correct-costs", "--config", str(path)]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["asknews_settlements"], report["asknews_written"]) == (1, 0)
    assert _settled_rows(conn) == 0
    assert main([*args, "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["asknews_total_usd"], report["asknews_written"]) == (0.125, 1)
    assert spending(conn, SCOPE) == (125_000, 0)
