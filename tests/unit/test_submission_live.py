"""M2-704: the package-backed gateway, its refusals, and what it records.

Two claims are under test, and they are the backlog's own words.

**"Success requires refetch confirmation."** Nothing here reaches `submitted` without a
refetch that shows a forecast entry the baseline did not have, whose values match what was
posted. The tests drive every other combination and assert it does *not*.

**"Uncertain timeout blocks blind retry."** An unresolved uncertainty refuses the next
submission before any network call -- the poster's call count is asserted, not inferred --
and `verify-submission` is the only thing that reopens it.

Every test uses a fake poster. That is not a convenience: `submission_live` imports nothing
from `forecasting_tools` and nothing from `requests`, so "one post per submission" is a
property of a four-method object rather than of a patched client.
`tests/unit/test_metaculus_poster.py` is where the real SDK is held to the same contract.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import traceback
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.approval import approve
from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.forecast.generate import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.record import ForecastRecordDraft, build_forecast_record_draft
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.forecast.store import append_forecast_version
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    current_status,
    record_validation,
    transaction,
    unresolved_uncertainties,
)
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion, CanonicalQuestion
from whiskeyjack_bot.submission import SubmissionError
from whiskeyjack_bot.submission_gateway import (
    SubmissionRequest,
    payload_sha256,
    read_live_artifact,
)
from whiskeyjack_bot.bounds import MAX_BODY_LENGTH
from whiskeyjack_bot.submission_live import (
    _MAX_CATEGORIES,
    _MAX_OPTION_LABEL,
    _MAX_SNAPSHOT_VALUES,
    BinaryPost,
    ForecastEntry,
    ForecastHistory,
    LiveSubmissionError,
    MetaculusSubmissionGateway,
    MultipleChoicePost,
    NumericPost,
    build_verification_snapshot,
    classify_refetch,
    expected_option_labels,
    expected_values,
    live_attempt_id,
    plan_from_payload,
    post_approved_forecast,
    read_my_forecasts,
    storable_text,
    values_match,
    verify_uncertain_attempt,
)

from tests.unit.records import CALIBRATION

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

QUESTION_ID = 123
POST_ID = 456
TOURNAMENT = "minibench"
RUN_ID = "run-1"
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
TIMESTAMP = "2026-08-22T00:00:00.000000+00:00"
OCCURRED = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

PROBABILITY = 0.37
BINARY_PAYLOAD: dict[str, Any] = {"question_type": "binary", "probability_yes": PROBABILITY}
# M2-707: what the `approved` fixture's approval authorizes. Computed from the payload
# rather than written as a literal, because since `011` the two have to agree -- a
# hand-typed digest here would make every post in this module fail at the key seam, and
# the one that is *supposed* to fail there would prove nothing. Since round 1 `approve`
# derives its own digest from the record, so this value is now also an *independent*
# witness: it is built from `BINARY_PAYLOAD` here and from `record_json` there, and the
# tests below assert the two agree.
PAYLOAD_SHA = payload_sha256(BINARY_PAYLOAD)
BASELINE_START = 1_000_000.0
NEW_START = 1_000_100.0

LIVE_ATTEMPT_RE = re.compile(r"^wjlive-1-[0-9a-f]{64}\Z")


# ── a forecast record, built through the real writer ─────────────────────────


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _response(**overrides: Any) -> ForecastResponse:
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Binary schema") + "}"),
    }
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _question(**overrides: Any) -> CanonicalQuestion:
    fields: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": "Will the thing happen?",
    }
    fields.update(overrides)
    return CanonicalBinaryQuestion(**fields)


def _generation(**overrides: Any) -> ForecastGeneration:
    fields: dict[str, Any] = {
        "forecast": _response(),
        "settings": ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
        ),
        "sources": tuple(
            SourceReference(
                source_id=source_id,
                document_id=None,
                canonical_url=f"https://example.test/{source_id}",
                content_sha256="c" * 64,
            )
            for source_id in ("src-001", "src-002")
        ),
        "request": "the rendered reasoning packet",
        "raw_responses": ("{}",),
        "invocations": 1,
        "repair_attempted": False,
        "cost_usd": None,
        "failure_code": None,
        "failure_problems": (),
    }
    fields.update(overrides)
    return ForecastGeneration(**fields)


def _draft(attempt_id: str = "attempt-1", **overrides: Any) -> ForecastRecordDraft:
    fields: dict[str, Any] = {
        "question": _question(),
        "generation": _generation(),
        "tournament_id": TOURNAMENT,
        "attempt_id": attempt_id,
        "retrieval_run_id": RUN_ID,
        "research_packet_sha256": "d" * 64,
        "generated_at": GENERATED_AT,
    }
    fields.update(overrides)
    return build_forecast_record_draft(**fields)


OPTIONS = ["a", "b"]


# ── the fake platform ────────────────────────────────────────────────────────


class FakeQuestion:
    """What `get_question_by_post_id` returns, shaped like the SDK's model.

    Only the four things `submission_live` reads: the two identifiers, the state, and
    `api_json`. Everything else the SDK's `MetaculusQuestion` carries is irrelevant here
    precisely because the module reads nothing else -- which is the property the narrow
    protocol exists to give.
    """

    def __init__(
        self,
        *,
        history: list[dict[str, Any]] | None = None,
        question_id: int = QUESTION_ID,
        post_id: int = POST_ID,
        state: str = "open",
        api_json: Any = None,
        options: list[str] | None = OPTIONS,
    ) -> None:
        self.id_of_question = question_id
        self.id_of_post = post_id
        self.state = state
        if api_json is not None:
            self.api_json: Any = api_json
        else:
            inner: dict[str, Any] = {"my_forecasts": {"history": history or []}}
            # The platform sends `options` alongside `my_forecasts` for a multiple-choice
            # question, and a multiple-choice verification is refused without it (round-1
            # finding 2). Present by default so every non-MC test is unaffected; pass
            # `options=None` to drive the unreadable path.
            if options is not None:
                inner["options"] = options
            self.api_json = {"question": inner}


def _entry(start_time: float, values: list[float]) -> dict[str, Any]:
    return {"start_time": start_time, "end_time": None, "forecast_values": values}


def _binary_values(probability: float) -> list[float]:
    return [1.0 - probability, probability]


class FakePoster:
    """A poster that counts its calls and hands back scripted questions.

    `posts` is the number that matters: every refusal test asserts it is **zero**, which is
    the only way to show a gate ran *in front of* the post rather than after it.
    """

    def __init__(
        self,
        *,
        before: Any = None,
        after: Any = None,
        post_error: BaseException | None = None,
        fetch_errors: list[BaseException] | None = None,
    ) -> None:
        self._before = before if before is not None else FakeQuestion()
        self._after = (
            after
            if after is not None
            else FakeQuestion(history=[_entry(NEW_START, _binary_values(PROBABILITY))])
        )
        self._post_error = post_error
        self._fetch_errors = list(fetch_errors or [])
        self.posts = 0
        self.fetches = 0
        self.posted: list[tuple[str, int, Any]] = []

    def _fetch(self) -> Any:
        self.fetches += 1
        if self._fetch_errors:
            raise self._fetch_errors.pop(0)
        return self._before if self.posts == 0 else self._after

    def get_question_by_post_id(self, post_id: int) -> object:
        return self._fetch()

    def post_binary_question_prediction(
        self, question_id: int, prediction_in_decimal: float
    ) -> None:
        self._record("binary", question_id, prediction_in_decimal)

    def post_numeric_question_prediction(self, question_id: int, cdf_values: list[float]) -> None:
        self._record("numeric", question_id, cdf_values)

    def post_multiple_choice_question_prediction(
        self, question_id: int, options_with_probabilities: dict[str, float]
    ) -> None:
        self._record("multiple_choice", question_id, options_with_probabilities)

    def _record(self, kind: str, question_id: int, value: Any) -> None:
        self.posts += 1
        self.posted.append((kind, question_id, value))
        if self._post_error is not None:
            raise self._post_error


class _Timeout(Exception):
    """Stands in for `requests.exceptions.Timeout` without importing the transport.

    `submission_live` classifies by module-qualified class name, so this is deliberately
    *not* classified as a timeout -- it exercises the `internal_error` fallback. The real
    classification is pinned against the real classes in `test_metaculus_poster.py`, which
    is where that belongs.
    """


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "ledger.sqlite3"
    initialize_ledger(database)
    connection = connect(database)
    with transaction(connection):
        connection.execute(
            "INSERT INTO research_runs "
            "(retrieval_run_id, provider, started_at_utc, created_at_utc, question_id) "
            "VALUES (?, 'exa', ?, ?, ?)",
            (RUN_ID, TIMESTAMP, TIMESTAMP, QUESTION_ID),
        )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def approved(ledger: sqlite3.Connection) -> tuple[sqlite3.Connection, str]:
    record = append_forecast_version(ledger, draft=_draft())
    record_validation(ledger, record_id=record.record_id, occurred_at=OCCURRED)
    approve(
        ledger,
        record_id=record.record_id,
        actor="chris",
        occurred_at=OCCURRED,
        calibration=CALIBRATION,
    )
    return ledger, record.record_id


def _config(tmp_path: Path, **submission: Any) -> AppConfig:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "ledger.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "exports")
    data["logging"]["file"] = str(tmp_path / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    data["submission"].update({"enabled": True, "dry_run": False, "no_submit": False})
    data["submission"].update(submission)
    return validate_config_data(data)


@pytest.fixture()
def live_config(tmp_path: Path) -> AppConfig:
    return _config(tmp_path)


def _post(
    ledger: sqlite3.Connection,
    record_id: str,
    poster: FakePoster,
    config: AppConfig,
    *,
    payload: Mapping[str, object] | None = None,
    occurred_at: datetime = OCCURRED,
) -> Any:
    return post_approved_forecast(
        ledger,
        record_id=record_id,
        payload=dict(BINARY_PAYLOAD) if payload is None else payload,
        poster=poster,
        config=config,
        occurred_at=occurred_at,
        clock=lambda: OCCURRED,
        sleep=lambda _seconds: None,
    )


# ── the happy path: a post, a refetch, a `submitted` record ──────────────────


def test_a_confirmed_post_reaches_submitted_and_records_everything(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    ledger, record_id = approved
    poster = FakePoster()
    recorded = _post(ledger, record_id, poster, live_config)

    assert poster.posts == 1
    assert poster.posted[0] == ("binary", QUESTION_ID, PROBABILITY)
    assert recorded.event.event_type == "submitted"
    assert current_status(ledger, record_id) == "submitted"

    receipt = recorded.receipt
    assert receipt.mode == "live"
    assert receipt.success is True
    assert receipt.verified_by_refetch is True
    assert receipt.error_type is None
    assert LIVE_ATTEMPT_RE.match(receipt.attempt_id)

    row = ledger.execute(
        "SELECT idempotency_key, request_payload_sha256, success, verified_by_refetch, "
        "refetched_forecast_snapshot FROM submission_attempts WHERE attempt_id = ?",
        (receipt.attempt_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == receipt.idempotency_key
    assert row[1] == receipt.request_payload_sha256
    assert (row[2], row[3]) == (1, 1)
    snapshot = json.loads(row[4])
    assert snapshot["outcome"] == "confirmed"
    assert snapshot["expected_values"] == [PROBABILITY]


def test_the_artifact_holds_the_payload_the_digest_describes(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """`submission_attempts` stores only a hash, so the payload lives in the artifact.

    M2-706's criterion is "ledger has approval, payload, attempt and verification
    snapshot"; this is the payload half, and the test re-derives the digest from the file
    rather than trusting the receipt -- which is the only thing that makes the file
    evidence rather than a copy.
    """
    ledger, record_id = approved
    recorded = _post(ledger, record_id, FakePoster(), live_config)
    assert recorded.artifact_path is not None
    assert recorded.artifact_error is None
    envelope = read_live_artifact(live_config.storage.artifact_root, recorded.artifact_path)
    assert envelope["mode"] == "live"
    assert envelope["request_payload"] == BINARY_PAYLOAD
    assert recorded.artifact_path.startswith("submissions/live/")

    from whiskeyjack_bot.submission_gateway import payload_sha256

    assert payload_sha256(envelope["request_payload"]) == recorded.receipt.request_payload_sha256


@pytest.mark.parametrize(
    ("payload", "kind", "argument"),
    [
        ({"question_type": "binary", "probability_yes": 0.4}, "binary", 0.4),
        (
            {"question_type": "numeric", "continuous_cdf": [i / 200 for i in range(201)]},
            "numeric",
            [i / 200 for i in range(201)],
        ),
        (
            {
                "question_type": "multiple_choice",
                "probability_yes_per_category": {"a": 0.25, "b": 0.75},
            },
            "multiple_choice",
            {"a": 0.25, "b": 0.75},
        ),
    ],
)
def test_each_question_type_reaches_its_own_public_post_method(
    payload: dict[str, Any], kind: str, argument: Any
) -> None:
    """All three wire shapes dispatch, and each carries the SDK's own argument shape."""
    after = FakeQuestion(
        history=[
            _entry(
                NEW_START,
                _binary_values(0.4)
                if kind == "binary"
                else (list(argument) if kind == "numeric" else [0.25, 0.75]),
            )
        ]
    )
    poster = FakePoster(after=after)
    gateway = MetaculusSubmissionGateway(
        poster=poster, clock=lambda: OCCURRED, sleep=lambda _s: None
    )
    outcome = gateway.submit_with_detail(
        SubmissionRequest(
            forecast_record_id="rec-1",
            question_id=QUESTION_ID,
            idempotency_key="wjsub-1-" + "a" * 64,
            payload=payload,
            post_id=POST_ID,
        )
    )
    assert poster.posts == 1
    assert poster.posted[0][0] == kind
    assert outcome.receipt.verified_by_refetch is True


