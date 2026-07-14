"""M0-101 acceptance: client uses the configured token env var and pacing;
the secret is redacted from logs and absent from repr; a missing token fails
before any network attempt."""

import copy
import logging
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.logging_setup import SecretRedactionFilter, configure_logging
from whiskeyjack_bot.metaculus.client import MissingCredentialError, build_client

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_TOKEN = "fake-metaculus-token-abcdef123456"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    return validate_config_data(data)


def test_missing_token_raises_named_error_without_network(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        build_client(config)
    assert "METACULUS_TOKEN" in str(excinfo.value)
    assert excinfo.value.env_var_name == "METACULUS_TOKEN"


def test_empty_token_counts_as_missing(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", "")
    with pytest.raises(MissingCredentialError):
        build_client(config)


def test_client_config_plumb_through(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    client = build_client(config)
    assert client.base_url == "https://www.metaculus.com/api"
    assert client.timeout == 30
    assert client.sleep_time_between_requests_min == 3.5
    assert client.sleep_jitter_seconds == 1.0
    assert client.token == FAKE_TOKEN


def test_token_absent_from_repr_and_str(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    client = build_client(config)
    assert FAKE_TOKEN not in repr(client)
    assert FAKE_TOKEN not in str(client)


def test_custom_token_env_name_honored(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openrouter/test-model"
    data["metaculus"]["token_env"] = "ALTERNATE_METACULUS_TOKEN"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    alt_config = validate_config_data(data)
    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    monkeypatch.delenv("ALTERNATE_METACULUS_TOKEN", raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        build_client(alt_config)
    assert excinfo.value.env_var_name == "ALTERNATE_METACULUS_TOKEN"
    monkeypatch.setenv("ALTERNATE_METACULUS_TOKEN", FAKE_TOKEN)
    assert build_client(alt_config).token == FAKE_TOKEN


# ── redaction filter ─────────────────────────────────────────────────────────


def test_filter_redacts_token_value_in_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    record = logging.LogRecord(
        "any", logging.INFO, __file__, 1, f"posting with header Token {FAKE_TOKEN}", None, None
    )
    SecretRedactionFilter(["METACULUS_TOKEN"]).filter(record)
    assert FAKE_TOKEN not in record.getMessage()
    assert "<redacted:METACULUS_TOKEN>" in record.getMessage()


def test_filter_redacts_interpolated_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    record = logging.LogRecord("any", logging.INFO, __file__, 1, "token is %s", (FAKE_TOKEN,), None)
    SecretRedactionFilter(["METACULUS_TOKEN"]).filter(record)
    assert FAKE_TOKEN not in record.getMessage()


def test_configured_logging_redacts_across_all_loggers(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    configure_logging(config)
    # Simulate a third-party logger (e.g. the SDK) leaking the token.
    logging.getLogger("forecasting_tools.helpers.metaculus_client").warning(
        "auth header: Token %s", FAKE_TOKEN
    )
    captured = capsys.readouterr()
    log_file = config.logging.file
    file_text = log_file.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in captured.err
    assert FAKE_TOKEN not in file_text
    assert "<redacted:METACULUS_TOKEN>" in file_text


def test_exception_text_is_redacted_in_formatted_output(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Cross-review finding 1: str(exc_info[1]) is rendered by the formatter,
    # which the message-level filter cannot reach.
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    configure_logging(config)
    logger = logging.getLogger("whiskeyjack_bot.test_exception_redaction")
    try:
        raise RuntimeError(f"401 unauthorized for Token {FAKE_TOKEN}")
    except RuntimeError:
        logger.exception("request failed")
    captured = capsys.readouterr()
    file_text = config.logging.file.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in captured.err
    assert FAKE_TOKEN not in file_text
    assert "<redacted:METACULUS_TOKEN>" in file_text


def test_configure_logging_is_idempotent(config: AppConfig) -> None:
    configure_logging(config)
    first = [h for h in logging.getLogger().handlers if getattr(h, "_whiskeyjack", False)]
    configure_logging(config)
    second = [h for h in logging.getLogger().handlers if getattr(h, "_whiskeyjack", False)]
    assert len(first) == len(second) == 2
