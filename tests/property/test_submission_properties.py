"""Property tests for the idempotency-key derivation and its readers (M2-702).

The CLAUDE.md pre-review fuzz pass for a hash/canonicalizer: never raises outside the
module's own error type, replay-stability across the persisted form, a stated ordering or
identity claim, and no value leak in any message *or rendered traceback*. Two are specific
to this item:

**The canonical material is injective over the accepted domain.** SHA-256 non-collision is
not testable and asserting it would be theatre; what *is* testable, and what actually
decides whether two submissions share a key, is that two different accepted inputs render
to two different strings. The strategies are deliberately small pools chosen so that a
field-boundary smear is *reachable* -- `("a", 1, 21)` and `("a", 12, 1)` concatenate to the
same text, and a derivation that joined its inputs without delimiters would pass a fuzz
pass over unconstrained draws while failing here.

**The readers never write.** A duplicate check that mutated the ledger would be a defect
the unit tests would not name, because they only look at the cases they thought of.

Every property here was re-run against a deliberately naive derivation and confirmed to
fail first; three of M1-303's ten new properties passed against the pre-fix tree
(docs/LESSONS.md, lesson 5).
"""

from __future__ import annotations

import itertools
import re
import sqlite3
import traceback
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from strategies import ENCODABLE_TEXT, HOSTILE_TEXT

from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.submission import (
    SubmissionError,
    attempt_for_key,
    canonical_key_json,
    require_key_unused,
    submission_key,
    submission_key_for_approved_record,
    submission_key_for_record,
)

TS = "2026-08-20T00:00:00.000000+00:00"
WHEN = datetime(2026, 8, 20, tzinfo=timezone.utc)
SHA = "b" * 64
DIGEST = "d" * 64
PLANTED_SECRET = "privateFAKE123456"

KEY_RE = re.compile(r"^wjsub-1-[0-9a-f]{64}\Z")

# Anything at all, in any position a caller controls.
ANYTHING = st.one_of(
    HOSTILE_TEXT,
    st.none(),
    st.booleans(),
    st.integers(),
    st.integers(min_value=2**63, max_value=2**80),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=8),
    st.lists(st.integers(), max_size=3),
    st.datetimes(),
    st.sampled_from([SHA, DIGEST, "", "x" * 201, object()]),
)

# The accepted domain, spelled as strategies rather than as a filter, so that what the
# module promises about it is visible here.
TOURNAMENTS = ENCODABLE_TEXT.filter(lambda text: 0 < len(text) <= 200)
# The subset a row may actually hold. `submission_key` accepts any non-blank-by-length
# tournament and derives a key from it in memory, but `006_non_blank_identifiers.sql`
# refuses a whitespace-only `forecast_records.tournament_id` at INSERT -- so a property
# that *seeds a row* has a narrower domain than one that only derives.
#
# Latent since 006 rather than new: `ENCODABLE_TEXT` has always been able to produce " ",
# and this suite only stopped drawing one by luck. Surfaced while adding M1-602's
# `007_forecast_version_chain.sql`, which changed nothing about this clause. `str.strip()`
# is the right comparison because 006's trim() set is exactly the codepoints Python calls
# whitespace -- that correspondence is what 004's header spells out and why it enumerates
# them instead of calling one-argument trim().
SEEDABLE_TOURNAMENTS = TOURNAMENTS.filter(lambda text: text.strip() != "")
IDENTIFIER_INTS = st.integers(min_value=1, max_value=2**63 - 1)
DIGESTS = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

# A collision property needs *colliding draws*, and that is a constraint on the pool, not
# a hope about the search (M1-303: three of ten new properties passed against broken code).
# These six tuples contain three pairs that concatenate identically under the obvious naive
# derivation, `tournament_id + question_id + forecast_version`::
#
#     ("a",  1,  21)  and  ("a",  12, 1)   -> "a121"
#     ("a1", 21, 1)   and  ("a",  121, 1)  -> "a1211"
#     ("12", 1,  1)   and  ("1",  21, 1)   -> "1211"
#
# Six of the thirty-six ordered pairs therefore collide under a delimiter-free join, so the
# property reaches the failure in a handful of draws rather than never. The pairs are also
# asserted directly below, so the claim does not depend on the draw at all.
SMEAR_CANDIDATES: list[tuple[str, int, int, str]] = [
    ("a", 1, 21, "a" * 64),
    ("a", 12, 1, "a" * 64),
    ("a1", 21, 1, "a" * 64),
    ("a", 121, 1, "a" * 64),
    ("12", 1, 1, "a" * 64),
    ("1", 21, 1, "a" * 64),
]