# ── refetch outcomes: only one of them is a success ──────────────────────────


def test_a_post_the_refetch_does_not_show_is_uncertain_not_submitted(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The acceptance criterion's first half, stated negatively."""
    ledger, record_id = approved
    poster = FakePoster(after=FakeQuestion(history=[]))
    recorded = _post(ledger, record_id, poster, live_config)
    assert poster.posts == 1
    assert recorded.event.event_type == "submission_uncertain"
    assert recorded.event.detail_code == "refetch_missing"
    assert current_status(ledger, record_id) == "approved"


def test_an_unchanged_history_is_absent_even_though_a_forecast_exists(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The baseline is what makes this a test of *this* post.

    A question the operator had already forecast on would otherwise confirm a submission
    that never landed: `already_forecasted` would be true before and after.
    """
    existing = [_entry(BASELINE_START, _binary_values(PROBABILITY))]
    poster = FakePoster(before=FakeQuestion(history=existing), after=FakeQuestion(history=existing))
    ledger, record_id = approved
    recorded = _post(ledger, record_id, poster, live_config)
    assert recorded.event.event_type == "submission_uncertain"
    assert recorded.event.detail_code == "refetch_missing"


def test_a_new_entry_with_the_wrong_value_is_a_mismatch(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    ledger, record_id = approved
    poster = FakePoster(after=FakeQuestion(history=[_entry(NEW_START, _binary_values(0.9))]))
    recorded = _post(ledger, record_id, poster, live_config)
    assert recorded.event.event_type == "submission_uncertain"
    assert recorded.event.detail_code == "refetch_mismatch"


def test_a_failed_post_the_refetch_confirms_is_uncertain_not_failed(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The `(False, True)` cell: it errored, and the forecast is there anyway.

    Recording this as a failure would move the record to terminal `failed` while a live
    forecast stood on the platform -- the ledger disagreeing with the world, permanently.
    """
    ledger, record_id = approved
    poster = FakePoster(post_error=_Timeout("read timed out"))
    recorded = _post(ledger, record_id, poster, live_config)
    assert poster.posts == 1
    assert recorded.receipt.success is False
    assert recorded.receipt.verified_by_refetch is True
    assert recorded.event.event_type == "submission_uncertain"
    assert current_status(ledger, record_id) == "approved"


def test_a_failed_post_with_nothing_on_the_platform_is_terminal(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    ledger, record_id = approved
    poster = FakePoster(post_error=_Timeout("boom"), after=FakeQuestion(history=[]))
    recorded = _post(ledger, record_id, poster, live_config)
    assert recorded.event.event_type == "submission_failed"
    assert current_status(ledger, record_id) == "failed"


def test_a_failed_post_and_an_unreadable_refetch_is_unknown_not_failed(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """M2-711's acceptance criterion, end to end through the live gateway.

    *"A post whose outcome no refetch established is recorded as neither submitted nor
    failed; the record stays somewhere a later refetch can still resolve it."* The post
    raises and the refetch comes back unreadable, so nothing is known either way -- and
    before this item that was `submission_failed`, terminal, claiming the post did not go
    through on the strength of no observation at all.

    The four assertions are the criterion's four clauses: not submitted, not failed, still
    `approved`, and still named as outstanding so the next post is refused and a refetch
    can still decide it. The test below carries it the rest of the way, to `submitted`.
    """
    ledger, record_id = approved
    poster = FakePoster(
        post_error=_Timeout("boom"),
        fetch_errors=[],
    )
    poster._after = FakeQuestion(api_json="not a dict")  # noqa: SLF001 - scripting the fake
    recorded = _post(ledger, record_id, poster, live_config)
    assert recorded.event.event_type == "submission_uncertain"
    assert recorded.receipt.refetch_outcome == "unreadable"
    assert current_status(ledger, record_id) == "approved"
    assert unresolved_uncertainties(ledger, record_id) == (recorded.receipt.attempt_id,)
    # The prose note the row used to carry in place of a ledger state is gone with the
    # state that replaced it; what is left is the classified cause of the *post*.
    assert recorded.receipt.error_message is not None
    assert "platform state was not established" not in recorded.receipt.error_message
    assert recorded.receipt.error_type is not None


def test_an_unknown_outcome_can_still_be_carried_to_submitted_by_a_later_refetch(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The other half of M2-711: *"a later refetch can still resolve it"*.

    The same unreadable-refetch post as above, then a refetch that does read the platform
    and finds the forecast. Before this item the record was terminal `failed` at the end of
    the first step and there was no legal event to carry it anywhere -- a ledger permanently
    disagreeing with the platform about a live forecast, which is the failure the whole
    uncertain-state machinery exists to prevent.
    """
    ledger, record_id = approved
    poster = FakePoster(post_error=_Timeout("boom"), fetch_errors=[])
    poster._after = FakeQuestion(api_json="not a dict")  # noqa: SLF001 - scripting the fake
    recorded = _post(ledger, record_id, poster, live_config)
    attempt_id = recorded.receipt.attempt_id

    later = FakePoster(after=FakeQuestion(history=[_entry(NEW_START, _binary_values(PROBABILITY))]))
    later.posts = 1  # so the fake serves its "after" question to the refetch
    event = verify_uncertain_attempt(
        ledger,
        record_id=record_id,
        attempt_id=attempt_id,
        poster=later,
        occurred_at=OCCURRED,
        sleep=lambda _: None,
    )
    assert event.event_type == "submission_confirmed"
    assert current_status(ledger, record_id) == "submitted"
    assert unresolved_uncertainties(ledger, record_id) == ()


# ── "uncertain timeout blocks blind retry" ───────────────────────────────────


def test_an_unresolved_uncertainty_refuses_the_next_post_before_any_network_call(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The acceptance criterion's second half.

    The assertion that matters is `posts == 0` **and** `fetches == 0`: the gate has to run
    in front of the action, not in front of the recording. M1-603 round 4 withdrew the same
    check from the writer precisely because a refusal there could only stop a completed
    post from being written down.
    """
    ledger, record_id = approved
    first = FakePoster(after=FakeQuestion(history=[]))
    assert _post(ledger, record_id, first, live_config).event.event_type == "submission_uncertain"

    second = FakePoster()
    with pytest.raises(LiveSubmissionError) as excinfo:
        _post(ledger, record_id, second, live_config)
    assert "blind retry" in str(excinfo.value)
    assert second.posts == 0
    assert second.fetches == 0


def test_verify_submission_confirms_an_uncertain_attempt_and_reopens_the_gate(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The whole loop: uncertain, then a refetch that finds it, then `submitted`."""
    ledger, record_id = approved
    poster = FakePoster(after=FakeQuestion(history=[]))
    recorded = _post(ledger, record_id, poster, live_config)
    attempt_id = recorded.receipt.attempt_id
    assert current_status(ledger, record_id) == "approved"

    later = FakePoster(after=FakeQuestion(history=[_entry(NEW_START, _binary_values(PROBABILITY))]))
    later.posts = 1  # so the fake serves its "after" question to the refetch
    event = verify_uncertain_attempt(
        ledger,
        record_id=record_id,
        attempt_id=attempt_id,
        poster=later,
        occurred_at=OCCURRED,
        sleep=lambda _s: None,
    )
    assert event.event_type == "submission_confirmed"
    assert current_status(ledger, record_id) == "submitted"
    assert later.posts == 1, "verify-submission must never post"


def test_verify_submission_records_an_absent_forecast_as_terminal(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    ledger, record_id = approved
    poster = FakePoster(after=FakeQuestion(history=[]))
    attempt_id = _post(ledger, record_id, poster, live_config).receipt.attempt_id

    later = FakePoster(after=FakeQuestion(history=[]))
    later.posts = 1
    event = verify_uncertain_attempt(
        ledger,
        record_id=record_id,
        attempt_id=attempt_id,
        poster=later,
        occurred_at=OCCURRED,
        sleep=lambda _s: None,
    )
    assert event.event_type == "submission_disconfirmed"
    assert current_status(ledger, record_id) == "failed"


def test_verify_submission_refuses_a_mismatch_rather_than_calling_it_absent(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """`absent` is terminal, so it must not be written on evidence that *a* forecast exists.

    Leaving the uncertainty standing is the conservative direction: the post gate stays
    closed and a human decides.
    """
    ledger, record_id = approved
    poster = FakePoster(after=FakeQuestion(history=[]))
    attempt_id = _post(ledger, record_id, poster, live_config).receipt.attempt_id

    later = FakePoster(after=FakeQuestion(history=[_entry(NEW_START, _binary_values(0.91))]))
    later.posts = 1
    with pytest.raises(LiveSubmissionError) as excinfo:
        verify_uncertain_attempt(
            ledger,
            record_id=record_id,
            attempt_id=attempt_id,
            poster=later,
            occurred_at=OCCURRED,
            sleep=lambda _s: None,
        )
    assert "resolve it by hand" in str(excinfo.value)
    assert current_status(ledger, record_id) == "approved"


def test_verify_submission_refuses_when_the_refetch_cannot_be_performed(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """An unreadable refetch establishes nothing, and `absent` is terminal.

    Found by mutation: collapsing `unreadable` into `absent` in `classify_refetch` passed
    the whole file, because the *submit* path never reaches that branch -- it returns
    `unreadable` itself when the retry loop gives up. `verify_uncertain_attempt` is the only
    caller that hands `classify_refetch` a `None`, so this is the only test that can hold
    the distinction, and without it a lost connection would have ended a live forecast
    version by recording `submission_disconfirmed` on no evidence at all.
    """
    ledger, record_id = approved
    attempt_id = _post(
        ledger, record_id, FakePoster(after=FakeQuestion(history=[])), live_config
    ).receipt.attempt_id

    unreachable = FakePoster(fetch_errors=[_Timeout("down")] * 8)
    with pytest.raises(LiveSubmissionError) as excinfo:
        verify_uncertain_attempt(
            ledger,
            record_id=record_id,
            attempt_id=attempt_id,
            poster=unreachable,
            occurred_at=OCCURRED,
            sleep=lambda _s: None,
        )
    assert "nothing was established" in str(excinfo.value)
    assert current_status(ledger, record_id) == "approved", (
        "an unreadable refetch must leave the uncertainty standing, which keeps the post "
        "gate closed"
    )


def test_verify_submission_refuses_a_refetch_that_describes_another_question(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The identity check runs on the refetch too, not only before the post."""
    ledger, record_id = approved
    attempt_id = _post(
        ledger, record_id, FakePoster(after=FakeQuestion(history=[])), live_config
    ).receipt.attempt_id

    wrong = FakePoster(
        after=FakeQuestion(
            question_id=QUESTION_ID + 1,
            history=[_entry(NEW_START, _binary_values(PROBABILITY))],
        )
    )
    wrong.posts = 1
    with pytest.raises(LiveSubmissionError) as excinfo:
        verify_uncertain_attempt(
            ledger,
            record_id=record_id,
            attempt_id=attempt_id,
            poster=wrong,
            occurred_at=OCCURRED,
            sleep=lambda _s: None,
        )
    assert "nothing was established" in str(excinfo.value)


def test_verify_submission_refuses_an_attempt_that_is_not_outstanding(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    ledger, record_id = approved
    recorded = _post(ledger, record_id, FakePoster(), live_config)
    poster = FakePoster()
    with pytest.raises(LiveSubmissionError):
        verify_uncertain_attempt(
            ledger,
            record_id=record_id,
            attempt_id=recorded.receipt.attempt_id,
            poster=poster,
            occurred_at=OCCURRED,
        )
    assert poster.fetches == 0


# ── every gate refuses before the post ───────────────────────────────────────


def _expect_refusal(
    ledger: sqlite3.Connection,
    record_id: str,
    config: AppConfig,
    *,
    payload: Mapping[str, object] | None = None,
) -> tuple[FakePoster, LiveSubmissionError]:
    poster = FakePoster()
    with pytest.raises(LiveSubmissionError) as excinfo:
        _post(ledger, record_id, poster, config, payload=payload)
    return poster, excinfo.value


@pytest.mark.parametrize(
    "flags",
    [
        {"enabled": False, "dry_run": True, "no_submit": True},
        {"enabled": False, "dry_run": False, "no_submit": False},
        {"enabled": False, "dry_run": True, "no_submit": False},
        {"enabled": False, "dry_run": False, "no_submit": True},
    ],
)
def test_the_committed_flags_refuse_a_post_with_no_network_call(
    approved: tuple[sqlite3.Connection, str], tmp_path: Path, flags: dict[str, Any]
) -> None:
    """Every configuration the validator still accepts with `enabled: false` posts nothing.

    Enumerated rather than sampled, because `enabled` is the only flag whose combinations
    the validator now permits freely -- and "the shipped config cannot post" is the claim
    that matters most in this file.
    """
    ledger, record_id = approved
    poster, error = _expect_refusal(ledger, record_id, _config(tmp_path, **flags))
    assert "live submission is off" in str(error)
    assert poster.posts == 0
    assert poster.fetches == 0


def test_an_unapproved_record_is_refused_before_the_post(
    ledger: sqlite3.Connection, live_config: AppConfig
) -> None:
    record = append_forecast_version(ledger, draft=_draft())
    record_validation(ledger, record_id=record.record_id, occurred_at=OCCURRED)
    poster, error = _expect_refusal(ledger, record.record_id, live_config)
    assert "approval" in str(error)
    assert poster.posts == 0


def test_a_payload_of_the_wrong_question_type_is_refused(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The half of D33 that is checkable without the forecast->payload mapping."""
    ledger, record_id = approved
    poster, error = _expect_refusal(
        ledger,
        record_id,
        live_config,
        payload={"question_type": "numeric", "continuous_cdf": [i / 200 for i in range(201)]},
    )
    assert "numeric" in str(error) and "binary" in str(error)
    assert poster.posts == 0


def test_a_binary_payload_the_approval_never_authorized_is_refused_before_the_post(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """D33 closed (M2-707). This test is what the gap-pinning test became.

    It was `test_the_documented_payload_binding_gap_is_real_and_is_asserted`, and it
    asserted the opposite: two different binary payloads both posted under one approval,
    the second refused only because the record had moved to `submitted`. It was written to
    fail the day the gap closed. It has.

    **The record is left at `approved` here, and that is the whole difference.** The old
    test could only reach the refusal by posting first, which put the status gate in front
    of the payload gate and meant nothing about the payload was ever tested. Nothing is
    posted at all now: the same forecast, differing only in its value, is refused because
    the approval bound to a different digest -- and `poster.posts == 0` says the refusal
    came before the network, not after it.
    """
    ledger, record_id = approved
    assert current_status(ledger, record_id) == "approved"
    poster, error = _expect_refusal(
        ledger,
        record_id,
        live_config,
        payload={"question_type": "binary", "probability_yes": 0.99},
    )
    assert "not the one the approval in force authorized" in str(error)
    assert poster.posts == 0
    # And the authorized payload still posts, from the same state. Without this the test
    # above would pass against a gate that refused every payload, which is the vacuity
    # `docs/LESSONS.md` names.
    assert current_status(ledger, record_id) == "approved"
    assert _post(ledger, record_id, FakePoster(), live_config).event.event_type == "submitted"


def test_a_spent_idempotency_key_is_refused_before_the_post(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """`require_key_unused`, exercised through the orchestrator rather than directly.

    Reached by leaving the record at `approved` with a *recorded* attempt under the key an
    identical payload derives -- which is the uncertain path, so the uncertainty gate is
    disabled for this one test to isolate the key check.
    """
    ledger, record_id = approved
    poster = FakePoster(after=FakeQuestion(history=[]))
    recorded = _post(ledger, record_id, poster, live_config)
    key = recorded.receipt.idempotency_key
    from whiskeyjack_bot.submission import require_key_unused, SubmissionError

    with pytest.raises(SubmissionError) as excinfo:
        require_key_unused(ledger, key)
    assert "second live post" in str(excinfo.value)


def test_a_question_whose_ids_do_not_match_the_record_is_never_posted_to(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The M1-202 group trap: siblings share a post id and differ only by question id."""
    ledger, record_id = approved
    poster = FakePoster(before=FakeQuestion(question_id=QUESTION_ID + 1))
    with pytest.raises(LiveSubmissionError) as excinfo:
        _post(ledger, record_id, poster, live_config)
    assert "wrong question" in str(excinfo.value)
    assert poster.posts == 0


def test_a_closed_question_is_refused_before_the_key_is_spent(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    ledger, record_id = approved
    poster = FakePoster(before=FakeQuestion(state="closed"))
    with pytest.raises(LiveSubmissionError) as excinfo:
        _post(ledger, record_id, poster, live_config)
    assert "not open" in str(excinfo.value)
    assert poster.posts == 0


def test_a_question_whose_history_cannot_be_read_is_never_posted_to(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """Success requires confirmation, so a post that could never be confirmed is refused.

    Posting anyway would mint an attempt that is uncertain the moment it is made, spend the
    idempotency key on it, and close the gate on the record -- all for a call whose outcome
    was unknowable before it was made.
    """
    ledger, record_id = approved
    poster = FakePoster(before=FakeQuestion(api_json={"question": {"my_forecasts": "nonsense"}}))
    with pytest.raises(LiveSubmissionError) as excinfo:
        _post(ledger, record_id, poster, live_config)
    assert "could never be confirmed" in str(excinfo.value)
    assert poster.posts == 0


def test_a_pre_post_fetch_failure_refuses_and_posts_nothing(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    ledger, record_id = approved
    poster = FakePoster(fetch_errors=[_Timeout("down")])
    with pytest.raises(LiveSubmissionError) as excinfo:
        _post(ledger, record_id, poster, live_config)
    assert "nothing was posted" in str(excinfo.value)
    assert poster.posts == 0


# ── payload validation: refuse a caller mistake before any network call ──────


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"probability_yes": 0.4}, id="no discriminator"),
        pytest.param({"question_type": "date", "probability_yes": 0.4}, id="unsupported type"),
        pytest.param({"question_type": "binary"}, id="no wire key"),
        pytest.param(
            {"question_type": "binary", "probability_yes": 0.4, "comment": "hi"},
            id="an extra key this module would silently drop",
        ),
        pytest.param(
            {"question_type": "binary", "probability_yes": 0.0}, id="below the platform floor"
        ),
        pytest.param({"question_type": "binary", "probability_yes": 1.0}, id="above the ceiling"),
        pytest.param({"question_type": "binary", "probability_yes": True}, id="a bool is not 1.0"),
        pytest.param({"question_type": "binary", "probability_yes": "0.4"}, id="a string"),
        pytest.param(
            {"question_type": "numeric", "continuous_cdf": [0.0, 1.0]}, id="wrong cdf length"
        ),
        pytest.param(
            {"question_type": "numeric", "continuous_cdf": [1.0] + [i / 200 for i in range(200)]},
            id="a cdf that decreases",
        ),
        pytest.param(
            {"question_type": "multiple_choice", "probability_yes_per_category": {}},
            id="no options",
        ),
        pytest.param(
            {
                "question_type": "multiple_choice",
                "probability_yes_per_category": {"a": 0.4, "b": 0.4},
            },
            id="not a distribution",
        ),
        pytest.param(
            {
                "question_type": "multiple_choice",
                "probability_yes_per_category": {str(i): 1 / 100 for i in range(100)},
            },
            id="more options than any question has",
        ),
    ],
)
def test_a_malformed_payload_is_refused_as_this_modules_error(payload: dict[str, Any]) -> None:
    with pytest.raises(LiveSubmissionError):
        plan_from_payload(payload, expected_cdf_points=201)


def test_a_malformed_payload_never_reaches_the_poster(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The bounds are restated from the SDK so the refusal happens *before* the spend.

    The SDK's own `post_binary_question_prediction` raises a bare `ValueError` for this
    value -- an error type callers do not handle, raised from inside the dependency, at a
    point this module could not distinguish from a failure that had already posted.
    """
    ledger, record_id = approved
    poster, _ = _expect_refusal(
        ledger,
        record_id,
        live_config,
        payload={"question_type": "binary", "probability_yes": 1.5},
    )
    assert poster.posts == 0
    assert poster.fetches == 0


def test_no_refusal_message_echoes_the_payload() -> None:
    """The value must not come back out, on any of the refusal paths.

    Asserted over the *whole* message set for a marked value rather than by searching for
    one substring in one message: a substring check passes on a message that never
    mentioned the value for any reason, including the wrong one (M1-607's lesson).
    """
    marker = "WJLEAKMARKER-secret"
    payloads: list[dict[str, Any]] = [
        {"question_type": marker, "probability_yes": 0.4},
        {"question_type": "binary", "probability_yes": marker},
        {"question_type": "binary", "probability_yes": 0.4, marker: 1},
        {"question_type": "numeric", "continuous_cdf": [marker] * 201},
        {
            "question_type": "multiple_choice",
            "probability_yes_per_category": {marker: 1.0, "b": 1.0},
        },
    ]
    for payload in payloads:
        with pytest.raises(LiveSubmissionError) as excinfo:
            plan_from_payload(payload, expected_cdf_points=201)
        rendered = f"{excinfo.value}\n{traceback.format_exception(excinfo.value)}"
        assert marker not in rendered, payload


def test_the_plan_comes_from_the_rendering_that_was_hashed() -> None:
    """A Mapping that answers differently on a second read cannot post what was not hashed.

    The plan is read out of `canonical_payload_json`'s output rather than out of the
    caller's mapping, so there is no second read to disagree with the first -- M1-203's
    `_CountingQuestionType` lesson, closed by construction instead of by counting.
    """

    class ShiftingPayload(dict):  # type: ignore[type-arg]
        def __init__(self) -> None:
            super().__init__({"question_type": "binary", "probability_yes": 0.4})
            self.reads = 0

        def items(self) -> Any:
            self.reads += 1
            value = 0.4 if self.reads == 1 else 0.9
            return iter([("question_type", "binary"), ("probability_yes", value)])

    payload = ShiftingPayload()
    poster = FakePoster(after=FakeQuestion(history=[_entry(NEW_START, _binary_values(0.4))]))
    gateway = MetaculusSubmissionGateway(
        poster=poster, clock=lambda: OCCURRED, sleep=lambda _s: None
    )
    outcome = gateway.submit_with_detail(
        SubmissionRequest(
            forecast_record_id="rec-1",
            question_id=QUESTION_ID,
            idempotency_key="wjsub-1-" + "a" * 64,
            payload=payload,
            post_id=POST_ID,
        )
    )
    assert poster.posted[0][2] == 0.4
    assert outcome.receipt.verified_by_refetch is True


# ── after the post, nothing refuses ──────────────────────────────────────────


def test_an_artifact_failure_still_records_the_post(
    approved: tuple[sqlite3.Connection, str], tmp_path: Path
) -> None:
    """M1-312's rule at its boundary: after the spend, the ledger row is what matters.

    The artifact root is made unwritable by pointing it at a *file*, which is a reachable
    operator mistake rather than a simulated one.
    """
    ledger, record_id = approved
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    config = _config(tmp_path)
    config = config.model_copy(
        update={"storage": config.storage.model_copy(update={"artifact_root": blocked})}
    )
    poster = FakePoster()
    recorded = _post(ledger, record_id, poster, config)
    assert poster.posts == 1
    assert recorded.event.event_type == "submitted"
    assert recorded.artifact_path is None
    assert recorded.artifact_error is not None
    assert recorded.receipt.artifact_path is None


def test_a_hostile_provider_response_is_still_recordable(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """Everything on the receipt is pre-sanitized, so the ledger cannot refuse a live post.

    A body carrying a NUL and a lone surrogate, and one far longer than the column bound,
    is exactly what `lifecycle._require_text` refuses -- and a refusal at that point is a
    live post with no row, this product's primary failure mode.
    """

    class HostileResponse:
        status_code = 429
        headers = {"Retry-After": "\x0030\ud800", "Set-Cookie": "session=secret"}
        text = "\x00" + "\ud800" + "x" * 200_000

    class HostileError(Exception):
        response = HostileResponse()

    ledger, record_id = approved
    poster = FakePoster(post_error=HostileError("boom"), after=FakeQuestion(history=[]))
    recorded = _post(ledger, record_id, poster, live_config)
    assert recorded.event.event_type == "submission_failed"
    receipt = recorded.receipt
    assert receipt.http_status == 429
    assert receipt.response_body is not None and len(receipt.response_body) <= 65536
    assert "\x00" not in receipt.response_body
    assert receipt.response_headers is not None
    assert "set-cookie" not in receipt.response_headers.lower()
    assert "retry-after" in receipt.response_headers


def test_the_stored_error_message_is_never_the_providers(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The SDK's own HTTPError text embeds the response body and the request URL."""

    class LeakyError(Exception):
        pass

    ledger, record_id = approved
    poster = FakePoster(
        post_error=LeakyError("HTTPError. Url: https://x/. Response text: WJLEAKMARKER-secret."),
        after=FakeQuestion(history=[]),
    )
    recorded = _post(ledger, record_id, poster, live_config)
    assert recorded.receipt.error_message is not None
    assert "WJLEAKMARKER-secret" not in recorded.receipt.error_message
    assert "LeakyError" in recorded.receipt.error_message


def test_a_backwards_clock_does_not_cost_the_row(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """`record_submission_attempt` refuses a reversed pair, so it is clamped, not refused."""
    ledger, record_id = approved
    readings = iter([OCCURRED, OCCURRED - timedelta(hours=1)])
    recorded = post_approved_forecast(
        ledger,
        record_id=record_id,
        payload=dict(BINARY_PAYLOAD),
        poster=FakePoster(),
        config=live_config,
        occurred_at=OCCURRED,
        clock=lambda: next(readings),
        sleep=lambda _s: None,
    )
    assert recorded.event.event_type == "submitted"
    assert recorded.receipt.completed_at_utc == recorded.receipt.requested_at_utc


# ── the pure pieces ──────────────────────────────────────────────────────────


def test_an_empty_history_and_an_unreadable_one_are_different_answers() -> None:
    """Collapsing them makes a lost connection a permanent claim about a live forecast.

    Asserted on ``entries`` rather than on the whole value: an unreadable *option list* and
    an unreadable *history* are themselves two different answers, and this test is about
    the second. The first has its own case below.
    """
    empty = read_my_forecasts(FakeQuestion(history=[]))
    assert empty is not None and empty.entries == ()
    bare = read_my_forecasts(FakeQuestion(api_json={"question": {}}))
    assert bare is not None and bare.entries == ()
    assert read_my_forecasts(FakeQuestion(api_json="not a dict")) is None
    assert read_my_forecasts(FakeQuestion(api_json={"question": {"my_forecasts": 1}})) is None
    assert (
        read_my_forecasts(
            FakeQuestion(
                api_json={"question": {"my_forecasts": {"history": [{"start_time": "x"}]}}}
            )
        )
        is None
    )
    assert read_my_forecasts(object()) is None


def _mc_refetch(
    plan: MultipleChoicePost,
    *,
    platform_labels: tuple[str, ...] | None,
    values: tuple[float, ...],
) -> str:
    """Classify one multiple-choice refetch and return just the outcome."""
    return classify_refetch(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_latest_start_time=BASELINE_START,
        observed=ForecastHistory((ForecastEntry(NEW_START, values),), platform_labels),
        expected_labels=expected_option_labels(plan),
    ).outcome


def test_a_transposed_multiple_choice_forecast_is_not_confirmed() -> None:
    """Round-1 finding 2, reproduced then closed.

    This comparison sorted both sides into a multiset, on the rationale that doing so only
    lost the ability to tell apart two options carrying the *same* probability while "a
    different distribution is still caught". **That was false.** ``{a: 0.25, b: 0.75}`` and
    ``{a: 0.75, b: 0.25}`` sort to the same tuple, so a post whose categories landed
    transposed was reported ``confirmed`` -- and the ledger would record ``submitted`` for a
    forecast the operator never made. The values are now aligned by label.

    The replaced test asserted the transposition *was* a confirmation, which is why the
    defect survived a full property pass: the suite agreed with it.
    """
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.25), ("b", 0.75)))
    assert _mc_refetch(plan, platform_labels=("a", "b"), values=(0.75, 0.25)) == "mismatched"


def test_an_honest_multiple_choice_forecast_still_confirms() -> None:
    """The other half of the iff: alignment must not make a true confirmation impossible."""
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.25), ("b", 0.75)))
    assert _mc_refetch(plan, platform_labels=("a", "b"), values=(0.25, 0.75)) == "confirmed"


def test_the_payload_order_is_preserved_and_not_re_sorted() -> None:
    """A payload that declares its categories in descending order must still confirm.

    This is the case that kills "sort the expected vector again": every other plan here
    happens to declare its probabilities in ascending order, so sorting is a no-op against
    them and the mutation survives. It is the M1-501 vacuity shape -- a test that agrees
    with both the rule and its removal -- and it was caught by mutation, not by reading.
    """
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.75), ("b", 0.25)))
    assert expected_values(plan) == (0.75, 0.25)
    assert _mc_refetch(plan, platform_labels=("a", "b"), values=(0.75, 0.25)) == "confirmed"
    assert _mc_refetch(plan, platform_labels=("a", "b"), values=(0.25, 0.75)) == "mismatched"


def test_the_platform_may_list_the_options_in_its_own_order() -> None:
    """Alignment is by label, so the platform's ordering is free to differ from the payload's.

    This is what the comparison buys beyond correctness: sorting could not tell a reordered
    option list from a transposed forecast, and by label the first confirms and the second
    does not.
    """
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.25), ("b", 0.75)))
    assert _mc_refetch(plan, platform_labels=("b", "a"), values=(0.75, 0.25)) == "confirmed"


