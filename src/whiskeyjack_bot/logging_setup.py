"""JSON logging with secret redaction.

Every handler this module installs carries :class:`SecretRedactionFilter`,
which replaces the *value* of any configured credential environment variable
with ``<redacted:VAR_NAME>`` in the final message — including records emitted
by third-party loggers (the forecasting-tools SDK logs freely). Redaction is
not configurable off (the config schema locks ``redact_secrets: true``).
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


class SecretRedactionFilter(logging.Filter):
    """Scrub configured credential values out of log records.

    Values are re-read from the environment on every record so a credential
    set after logging setup is still redacted.
    """

    def __init__(self, env_var_names: list[str]):
        super().__init__()
        self._env_var_names = list(env_var_names)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed record; let it through unformatted
            return True
        redacted = message
        for name in self._env_var_names:
            value = os.environ.get(name)
            if value and len(value) >= _MIN_SECRET_LENGTH and value in redacted:
                redacted = redacted.replace(value, f"<redacted:{name}>")
        if redacted is not message:
            record.msg = redacted
            record.args = None
        return True


class JsonFormatter(logging.Formatter):
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
        return json.dumps(payload, ensure_ascii=False)


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

    redaction = SecretRedactionFilter(config.secret_env_var_names())
    formatter = JsonFormatter()

    stream_handler = logging.StreamHandler()
    log_file: Path = config.logging.file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")

    for handler in (stream_handler, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redaction)
        setattr(handler, "_whiskeyjack", True)  # noqa: B010 - marker for idempotent re-setup
        root.addHandler(handler)
