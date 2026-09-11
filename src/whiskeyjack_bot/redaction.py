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


# The process's configured secret names (M1-613), filled by `config.load_config` so that a
# writer holding no config -- `tournament_state.append` has about thirty call sites across
# seven modules, most without one -- still redacts what the running profile calls a secret.
# Every live process loads its profile through `load_config` before it touches a ledger. A
# process that never loaded one has no configured secrets, and redacting with an empty set is
# the honest answer for it rather than an error. Additive: registering never forgets a name,
# and redacting more names is never less safe.
_REGISTERED: set[str] = set()


def register_secret_env_var_names(env_var_names: Sequence[str]) -> None:
    """Add configured secret variable names to the process registry."""
    _REGISTERED.update(name for name in env_var_names if isinstance(name, str) and name)


def registered_secret_env_var_names() -> tuple[str, ...]:
    """The registry, sorted so a caller's behaviour cannot depend on set order."""
    return tuple(sorted(_REGISTERED))


def redact_leaves(value: object, env_var_names: Sequence[str]) -> object:
    """Redact every string inside a JSON-shaped value, keys included, and nothing else.

    Leaf by leaf rather than over the rendered JSON text: a secret whose characters also
    occur in a number (an all-digit token, an account ID) would otherwise be substituted into
    the middle of that number, and the result would stop being JSON -- which, behind
    `tournament_events`' `CHECK(json_valid(data))`, is a failed write that stops the worker.
    """
    if isinstance(value, str):
        return redact_secrets(value, env_var_names)
    if isinstance(value, dict):
        return {
            (redact_secrets(key, env_var_names) if isinstance(key, str) else key): redact_leaves(
                item, env_var_names
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_leaves(item, env_var_names) for item in value]
    return value
