"""T-902: a mocked Metaculus tournament, driven through fetch into canonical questions.

``CODEX_HANDOFF.md``'s mocked-integration list opens with "MiniBench fetch with
binary/numeric/multiple-choice fixtures" and "group post unpacking". Both halves already
have unit coverage on either side of one seam and nothing across it:
``tests/unit/test_fetch.py`` drives ``fetch_open_questions_live`` against a fake that
returns ``[]``, so nothing it fetches is ever normalized; ``tests/unit/test_questions.py``
normalizes the same fixtures but never goes through ``fetch``; ``tests/unit/test_groups.py``
expands the group post directly from raw JSON. **This module is the composition** --
``resolve_tournament_id`` -> ``build_client`` -> ``get_all_open_questions_from_tournament``
-> ``normalize_questions`` -- on the mixed batch a real MiniBench fetch actually returns.

It is also what ``T-901``'s notes handed over: *"nothing offline proves a real MiniBench
post normalizes to a record of this shape -- that is T-902's mocked-integration surface."*
The questions here are parsed from the committed API posts by the SDK's own
``DataOrganizer``, so the shape under test is the SDK's, not a hand-built stand-in.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fake_platform import (
    BINARY_QUESTION_ID,
    GROUP_POST_ID,
    MULTIPLE_CHOICE_QUESTION_ID,
    NUMERIC_QUESTION_ID,
    FakeTournament,
    flat_questions,
    install_tournament,
    unpacked_group_questions,
)
from forecasting_tools.data_models.questions import DateQuestion, DiscreteQuestion, NumericQuestion

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.metaculus.fetch import fetch_open_questions_live
from whiskeyjack_bot.questions.normalize import normalize_questions

V1_TYPES = {"binary", "multiple_choice", "numeric"}
"""``CODEX_HANDOFF.md:183`` -- ``supported_question_types`` may include only these in v1."""


def test_a_mocked_tournament_yields_one_canonical_question_of_each_v1_type(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first handoff bullet, end to end.

    Asserted as **set equality** rather than membership: a normalizer that dropped the
    multiple-choice question, or emitted a fourth of something, passes every
    ``"binary" in ...`` form of this test (``M1-501``'s vacuity finding).
    """
    tournament = install_tournament(monkeypatch, FakeTournament())

    resolved, fetched = fetch_open_questions_live(config)
    result = normalize_questions(fetched)

    assert tournament.calls, "the fetch never reached the platform"
    assert {q.qtype for q in result.questions} == V1_TYPES
    assert {q.question_id for q in result.questions} == {
        BINARY_QUESTION_ID,
        NUMERIC_QUESTION_ID,
        MULTIPLE_CHOICE_QUESTION_ID,
    }
    assert result.deferrals == ()
    assert resolved.id == config.metaculus.tournament.id


