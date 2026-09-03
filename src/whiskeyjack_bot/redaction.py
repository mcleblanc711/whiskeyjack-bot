"""Secret-value redaction shared by the logging layer and the ledger/artifact writers (M1-605).

:func:`redact_secrets` is the one substitution rule in the project: replace the *value* of
a named environment variable, wherever it appears in a piece of text, with
``<redacted:VAR_NAME>``. ``logging_setup.py`` already did this for log records; this module
is that same rule pulled out so ``lifecycle.py`` and ``forecast/artifacts.py`` can apply it
to what they persist, without either of them depending on ``logging`` or on each other.

Imports nothing from this package, the same posture as ``bounds.py`` -- ``lifecycle.py``
takes no dependency on ``config.py`` by staying at a plain ``Sequence[str]`` of names rather
than an ``AppConfig``, and this module must not force that choice either way.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

# Values shorter than this are never treated as redactable secrets: replacing a 1-3
# character string would mangle unrelated text far more often than it would protect a real
# credential. Matches the floor `logging_setup.py` already used.
MIN_SECRET_LENGTH = 4


def redact_secrets(text: str, env_var_names: Sequence[str]) -> str:
    """Replace the value of any named environment variable found in *text*.

    Values are re-read from the environment on every call, so a credential set after this
    module is imported is still redacted. Returns *text* itself (identity preserved) when
    nothing matched.
    """
    redacted = text
    for name in env_var_names:
        value = os.environ.get(name)
        if value and len(value) >= MIN_SECRET_LENGTH and value in redacted:
            redacted = redacted.replace(value, f"<redacted:{name}>")
    return redacted
