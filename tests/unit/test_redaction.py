"""Unit behavior of the shared secret-redaction primitive (M1-605)."""

from __future__ import annotations

import pytest

from whiskeyjack_bot.redaction import MIN_SECRET_LENGTH, redact_secrets

FAKE_TOKEN = "fake-metaculus-token-abcdef123456"
FAKE_MODEL_KEY = "fake-model-api-key-987654321"


def test_redacts_a_matching_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    text = f"posting with header Token {FAKE_TOKEN}"
    redacted = redact_secrets(text, ["METACULUS_TOKEN"])
    assert FAKE_TOKEN not in redacted
    assert "<redacted:METACULUS_TOKEN>" in redacted


def test_redacts_multiple_configured_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("MODEL_API_KEY", FAKE_MODEL_KEY)
    text = f"token={FAKE_TOKEN} key={FAKE_MODEL_KEY}"
    redacted = redact_secrets(text, ["METACULUS_TOKEN", "MODEL_API_KEY"])
    assert FAKE_TOKEN not in redacted
    assert FAKE_MODEL_KEY not in redacted
    assert "<redacted:METACULUS_TOKEN>" in redacted
    assert "<redacted:MODEL_API_KEY>" in redacted


def test_identity_preserved_when_nothing_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    text = "an ordinary log line with no secret in it"
    assert redact_secrets(text, ["METACULUS_TOKEN"]) is text


def test_unset_env_var_is_never_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    text = "nothing to redact here"
    assert redact_secrets(text, ["METACULUS_TOKEN"]) == text


def test_values_shorter_than_the_floor_are_never_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    short_value = "a" * (MIN_SECRET_LENGTH - 1)
    monkeypatch.setenv("SHORT_SECRET", short_value)
    text = f"value is {short_value}"
    assert redact_secrets(text, ["SHORT_SECRET"]) == text


def test_no_configured_names_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", FAKE_TOKEN)
    text = f"token={FAKE_TOKEN}"
    assert redact_secrets(text, []) == text