# Values that always carry an unmistakable marker. A leak property drawn from
# unconstrained input asserts "repr(value) not in the traceback", and for a short repr --
# `0`, `1`, `[]` -- that substring is in every traceback already, from line numbers and
# source text. The property then fails for a reason that has nothing to do with leaking.
# Planting a distinctive token is the same technique M1-301 used for the secret scanner,
# and it is the only version of this property that means what it says.
# Every shape here is refused in *every* one of the four positions, so the property below
# is never vacuous: it always reaches a refusal, and the refusal is always the thing under
# test. `test_the_leaky_shapes_are_all_actually_refused` pins that claim rather than
# leaving it as a comment.
LEAKY_REFUSED = st.one_of(
    st.just(PLANTED_SECRET.encode()),
    st.lists(st.just(PLANTED_SECRET), min_size=1, max_size=2),
    st.dictionaries(st.just(PLANTED_SECRET), st.just(1), min_size=1),
    st.just(PLANTED_SECRET * 30),  # over the 200-character bound in every text position
    st.builds(lambda a: f"\ud800{PLANTED_SECRET}{a}"[:200], HOSTILE_TEXT),
)

# A secret-bearing string that is exactly 64 characters and is not hex: accepted-looking
# right up to the last check, and refused only in the digest position.
NON_HEX_DIGEST = st.just(f"{PLANTED_SECRET}{'a' * (64 - len(PLANTED_SECRET))}")

ACCEPTED = st.tuples(TOURNAMENTS, IDENTIFIER_INTS, IDENTIFIER_INTS, DIGESTS)
SMEARED = st.sampled_from(SMEAR_CANDIDATES)


def _key(values: tuple[str, int, int, str]) -> str:
    tournament_id, question_id, forecast_version, digest = values
    return submission_key(
        tournament_id=tournament_id,
        question_id=question_id,
        forecast_version=forecast_version,
        request_payload_sha256=digest,
    )


def _material(values: tuple[str, int, int, str]) -> str:
    tournament_id, question_id, forecast_version, digest = values
    return canonical_key_json(
        tournament_id=tournament_id,
        question_id=question_id,
        forecast_version=forecast_version,
        request_payload_sha256=digest,
    )


# --------------------------------------------------------------------------------------
# One session ledger. Same scope and same reasoning as `test_approval_properties.py`:
# the fixture is shared across every example anyway, and the ledger is append-only, so
# records written by one example cannot disturb another.
# --------------------------------------------------------------------------------------

_CONNECTION: sqlite3.Connection | None = None
_COUNTER = itertools.count()


@pytest.fixture(scope="session", autouse=True)
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    global _CONNECTION
    db = tmp_path_factory.mktemp("submission-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    _CONNECTION = conn
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES ('run-1', 'asknews', 1, ?, ?)",
            (TS, TS),
        )
        yield conn
    finally:
        _CONNECTION = None
        conn.close()


def _conn() -> sqlite3.Connection:
    assert _CONNECTION is not None
    return _CONNECTION


# The head of each seeded chain, keyed by the pair `001` declares UNIQUE with the version.
_CHAIN_HEADS: dict[tuple[str, int], tuple[str, int]] = {}