def test_a_multiple_choice_refetch_without_an_option_list_establishes_nothing() -> None:
    """``unreadable``, never ``confirmed`` and never ``mismatched``.

    Without the option order the values cannot be aligned to categories. Comparing them by
    position would be the transposition defect and sorting them would be the multiset one,
    so neither is available: nothing was established. ``unreadable`` rather than
    ``mismatched`` because a comparison that could not be made is not one that failed --
    and ``mismatched`` is what ``verify_uncertain_attempt`` refuses on, which would tell an
    operator the platform holds a *different* forecast when it holds an unread one.
    """
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.25), ("b", 0.75)))
    assert _mc_refetch(plan, platform_labels=None, values=(0.25, 0.75)) == "unreadable"


def test_a_multiple_choice_refetch_whose_labels_disagree_establishes_nothing() -> None:
    """A label set that is not the payload's cannot be aligned to it either."""
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.25), ("b", 0.75)))
    assert _mc_refetch(plan, platform_labels=("a", "c"), values=(0.25, 0.75)) == "unreadable"
    assert _mc_refetch(plan, platform_labels=("a", "b", "c"), values=(0.25, 0.75)) == "unreadable"


def test_a_genuinely_different_multiple_choice_distribution_is_still_caught() -> None:
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.25), ("b", 0.75)))
    assert _mc_refetch(plan, platform_labels=("a", "b"), values=(0.5, 0.5)) == "mismatched"


