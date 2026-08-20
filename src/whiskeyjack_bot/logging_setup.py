"""JSON logging with secret redaction.

Every handler this module installs redacts twice: :class:`SecretRedactionFilter`
rewrites the record message, and :class:`JsonFormatter` redacts every string
field it serializes — message *and* exception text, which filters cannot reach
because ``exc_info`` is rendered at format time. Both replace the *value* of
any configured credential environment variable with ``<redacted:VAR_NAME>``,
including in records emitted by third-party loggers (the forecasting-tools SDK
logs freely). Redaction is not configurable off (the config schema locks
``redact_secrets: true``).

Redaction covers *credentials*. :class:`PayloadDebugFilter` covers the other thing a
third-party logger can emit — **content**: the pinned ``forecasting-tools`` logs the
whole prompt and the whole raw model response at DEBUG, and ``logging.level: DEBUG``
is accepted configuration. See its docstring (M1-402 review round 1, finding 2).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from whiskeyjack_bot.config import AppConfig

# Values shorter than this are never treated as redactable secrets: replacing
# a 1-3 character string would mangle unrelated log text far more often than
# it would protect a real credential.
_MIN_SECRET_LENGTH = 4

# Third-party loggers that see a model call's payloads. ``forecasting_tools`` logs
# ``f"Invoking model with prompt: {prompt}"`` and ``f"Model responded with: {response}"``
# at DEBUG (``ai_models/general_llm.py``), and litellm sits under it holding the same
# messages. ``httpx``/``httpcore`` are deliberately absent: their DEBUG records carry
# request lines and headers rather than bodies, and any credential in them is already
# handled by the redaction above -- listing them would suppress genuinely useful
# transport diagnostics for no content gain.
_PAYLOAD_LOGGER_PREFIXES = ("forecasting_tools", "litellm", "LiteLLM")


def _redact_text(text: str, env_var_names: list[str]) -> str:
    """Replace the value of any named environment variable found in *text*.

    Values are re-read from the environment on every call so a credential set
    after logging setup is still redacted. Returns *text* itself (identity
    preserved) when nothing matched.
    """
    redacted = text
    for name in env_var_names:
        value = os.environ.get(name)
        if value and len(value) >= _MIN_SECRET_LENGTH and value in redacted:
            redacted = redacted.replace(value, f"<redacted:{name}>")
    return redacted


class SecretRedactionFilter(logging.Filter):
    """Scrub configured credential values out of log record messages.

    Only covers the formatted message; exception text is redacted by
    :class:`JsonFormatter`, which owns every field it serializes.
    """

    def __init__(self, env_var_names: list[str]):
        super().__init__()
        self._env_var_names = list(env_var_names)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed record; let it through unformatted
            return True
        redacted = _redact_text(message, self._env_var_names)
        if redacted is not message:
            record.msg = redacted
            record.args = None
        return True


class PayloadDebugFilter(logging.Filter):
    """Drop sub-INFO records from libraries that log the model call's payloads.

    ``logging.level: DEBUG`` is accepted configuration, and at DEBUG the pinned
    ``forecasting-tools`` writes the full reasoning packet and the full *unvalidated*
    model response through this project's own handlers into
    ``logging.file``. That breaches two hard constraints at once: a message never
    echoes field values, and hidden chain-of-thought is never persisted -- an
    unvalidated response is exactly where deliberation the prompt forbids would
    appear, and it is logged before any schema check can reject it. Reproduced by
    execution in M1-402 review round 1, finding 2.

    **The whole sub-INFO range is dropped, not the two known messages.** Matching
    their text would be a check whose unknown case is "pass": the library rewords a
    log line, or adds a third, and the leak reopens silently with nothing to notice it
    (``docs/LESSONS.md`` #7, and the "close the class, not the reported instance"
    rule from M1-308 round 7). Nothing in that range is worth the exposure -- INFO and
    above from the same libraries still reach the log, so real diagnostics survive.

    It is a **handler** filter rather than a level set on those loggers, because a
    library may raise its own level at any time (litellm does exactly that when its
    verbose flag is set) and would silently undo a ``setLevel``. A filter on the
    handlers this project installs cannot be undone by the library, and those handlers
    are the ones that persist. A library writing to a handler of its own making is
    outside this module's reach and is recorded as a standing risk rather than
    claimed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        return not record.name.startswith(_PAYLOAD_LOGGER_PREFIXES)


class JsonFormatter(logging.Formatter):
    """Serialize records as JSON with every string field redacted.

    Redaction happens here field-by-field, not only in the filter: the filter
    cannot reach exception text (``exc_info`` is rendered at format time), and
    redacting before ``json.dumps`` avoids missing secrets whose characters
    would be escaped differently in the serialized form.
    """

    def __init__(self, env_var_names: list[str]):
        super().__init__()
        self._env_var_names = list(env_var_names)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc_message"] = str(record.exc_info[1])
        redacted = {
            key: _redact_text(value, self._env_var_names) if isinstance(value, str) else value
            for key, value in payload.items()
        }
        return json.dumps(redacted, ensure_ascii=False)


def configure_logging(config: AppConfig) -> None:
    """Install stderr + file JSON handlers with redaction on the root logger.

    Idempotent: handlers installed by a previous call are replaced, not
    stacked.
    """
    root = logging.getLogger()
    root.setLevel(config.logging.level)

    for handler in [h for h in root.handlers if getattr(h, "_whiskeyjack", False)]:
        root.removeHandler(handler)
        handler.close()

    secret_names = config.secret_env_var_names()
    redaction = SecretRedactionFilter(secret_names)
    payload_debug = PayloadDebugFilter()
    formatter = JsonFormatter(secret_names)

    stream_handler = logging.StreamHandler()
    log_file: Path = config.logging.file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")

    for handler in (stream_handler, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redaction)
        handler.addFilter(payload_debug)
        setattr(handler, "_whiskeyjack", True)  # noqa: B010 - marker for idempotent re-setup
        root.addHandler(handler)