def _seed(tournament_id: str, question_id: int) -> tuple[str, int]:
    """Append one draft record to this question's chain; return its id and version.

    `001` declares UNIQUE (question_id, tournament_id, forecast_version) and `004` indexes
    `attempt_id` UNIQUE, so no two seeded rows may collide -- hypothesis reuses a shrunk
    draw across examples, and two examples drawing the same tournament and question land on
    the same constraint. `forecast_version` is still a real input to the derivation: the
    caller is handed back the value that was stored and derives from it.

    This used to take the version from a global counter, which produced a version 5 with no
    versions 1-4 behind it. M1-602's `007_forecast_version_chain.sql` refuses that, and
    rightly: it is a chain with a hole in it. So the seed keeps a real chain per
    (tournament, question) and appends to it, which satisfies both the UNIQUE constraint
    and the parent clause, and is what the ledger actually looks like.
    """
    serial = next(_COUNTER)
    record_id = f"rec-{serial}"
    parent_record_id, parent_version = _CHAIN_HEADS.get((tournament_id, question_id), (None, 0))
    forecast_version = parent_version + 1
    _conn().execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, parent_record_id, "
        "question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, ?, ?, ?, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
        "'run-1', ?, '{}', '{}', ?, ?, ?)",
        (
            record_id,
            question_id,
            tournament_id,
            forecast_version,
            parent_record_id,
            TS,
            TS,
            SHA,
            record_id,
        ),
    )
    _CHAIN_HEADS[(tournament_id, question_id)] = (record_id, forecast_version)
    return record_id, forecast_version


def _row_counts() -> tuple[int, ...]:
    conn = _conn()
    return tuple(
        int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in ("forecast_records", "submission_attempts", "lifecycle_events")
    )


def _rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# --------------------------------------------------------------------------------------
# Invariant 1: nothing raises outside SubmissionError.
# --------------------------------------------------------------------------------------


@given(value=ANYTHING, position=st.integers(min_value=0, max_value=3))
def test_the_derivation_raises_only_submission_error(value: object, position: int) -> None:
    arguments: list[Any] = ["minibench", 100, 1, DIGEST]
    arguments[position] = value
    try:
        submission_key(
            tournament_id=arguments[0],
            question_id=arguments[1],
            forecast_version=arguments[2],
            request_payload_sha256=arguments[3],
        )
    except SubmissionError:
        pass


@given(value=ANYTHING)
def test_the_readers_raise_only_submission_error(value: object) -> None:
    conn = _conn()
    for call in (
        lambda: attempt_for_key(conn, value),  # type: ignore[arg-type]
        lambda: require_key_unused(conn, value),  # type: ignore[arg-type]
        lambda: submission_key_for_record(conn, value, request_payload_sha256=DIGEST),  # type: ignore[arg-type]
        lambda: submission_key_for_approved_record(conn, value, request_payload_sha256=DIGEST),  # type: ignore[arg-type]
        lambda: submission_key_for_record(
            conn,
            "rec-absent",
            request_payload_sha256=value,  # type: ignore[arg-type]
        ),
        lambda: submission_key_for_approved_record(
            conn,
            "rec-absent",
            request_payload_sha256=value,  # type: ignore[arg-type]
        ),
    ):
        try:
            call()
        except SubmissionError:
            pass


# --------------------------------------------------------------------------------------
# Invariant 2: the derivation is a function of exactly its four inputs.
# --------------------------------------------------------------------------------------


@given(values=ACCEPTED)
def test_the_same_inputs_always_give_the_same_key(values: tuple[str, int, int, str]) -> None:
    assert _key(values) == _key(values)


@given(values=ACCEPTED)
def test_every_accepted_input_yields_a_well_formed_key(values: tuple[str, int, int, str]) -> None:
    key = _key(values)
    assert KEY_RE.match(key)
    # The writer's bound (`lifecycle._MAX_IDENTIFIER`), restated as a number rather than
    # imported: a private constant imported to assert against tests the constant.
    assert len(key) <= 200