def test_every_fetched_question_keeps_the_identity_the_platform_sent(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normalization must not renumber anything on the way through.

    The pairing is the claim: each canonical question carries the ``(question_id, post_id)``
    of the SDK object it came from. Asserting the ids exist, or that the counts match, would
    pass against a normalizer that transposed two questions' identities.
    """
    install_tournament(monkeypatch, FakeTournament())

    _, fetched = fetch_open_questions_live(config)
    canonical = normalize_questions(fetched).questions

    assert {(q.id_of_question, q.id_of_post) for q in fetched} == {
        (q.question_id, q.post_id) for q in canonical
    }


def test_the_configured_tournament_and_group_mode_reach_the_platform(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D31's precedence, observed at the platform rather than at the resolver.

    ``tests/unit/test_fetch.py`` covers ``resolve_tournament_id`` directly. What it cannot
    show is that the resolved value is the one *sent*: the discriminating form is a config
    whose id differs from the default, so an implementation that ignored the override and
    sent the committed ``"minibench"`` fails. ``group_question_mode`` rides along for the
    same reason the fake defaults it to ``"exclude"`` -- a dropped keyword would otherwise
    read as agreement.
    """
    from fake_platform import config_data

    data = config_data(tmp_path)  # type: ignore[arg-type]
    data["metaculus"]["tournament"]["id"] = "bot-testing-area"
    config = validate_config_data(data)
    tournament = install_tournament(monkeypatch, FakeTournament())

    fetch_open_questions_live(config)

    assert tournament.tournament_ids == ["bot-testing-area"]
    assert tournament.group_modes == [config.metaculus.group_question_mode]
    assert tournament.group_modes == ["unpack_subquestions"]


def test_a_group_posts_siblings_stay_distinct_through_normalization(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second handoff bullet, against ``M1-202``'s actual trap.

    **Named for what it can fail on.** The expansion itself is the SDK's -- a live fetch
    with ``unpack_subquestions`` is what produces these siblings, and
    ``tests/unit/test_groups.py`` is where our mirror of it is held to the SDK's. What this
    asserts is the half that is ours: that normalization carries the group through without
    collapsing it.

    Group expansion deep-copies the parent post, so every sibling carries the **same**
    ``post_id``. A test that keyed on ``post_id`` -- the obvious identity field -- could not
    fail if the siblings collapsed to one question. So both halves are asserted: one
    canonical question per subquestion with **distinct** ``question_id``s, and exactly one
    ``post_id`` across all of them.
    """
    siblings = unpacked_group_questions()
    install_tournament(monkeypatch, FakeTournament(siblings))

    _, fetched = fetch_open_questions_live(config)
    canonical = normalize_questions(fetched).questions

    assert len(canonical) == len(siblings)
    question_ids = [q.question_id for q in canonical]
    assert len(set(question_ids)) == len(question_ids)
    assert {q.post_id for q in canonical} == {GROUP_POST_ID}
    assert all(q.question_ids_of_group == question_ids for q in canonical)


def test_a_group_and_the_flat_questions_normalize_as_one_batch(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a real MiniBench fetch returns, which neither existing suite assembles.

    ``test_groups.py`` normalizes the expanded group **alone**; the duplicate-id guard in
    ``normalize_questions`` runs over whatever batch it is given, and the batch a live fetch
    hands it is mixed. This is the only place that shape is exercised.
    """
    install_tournament(monkeypatch, FakeTournament(flat_questions() + unpacked_group_questions()))

    _, fetched = fetch_open_questions_live(config)
    canonical = normalize_questions(fetched).questions

    ids = [q.question_id for q in canonical]
    assert len(canonical) == len(fetched)
    assert len(set(ids)) == len(ids)
    assert BINARY_QUESTION_ID in ids and GROUP_POST_ID in {q.post_id for q in canonical}


def test_an_unsupported_type_is_deferred_without_costing_its_siblings(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``M1-203``/D21 at the batch boundary, with the SDK inheritance trap in the batch.

    ``DiscreteQuestion`` subclasses ``NumericQuestion``, so a normalizer dispatching on
    ``isinstance`` would silently emit it as a fourth numeric forecast -- a wrong forecast
    rather than an error. Driven here through a fetch alongside the three supported types,
    because the failure that matters is one unsupported question either taking the batch
    down or slipping into it.
    """
    unsupported = [
        DiscreteQuestion(
            question_text="[SYNTHETIC] How many?",
            lower_bound=0.0,
            upper_bound=10.0,
            open_lower_bound=False,
            open_upper_bound=False,
        ),
        DateQuestion(
            question_text="[SYNTHETIC] When?",
            lower_bound=datetime(2026, 1, 1, tzinfo=timezone.utc),
            upper_bound=datetime(2027, 1, 1, tzinfo=timezone.utc),
            open_lower_bound=False,
            open_upper_bound=False,
        ),
    ]
    assert isinstance(unsupported[0], NumericQuestion), "the inheritance trap is real"
    install_tournament(monkeypatch, FakeTournament(flat_questions() + unsupported))

    _, fetched = fetch_open_questions_live(config)
    result = normalize_questions(fetched)

    assert {q.qtype for q in result.questions} == V1_TYPES
    assert len(result.questions) == 3
    assert {d.question_type for d in result.deferrals} == {"discrete", "date"}
    assert {d.reason for d in result.deferrals} == {"deferred_v1_type"}