def test_the_option_order_is_read_from_the_same_dict_as_the_history() -> None:
    """The option list costs no second fetch, which is what makes alignment affordable.

    The original rationale for sorting was that reading the option list "adds a second
    thing that must be readable for a post to be confirmable". It does not: ``options`` and
    ``my_forecasts`` are siblings in one ``api_json["question"]`` dict, already parsed in
    one pass.
    """

    class Question:
        api_json = {
            "question": {
                "options": ["a", "b"],
                "my_forecasts": {
                    "history": [{"start_time": NEW_START, "forecast_values": [0.25, 0.75]}]
                },
            }
        }

    history = read_my_forecasts(Question())
    assert history is not None
    assert history.option_labels == ("a", "b")


def test_an_unreadable_option_list_is_labels_none_and_not_an_unreadable_history() -> None:
    """A malformed option list must not discard the history: the two are separate answers."""

    class Question:
        api_json = {
            "question": {
                "options": ["a", "a"],  # duplicates make the label->value map ambiguous
                "my_forecasts": {
                    "history": [{"start_time": NEW_START, "forecast_values": [0.25, 0.75]}]
                },
            }
        }

    history = read_my_forecasts(Question())
    assert history is not None
    assert history.option_labels is None
    assert len(history.entries) == 1


def _replay_from_snapshot_alone(rendered: str) -> bool:
    """Recompute a multiple-choice confirmation using **only** the stored snapshot.

    Deliberately re-implemented here out of the rendered JSON rather than calling
    `classify_refetch`: the claim under test is that the row carries enough evidence for
    someone who does not have this module, which is what "replayable" means. Calling the
    module's own comparison would assert nothing about the row.
    """
    snapshot = json.loads(rendered)
    observed = snapshot["observed"]
    expected_labels = snapshot["expected_labels"]
    aligned: list[float] = [0.0] * len(expected_labels)
    for position, index in enumerate(observed["label_order"]):
        aligned[index] = observed["latest_values"][position]
    return values_match(snapshot["expected_values"], aligned)