@given(left=SMEARED, right=SMEARED)
def test_the_canonical_material_is_injective_over_the_accepted_domain(
    left: tuple[str, int, int, str], right: tuple[str, int, int, str]
) -> None:
    """Different accepted inputs render to different strings, and equal ones to equal.

    Stated both ways round on purpose. "Different inputs differ" alone is satisfied by a
    derivation that also makes *equal* inputs differ, which would mint a new key on every
    replay -- the failure this whole module exists to prevent.
    """
    assert (_material(left) == _material(right)) == (left == right)
    assert (_key(left) == _key(right)) == (left == right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (SMEAR_CANDIDATES[0], SMEAR_CANDIDATES[1]),
        (SMEAR_CANDIDATES[2], SMEAR_CANDIDATES[3]),
        (SMEAR_CANDIDATES[4], SMEAR_CANDIDATES[5]),
    ],
)
def test_field_boundaries_cannot_be_smeared(
    left: tuple[str, int, int, str], right: tuple[str, int, int, str]
) -> None:
    """The three pairs named above, asserted without a draw.

    `("a", 1, 21)` and `("a", 12, 1)` are two different submissions of two different
    questions. A derivation that joined its fields without delimiters would give them one
    idempotency key, and the second would be refused as a duplicate of the first -- a
    forecast silently never posted, which is the worst failure this module can have.
    """
    assert left != right
    assert _material(left) != _material(right)
    assert _key(left) != _key(right)


@given(left=ACCEPTED, right=ACCEPTED)
def test_equal_inputs_give_equal_keys_over_the_whole_accepted_domain(
    left: tuple[str, int, int, str], right: tuple[str, int, int, str]
) -> None:
    """The other direction of the identity claim, over the full domain rather than the
    smear pool: two derivations agree exactly when their inputs do."""
    assert (_key(left) == _key(right)) == (_material(left) == _material(right))
    if left == right:
        assert _key(left) == _key(right)


@given(values=ACCEPTED, other=DIGESTS)
def test_a_changed_payload_hash_always_changes_the_key(
    values: tuple[str, int, int, str], other: str
) -> None:
    assume(other != values[3])
    assert _key((values[0], values[1], values[2], other)) != _key(values)


# --------------------------------------------------------------------------------------
# Invariant 3: replay-stability across the persisted form.
# --------------------------------------------------------------------------------------


@given(tournament_id=SEEDABLE_TOURNAMENTS, question_id=IDENTIFIER_INTS, digest=DIGESTS)
@settings(max_examples=100)
def test_a_key_survives_the_store_and_load_round_trip(
    tournament_id: str, question_id: int, digest: str
) -> None:
    """Derived in memory, then re-derived from what SQLite hands back.

    This is M1-305's rule applied to the key: a derivation carrying a distinction JSON or
    SQLite drops is stable in memory and changes here, and that is exactly the hash that
    passes every test which never went through the ledger.
    """
    record_id, forecast_version = _seed(tournament_id, question_id)
    in_memory = submission_key(
        tournament_id=tournament_id,
        question_id=question_id,
        forecast_version=forecast_version,
        request_payload_sha256=digest,
    )
    from_ledger = submission_key_for_record(_conn(), record_id, request_payload_sha256=digest)
    assert from_ledger == in_memory


# --------------------------------------------------------------------------------------
# Invariant 4: the readers never write.
# --------------------------------------------------------------------------------------


@given(value=ANYTHING)
def test_a_read_leaves_the_ledger_where_it_was(value: object) -> None:
    before = _row_counts()
    conn = _conn()
    for call in (
        lambda: attempt_for_key(conn, value),  # type: ignore[arg-type]
        lambda: require_key_unused(conn, value),  # type: ignore[arg-type]
        lambda: submission_key_for_record(conn, value, request_payload_sha256=DIGEST),  # type: ignore[arg-type]
        lambda: submission_key_for_approved_record(conn, value, request_payload_sha256=DIGEST),  # type: ignore[arg-type]
    ):
        try:
            call()
        except SubmissionError:
            pass
    assert _row_counts() == before


# --------------------------------------------------------------------------------------
# Invariant 5: no value reaches a message or a rendered traceback.
# --------------------------------------------------------------------------------------


@given(value=LEAKY_REFUSED, position=st.integers(min_value=0, max_value=3))
def test_a_refused_value_never_reaches_the_message_or_the_traceback(
    value: object, position: int
) -> None:
    """`str(exc)` is not enough: a chained cause reprints the value through the rendered
    traceback, which is why every sanitizing raise in the module uses `from None`."""
    arguments: list[Any] = ["minibench", 100, 1, DIGEST]
    arguments[position] = value
    with pytest.raises(SubmissionError) as excinfo:
        submission_key(
            tournament_id=arguments[0],
            question_id=arguments[1],
            forecast_version=arguments[2],
            request_payload_sha256=arguments[3],
        )
    assert PLANTED_SECRET not in str(excinfo.value)
    assert PLANTED_SECRET not in _rendered(excinfo.value)


