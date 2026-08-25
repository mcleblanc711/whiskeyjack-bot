"""Contract tests for the single-attempt poster, against the pinned SDK (M2-704).

**These tests exist because the premise is a measurement, not a belief.** M2-704's
acceptance criterion is "uncertain timeout blocks blind retry", and the pinned
``forecasting-tools==0.2.92`` blind-retries every POST four times from inside
``MetaculusClient``. The first two tests below assert that -- they fail if a future release
drops the retry, which is exactly when the guard's premise disappears and someone should
find out. The rest assert the guard works and reads still retry.

Companion to ``test_dependency_pins.py``: that file makes an upgrade a red build, and this
one says what the upgrade would have to be re-checked against.

No network. ``requests.post`` and ``requests.get`` are stubbed per test and counted, so
"how many requests were sent" is a number rather than an inference.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
import requests
from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.metaculus.client import (
    _EXPECTED_POST_PARAMETERS,
    PosterContractError,
    SingleAttemptPoster,
    _assert_single_post_is_reachable,
)
from whiskeyjack_bot.submission_live import (
    _DETAIL_FOR_ERROR,
    _LIVE_ERROR_TYPES,
    classify_error,
    http_details,
)

FORECAST_URL_FRAGMENT = "/questions/forecast/"


@pytest.fixture()
def client() -> MetaculusClient:
    """A client that never waits between requests, so a test costs no wall clock."""
    built = MetaculusClient(token="fake-token-for-tests")
    built.sleep_time_between_requests_min = 0.0
    built.sleep_jitter_seconds = 0.0
    return built


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the retry decorator's own backoff, which sleeps up to 75 seconds.

    Patched on the ``time`` module the SDK's ``util.misc`` imported, and restored by
    monkeypatch. Without it the four-retry test below would take over two minutes.
    """
    import forecasting_tools.util.misc as misc

    monkeypatch.setattr(misc.time, "sleep", lambda _seconds: None)