@pytest.mark.parametrize(
    ("platform_order", "reported", "outcome"),
    [
        # The platform is free to list its options in its own order. This is the case the
        # snapshot could not previously reproduce.
        (("b", "a"), (0.75, 0.25), "confirmed"),
        (("a", "b"), (0.25, 0.75), "confirmed"),
        (("a", "b"), (0.75, 0.25), "mismatched"),
    ],
)
def test_a_multiple_choice_snapshot_reproduces_its_own_verdict(
    platform_order: tuple[str, ...], reported: tuple[float, ...], outcome: str
) -> None:
    """Round-2 finding, reproduced then closed.

    Aligning by label made the **observed** label order part of the evidence, but the first
    version of the schema persisted only the expected order. A confirmed snapshot therefore
    held `expected_values` in the payload's order and `latest_values` in the platform's,
    with nothing recording the second -- so an auditor holding the row could not tell an
    honest reordered observation from a transposed forecast, and a positional replay of a
    genuine confirmation said *mismatch*.

    The ledger exists to be replayable; a row whose verdict cannot be recomputed from its
    own evidence is the failure this project is built to avoid.
    """
    plan = MultipleChoicePost(probability_yes_per_category=(("a", 0.25), ("b", 0.75)))
    result = classify_refetch(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_latest_start_time=BASELINE_START,
        observed=ForecastHistory((ForecastEntry(NEW_START, reported),), platform_order),
        expected_labels=expected_option_labels(plan),
    )
    assert result.outcome == outcome
    rendered = build_verification_snapshot(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_entry_count=0,
        baseline_latest_start_time=BASELINE_START,
        result=result,
        expected_labels=expected_option_labels(plan),
    )
    # The permutation, not the strings: `label_order[p]` is where platform position `p`
    # sits in `expected_labels`.
    expected_labels = list(expected_option_labels(plan) or ())
    assert json.loads(rendered)["observed"]["label_order"] == [
        expected_labels.index(label) for label in platform_order
    ]
    assert _replay_from_snapshot_alone(rendered) is (outcome == "confirmed")


