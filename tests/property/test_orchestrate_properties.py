"""Properties of ``research/orchestrate.py``'s one pure function (M1-315).

``derive_queries`` is the only new pure function on this branch, and it is the one that
decides what a retrieval run costs: AskNews bills two calls per query. It is also the first
thing on the paid path that touches untrusted content -- a question title comes from a
snapshot file, which came from provider JSON.

The four invariants ``CLAUDE.md`` asks of any new pure function, minus the one that does not
apply (there is no ordering claim to make total):

1. nothing but :class:`OrchestrationError` escapes it, on any input;
2. it is deterministic, so two runs of the same question bill for the same queries;
3. every query it returns is storable -- which is what makes the refusal in (1) meaningful,
   since the alternative is a ``UnicodeEncodeError`` from the sqlite3 binding layer *after*
   the run has been paid for;
4. no message echoes the title, the parent title, or any part of either.

Each property carries an anti-vacuity ``event``/assertion, because a strategy that never
reaches the branch an assertion is about is this project's most expensive recurring defect
(``docs/LESSONS.md``); the surrogate arm in particular is easy to generate and easy to miss.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from hypothesis import HealthCheck, event, find, given, settings
from hypothesis import strategies as st
from strategies import ENCODABLE_TEXT, SURROGATE_TEXT

from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.orchestrate import OrchestrationError, derive_queries

MARKER = "Qz7leakcanaryZq"

# `min_length=1` on the schema, so a title strategy has to produce something. Blank and
# whitespace-only titles are generated deliberately, because the schema accepts a lone space
# and a lone space is not a query.
#
# **Lone surrogates are absent, and their absence is the finding.** The first version of this
# strategy drew from `HOSTILE_TEXT`, which includes them, and every property failed *inside
# the strategy*: pydantic's `str` refuses a lone surrogate (`string_unicode`), so a validated
# `CanonicalQuestion` cannot carry one. Building through the real model rather than a stub is
# what surfaced that, and it demoted `_require_storable` from a live defence to a totality
# backstop. The backstop still has a property below -- reached the only way it can be, past
# the validator.
TITLES = st.one_of(
    ENCODABLE_TEXT.filter(lambda value: value != ""),
    st.sampled_from(["   ", "\t\n", "a", "Will X happen?", "  spaced \n out  ", "😀 emoji"]),
)
PARENTS = st.none() | TITLES


@st.composite
def questions(draw: st.DrawFn) -> CanonicalBinaryQuestion:
    """A schema-valid canonical question over the title classes a snapshot can produce.

    Built through the real model rather than a stub, so every title this yields is one
    normalization could really hand over -- which is the whole point of asking what
    ``derive_queries`` does with untrusted content, and is what caught the claim above.
    """
    return CanonicalBinaryQuestion(
        question_id=draw(st.integers(min_value=1, max_value=10**6)),
        post_id=draw(st.integers(min_value=1, max_value=10**6)),
        title=draw(TITLES),
        group_parent_title=draw(PARENTS),
    )


def storable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(question=questions())
def test_only_the_modules_own_error_escapes(question: CanonicalBinaryQuestion) -> None:
    """(1) and (3), together: it either returns storable queries or raises its own type."""
    try:
        queries = derive_queries(question)
    except OrchestrationError:
        event("refused")
        return
    event("derived")
    assert queries
    for query in queries:
        assert query.strip() == query and query, "a query is non-blank and trimmed"
        assert " ".join(query.split()) == query, "whitespace is collapsed exactly once"
        assert storable(query), "a query that cannot be bound would fail after the spend"


@given(question=questions())
def test_the_result_is_deterministic(question: CanonicalBinaryQuestion) -> None:
    """(2). Two runs of one question must bill for the same queries, or a rerun costs more
    than the run it repeats."""
    try:
        first = derive_queries(question)
    except OrchestrationError:
        with pytest.raises(OrchestrationError):
            derive_queries(question)
        return
    assert derive_queries(question) == first
    assert len(set(first)) == len(first), "a repeated query would be billed twice"


@given(
    title=st.builds(lambda a, b: f"{a}{MARKER}{b}", ENCODABLE_TEXT, ENCODABLE_TEXT),
    surrogate=SURROGATE_TEXT,
    in_parent=st.booleans(),
)
def test_no_message_echoes_the_question_text(title: str, surrogate: str, in_parent: bool) -> None:
    """(4), and the only exercise of the totality backstop.

    The unstorable half is what forces a message at all, so the canary is planted in the
    *other* field -- the one that is perfectly renderable and still must not be rendered.
    """
    # `model_construct`, because the validator refuses what this property is about. That is
    # the documented shape of the backstop -- a caller holding an object built past the
    # validator -- and not a claim that a snapshot can produce one.
    question = CanonicalBinaryQuestion.model_construct(
        question_id=1,
        post_id=1,
        title=(title if in_parent else f"{title}{surrogate}"),
        group_parent_title=(f"{title}{surrogate}" if in_parent else None),
    )
    with pytest.raises(OrchestrationError) as caught:
        derive_queries(question)
    rendered = f"{caught.value}{caught.value.args}"
    assert MARKER not in rendered
    assert surrogate not in rendered


def test_the_leak_canary_can_actually_fail() -> None:
    """The canary must be able to appear in a message, or the property above proves nothing."""
    assert MARKER in f"{OrchestrationError(f'title was {MARKER}')}"


@given(question=questions())
def test_every_derived_query_can_reach_the_ledger(question: CanonicalBinaryQuestion) -> None:
    """(3) again, against the layer that would actually have raised.

    ``research_runs.queries_json`` is where these land, so the claim is checked by binding
    them -- the exact operation whose ``UnicodeEncodeError`` quotes its input.
    """
    try:
        queries = derive_queries(question)
    except OrchestrationError:
        return
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("SELECT ?", (json.dumps(list(queries)),))
    finally:
        connection.close()


def _refuses(question: CanonicalBinaryQuestion) -> bool:
    try:
        derive_queries(question)
    except OrchestrationError:
        return True
    return False


def test_the_strategy_reaches_both_arms() -> None:
    """Anti-vacuity for the whole file: the strategy must produce both outcomes.

    Written with ``find`` rather than by accumulating ``event`` tags across the other tests,
    because that version depended on those tests having already run -- an anti-vacuity check
    that is itself order-dependent is one more thing that can quietly stop checking. This one
    searches for each arm on its own and raises if it cannot reach one.

    If this fails, every property above has been passing on a single arm, which is the defect
    class this project keeps paying for rather than a nuisance.
    """
    assert _refuses(find(questions(), _refuses))
    assert not _refuses(find(questions(), lambda question: not _refuses(question)))
