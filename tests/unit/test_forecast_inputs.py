"""M1-402: the reasoning packet handed to the forecaster.

Two properties carry the weight here. ``source_id`` must be a function of packet
*identity* rather than of the order a caller happened to hand documents over --
otherwise a replayed packet cites different ids for the same evidence, and every
citation in a stored forecast becomes unresolvable. And the community prediction must
not appear, which D22 and the v1 hard constraints make non-negotiable.
"""

import hashlib
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.forecast.inputs import (
    ForecastInputError,
    build_model_input,
    render_model_input,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
)
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
from whiskeyjack_bot.research.packet import ResearchPacket, build_packet

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
SECRET = "privateFAKE123456"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(run_id: str = "run-1", question_id: int = 42) -> ResearchRun:
    return ResearchRun(
        retrieval_run_id=run_id,
        question_id=question_id,
        provider="asknews",
        started_at_utc=NOW,
    )


def _doc(url: str, body: str, run_id: str = "run-1", **overrides: Any) -> ResearchDocument:
    fields: dict[str, Any] = {
        "retrieval_run_id": run_id,
        "original_url": url,
        "canonical_url": url,
        "retrieved_at_utc": NOW,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": _hash(body),
    }
    fields.update(overrides)
    return ResearchDocument(**fields)


def _packet(*documents: ResearchDocument, question_id: int = 42) -> ResearchPacket:
    return build_packet(question_id, [_run(question_id=question_id)], list(documents))


def _question(**overrides: Any) -> CanonicalBinaryQuestion:
    fields: dict[str, Any] = {"question_id": 42, "post_id": 7, "title": "Will X happen?"}
    fields.update(overrides)
    return CanonicalBinaryQuestion(**fields)


def _render(**kwargs: Any) -> dict[str, Any]:
    built = build_model_input(**kwargs)
    return json.loads(render_model_input(built))


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SECRET in str(exc) or SECRET in rendered


# --- what the model is sent ------------------------------------------------------