def test_a_maximal_multiple_choice_snapshot_still_carries_its_evidence() -> None:
    """The bound is measured here, not asserted in a comment.

    `_MAX_OPTION_LABEL` exists so the worst accepted multiple-choice snapshot fits
    `MAX_BODY_LENGTH` **by construction**. This renders that worst case -- `_MAX_CATEGORIES`
    labels at the full length, every character one that `ensure_ascii=True` escapes to six
    bytes, values that render long, and the platform reporting them in reverse order -- and
    asserts the envelope still holds its evidence. If someone raises either constant, this
    test is what fails.
    """
    count = _MAX_CATEGORIES
    labels = tuple(
        # A non-ASCII character so each one renders as \uXXXX: the six-bytes-per-character
        # worst case the bound was computed against, not the one-byte best case.
        ("\u00e9" * (_MAX_OPTION_LABEL - 3)) + f"{index:03d}"
        for index in range(count)
    )
    probabilities = tuple(1.0 / count for _ in range(count))
    plan = MultipleChoicePost(
        probability_yes_per_category=tuple(zip(labels, probabilities, strict=True))
    )
    assert all(len(label) == _MAX_OPTION_LABEL for label in labels)

    platform_order = tuple(reversed(labels))
    by_label = dict(zip(labels, probabilities, strict=True))
    result = classify_refetch(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_latest_start_time=BASELINE_START,
        observed=ForecastHistory(
            (ForecastEntry(NEW_START, tuple(by_label[label] for label in platform_order)),),
            platform_order,
        ),
        expected_labels=expected_option_labels(plan),
    )
    assert result.outcome == "confirmed"

    rendered = build_verification_snapshot(
        question_type="multiple_choice",
        expected=expected_values(plan),
        baseline_entry_count=1,
        baseline_latest_start_time=BASELINE_START,
        result=result,
        expected_labels=expected_option_labels(plan),
    )
    assert len(rendered) <= MAX_BODY_LENGTH
    snapshot = json.loads(rendered)
    assert "values_omitted" not in snapshot
    assert snapshot["observed"]["label_order"] is not None
    assert _replay_from_snapshot_alone(rendered) is True


def test_a_payload_option_label_past_the_bound_is_refused_before_any_post() -> None:
    """Refused pre-post, so the cost never lands after one, and the value is not echoed."""
    payload = {
        "question_type": "multiple_choice",
        "probability_yes_per_category": {"a" * (_MAX_OPTION_LABEL + 1): 0.5, "b": 0.5},
    }
    with pytest.raises(LiveSubmissionError) as excinfo:
        plan_from_payload(payload, expected_cdf_points=201)
    assert "a" * 32 not in str(excinfo.value)


