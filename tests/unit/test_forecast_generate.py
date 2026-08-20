"""M1-402 acceptance: one configured GeneralLlm call, with at most one bounded repair.

Every case counts invocations rather than asserting an outcome, because the criterion
is about *how many* billable calls a shape of failure costs. ``client.calls == []`` is
the assertion that a refusal happened before anything was spent, and the three
independent network guards (pytest-socket, the DNS refusal in tests/conftest.py, the
socket.connect refusal in tests/unit/conftest.py) enforce that no real call is
possible here at all.
"""

import copy
import hashlib
import json
import logging
import re
import subprocess
import sys
import traceback
import warnings
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

import pytest
import yaml
from forecasting_tools.ai_models.general_llm import GeneralLlm

from whiskeyjack_bot.config import (
    MAX_MODEL_INVOCATIONS,
    AppConfig,
    ConfigError,
    validate_config_data,
)
from whiskeyjack_bot.forecast.generate import (
    ForecastGeneration,
    ForecastGenerationError,
    build_forecaster_client,
    generate_forecast,
)
from whiskeyjack_bot.forecast.schema import BinaryForecastResponse
from whiskeyjack_bot.logging_setup import (
    PayloadDebugFilter,
    SecretRedactionFilter,
    configure_logging,
)
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.prompt import LoadedPrompt, load_prompt
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
from whiskeyjack_bot.research.packet import ResearchPacket, build_packet

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "prompts" / "forecaster.md"
PROMPT_TEXT = PROMPT_PATH.read_text(encoding="utf-8")
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
MODEL_NAME = "openrouter/test-model"
FAKE_KEY = "fakeOPENROUTERkey1234"
# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic secret
# shape here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = MODEL_NAME
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    return validate_config_data(data)


@pytest.fixture()
def prompt(config: AppConfig) -> LoadedPrompt:
    return load_prompt(PROMPT_PATH, config.forecast.prompt_version)


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None
    return match.group(1)


def good_reply(**overrides: Any) -> str:
    payload = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Binary schema") + "}"),
        "question_id": 42,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _question(**overrides: Any) -> CanonicalBinaryQuestion:
    fields: dict[str, Any] = {"question_id": 42, "post_id": 7, "title": "Will X happen?"}
    fields.update(overrides)
    return CanonicalBinaryQuestion(**fields)


def _packet(question_id: int = 42) -> ResearchPacket:
    run = ResearchRun(
        retrieval_run_id="run-1",
        question_id=question_id,
        provider="asknews",
        started_at_utc=NOW,
    )
    document = ResearchDocument(
        retrieval_run_id="run-1",
        original_url="https://a.example/x",
        canonical_url="https://a.example/x",
        retrieved_at_utc=NOW,
        source_type="news",
        provenance="direct_api",
        content_sha256=hashlib.sha256(b"x").hexdigest(),
        summary="a summary",
    )
    return build_packet(question_id, [run], [document])


class _Model:
    """A recording stand-in for ``GeneralLlm``.

    ``model`` is part of the protocol because ``generate_forecast`` checks it against
    config, so a double that wanted to skip it would be refused -- which is the point:
    a client carries no memory of which config built it.
    """

    def __init__(self, *replies: str, raises: BaseException | None = None) -> None:
        self.model = MODEL_NAME
        self.replies = list(replies)
        self.raises = raises
        self.calls: list[Any] = []

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        self.calls.append(prompt)
        if self.raises is not None:
            raise self.raises
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]


def _generate(client: Any, config: AppConfig, loaded: LoadedPrompt, **overrides: Any) -> Any:
    call: dict[str, Any] = {
        "config": config,
        "question": _question(),
        "packet": _packet(),
        "prompt": loaded,
        "tournament_id": "minibench",
        "now": NOW,
        "client": client,
    }
    call.update(overrides)
    return generate_forecast(**call)


def _leaks(exc: BaseException, *needles: str) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return any(n in str(exc) or n in rendered for n in needles)


