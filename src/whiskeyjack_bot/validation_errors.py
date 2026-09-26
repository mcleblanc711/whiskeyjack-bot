"""The one rendering of a pydantic ``ValidationError`` that echoes no input (M0-008).

Every module that catches a ``ValidationError`` rebuilds it as its own sanitized error
through :func:`sanitized_problems`. Before M0-008 seven modules each carried a private
copy of this rule, and they disagreed. ``errors(include_input=False, include_url=False)``
was read as "the offending value cannot escape", and **it is not sufficient**, for two
reasons. M1-602's round-1 review reproduced both by execution:

1. **``msg`` interpolates the input.** A ``union_tag_invalid`` error reads ``Input tag 'X'
   found using 'k' does not match ...``, where ``X`` is the untrusted value, and a
   ``value_error`` carries whatever text the raising code chose. So a pydantic built-in
   ``msg`` is **never** rendered, and neither is a plain ``ValueError`` raised by a
   validator. A problem renders as ``<location>: <type>``, where ``type`` is a slug from
   pydantic's fixed catalogue that carries no input.
2. **``loc`` can itself be input.** Under ``extra="forbid"`` the location of an unexpected
   key *is* that key, and a ``dict`` field's keys are the caller's. So a location part
   survives only if the schema authored it. A string must be a field name declared
   somewhere in the model tree. An int must be a list index, which means the part before
   it names a sequence-valued field. An int anywhere else is a mapping key lifted from the
   input; the case is allowlist's round-5 finding, where an unquoted numeric YAML key
   reached ``loc`` through ``invalid_key``. Anything else is :data:`WITHHELD`.

**Authored messages (D50).** The project's own validators still need to say what is wrong.
``submission.enabled requires require_human_approval: true`` is the whole diagnosis, and
the forecast repair turn is how a model learns that its rationale was too long. So a
validator raises :func:`authored_error` with a slug and a sentence it wrote, and that error
renders as ``<location>: <sentence> [<slug>]``. The sentence is safe to render because of
two guarantees, both enforced by ``tests/unit/test_validation_errors.py`` scanning
``src/``:

- this is the only module that constructs a ``PydanticCustomError`` (the only way a
  non-catalogue type reaches ``err["type"]``);
- every ``authored_error`` call passes a literal slug and a literal sentence, with each
  slug mapping to exactly one sentence. The sentence may be a string constant, an f-string
  over UPPER_CASE module constants only, or a module constant that is itself one of those.

Together they make the type *determine* the text, so rendering it adds nothing an input
could reach. A validator that raises a plain ``ValueError`` still works and is still safe.
It renders as ``value_error`` with no sentence: the default is to withhold, and opting in
is visible in the diff.

Imports nothing from this package, so ``config.py`` (the root of most imports) can use it.
"""

from __future__ import annotations

import types
from collections.abc import Sequence
from typing import Annotated, Any, Final, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticCustomError
from pydantic_core.core_schema import ErrorType

# Substituted for any location part the schema did not author.
WITHHELD: Final = "<withheld>"

# Pydantic's own error vocabulary. A type outside it can only have come from a
# ``PydanticCustomError``, and this module is the only constructor of one in ``src/``.
BUILTIN_ERROR_TYPES: Final[frozenset[str]] = frozenset(get_args(ErrorType))

_SEQUENCE_ORIGINS: Final = (list, tuple, set, frozenset, Sequence)


def authored_error(slug: str, sentence: str) -> PydanticCustomError:
    """A validator error whose type is ``slug`` and whose rendered text is ``sentence``.

    Raise it from inside a pydantic validator. It carries no context, so pydantic
    formats nothing into the sentence. ``slug`` and ``sentence`` must be literals written
    in the source; see the module docstring for how the test suite holds every call site
    to that.
    """
    return PydanticCustomError(slug, sentence)


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    """Every pydantic model reachable from one annotation (promoted from forecast/schema)."""
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for arg in get_args(annotation):
        found.extend(_nested_models(arg))
    return found


def _models(schema: Any) -> list[type[BaseModel]]:
    """Every model in the tree rooted at ``schema``: a model class or a union annotation."""
    seen: list[type[BaseModel]] = []
    stack = _nested_models(schema)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.append(current)
        for field in current.model_fields.values():
            stack.extend(_nested_models(field.annotation))
    return seen


def schema_field_names(schema: Any) -> frozenset[str]:
    """Field names declared anywhere in ``schema`` or a model nested inside it.

    The whole tree rather than the top level: a response four levels deep would otherwise
    withhold ``base_rate.prior_probability`` (a name the schema authored) and turn every
    nested diagnostic into ``<withheld>.<withheld>``. That stays safe because the set is
    still schema-authored only. A key the input invented is in no ``model_fields``
    anywhere, so it is still withheld.
    """
    return frozenset(name for model in _models(schema) for name in model.model_fields)


def _is_sequence(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType, Annotated):
        return any(_is_sequence(arg) for arg in get_args(annotation))
    if annotation in _SEQUENCE_ORIGINS:
        return True
    return origin is not None and isinstance(origin, type) and issubclass(origin, _SEQUENCE_ORIGINS)


def schema_sequence_field_names(schema: Any) -> frozenset[str]:
    """Field names whose value is a sequence, so an int after them is a list index."""
    return frozenset(
        name
        for model in _models(schema)
        for name, field in model.model_fields.items()
        if _is_sequence(field.annotation)
    )


def _location(loc: tuple[int | str, ...], known: frozenset[str], sequences: frozenset[str]) -> str:
    parts: list[str] = []
    previous: int | str | None = None
    for part in loc:
        if isinstance(part, int):
            # A list index only directly after a sequence field; any other int is a key.
            parts.append(str(part) if previous in sequences else WITHHELD)
        else:
            parts.append(part if part in known else WITHHELD)
        previous = part
    return ".".join(parts) or "<root>"


def sanitized_problems(exc: ValidationError, schema: Any) -> list[str]:
    """One ``<location>: <detail>`` line per error, carrying no input.

    ``schema`` is the model class, or the union annotation, that was validated. Its tree
    is what "authored" means for the location parts.
    """
    known = schema_field_names(schema)
    sequences = schema_sequence_field_names(schema)
    problems: list[str] = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        location = _location(err["loc"], known, sequences)
        kind = err["type"]
        if kind in BUILTIN_ERROR_TYPES:
            problems.append(f"{location}: {kind}")
        else:
            problems.append(f"{location}: {err['msg']} [{kind}]")
    return problems