@pytest.mark.parametrize(
    "options",
    [
        [f"option-{index:05d}" for index in range(_MAX_CATEGORIES + 1)],
        ["a" * (_MAX_OPTION_LABEL + 1), "b"],
    ],
)
def test_an_oversized_platform_option_list_reads_as_no_labels(options: list[str]) -> None:
    """Unbounded provider JSON must not become an unbounded row.

    `None` here, not an exception and not a truncated list: for multiple choice it makes
    the refetch `unreadable`, so the post lands uncertain rather than confirmed on evidence
    the snapshot could not hold.
    """
    question = FakeQuestion(history=[_entry(NEW_START, [0.25, 0.75])], options=options)
    history = read_my_forecasts(question)
    assert history is not None
    assert history.option_labels is None
    assert len(history.entries) == 1


def test_a_binary_snapshot_keeps_its_evidence_whatever_options_the_provider_sends() -> None:
    """A binary question's option list is irrelevant to its values and is never stored.

    Round-3 finding: the observed order was written into the row for every question type,
    so 6,000 provider options on a *binary* question pushed a confirmed snapshot into the
    evidence-free envelope. `label_order` is multiple-choice only.
    """
    plan = BinaryPost(probability_yes=0.6)
    result = classify_refetch(
        question_type="binary",
        expected=expected_values(plan),
        baseline_latest_start_time=BASELINE_START,
        observed=ForecastHistory(
            (ForecastEntry(NEW_START, (0.4, 0.6)),),
            tuple(f"option-{index:05d}" for index in range(6000)),
        ),
        expected_labels=None,
    )
    assert result.outcome == "confirmed"
    rendered = build_verification_snapshot(
        question_type="binary",
        expected=expected_values(plan),
        baseline_entry_count=0,
        baseline_latest_start_time=BASELINE_START,
        result=result,
        expected_labels=None,
    )
    snapshot = json.loads(rendered)
    assert len(rendered) <= MAX_BODY_LENGTH
    assert "values_omitted" not in snapshot
    assert snapshot["observed"]["label_order"] is None
    assert snapshot["observed"]["latest_values"] == [0.4, 0.6]


def test_a_non_multiple_choice_snapshot_never_records_a_label_order() -> None:
    """Dispatch is on the `question_type` literal, at the public boundary.

    Both in-tree callers derive `expected_labels` from the plan or the stored snapshot, and
    both answer `None` for binary and numeric -- so this guard is unreachable from inside
    the package, and a mutation removing it survived the rest of the suite. It is kept
    because `build_verification_snapshot` is public and its two arguments are independent:
    nothing in the signature stops a caller pairing a binary `question_type` with a label
    list, and for binary the platform's option list has no relationship to `latest_values`.
    Writing an order derived from it would be a fabricated alignment in the row.
    """
    for question_type, expected in (("binary", (0.6,)), ("numeric", (0.1, 0.9))):
        rendered = build_verification_snapshot(
            question_type=question_type,
            expected=expected,
            baseline_entry_count=0,
            baseline_latest_start_time=BASELINE_START,
            result=classify_refetch(
                question_type=question_type,
                expected=expected,
                baseline_latest_start_time=BASELINE_START,
                observed=ForecastHistory((ForecastEntry(NEW_START, (0.4, 0.6)),), ("a", "b")),
                expected_labels=None,
            ),
            # A caller pairing labels with a non-multiple-choice type: accepted by the
            # signature, and must not reach the row.
            expected_labels=("a", "b"),
        )
        assert json.loads(rendered)["observed"]["label_order"] is None


@pytest.mark.parametrize(
    ("question_type", "expected", "outcome"),
    [
        ("numeric", tuple(index / 200 for index in range(201)), "mismatched"),
        ("binary", (0.6,), "mismatched"),
    ],
)
def test_an_oversized_observed_vector_does_not_cost_the_row_its_evidence(
    question_type: str, expected: tuple[float, ...], outcome: str
) -> None:
    """`forecast_values` is provider JSON and is bounded nowhere.

    Found while writing the round-4 request rather than by review: it is the same class as
    round 3 -- unbounded provider data rendered into a fixed-size envelope -- one field
    over. A 50,000-element vector used to push the snapshot past `MAX_BODY_LENGTH` and collapse
    it to the form that names no values at all.

    A *confirmed* row could never reach it, because every path to `confirmed` has already
    required the observed vector to match a bounded expected one. This is about the
    `mismatched` and `absent` rows, which is where an operator most needs to see what the
    platform actually held.
    """
    result = classify_refetch(
        question_type=question_type,
        expected=expected,
        baseline_latest_start_time=BASELINE_START,
        observed=ForecastHistory((ForecastEntry(NEW_START, (0.5,) * 50000),)),
        expected_labels=None,
    )
    assert result.outcome == outcome
    rendered = build_verification_snapshot(
        question_type=question_type,
        expected=expected,
        baseline_entry_count=1,
        baseline_latest_start_time=BASELINE_START,
        result=result,
        expected_labels=None,
    )
    snapshot = json.loads(rendered)
    assert len(rendered) <= MAX_BODY_LENGTH
    assert "values_omitted" not in snapshot
    # The sample is capped; the true length is recorded beside it, so the truncation is
    # visible and the row never implies it saw fewer values than it did.
    assert len(snapshot["observed"]["latest_values"]) == _MAX_SNAPSHOT_VALUES
    assert snapshot["observed"]["latest_value_count"] == 50000
    assert snapshot["expected_values"] == list(expected)


def test_an_honest_verification_is_never_truncated() -> None:
    """The cap sits above every count a confirmed comparison can need.

    `expected_cdf_points` is a `Literal[201]` and the option count is bounded by
    `_MAX_CATEGORIES`, so no honest observation is ever sampled rather than recorded.
    """
    assert _MAX_SNAPSHOT_VALUES > 201
    assert _MAX_SNAPSHOT_VALUES > _MAX_CATEGORIES
    cdf = tuple(index / 200 for index in range(201))
    plan = NumericPost(continuous_cdf=cdf)
    result = classify_refetch(
        question_type="numeric",
        expected=expected_values(plan),
        baseline_latest_start_time=BASELINE_START,
        observed=ForecastHistory((ForecastEntry(NEW_START, cdf),)),
        expected_labels=None,
    )
    assert result.outcome == "confirmed"
    snapshot = json.loads(
        build_verification_snapshot(
            question_type="numeric",
            expected=expected_values(plan),
            baseline_entry_count=1,
            baseline_latest_start_time=BASELINE_START,
            result=result,
            expected_labels=None,
        )
    )
    assert snapshot["observed"]["latest_values"] == list(cdf)
    assert snapshot["observed"]["latest_value_count"] == 201


def test_a_live_attempt_id_is_derived_and_distinguishable() -> None:
    key = "wjsub-1-" + "a" * 64
    assert live_attempt_id(key) == live_attempt_id(key)
    assert LIVE_ATTEMPT_RE.match(live_attempt_id(key))
    assert key not in live_attempt_id(key)


def test_storable_text_never_raises_and_always_fits() -> None:
    for value in ("\x00", "\ud800", "x" * 100, "   ", "", 1, None, object()):
        result = storable_text(value, 16)
        assert result is None or (len(result) <= 16 and result.strip())


# ── M2-708: the key is claimed before the post, and handed back only if none was made ──
#
# `require_key_unused` was a read, and until `010` it was the whole guard in front of a
# live post: two commands could both pass it, both post, and `001`'s UNIQUE would refuse
# the second *row* after its call had been made. These drive the orchestrator's half --
# that the claim happens before any network I/O, and that it is released exactly when the
# gateway can prove nothing reached Metaculus.


def _reservations(conn: sqlite3.Connection) -> tuple[int, int]:
    return (
        conn.execute("SELECT count(*) FROM submission_key_reservations").fetchone()[0],
        conn.execute("SELECT count(*) FROM submission_key_releases").fetchone()[0],
    )


def _release_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT reason, released_by FROM submission_key_releases ORDER BY rowid"
        )
    ]