@given(value=LEAKY_REFUSED)
def test_a_refused_read_never_reaches_the_message_or_the_traceback(value: object) -> None:
    conn = _conn()
    for call in (
        lambda: attempt_for_key(conn, value),  # type: ignore[arg-type]
        lambda: require_key_unused(conn, value),  # type: ignore[arg-type]
        lambda: submission_key_for_record(conn, value, request_payload_sha256=DIGEST),  # type: ignore[arg-type]
        lambda: submission_key_for_approved_record(conn, value, request_payload_sha256=DIGEST),  # type: ignore[arg-type]
        lambda: submission_key_for_record(
            conn,
            "rec-absent",
            request_payload_sha256=value,  # type: ignore[arg-type]
        ),
        lambda: submission_key_for_approved_record(
            conn,
            "rec-absent",
            request_payload_sha256=value,  # type: ignore[arg-type]
        ),
    ):
        with pytest.raises(SubmissionError) as excinfo:
            call()
        assert PLANTED_SECRET not in str(excinfo.value)
        assert PLANTED_SECRET not in _rendered(excinfo.value)


@given(tournament_id=TOURNAMENTS, digest=DIGESTS)
def test_a_stored_tournament_is_never_echoed_by_a_refusal(tournament_id: str, digest: str) -> None:
    """The stored value reaches the derivation, so a refusal downstream of the read must
    still not print it."""
    record_id, _ = _seed(f"{PLANTED_SECRET}-{tournament_id}"[:200], 1)
    try:
        submission_key_for_record(_conn(), record_id, request_payload_sha256=digest)
    except SubmissionError as exc:
        assert PLANTED_SECRET not in _rendered(exc)


@given(value=NON_HEX_DIGEST)
def test_a_digest_shaped_value_refused_late_is_still_not_echoed(value: str) -> None:
    """Exactly 64 characters and not hex: refused by the last check in `_require_sha256`,
    which is the one place a message could plausibly quote what it was handed."""
    with pytest.raises(SubmissionError) as excinfo:
        submission_key(
            tournament_id="minibench",
            question_id=100,
            forecast_version=1,
            request_payload_sha256=value,
        )
    assert PLANTED_SECRET not in str(excinfo.value)
    assert PLANTED_SECRET not in _rendered(excinfo.value)


@pytest.mark.parametrize(
    "value",
    [
        PLANTED_SECRET.encode(),
        [PLANTED_SECRET],
        {PLANTED_SECRET: 1},
        PLANTED_SECRET * 30,
        "\ud800" + PLANTED_SECRET,
    ],
)
@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_the_leaky_shapes_are_all_actually_refused(value: object, position: int) -> None:
    """The vacuity pin for `LEAKY_REFUSED`.

    A leak property that silently stopped reaching a refusal would pass forever while
    testing nothing -- the failure mode M1-308 and M1-303 both paid for. This asserts the
    premise the property rests on, in every position, without a `try`.
    """
    arguments: list[Any] = ["minibench", 100, 1, DIGEST]
    arguments[position] = value
    with pytest.raises(SubmissionError):
        submission_key(
            tournament_id=arguments[0],
            question_id=arguments[1],
            forecast_version=arguments[2],
            request_payload_sha256=arguments[3],
        )


@given(tournament_id=SEEDABLE_TOURNAMENTS, digest=DIGESTS)
def test_the_gated_seam_never_mints_a_key_for_an_unapproved_record(
    tournament_id: str, digest: str
) -> None:
    """Over the whole accepted domain, not just the states the unit tests name.

    Every record this fixture seeds is a `draft`, and a draft holds no approval in force,
    so the gated seam must refuse every one of them -- whatever the tournament, question
    or payload happens to be.
    """
    record_id, _ = _seed(tournament_id, 1)
    with pytest.raises(SubmissionError, match="holds no approval in force"):
        submission_key_for_approved_record(_conn(), record_id, request_payload_sha256=digest)
