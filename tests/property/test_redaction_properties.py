"""Property tests for the shared secret-redaction primitive (M1-605).

The ``CLAUDE.md`` pre-review fuzz pass for a pure function: never raises (including on the
hostile/surrogate text that has broken other text-handling code in this project before), and
no leak -- a planted secret value never survives redaction when its environment variable is
configured and set.
"""

from __future__ import annotations

import json
import os

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.redaction import redact_leaves, redact_secrets

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


# --------------------------------------------------------------------------------------
# M1-613: `redact_leaves`, the structural walk `tournament_state.append` applies to every
# payload before it is persisted. Round 1 found the first draft recursing, which lowered the
# nesting depth the journal accepted below what the base stored, so depth is a property here
# and not only a regression test.
# --------------------------------------------------------------------------------------

JSON_LEAVES = st.one_of(
    HOSTILE_TEXT,
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
)

JSON_VALUES = st.recursive(
    JSON_LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=12,
)


def _plant(value: object, secret: str) -> object:
    """Put the secret somewhere the walk has to reach: a value, a key and a nested list."""
    return {"payload": value, "note": f"echoing {secret}", secret: [{"deep": secret}]}


def _shape(value: object) -> object:
    """Container structure with every leaf *and key* erased -- tuples normalize to lists.

    Keys are erased because redacting them is the point, so their text is expected to
    change. Insertion order carries the correspondence instead, which the walk preserves.
    (These strategies cannot make two keys redact to the same text, since generated keys
    are at most 8 characters and the planted secret is far longer. M1-339's collision
    properties at the end of this file draw keys that do.)
    """
    if isinstance(value, dict):
        return {"?": [_shape(v) for v in value.values()]}
    if isinstance(value, (list, tuple)):
        return ["?", [_shape(v) for v in value]]
    return "leaf"


@given(value=JSON_VALUES, names=st.lists(st.text(max_size=12), max_size=4))
@settings(max_examples=200, deadline=None)
def test_leaves_never_raises(value: object, names: list[str]) -> None:
    redact_leaves(value, names)


@given(value=JSON_VALUES)
@_STATIC_ENV_SETTINGS
def test_a_planted_secret_never_survives_the_walk(value: object) -> None:
    """The no-leak property, guarded against vacuity: the secret is asserted *present* in the
    rendered input before it is asserted absent from the rendered output."""
    planted = _plant(value, FAKE_SECRET)
    assert FAKE_SECRET in json.dumps(planted, default=repr)

    redacted = redact_leaves(planted, [SECRET_ENV_VAR])
    assert FAKE_SECRET not in json.dumps(redacted, default=repr)


@given(value=JSON_VALUES)
@_STATIC_ENV_SETTINGS
def test_the_walk_preserves_structure(value: object) -> None:
    """Redaction rewrites leaves, never the shape: a dropped branch would be a silently
    truncated journal entry rather than a visible failure."""
    planted = _plant(value, FAKE_SECRET)
    assert _shape(redact_leaves(planted, [SECRET_ENV_VAR])) == _shape(planted)


@given(value=JSON_VALUES)
@settings(max_examples=100, deadline=None)
def test_no_configured_names_is_the_identity_on_structures(value: object) -> None:
    """A process that loaded no profile stores what it was given (tuples aside)."""
    assert redact_leaves(value, []) == value


@given(value=JSON_VALUES)
@_STATIC_ENV_SETTINGS
def test_the_walk_survives_nesting_deeper_than_the_recursion_limit(value: object) -> None:
    """The round-1 blocker as a property. `append` accepts `dict[str, Any]` with no declared
    nesting limit, so the walk must not be the thing that sets one: 900 levels is past what
    any recursive implementation survives at the default limit, and well inside the depth
    `json.dumps` accepts (measured: first failure at 993 on both the base and this tree)."""
    nested: object = _plant(value, FAKE_SECRET)
    for _ in range(900):
        nested = {"nested": nested}

    redacted = redact_leaves(nested, [SECRET_ENV_VAR])
    for _ in range(900):
        assert isinstance(redacted, dict)
        redacted = redacted["nested"]
    assert FAKE_SECRET not in json.dumps(redacted, default=repr)


# --------------------------------------------------------------------------------------
# M1-339: two distinct keys that redact to the same text. The documented representation is
# that both entries survive, the later one under "<key><collision:N>".
# --------------------------------------------------------------------------------------

_MARKER = f"<redacted:{SECRET_ENV_VAR}>"

# Keys assembled from fragments that include the secret *and its marker*, so a key holding
# the secret and a key already spelling the marker are both drawable: those are the pairs
# that collide. Short plain fragments keep ordinary keys distinct most of the time.
_COLLIDING_KEYS = st.lists(
    st.sampled_from(["a", "b", FAKE_SECRET, _MARKER, "<collision:2>"]), min_size=1, max_size=3
).map("".join)

_COLLIDING_VALUES = st.recursive(
    JSON_LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(_COLLIDING_KEYS, children, max_size=5),
    ),
    max_leaves=10,
)


def _entry_counts(value: object) -> object:
    """Every mapping's entry count, in walk order: the thing a collision used to shrink."""
    if isinstance(value, dict):
        return [len(value), [_entry_counts(v) for v in value.values()]]
    if isinstance(value, (list, tuple)):
        return [_entry_counts(v) for v in value]
    return None


def _collides(value: object) -> bool:
    if isinstance(value, dict):
        redacted = [redact_secrets(k, [SECRET_ENV_VAR]) for k in value]
        return len(set(redacted)) < len(redacted) or any(_collides(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_collides(v) for v in value)
    return False


@given(value=st.dictionaries(_COLLIDING_KEYS, _COLLIDING_VALUES, min_size=1, max_size=5))
@_STATIC_ENV_SETTINGS
def test_colliding_keys_keep_every_entry_and_leak_nothing(value: dict[str, object]) -> None:
    collides = _collides(value)
    event(f"collision: {collides}")
    redacted = redact_leaves(value, [SECRET_ENV_VAR])
    # Every entry survives: no mapping lost a key to a collision.
    assert _entry_counts(redacted) == _entry_counts(value)
    rendered = json.dumps(redacted, ensure_ascii=True, sort_keys=True)
    assert FAKE_SECRET not in rendered
    # Deterministic, which check_storage relies on (the witness file and the row are two
    # separate calls), and replay-stable through the persisted form. Compared as bytes: a
    # surrogate *pair* drawn as two code points loads back as one, so the objects may differ
    # while the persisted form, which is what replay reads, cannot (M1-306).
    assert redact_leaves(value, [SECRET_ENV_VAR]) == redacted
    assert json.dumps(json.loads(rendered), ensure_ascii=True, sort_keys=True) == rendered
