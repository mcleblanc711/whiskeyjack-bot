"""Property tests for the shared secret-redaction primitive (M1-605).

The ``CLAUDE.md`` pre-review fuzz pass for a pure function: never raises (including on the
hostile/surrogate text that has broken other text-handling code in this project before), and
no leak -- a planted secret value never survives redaction when its environment variable is
configured and set.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.redaction import redact_secrets

FAKE_SECRET = "fake-planted-secret-value-0123456789"
SECRET_ENV_VAR = "FAKE_REDACTION_TEST_SECRET"

# Set once, not varied per-example: this is deliberately not a `monkeypatch` fixture, which
# hypothesis's function-scoped-fixture health check would flag anyway since it is never reset
# between generated inputs -- a fixed real value is exactly what these properties need.
os.environ[SECRET_ENV_VAR] = FAKE_SECRET

_STATIC_ENV_SETTINGS = settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


@given(text=HOSTILE_TEXT, names=st.lists(st.text(max_size=12), max_size=4))
@settings(max_examples=200, deadline=None)
def test_never_raises(text: str, names: list[str]) -> None:
    redact_secrets(text, names)


@given(prefix=HOSTILE_TEXT, suffix=HOSTILE_TEXT)
@_STATIC_ENV_SETTINGS
def test_a_planted_secret_never_survives_redaction(prefix: str, suffix: str) -> None:
    text = f"{prefix}{FAKE_SECRET}{suffix}"
    redacted = redact_secrets(text, [SECRET_ENV_VAR])
    assert FAKE_SECRET not in redacted
    assert f"<redacted:{SECRET_ENV_VAR}>" in redacted


@given(text=HOSTILE_TEXT)
@settings(max_examples=100, deadline=None)
def test_no_configured_names_is_the_identity(text: str) -> None:
    assert redact_secrets(text, []) == text


@given(prefix=HOSTILE_TEXT, suffix=HOSTILE_TEXT)
@_STATIC_ENV_SETTINGS
def test_redaction_is_idempotent(prefix: str, suffix: str) -> None:
    """A second pass over already-redacted text changes nothing further.

    Once ``FAKE_SECRET`` has been replaced by its marker, the marker itself does not
    contain the secret value, so redacting again is a no-op -- the vacuous-property trap
    (``docs/LESSONS.md``) this test guards against is a redaction that only removes the
    *first* occurrence and leaves a second copy for a second pass to also find.
    """
    text = f"{prefix}{FAKE_SECRET}{suffix}{FAKE_SECRET}"
    once = redact_secrets(text, [SECRET_ENV_VAR])
    twice = redact_secrets(once, [SECRET_ENV_VAR])
    assert FAKE_SECRET not in once
    assert once == twice