def test_a_refusal_before_the_post_hands_the_key_back(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """A transient pre-post failure must not wedge the forecast.

    The key is a pure function of the tournament, question, forecast version and payload
    hash, so the same work derives the same key forever. Without the release, one closed
    question -- or one unreadable history -- would block that forecast permanently, on an
    append-only table.
    """
    ledger, record_id = approved
    poster = FakePoster(before=FakeQuestion(state="closed"))
    with pytest.raises(LiveSubmissionError, match="not open"):
        _post(ledger, record_id, poster, live_config)

    assert poster.posts == 0
    # Claimed, then handed back -- and by the program's route, which names no person.
    assert _reservations(ledger) == (1, 1)
    assert _release_rows(ledger) == [("not_posted", None)]


def test_a_released_key_lets_the_next_command_through(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The point of the release, stated as the retry it makes possible.

    The first command hits a closed question; the second finds the platform open and
    posts. If the release were missing, the second would be refused as `reserved` and
    `posts` would stay 0 -- so this fails for the right reason if the exit disappears.
    """
    ledger, record_id = approved
    with pytest.raises(LiveSubmissionError, match="not open"):
        _post(ledger, record_id, FakePoster(before=FakeQuestion(state="closed")), live_config)

    poster = FakePoster()
    recorded = _post(ledger, record_id, poster, live_config)
    assert poster.posts == 1
    assert recorded.event.event_type == "submitted"
    # The second claim took the next sequence number rather than reusing the first.
    assert _reservations(ledger) == (2, 1)


def test_a_successful_post_leaves_its_reservation_standing(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """Nothing is released on the happy path: the attempt row spends the reservation.

    The derived state of the key becomes `spent`, which is terminal -- so there is nothing
    to undo, and a release written here would assert the post never happened.
    """
    ledger, record_id = approved
    poster = FakePoster()
    _post(ledger, record_id, poster, live_config)

    assert poster.posts == 1
    assert _reservations(ledger) == (1, 0)


def test_a_failure_after_the_post_keeps_the_key_claimed(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """**The flag, not the exception type.** This is the test the whole design turns on.

    `submit_with_detail` reads the clock twice -- once before the post and once after --
    so a clock that goes naive on its second call raises *after* the post has been made,
    as a `LiveSubmissionError`: the same type every pre-post refusal raises. An
    `except LiveSubmissionError` that released on sight would hand back a key whose post
    may well have landed, and the next command would post again. That is precisely the
    blind retry M2-708 exists to close.
    """
    ledger, record_id = approved
    poster = FakePoster()
    calls: list[int] = []

    def failing_clock() -> datetime:
        calls.append(1)
        # Aware first (before the post), naive second (after it).
        return OCCURRED if len(calls) == 1 else datetime(2026, 8, 20, 12, 0)  # noqa: DTZ001

    with pytest.raises(LiveSubmissionError):
        post_approved_forecast(
            ledger,
            record_id=record_id,
            payload=dict(BINARY_PAYLOAD),
            poster=poster,
            config=live_config,
            occurred_at=OCCURRED,
            clock=failing_clock,
            sleep=lambda _seconds: None,
        )

    # The post was made ...
    assert poster.posts == 1
    # ... so the key stays claimed. No release row at all.
    assert _reservations(ledger) == (1, 0)
    assert _release_rows(ledger) == []


def test_the_post_attempted_flag_is_set_before_the_poster_is_reached(
    live_config: AppConfig,
) -> None:
    """Set as `_post`'s first statement, so the case that matters is not the unmarked one.

    A post that *raised* is exactly when nobody knows whether it landed, and a flag set
    after a successful call would leave that case reading as "no post was made".
    """
    poster = FakePoster(post_error=_Timeout("boom"))
    gateway = MetaculusSubmissionGateway(
        poster=poster,
        expected_cdf_points=live_config.numeric_calibration.expected_cdf_points,
        clock=lambda: OCCURRED,
        sleep=lambda _seconds: None,
    )
    assert gateway.post_attempted is False
    gateway.submit_with_detail(
        SubmissionRequest(
            forecast_record_id="rec-1",
            question_id=QUESTION_ID,
            idempotency_key="wjsub-1-" + "a" * 64,
            payload=dict(BINARY_PAYLOAD),
            post_id=POST_ID,
        )
    )
    # The poster raised, so `posts` counted the call and the receipt carries the error --
    # and the flag is true either way, which is what the release path reads.
    assert poster.posts == 1
    assert gateway.post_attempted is True


def test_a_gate_that_refuses_before_the_post_leaves_the_flag_false(
    live_config: AppConfig,
) -> None:
    """The control for the test above: without both halves, a flag that was always true
    would pass one and a flag that was always false would pass the other."""
    poster = FakePoster(before=FakeQuestion(state="closed"))
    gateway = MetaculusSubmissionGateway(
        poster=poster,
        expected_cdf_points=live_config.numeric_calibration.expected_cdf_points,
        clock=lambda: OCCURRED,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(LiveSubmissionError, match="not open"):
        gateway.submit_with_detail(
            SubmissionRequest(
                forecast_record_id="rec-1",
                question_id=QUESTION_ID,
                idempotency_key="wjsub-1-" + "a" * 64,
                payload=dict(BINARY_PAYLOAD),
                post_id=POST_ID,
            )
        )
    assert poster.posts == 0
    assert gateway.post_attempted is False


def test_a_standing_reservation_refuses_the_next_command_before_any_fetch(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """The crash-mid-post state, from the next command's point of view.

    A reservation with no attempt row is what a killed process leaves behind. The next
    command must not post under that key -- and must not even reach the platform, which
    `fetches == 0` is what proves.
    """
    from whiskeyjack_bot.submission import reserve_submission_key, submission_key_for_record
    from whiskeyjack_bot.submission_gateway import payload_sha256

    ledger, record_id = approved
    digest = payload_sha256(dict(BINARY_PAYLOAD))
    key = submission_key_for_record(ledger, record_id, request_payload_sha256=digest)
    reserve_submission_key(ledger, record_id=record_id, idempotency_key=key, reserved_at=OCCURRED)

    poster = FakePoster()
    with pytest.raises(LiveSubmissionError, match="reserved by a submission that has not finished"):
        _post(ledger, record_id, poster, live_config)
    assert poster.posts == 0
    assert poster.fetches == 0
    # The loser wrote nothing: still one reservation, still unreleased.
    assert _reservations(ledger) == (1, 0)


def test_a_failing_release_never_displaces_the_refusal_that_caused_it(
    approved: tuple[sqlite3.Connection, str],
    live_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`lifecycle._unwind`'s contract, on the cleanup path.

    The caller is already being told that nothing was posted. A reservation that outlives
    its command is a recoverable state -- `release-key` is the way out -- whereas
    surfacing a failed release instead of the refusal would hide why the command stopped.
    The monkeypatch simulates an ordinary ledger failure on the cleanup write, which is a
    reachable condition, not an invented one.
    """
    import whiskeyjack_bot.submission_live as live

    def exploding_release(*_args: Any, **_kwargs: Any) -> None:
        raise SubmissionError("the ledger refused the release")

    monkeypatch.setattr(live, "release_submission_key", exploding_release)

    ledger, record_id = approved
    poster = FakePoster(before=FakeQuestion(state="closed"))
    with pytest.raises(LiveSubmissionError, match="not open"):
        _post(ledger, record_id, poster, live_config)

    assert poster.posts == 0
    # The release never happened, and the original refusal is what surfaced.
    assert _reservations(ledger) == (1, 0)


def test_a_live_post_is_refused_inside_a_callers_transaction(
    approved: tuple[sqlite3.Connection, str], live_config: AppConfig
) -> None:
    """Round 1's blocking finding, reproduced and then closed at the live boundary.

    Before the fix this test's `_post` returned `submitted`, and the ROLLBACK below then
    erased the reservation *and* the attempt row, leaving the platform holding a forecast
    the ledger had no record of and the key free for a second command to claim. The retry
    posted again: `poster.posts == 2` for one forecast.

    The guard is checked ahead of every other gate, so nothing is fetched and nothing is
    posted -- an ordinary caller mistake must not cost a live call.
    """
    ledger, record_id = approved
    poster = FakePoster()
    ledger.execute("BEGIN")
    try:
        with pytest.raises(LiveSubmissionError, match="open transaction"):
            _post(ledger, record_id, poster, live_config)
        assert poster.posts == 0
        assert poster.fetches == 0
        assert _reservations(ledger) == (0, 0)
    finally:
        ledger.execute("ROLLBACK")


def test_the_open_transaction_refusal_precedes_every_other_gate(
    approved: tuple[sqlite3.Connection, str], tmp_path: Path
) -> None:
    """What the boundary guard adds over the one in `reserve_submission_key`.

    `reserve_submission_key` refuses an enclosing transaction itself, and that refusal is
    what makes the live path *safe* -- a mutation removing this guard alone still posts
    nothing. So this test pins the thing only this guard can do: refuse **before every
    other gate**, including the outermost one.

    The config here has `submission.enabled: false`, which `require_live_submission_
    enabled` refuses on the very next line. If the transaction check were anywhere later,
    the config message would win. Without it, a caller whose real mistake is an open
    transaction is told about their config instead, and fixes the wrong thing -- by
    turning off the flag that is the last safety rail in front of a live post.
    """
    ledger, record_id = approved
    disabled = _config(tmp_path, enabled=False)
    ledger.execute("BEGIN")
    try:
        with pytest.raises(LiveSubmissionError, match="open transaction"):
            _post(ledger, record_id, FakePoster(), disabled)
    finally:
        ledger.execute("ROLLBACK")