class _Counter:
    """Counts calls and raises or returns whatever the test asked for."""

    def __init__(self, outcome: Any) -> None:
        self.calls = 0
        self.urls: list[str] = []
        self._outcome = outcome

    def __call__(self, url: str, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.urls.append(url)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _response(status: int, body: bytes, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.reason = "Too Many Requests" if status == 429 else "Bad Request"
    response.url = "https://example.invalid/api/questions/forecast/"
    response._content = body
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


# ── the premise: the pinned SDK really does blind-retry a POST ────────────────


def test_the_pinned_sdk_posts_four_times_on_a_timeout(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement M2-704's whole design rests on.

    A timeout that actually landed on the platform is re-posted three more times under one
    idempotency key with no refetch in between. If this test starts failing, the retry is
    gone from the dependency and `SingleAttemptPoster` has become a no-op worth deleting --
    which should be a decision, not a discovery.
    """
    counter = _Counter(requests.exceptions.Timeout("boom"))
    monkeypatch.setattr(requests, "post", counter)
    with pytest.raises(requests.exceptions.Timeout):
        client.post_binary_question_prediction(123, 0.4)
    assert counter.calls == 4


def test_the_pinned_sdk_posts_four_times_on_an_http_error(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4xx is retried too, which is less obvious than the timeout case.

    `retry_on_exceptions` is `RequestException`, and `HTTPError` subclasses it -- so a
    permanent client error is hammered three more times rather than failing once.
    """
    counter = _Counter(_response(400, b'{"detail": "no"}'))
    monkeypatch.setattr(requests, "post", counter)
    with pytest.raises(requests.exceptions.HTTPError):
        client.post_binary_question_prediction(123, 0.4)
    assert counter.calls == 4


# ── the guard: exactly one write, reads unchanged ────────────────────────────


def test_the_poster_posts_exactly_once_on_a_timeout(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = _Counter(requests.exceptions.Timeout("boom"))
    monkeypatch.setattr(requests, "post", counter)
    with pytest.raises(requests.exceptions.Timeout):
        SingleAttemptPoster(client).post_binary_question_prediction(123, 0.4)
    assert counter.calls == 1


def test_the_poster_posts_exactly_once_on_an_http_error(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = _Counter(_response(429, b'{"detail": "slow down"}', {"Retry-After": "30"}))
    monkeypatch.setattr(requests, "post", counter)
    with pytest.raises(requests.exceptions.HTTPError):
        SingleAttemptPoster(client).post_binary_question_prediction(123, 0.4)
    assert counter.calls == 1


def test_every_post_method_sends_exactly_one_request(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three, not only the binary one a happy-path test would reach.

    They share `_post_question_prediction`, so one passing is weak evidence for the others
    -- and a future refactor that gave one of them its own path would not be caught by a
    binary-only test.
    """
    poster = SingleAttemptPoster(client)
    for call in (
        lambda: poster.post_binary_question_prediction(123, 0.4),
        lambda: poster.post_numeric_question_prediction(123, [0.0, 0.5, 1.0]),
        lambda: poster.post_multiple_choice_question_prediction(123, {"a": 0.4, "b": 0.6}),
    ):
        counter = _Counter(requests.exceptions.Timeout("boom"))
        monkeypatch.setattr(requests, "post", counter)
        with pytest.raises(requests.exceptions.Timeout):
            call()
        assert counter.calls == 1


def test_a_successful_post_goes_to_the_forecast_endpoint(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = _Counter(_response(201, b"{}"))
    monkeypatch.setattr(requests, "post", counter)
    SingleAttemptPoster(client).post_binary_question_prediction(123, 0.4)
    assert counter.calls == 1
    assert counter.urls[0].endswith(FORECAST_URL_FRAGMENT)


def test_the_refetch_keeps_its_retry(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads may retry and writes may not -- the line this adapter draws.

    A GET is idempotent, so retrying it costs nothing and is what keeps "the refetch could
    not be performed" an edge case rather than a routine outcome.
    """
    counter = _Counter(requests.exceptions.ConnectionError("down"))
    monkeypatch.setattr(requests, "get", counter)
    with pytest.raises(requests.exceptions.ConnectionError):
        SingleAttemptPoster(client).get_question_by_post_id(456)
    assert counter.calls == 4


def test_the_class_method_is_restored_after_every_call(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shadow must not outlive the call that installed it.

    A leaked instance attribute would silently disable the retry for every later use of
    that client -- including the refetch, which is meant to keep it.
    """
    before = client.__dict__.get("_post_question_prediction")
    monkeypatch.setattr(requests, "post", _Counter(requests.exceptions.Timeout("boom")))
    with pytest.raises(requests.exceptions.Timeout):
        SingleAttemptPoster(client).post_binary_question_prediction(123, 0.4)
    assert client.__dict__.get("_post_question_prediction") == before
    assert "_post_question_prediction" not in client.__dict__

    counter = _Counter(requests.exceptions.ConnectionError("down"))
    monkeypatch.setattr(requests, "get", counter)
    with pytest.raises(requests.exceptions.ConnectionError):
        SingleAttemptPoster(client).get_question_by_post_id(456)
    assert counter.calls == 4, "the refetch lost its retry, so the shadow leaked"


# ── the import-time contract guard ───────────────────────────────────────────


def test_the_contract_guard_refuses_a_missing_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the guard's own failure path; a guard nobody has seen fail is a guess."""
    monkeypatch.setattr("whiskeyjack_bot.metaculus.client._RAW_POST_PREDICTION", None)
    with pytest.raises(PosterContractError) as excinfo:
        _assert_single_post_is_reachable()
    assert "_post_question_prediction" in str(excinfo.value)


def test_the_contract_guard_refuses_a_changed_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    def renamed(self: object, question: int, body: dict[str, object]) -> None:  # pragma: no cover
        raise AssertionError("never called")

    monkeypatch.setattr("whiskeyjack_bot.metaculus.client._RAW_POST_PREDICTION", renamed)
    with pytest.raises(PosterContractError) as excinfo:
        _assert_single_post_is_reachable()
    assert "single-attempt guard cannot be applied" in str(excinfo.value)


def test_the_contract_guard_passes_against_the_pinned_sdk() -> None:
    _assert_single_post_is_reachable()
    parameters = tuple(inspect.signature(MetaculusClient._post_question_prediction).parameters)
    assert parameters == _EXPECTED_POST_PARAMETERS


def test_the_adapter_has_every_member_the_protocol_declares() -> None:
    """Structural conformance at runtime; `mypy --strict` checks it statically in ``src``."""
    from whiskeyjack_bot.submission_live import MetaculusPoster

    for name in (
        "post_binary_question_prediction",
        "post_numeric_question_prediction",
        "post_multiple_choice_question_prediction",
        "get_question_by_post_id",
    ):
        assert hasattr(MetaculusPoster, name)
        adapter = inspect.signature(getattr(SingleAttemptPoster, name))
        declared = inspect.signature(getattr(MetaculusPoster, name))
        assert tuple(adapter.parameters) == tuple(declared.parameters), name


# ── error classification, pinned against the real exception classes ──────────


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (requests.exceptions.Timeout("t"), "timeout"),
        (requests.exceptions.ReadTimeout("t"), "timeout"),
        (requests.exceptions.ConnectTimeout("t"), "connection_error"),
        (requests.exceptions.ConnectionError("c"), "connection_error"),
        (requests.exceptions.ProxyError("p"), "connection_error"),
        (requests.exceptions.SSLError("s"), "connection_error"),
        (requests.exceptions.ChunkedEncodingError("c"), "connection_error"),
        (requests.exceptions.HTTPError("h"), "http_error"),
        (requests.exceptions.TooManyRedirects("r"), "http_error"),
        (requests.exceptions.RequestException("r"), "request_error"),
        (requests.exceptions.RetryError("r"), "request_error"),
        (ValueError("v"), "payload_rejected"),
        (RuntimeError("x"), "internal_error"),
    ],
)
def test_the_error_vocabulary_is_pinned_to_the_real_classes(
    exception: BaseException, expected: str
) -> None:
    """`submission_live` classifies by class name without importing ``requests``.

    That keeps the transport out of the seam and out of this package's declared
    dependencies -- and it is only safe because this test holds the vocabulary against the
    real classes. `RetryError` is in the table deliberately: it is *not* named in any of
    the module's sets, so it exercises the MRO walk falling back to `RequestException`.
    """
    assert classify_error(exception) == expected


def test_every_error_type_maps_to_a_ledger_detail_code() -> None:
    """Totality, so a new member cannot be added without deciding what it means."""
    assert set(_DETAIL_FOR_ERROR) == _LIVE_ERROR_TYPES


def test_the_http_status_is_recoverable_through_the_cause_chain(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK throws the response away; this is how the status still comes back.

    `raise_for_status_with_additional_info` re-raises a *new* `HTTPError` with no
    `response=`, chained `from` the original. The original carries the response, so
    `exc.__cause__.response` is reachable -- through public attributes only.
    """
    monkeypatch.setattr(
        requests,
        "post",
        _Counter(_response(429, b'{"detail": "slow down"}', {"Retry-After": "30"})),
    )
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        SingleAttemptPoster(client).post_binary_question_prediction(123, 0.4)
    status, body, headers = http_details(excinfo.value)
    assert status == 429
    assert body is not None and "slow down" in body
    assert headers is not None and "retry-after" in headers


def test_the_sdk_message_carries_the_response_body_and_is_never_what_we_store(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the leak channel `submission_live` refuses to use.

    The SDK builds its exception message out of the response text and the request URL. If a
    future version stopped doing that this test fails, which is the signal to revisit
    whether the module's own constant messages are still the right call.
    """
    monkeypatch.setattr(requests, "post", _Counter(_response(400, b'{"detail": "SECRETBODY"}')))
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        SingleAttemptPoster(client).post_binary_question_prediction(123, 0.4)
    assert "SECRETBODY" in str(excinfo.value)
