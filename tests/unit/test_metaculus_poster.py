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

import copy
import inspect
import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml
from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.logging_setup import ProviderResponseTextFilter, configure_logging

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

REPO_ROOT = Path(__file__).resolve().parents[2]

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


# ── round 1, finding 1: two adapters over one client ──────────────────────────


def test_two_posters_over_one_client_share_a_lock(client: MetaculusClient) -> None:
    """The lock is keyed on the client, because the attribute it guards is the client's."""
    assert SingleAttemptPoster(client)._lock is SingleAttemptPoster(client)._lock


def test_a_second_poster_cannot_reopen_the_blind_retry(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-1 finding 1, reproduced then closed.

    Two ``SingleAttemptPoster``s over one ``MetaculusClient`` used to hold *different*
    locks, so their override windows could interleave on the client attribute they both
    shadow. The damaging order is **A leaving while B is still inside**: A's exit restores
    the attribute to what A found (nothing), B's post then resolves the *class* attribute
    -- the decorated one -- and is retried. **Four POSTs for one logical post**, which is
    the blind retry this class exists to prevent.

    That order is the one the exact-state restore cannot fix by itself, so this test is
    specifically the lock's: it fails on a per-adapter lock even with the restore in place.
    The interleaving is forced with ordinary threading primitives and no patching of this
    project's code. With a per-client lock B cannot enter until A has left, so the sequence
    never forms and B's post is a single request.
    """
    counter = _Counter(requests.exceptions.Timeout("boom"))
    monkeypatch.setattr(requests, "post", counter)

    poster_a = SingleAttemptPoster(client)
    poster_b = SingleAttemptPoster(client)
    b_inside = threading.Event()
    a_left = threading.Event()
    failures: list[BaseException] = []

    def run_a() -> None:
        try:
            with poster_a._single_attempt():
                # Wait for B to be inside its own window before leaving. Under the shared
                # lock B cannot get there, so this times out -- and that is the point.
                b_inside.wait(timeout=1.0)
            a_left.set()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
            failures.append(exc)

    def run_b() -> None:
        try:
            with poster_b._single_attempt():
                b_inside.set()
                a_left.wait(timeout=2.0)
                with pytest.raises(requests.exceptions.Timeout):
                    client.post_binary_question_prediction(123, 0.4)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
            failures.append(exc)

    thread_a = threading.Thread(target=run_a)
    thread_b = threading.Thread(target=run_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10.0)
    thread_b.join(timeout=10.0)

    assert not failures, failures
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert counter.calls == 1


def test_the_window_restores_the_exact_prior_instance_state(client: MetaculusClient) -> None:
    """Whatever was in the instance dict on the way in is what is there on the way out.

    The window used to ``delattr`` unconditionally, which is right only while no window can
    nest inside another. That is a property of the lock, not of this function, so the
    restore no longer depends on it.
    """
    poster = SingleAttemptPoster(client)
    assert "_post_question_prediction" not in client.__dict__
    with poster._single_attempt():
        assert "_post_question_prediction" in client.__dict__
    assert "_post_question_prediction" not in client.__dict__

    marker = object()
    client.__dict__["_post_question_prediction"] = marker
    try:
        with poster._single_attempt():
            assert client.__dict__["_post_question_prediction"] is not marker
        assert client.__dict__["_post_question_prediction"] is marker
    finally:
        del client.__dict__["_post_question_prediction"]


def test_nested_windows_on_one_poster_still_post_once(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inner window's exit must not hand the outer one back the decorated method."""
    counter = _Counter(requests.exceptions.Timeout("boom"))
    monkeypatch.setattr(requests, "post", counter)
    poster = SingleAttemptPoster(client)
    with poster._single_attempt():
        with poster._single_attempt():
            pass
        with pytest.raises(requests.exceptions.Timeout):
            client.post_binary_question_prediction(123, 0.4)
    assert counter.calls == 1


# ── round 1, finding 3: the SDK logs the whole response body ──────────────────


@pytest.fixture()
def logging_config(tmp_path: Path) -> AppConfig:
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    return validate_config_data(data)


def test_a_failed_post_writes_no_response_body_to_the_log(
    client: MetaculusClient,
    logging_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-1 finding 3, reproduced then closed.

    ``raise_for_status_with_additional_info`` builds its message out of the request URL and
    the **full response text**, logs it at ERROR, and raises an ``HTTPError`` carrying the
    same string -- which the retry wrapper logs again as ``{e}``. Through this project's own
    handlers that put an unbounded copy of untrusted provider content into ``logging.file``,
    which the "an error message never echoes stored/file/field values" rule forbids.

    Asserted against the log **file**, not a captured record: the file is the artifact that
    persists, and it is what the finding was about.
    """
    configure_logging(logging_config)
    body = b'{"detail": "PROVIDER_BODY_CONTENT"}'
    monkeypatch.setattr(requests, "post", _Counter(_response(400, body)))
    with pytest.raises(requests.exceptions.HTTPError):
        SingleAttemptPoster(client).post_binary_question_prediction(123, 0.4)

    for handler in logging.getLogger().handlers:
        handler.flush()
    written = logging_config.logging.file.read_text(encoding="utf-8")
    assert "PROVIDER_BODY_CONTENT" not in written
    assert "example.invalid" not in written
    # The failure is still visible -- the record is replaced, not dropped. Losing it would
    # trade a content leak for a blind operator.
    assert any(
        json.loads(line)["logger"].startswith("forecasting_tools.util.misc")
        and json.loads(line)["level"] == "ERROR"
        for line in written.splitlines()
    )


def test_the_provider_response_filter_closes_the_module_not_the_message(
    logging_config: AppConfig,
) -> None:
    """Every logging call in that SDK module interpolates a response or an exception.

    Matching the one known message would be a check whose unknown case is "pass" -- the
    library rewords a line, or adds a third, and the leak reopens with nothing to notice
    (``docs/LESSONS.md`` #7). So the filter closes the module, exactly as
    ``PayloadDebugFilter`` closes the sub-INFO range of the payload loggers.
    """
    record = logging.LogRecord(
        name="forecasting_tools.util.misc",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Retrying due to error: %s",
        args=("PROVIDER_BODY_CONTENT",),
        exc_info=None,
    )
    assert ProviderResponseTextFilter().filter(record) is True
    assert "PROVIDER_BODY_CONTENT" not in record.getMessage()

    untouched = logging.LogRecord(
        name="whiskeyjack_bot.submission_live",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="a whiskeyjack message survives",
        args=None,
        exc_info=None,
    )
    assert ProviderResponseTextFilter().filter(untouched) is True
    assert untouched.getMessage() == "a whiskeyjack message survives"
