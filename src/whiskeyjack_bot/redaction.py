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
from typing import Any

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

    The walk keeps an explicit stack rather than recursing (M1-613 round 1). The obvious
    recursive form spent two interpreter frames per level, so it exhausted Python's recursion
    limit at roughly half the nesting depth `json.dumps` accepts -- and since `append` stores
    `canonical(journal_form(data))`, that *lowered* the depth the journal would take. Payloads
    the base persisted began raising a raw `RecursionError` instead. Iterating costs one list
    entry per level and nothing else.
    """
    names = tuple(env_var_names)

    def leaf(item: object) -> object:
        return redact_secrets(item, names) if isinstance(item, str) else item

    def empty(container: dict[Any, Any] | list[Any] | tuple[Any, ...]) -> Any:
        return {} if isinstance(container, dict) else []

    def entries(container: dict[Any, Any] | list[Any] | tuple[Any, ...]) -> Any:
        return iter(container.items()) if isinstance(container, dict) else iter(container)

    if not isinstance(value, (dict, list, tuple)):
        return leaf(value)

    root = empty(value)
    # (source container, its output, an iterator over the source, whether it is a mapping)
    stack: list[tuple[object, Any, Any, bool]] = [
        (value, root, entries(value), isinstance(value, dict))
    ]
    # Only the containers on the *current path*, so a value repeated as two siblings (a DAG,
    # which is ordinary) is fine and only a true cycle is caught. Without this the loop would
    # spin forever on one; raising here reproduces exactly what `json.dumps` already raises on
    # the same input, so `append` behaves as it did before this item.
    on_path = {id(value)}

    while stack:
        source, out, items, is_map = stack[-1]
        try:
            entry = next(items)
        except StopIteration:
            stack.pop()
            on_path.discard(id(source))
            continue

        if is_map:
            key, item = entry
            key = redact_secrets(key, names) if isinstance(key, str) else key
        else:
            key, item = None, entry

        if isinstance(item, (dict, list, tuple)):
            if id(item) in on_path:
                raise ValueError("Circular reference detected")
            child = empty(item)
            on_path.add(id(item))
            stack.append((item, child, entries(item), isinstance(item, dict)))
            placed: Any = child
        else:
            placed = leaf(item)

        if is_map:
            out[key] = placed
        else:
            out.append(placed)

    return root