# --- the invocation budget -------------------------------------------------------


def test_a_valid_response_returns_typed_output_in_one_call(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    client = _Model(good_reply())
    result = _generate(client, config, prompt)
    assert isinstance(result.forecast, BinaryForecastResponse)
    assert result.forecast.final_prediction.probability_yes == 0.37
    assert result.invocations == 1
    assert result.repair_attempted is False
    assert result.failure_code is None
    assert len(client.calls) == 1


def test_malformed_output_gets_exactly_one_repair_and_it_can_succeed(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    client = _Model("here is my answer, roughly 37%", good_reply())
    result = _generate(client, config, prompt)
    assert result.forecast is not None
    assert result.invocations == 2
    assert result.repair_attempted is True
    assert len(client.calls) == 2
    assert len(result.raw_responses) == 2


def test_output_that_stays_malformed_costs_two_calls_and_no_more(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """The bound is the point: a model that never returns JSON must not be able to
    spend a third call."""
    client = _Model("no", "still no", "and again")
    result = _generate(client, config, prompt)
    assert result.forecast is None
    assert result.invocations == 2
    assert len(client.calls) == 2
    assert result.failure_code == "malformed_response"
    assert result.failure_problems


def test_output_that_parses_but_breaks_the_schema_is_classified_separately(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    client = _Model(json.dumps({"schema_version": "1.0.0"}))
    result = _generate(client, config, prompt)
    assert result.failure_code == "schema_invalid"
    assert result.invocations == 2


def test_a_json_value_that_is_not_an_object_is_malformed_not_schema_invalid(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    client = _Model("[1, 2, 3]")
    result = _generate(client, config, prompt)
    assert result.failure_code == "malformed_response"


def test_allowed_tries_of_one_buys_no_repair(config: AppConfig, prompt: LoadedPrompt) -> None:
    """``model.allowed_tries`` is the total number of invocations, so 1 means the
    first answer is the only answer."""
    tightened = config.model_copy(
        update={"model": config.model.model_copy(update={"allowed_tries": 1})}
    )
    client = _Model("not json", good_reply())
    result = _generate(client, tightened, prompt)
    assert result.invocations == 1
    assert result.repair_attempted is False
    assert result.forecast is None
    assert len(client.calls) == 1


def test_a_fenced_reply_is_accepted_without_a_repair(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """The prompt says not to use Markdown fences; models do it anyway, and paying a
    second call for a wrapper we can strip locally is money for nothing."""
    client = _Model("```json\n" + good_reply() + "\n```")
    result = _generate(client, config, prompt)
    assert result.forecast is not None
    assert result.invocations == 1


def test_a_provider_failure_is_never_repaired(config: AppConfig, prompt: LoadedPrompt) -> None:
    """A repair repairs *output*. Re-issuing a call that raised is the transport retry
    this module deliberately disabled, so a provider error ends the attempt at one."""
    client = _Model(good_reply(), raises=RuntimeError(f"upstream said no; bearer {SECRET}"))
    result = _generate(client, config, prompt)
    assert result.invocations == 1
    assert len(client.calls) == 1
    assert result.failure_code == "provider_error"
    assert result.raw_responses == ()


def test_a_timeout_is_classified_as_a_timeout(config: AppConfig, prompt: LoadedPrompt) -> None:
    client = _Model(good_reply(), raises=TimeoutError())
    assert _generate(client, config, prompt).failure_code == "timeout"


def test_a_provider_exception_is_never_inspected(config: AppConfig, prompt: LoadedPrompt) -> None:
    """A provider error can quote the request, and the request carries the API key in
    a header. The message must not survive into the result or a log line."""
    client = _Model(good_reply(), raises=RuntimeError(f"auth header Bearer {FAKE_KEY} {SECRET}"))
    result = _generate(client, config, prompt)
    joined = " ".join(result.failure_problems)
    assert FAKE_KEY not in joined
    assert SECRET not in joined


def test_the_cost_of_an_untracked_call_is_unknown_and_not_free(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """The package coerces an untrackable cost to 0.0, so zero and unknown are
    indistinguishable at the source. Recording 0.0 would undercount against
    run_limits.max_cost_usd on exactly the runs most likely to be retried."""
    result = _generate(_Model(good_reply()), config, prompt)
    assert result.cost_usd is None


# --- the repair turn -------------------------------------------------------------


def test_the_repair_turn_shows_the_model_its_own_output_and_the_sanitized_problems(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    bad = json.dumps({"schema_version": "1.0.0"})
    client = _Model(bad, good_reply())
    result = _generate(client, config, prompt)
    first, second = client.calls
    assert [m["role"] for m in first] == ["system", "user"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[2]["content"] == bad
    assert "status_quo" in second[3]["content"]
    assert result.forecast is not None


def test_the_system_message_is_the_hashed_prompt_verbatim(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """``prompt_sha256`` is then the digest of exactly the instructions the model was
    given, with no separator this module invented spliced in."""
    client = _Model(good_reply())
    _generate(client, config, prompt)
    system = client.calls[0][0]
    assert system["role"] == "system"
    assert system["content"] == PROMPT_TEXT
    assert hashlib.sha256(system["content"].encode("utf-8")).hexdigest() == prompt.sha256


def test_the_user_message_is_the_reasoning_packet(config: AppConfig, prompt: LoadedPrompt) -> None:
    client = _Model(good_reply())
    result = _generate(client, config, prompt)
    user = client.calls[0][1]
    assert user["content"] == result.request
    assert json.loads(user["content"])["question_id"] == 42


# --- refusals, all before anything is spent --------------------------------------


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("packet for another question", {"packet": _packet(question_id=99)}),
        ("blank tournament id", {"tournament_id": "  "}),
        ("naive now", {"now": datetime(2026, 8, 19, 12, 0)}),
        ("question is not a question", {"question": object()}),
        ("packet is not a packet", {"packet": object()}),
        ("prompt is not a LoadedPrompt", {"prompt": object()}),
    ],
)
def test_a_caller_mistake_is_refused_before_any_billable_call(
    config: AppConfig, prompt: LoadedPrompt, label: str, overrides: Any
) -> None:
    client = _Model(good_reply())
    with pytest.raises(ForecastGenerationError):
        _generate(client, config, prompt, **overrides)
    assert client.calls == [], "refusal must happen before any billable call"


def test_a_prompt_of_another_version_is_refused(config: AppConfig, prompt: LoadedPrompt) -> None:
    """A LoadedPrompt carries no memory of which config loaded it, the same reason
    the Exa adapter repeats its configuration check at the spending site."""
    client = _Model(good_reply())
    stale = LoadedPrompt(version="1.0.0", sha256=prompt.sha256, text=prompt.text)
    with pytest.raises(ForecastGenerationError):
        _generate(client, config, prompt, prompt=stale)
    assert client.calls == []


def test_an_unsupported_question_type_is_refused(config: AppConfig, prompt: LoadedPrompt) -> None:
    narrowed = config.model_copy(
        update={
            "forecast": config.forecast.model_copy(
                update={"supported_question_types": ["multiple_choice"]}
            )
        }
    )
    client = _Model(good_reply())
    with pytest.raises(ForecastGenerationError):
        _generate(client, narrowed, prompt)
    assert client.calls == []


def test_an_int_enum_question_id_is_refused(config: AppConfig, prompt: LoadedPrompt) -> None:
    """Exact-type, not isinstance: an IntEnum satisfies isinstance and would break the
    %d safety claim on this module's one log line (M1-303 round 5)."""

    class _QuestionId(IntEnum):
        FORTY_TWO = 42

    client = _Model(good_reply())
    question = _question()
    object.__setattr__(question, "question_id", _QuestionId.FORTY_TWO)
    with pytest.raises(ForecastGenerationError):
        _generate(client, config, prompt, question=question)
    assert client.calls == []


def test_a_client_for_a_different_model_is_refused(config: AppConfig, prompt: LoadedPrompt) -> None:
    """``forecast_records.model_name`` is NOT NULL; a client pointed elsewhere writes
    an attribution claim the call contradicts."""
    client = _Model(good_reply())
    client.model = "anthropic/some-other-model"
    with pytest.raises(ForecastGenerationError):
        _generate(client, config, prompt)
    assert client.calls == []


def test_a_config_whose_provider_contradicts_its_model_name_is_refused(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    mismatched = config.model_copy(
        update={"model": config.model.model_copy(update={"provider": "anthropic"})}
    )
    client = _Model(good_reply())
    with pytest.raises(ForecastGenerationError):
        _generate(client, mismatched, prompt)
    assert client.calls == []


def test_a_model_name_with_no_prefix_carries_no_claim_to_check(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """The refusal above is a self-contradiction check, not a registry of litellm
    prefixes. A bare name asserts nothing and must still run."""
    bare = config.model_copy(update={"model": config.model.model_copy(update={"name": "gpt-x"})})
    client = _Model(good_reply())
    client.model = "gpt-x"
    assert _generate(client, bare, prompt).forecast is not None


def test_a_missing_credential_fails_before_the_client_exists(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(config.model.api_key_env, raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        build_forecaster_client(config)
    assert excinfo.value.env_var_name == config.model.api_key_env


def test_an_empty_credential_counts_as_missing(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.model.api_key_env, "")
    with pytest.raises(MissingCredentialError):
        build_forecaster_client(config)


# --- what is recorded ------------------------------------------------------------


def test_the_model_settings_come_from_config_not_from_the_client(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """``GeneralLlm.to_dict()`` dumps ``litellm_kwargs`` wholesale, API key included,
    and 0.2.92 contains no redaction anywhere. The settings M1-406 will persist are
    built from config instead, and this pins that they are."""
    result = _generate(_Model(good_reply()), config, prompt)
    assert result.settings.name == MODEL_NAME
    assert result.settings.provider == config.model.provider
    assert result.settings.temperature == config.model.temperature
    assert result.settings.max_output_tokens == config.model.max_output_tokens
    assert result.settings.prompt_sha256 == prompt.sha256
    assert result.settings.prompt_version == prompt.version


def test_the_result_repr_does_not_print_the_request_or_the_raw_responses(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """The M1-401 round-1 finding, one level up: the error paths were sanitized but
    the value object was not, and a default dataclass repr prints everything through
    any log line or frame-capturing traceback.

    Two fields are withheld and one deliberately is not. ``request`` is a whole
    research packet and ``raw_responses`` is unvalidated model output that may contain
    anything -- including, as here, a reply that never passed validation at all. The
    parsed ``forecast`` stays visible: it is the auditable product D24 asks for,
    bounded by the schema, and ``ResearchDocument`` -- which holds arbitrary provider
    text -- keeps its repr for the same reason. A result object that cannot be read in
    a debugger is its own defect.
    """
    client = _Model(f"garbage {SECRET}", good_reply())
    result = _generate(client, config, prompt)
    rendered = repr(result)
    assert result.raw_responses[0].startswith("garbage")
    assert SECRET not in rendered
    assert "Will X happen?" not in rendered
    assert "a summary" not in rendered
    assert "final_prediction" in rendered


def test_the_failure_code_is_in_the_ledgers_own_vocabulary(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """So a caller writes ``generation_failed`` without re-deriving the detail code."""
    from typing import get_args

    from whiskeyjack_bot.lifecycle import PreForecastFailureCode

    codes = set(get_args(PreForecastFailureCode))
    for client in (_Model("no", "no"), _Model(good_reply(), raises=RuntimeError("x"))):
        result = _generate(client, config, prompt)
        assert result.failure_code in codes


def test_a_successful_result_carries_no_failure_and_vice_versa(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    ok = _generate(_Model(good_reply()), config, prompt)
    assert (ok.forecast is None) == (ok.failure_code is not None)
    bad = _generate(_Model("no", "no"), config, prompt)
    assert (bad.forecast is None) == (bad.failure_code is not None)
    assert isinstance(ok, ForecastGeneration)


# --- secret hygiene --------------------------------------------------------------


def test_a_planted_secret_in_the_model_config_reaches_no_channel(
    config: AppConfig,
    prompt: LoadedPrompt,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Watches four channels: the raised exception, its rendered traceback, pydantic
    serializer warnings (which embed the offending value in their text), and stderr --
    which ``caplog`` never sees, because a logging interpolation failure prints raw
    arguments there in logging's own error report (M1-303 round 4)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    planted = config.model_copy(
        update={"model": config.model.model_copy(update={"name": f"openrouter/{SECRET}"})}
    )
    client = _Model(good_reply())
    client.model = f"openrouter/{SECRET}"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with caplog.at_level(logging.INFO, logger="whiskeyjack_bot.forecast.generate"):
            result = _generate(client, planted, prompt)
    captured = capsys.readouterr()
    assert SECRET not in captured.err
    assert SECRET not in captured.out
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert all(SECRET not in str(w.message) for w in caught)
    assert FAKE_KEY not in repr(result)
    # It does reach model settings, which is the field that exists to record it.
    assert result.settings.name == f"openrouter/{SECRET}"


def test_a_malformed_question_id_leaks_through_no_channel(
    config: AppConfig,
    prompt: LoadedPrompt,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A string question_id used to reach logging's %d and print itself to stderr in
    logging's "--- Logging error ---" report, which a test checking only the exception
    and caplog would miss."""
    client = _Model(good_reply())
    question = _question()
    object.__setattr__(question, "question_id", f"Q-{SECRET}")
    with caplog.at_level(logging.INFO, logger="whiskeyjack_bot.forecast.generate"):
        with pytest.raises(ForecastGenerationError) as excinfo:
            _generate(client, config, prompt, question=question)
    captured = capsys.readouterr()
    assert not _leaks(excinfo.value, SECRET)
    assert SECRET not in captured.err
    assert SECRET not in captured.out
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert client.calls == []


def test_the_api_key_never_reaches_the_result(
    config: AppConfig, prompt: LoadedPrompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.model.api_key_env, FAKE_KEY)
    result = _generate(_Model(good_reply()), config, prompt)
    assert FAKE_KEY not in json.dumps(result.settings.__dict__)
    assert FAKE_KEY not in result.request
    assert all(FAKE_KEY not in raw for raw in result.raw_responses)


def test_configured_logging_redacts_the_model_key(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    configure_logging(config)
    logging.getLogger("litellm").warning("authorization: %s", FAKE_KEY)
    captured = capsys.readouterr()
    file_text = config.logging.file.read_text(encoding="utf-8")
    assert FAKE_KEY not in captured.err
    assert FAKE_KEY not in file_text
    assert "<redacted:OPENROUTER_API_KEY>" in file_text


def test_redaction_filter_covers_the_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    record = logging.LogRecord("any", logging.INFO, __file__, 1, "key is %s", (FAKE_KEY,), None)
    SecretRedactionFilter(["OPENROUTER_API_KEY"]).filter(record)
    assert FAKE_KEY not in record.getMessage()


# --- the import graph ------------------------------------------------------------


def _added_modules(*modules: str) -> set[str]:
    """Modules a fresh interpreter loads for these imports.

    Must run out of process: inside pytest the SDKs are already in ``sys.modules``
    from other suites, so an in-process assertion would pass for the wrong reason.
    """
    imports = ";".join(f"import {name}" for name in modules)
    program = f"import sys;before=set(sys.modules);{imports};print(','.join(sorted(set(sys.modules)-before)))"
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    return {name for name in result.stdout.strip().split(",") if name}


_FORBIDDEN = {
    "forecasting_tools",
    "asknews_sdk",
    "litellm",
    "httpx",
    "requests",
    "urllib.request",
    "http.client",
    "ssl",
}


def test_the_response_schema_reaches_no_provider_client() -> None:
    """M1-406 must replay a stored response and reproduce the parsed forecast with
    zero API calls, and M1-306 established that zero-calls is a property of the import
    graph rather than of a mock count. If the schema cannot reach an SDK, a replay
    path built on it has no call to make."""
    added = _added_modules("whiskeyjack_bot.forecast.schema")
    assert not (added & _FORBIDDEN), sorted(added & _FORBIDDEN)


def test_the_probe_would_notice_a_regression() -> None:
    """The test above passes trivially if the probe is wrong. Importing the call
    module must make it report the SDK."""
    added = _added_modules("whiskeyjack_bot.forecast.generate")
    assert "forecasting_tools" in added
    assert "litellm" in added


# --- DEBUG logging (review round 1, finding 2) -----------------------------------


def _debug_config(config: AppConfig) -> AppConfig:
    return config.model_copy(
        update={"logging": config.logging.model_copy(update={"level": "DEBUG"})}
    )


class _Reply:
    """Stands in for the SDK's own response object; ``invoke`` reads only ``.data``."""

    def __init__(self, text: str) -> None:
        self.data = text


def _real_client_with_recorded_reply(monkeypatch: pytest.MonkeyPatch, reply: str) -> GeneralLlm:
    """A genuine ``GeneralLlm`` with only its network-facing method replaced.

    The point of using the real class rather than ``_Model`` is that the SDK's own
    logging path is what leaks; a double would route around the very thing under test.
    ``_mockable_direct_call_to_model`` is the seam the package names for this, and it
    sits below both DEBUG lines.
    """

    async def _fake(self: Any, prompt: Any) -> _Reply:
        return _Reply(reply)

    monkeypatch.setattr(GeneralLlm, "_mockable_direct_call_to_model", _fake)
    return GeneralLlm(
        model=MODEL_NAME,
        temperature=0.0,
        timeout=5,
        allowed_tries=1,
        max_tokens=100,
        api_key=FAKE_KEY,
        pass_through_unknown_kwargs=False,
    )


def test_debug_logging_persists_neither_the_packet_nor_the_raw_response(
    config: AppConfig,
    prompt: LoadedPrompt,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``logging.level: DEBUG`` is accepted configuration, and the pinned SDK logs the
    full prompt and the full response at DEBUG through this project's own handlers.

    Two hard constraints at once: a message never echoes field values, and hidden
    chain-of-thought is never persisted -- the raw response is logged *before* the
    schema can reject it, so a reply carrying the deliberation the prompt forbids is
    written to the log file whatever validation later decides.
    """
    packet_marker = "PACKETMARKERAAA"
    output_marker = "OUTPUTMARKERZZZ"
    debug = _debug_config(config)
    configure_logging(debug)
    client = _real_client_with_recorded_reply(
        monkeypatch, f"deliberation the prompt forbids, {output_marker}"
    )

    question = _question(title=f"Will {packet_marker} happen?")
    result = generate_forecast(
        config=debug,
        question=question,
        packet=_packet(),
        prompt=prompt,
        tournament_id="minibench",
        now=NOW,
        client=client,
    )

    captured = capsys.readouterr()
    log_text = debug.logging.file.read_text(encoding="utf-8")
    assert result.failure_code == "malformed_response"
    # The markers really were in play, so the assertions below are not vacuous.
    assert packet_marker in result.request
    assert any(output_marker in raw for raw in result.raw_responses)
    for marker in (packet_marker, output_marker):
        assert marker not in log_text
        assert marker not in captured.err


def test_the_payload_filter_keeps_real_diagnostics_from_the_same_libraries(
    config: AppConfig,
) -> None:
    """The filter drops a whole level range, so it has to be shown not to be "drop
    everything from these libraries" -- INFO and above must still reach the log."""
    configure_logging(_debug_config(config))
    logging.getLogger("forecasting_tools.ai_models.general_llm").warning("rate limit reached")
    logging.getLogger("litellm").info("provider selected")
    log_text = config.logging.file.read_text(encoding="utf-8")
    assert "rate limit reached" in log_text
    assert "provider selected" in log_text


def test_this_projects_own_debug_records_are_untouched(config: AppConfig) -> None:
    """The filter is scoped to libraries that see the payload, not to DEBUG at large:
    an operator who turns DEBUG on still gets this project's own diagnostics."""
    configure_logging(_debug_config(config))
    logging.getLogger("whiskeyjack_bot.forecast.generate").debug("our own detail")
    assert "our own detail" in config.logging.file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("logger_name", "level", "kept"),
    [
        ("forecasting_tools.ai_models.general_llm", logging.DEBUG, False),
        ("forecasting_tools", logging.DEBUG, False),
        ("litellm", logging.DEBUG, False),
        ("LiteLLM", logging.DEBUG, False),
        ("litellm.llms.custom_httpx", logging.DEBUG, False),
        ("forecasting_tools.ai_models.general_llm", logging.INFO, True),
        ("httpx", logging.DEBUG, True),
        ("whiskeyjack_bot.forecast.generate", logging.DEBUG, True),
    ],
)
def test_the_payload_filter_truth_table(logger_name: str, level: int, kept: bool) -> None:
    record = logging.LogRecord(logger_name, level, __file__, 1, "payload", None, None)
    assert PayloadDebugFilter().filter(record) is kept


# --- the invocation bound is unconditional (review round 1, finding 1) -----------


@pytest.mark.parametrize("value", [3, 5, 100])
def test_a_config_above_the_bound_is_refused_at_load(value: int) -> None:
    """The criterion is a hard upper bound, so the field that decides it is bounded
    where it fails earliest: at config load, and therefore at verify-env, rather than
    part-way through a forecast that has already been paid for.

    The first cut of this module honoured whatever the field held, and round 1
    reproduced `allowed_tries: 5` buying four repairs.
    """
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = MODEL_NAME
    data["model"]["allowed_tries"] = value
    with pytest.raises(ConfigError):
        validate_config_data(data)


def test_the_committed_default_is_inside_the_bound() -> None:
    """A bound that refused the shipped config would be a different kind of bug."""
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert data["model"]["allowed_tries"] <= MAX_MODEL_INVOCATIONS
    assert MAX_MODEL_INVOCATIONS == 2


def test_the_bound_is_refused_again_at_the_spending_site(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """ModelConfig refuses this at load, so reaching the call site means an AppConfig
    assembled some other way. Repeated here for the reason the Exa adapter repeats its
    configuration check at the point that spends money: a config object carries no
    memory of which validator built it."""
    unbounded = config.model_copy(
        update={
            "model": config.model.model_construct(
                **{**config.model.model_dump(), "allowed_tries": 5}
            )
        }
    )
    assert unbounded.model.allowed_tries == 5
    client = _Model("not json", "still not json", "and again")
    with pytest.raises(ForecastGenerationError):
        _generate(client, unbounded, prompt)
    assert client.calls == [], "refusal must happen before any billable call"


def test_no_configuration_can_buy_a_second_repair(config: AppConfig, prompt: LoadedPrompt) -> None:
    """The criterion, stated as a property of every accepted configuration rather than
    of the committed default: across the whole accepted range, a model that never
    returns usable JSON can never cost more than two calls."""
    for value in range(1, MAX_MODEL_INVOCATIONS + 1):
        bounded = config.model_copy(
            update={"model": config.model.model_copy(update={"allowed_tries": value})}
        )
        client = _Model("no", "no", "no", "no", "no")
        result = _generate(client, bounded, prompt)
        assert result.invocations == value
        assert len(client.calls) == value
        assert result.invocations <= MAX_MODEL_INVOCATIONS
        assert sum(1 for c in client.calls if any(m["role"] == "assistant" for m in c)) <= 1