def test_the_rendered_packet_carries_the_prompts_field_names() -> None:
    """The two name bridges, pinned. The prompt says ``question_text`` where the
    canonical model says ``title``, and ``question_type`` where it says ``qtype``; a
    rename on either side silently starves the model of the question."""
    payload = _render(
        question=_question(title="Will X happen?"),
        packet=_packet(_doc("https://a.example/x", "x")),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert payload["question_text"] == "Will X happen?"
    assert payload["question_type"] == "binary"
    assert "title" not in payload
    assert "qtype" not in payload


def test_the_conditional_fields_are_absent_unless_they_apply() -> None:
    """The prompt lists ``options`` "for multiple-choice questions" and the numeric
    bounds "when applicable", so they are dropped rather than sent as nulls. Every
    unconditionally listed field keeps its null: there, "we looked and there is none"
    and "we did not send it" are different facts."""
    binary = _render(
        question=_question(),
        packet=_packet(),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert "options" not in binary
    assert "lower_bound" not in binary
    assert binary["fine_print"] is None

    choice = _render(
        question=CanonicalMultipleChoiceQuestion(
            question_id=42, post_id=7, title="Which?", options=["A", "B"]
        ),
        packet=_packet(),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert choice["options"] == ["A", "B"]
    assert "lower_bound" not in choice

    numeric = _render(
        question=CanonicalNumericQuestion(
            question_id=42,
            post_id=7,
            title="How many?",
            lower_bound=0.0,
            upper_bound=100.0,
            open_lower_bound=False,
            open_upper_bound=True,
            cdf_size=201,
            unit_of_measure="widgets",
        ),
        packet=_packet(),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert numeric["lower_bound"] == 0.0
    assert numeric["upper_bound"] == 100.0
    assert numeric["open_upper_bound"] is True
    assert numeric["unit_of_measure"] == "widgets"
    assert "options" not in numeric


def test_the_group_parent_title_is_sent() -> None:
    """A deliberate departure from the prompt's Inputs list. M1-202 lifts the parent
    title out of the group post so "the forecaster always receives what is actually
    being asked": an unpacked subquestion's own title can be a bare option label."""
    payload = _render(
        question=_question(title="September 2024", group_parent_title="When will X ship?"),
        packet=_packet(),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert payload["group_parent_title"] == "When will X ship?"


def test_a_document_carries_the_trust_fields_the_prompts_rules_read() -> None:
    """The prompt caps how load-bearing an ``unverified_social`` or ``llm_reported``
    document may be *by reading these fields*. A rule the model is told to apply
    against fields it is never shown is not a rule."""
    document = _doc(
        "https://x.com/agency/status/1",
        "post",
        source_type="social",
        provenance="llm_reported",
        reliability_tag="unverified_social",
        summary="a claim",
    )
    payload = _render(
        question=_question(),
        packet=_packet(document),
        tournament_id="minibench",
        as_of=NOW,
    )
    sent = payload["research_documents"][0]
    assert sent["source_type"] == "social"
    assert sent["provenance"] == "llm_reported"
    assert sent["reliability_tag"] == "unverified_social"


def test_the_evidence_summary_falls_back_to_the_snippet() -> None:
    """AskNews fills ``summary`` and Exa fills ``snippet``; sending only one of them
    would hand the model evidence with no content from whichever provider it did not
    name."""
    payload = _render(
        question=_question(),
        packet=_packet(
            _doc("https://a.example/1", "a", summary="the summary", snippet="the snippet"),
            _doc("https://b.example/2", "b", snippet="only a snippet"),
            _doc("https://c.example/3", "c"),
        ),
        tournament_id="minibench",
        as_of=NOW,
    )
    summaries = {d["url"]: d["evidence_summary"] for d in payload["research_documents"]}
    assert summaries["https://a.example/1"] == "the summary"
    assert summaries["https://b.example/2"] == "only a snippet"
    assert summaries["https://c.example/3"] is None


def test_no_community_prediction_reaches_the_model() -> None:
    """D22 and the v1 hard constraints. This is structural -- ``CanonicalQuestion``
    carries no such field -- but an absence nobody checks is one a later field
    addition can quietly end."""
    rendered = render_model_input(
        build_model_input(
            question=_question(),
            packet=_packet(_doc("https://a.example/x", "x")),
            tournament_id="minibench",
            as_of=NOW,
        )
    )
    lowered = rendered.lower()
    for needle in ("community", "cp_", "aggregat", "crowd", "consensus"):
        assert needle not in lowered


# --- source ids ------------------------------------------------------------------


def test_source_ids_are_assigned_over_the_ledgers_own_order() -> None:
    first = _doc("https://a.example/early", "alpha")
    second = _doc("https://z.example/late", "zeta")
    built = build_model_input(
        question=_question(),
        packet=_packet(second, first),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert [s.source_id for s in built.sources] == ["src-001", "src-002"]
    assert [s.canonical_url for s in built.sources] == [
        "https://a.example/early",
        "https://z.example/late",
    ]


def test_the_same_evidence_in_a_different_order_renders_the_same_bytes() -> None:
    """The property that makes a stored citation resolvable. ``ResearchPacket`` keeps
    its tuples in supplied order on purpose and replay reads documents back ordered by
    ``canonical_url, content_sha256``, so anything keyed on the tuple order would
    assign different ids to the same evidence after a round trip."""
    documents = [
        _doc("https://a.example/1", "a"),
        _doc("https://b.example/2", "b"),
        _doc("https://c.example/3", "c"),
    ]
    forwards = build_model_input(
        question=_question(),
        packet=_packet(*documents),
        tournament_id="minibench",
        as_of=NOW,
    )
    backwards = build_model_input(
        question=_question(),
        packet=_packet(*reversed(documents)),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert render_model_input(forwards) == render_model_input(backwards)
    assert forwards.sources == backwards.sources


def test_a_source_reference_resolves_back_to_its_document() -> None:
    document = _doc("https://a.example/1", "a", document_id="doc-1")
    built = build_model_input(
        question=_question(),
        packet=_packet(document),
        tournament_id="minibench",
        as_of=NOW,
    )
    reference = built.sources[0]
    assert reference.document_id == "doc-1"
    assert reference.canonical_url == document.canonical_url
    assert reference.content_sha256 == document.content_sha256
    sent = json.loads(render_model_input(built))["research_documents"][0]
    assert sent["source_id"] == reference.source_id
    assert sent["url"] == reference.canonical_url


def test_source_ids_are_distinct_across_two_runs() -> None:
    packet = build_packet(
        42,
        [_run("run-1"), _run("run-2")],
        [
            _doc("https://a.example/1", "a", run_id="run-1"),
            _doc("https://a.example/1", "a", run_id="run-2"),
        ],
    )
    built = build_model_input(
        question=_question(), packet=packet, tournament_id="minibench", as_of=NOW
    )
    ids = [s.source_id for s in built.sources]
    assert ids == ["src-001", "src-002"]
    assert len(set(ids)) == 2


# --- refusals --------------------------------------------------------------------


def test_a_packet_for_another_question_is_refused_without_naming_either() -> None:
    with pytest.raises(ForecastInputError) as excinfo:
        build_model_input(
            question=_question(),
            packet=_packet(question_id=99),
            tournament_id="minibench",
            as_of=NOW,
        )
    assert "42" not in str(excinfo.value)
    assert "99" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("blank tournament", {"tournament_id": "   "}),
        ("tournament not a string", {"tournament_id": 7}),
        ("naive as_of", {"as_of": datetime(2026, 8, 19, 12, 0)}),
        ("as_of not a datetime", {"as_of": "2026-08-19T12:00:00Z"}),
        ("packet is not a packet", {"packet": object()}),
        ("question is not a question", {"question": object()}),
    ],
)
def test_a_malformed_argument_arrives_as_this_modules_error(label: str, kwargs: Any) -> None:
    call: dict[str, Any] = {
        "question": _question(),
        "packet": _packet(),
        "tournament_id": "minibench",
        "as_of": NOW,
    }
    call.update(kwargs)
    with pytest.raises(ForecastInputError):
        build_model_input(**call)


def test_a_planted_secret_in_the_question_reaches_no_refusal_message() -> None:
    with pytest.raises(ForecastInputError) as excinfo:
        build_model_input(
            question=_question(title=SECRET),
            packet=_packet(question_id=99),
            tournament_id="minibench",
            as_of=NOW,
        )
    assert not _leaks(excinfo.value)


def test_render_refuses_something_that_is_not_a_model_input() -> None:
    with pytest.raises(ForecastInputError):
        render_model_input(object())  # type: ignore[arg-type]


# --- the rendering rule ----------------------------------------------------------


def test_the_rendering_is_canonical_and_ascii_safe() -> None:
    """The persisted-form rule from M1-305/M1-306, applied to a message. A lone
    surrogate is reachable here through provider text, and ``ensure_ascii`` escapes it
    instead of failing to encode it."""
    built = build_model_input(
        question=_question(),
        packet=_packet(_doc("https://a.example/1", "a", title="\ud800", summary="café")),
        tournament_id="minibench",
        as_of=NOW,
    )
    rendered = render_model_input(built)
    assert rendered.isascii()
    assert rendered == render_model_input(built)
    assert json.loads(rendered)["research_documents"][0]["title"] == "\ud800"
