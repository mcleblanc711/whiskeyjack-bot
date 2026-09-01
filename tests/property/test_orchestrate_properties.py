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

# **Two title classes are absent, and both absences are findings.** This strategy builds
# through the real model, so anything the schema refuses fails *inside* it -- which is how
# both were caught, and why it is built that way.
#
# 1. **Lone surrogates.** The first version drew from `HOSTILE_TEXT`, which includes them,
#    and every property failed in the strategy: pydantic's `str` refuses a lone surrogate
#    (`string_unicode`), so a validated `CanonicalQuestion` cannot carry one. That demoted
#    `_require_storable` from a live defence to a totality backstop.
# 2. **Blank and whitespace-only titles.** These *were* generated here, and the comment that
#    stood in their place said the schema accepts a lone space because `min_length=1` counts
#    characters. That was true when this file was written and **T-901 made it false**:
#    `title` is now `NonBlankQuestionStr`, which composes the length bound with a strip check.
#    The daily master merge is what surfaced it -- four properties went red in the strategy,
#    not in an assertion. `derive_queries`' own blank-title refusal joins the surrogate guard
#    as a backstop, and the test that pins the *reachable* protection now lives beside the
#    surrogate one.
#
# `group_parent_title` is deliberately **not** narrowed the same way: T-901 tightened `title`
# and `SourceCategory.name` and left the parent a plain `str | None`, so a blank parent is
# still a state a validated question can hold -- which makes `derive_queries`' `if collapsed:`
# branch a live defence rather than a backstop, and it is generated here on purpose.
NON_BLANK = st.sampled_from(["a", "Will X happen?", "  spaced \n out  ", "😀 emoji"])
TITLES = st.one_of(ENCODABLE_TEXT.filter(lambda value: value.strip() != ""), NON_BLANK)
PARENTS = st.none() | TITLES | st.sampled_from(["   ", "\t\n"])


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
    """(1) and (3), together, plus the stronger claim T-901 made available.

    The stronger claim is ``pytest.fail`` rather than an ``event``: **no validated question
    reaches either refusal branch.** Until T-901 tightened ``title`` to
    ``NonBlankQuestionStr`` a whitespace-only title did reach one, so this was genuinely a
    two-armed property. It is now one-armed, and saying so as an assertion is the difference
    between a property that records the fact and one that would go quiet if the schema were
    ever loosened again -- which is exactly how this branch found the surrogate premise, one
    layer earlier.
    """
    try:
        queries = derive_queries(question)
    except OrchestrationError as exc:  # pragma: no cover - the assertion below is the point
        pytest.fail(f"a schema-valid question was refused: {type(exc).__name__}")
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

    **The two arms are the two query counts, and they were not always.** Until T-901 this
    searched for a question ``derive_queries`` *refuses*, and found one: a whitespace-only
    title. ``title`` is now ``NonBlankQuestionStr``, so that arm is unreachable through the
    model rather than merely unvisited -- and an anti-vacuity check whose arm has ceased to
    exist is the failure it is meant to catch, wearing the other hat. It failed for exactly
    that reason on the master merge, which is the check working.

    What is still reachable is the branch that decides what a run is billed for: a group
    sibling contributes the parent-joined query as well as its own, everything else derives
    one. The refusals are pinned past the validator in the unit suite, where a backstop can
    honestly be reached.
    """
    assert not _refuses(find(questions(), lambda question: not _refuses(question)))
    assert len(derive_queries(find(questions(), lambda q: len(derive_queries(q)) == 2))) == 2
    assert len(derive_queries(find(questions(), lambda q: len(derive_queries(q)) == 1))) == 1
