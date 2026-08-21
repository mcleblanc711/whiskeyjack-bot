"""M1-501 acceptance: the schema rejects missing or unknown required fields and invalid source IDs.

The criterion has two clauses and, as in M1-403, they are satisfied by different things.
*"Missing or unknown required fields"* is partly already true -- ``forecast/schema.py``
makes nine fields structurally required and forbids extras -- so what this file owes is
the **rest** of it: the three lists M1-501's row names, which parse today as empty or
omitted. *"Invalid source IDs"* is entirely new, and it is the half that matters most:
nothing before this resolved a citation against the documents the model was actually
shown.

Both halves are asserted here, including the part ``schema.py`` already enforces, because
a criterion split across two modules is one nobody can check in one place.

The comprehensive valid/invalid golden set is Codex's **T-901**, authored blind from
spec. This file uses the one fixture M1-403 shipped plus the prompt's own examples, and
pre-writes none of Codex's.
"""

from __future__ import annotations

import json
import re
import traceback
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.forecast.attribution import (
    AttributionFieldError,
    attribution_problems,
    validate_attribution_fields,
)
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastResponse,
    ForecastSchemaError,
    MultipleChoiceForecastResponse,
    NumericForecastResponse,
    response_model_for,
    validate_forecast_response,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN_PATH = FIXTURES / "forecasts" / "binary_golden.json"
GOLDEN_SOURCES_PATH = FIXTURES / "forecasts" / "binary_golden_sources.json"
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic secret
# shape here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"

QUESTION_ID = 123
# What the prompt's own shared-fields example cites: src-001 in ``base_rate``, src-002 in
# ``evidence_adjustments`` and ``load_bearing_facts``.
PROMPT_SOURCES = ("src-001", "src-002")


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _payload(heading: str = "Binary schema", **overrides: Any) -> dict[str, Any]:
    """The prompt's shared fields composed with one of its three prediction blocks."""
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block(heading) + "}"),
    }
    if payload["question_type"] != "binary":
        # The prompt's own rule, not a workaround: a non-binary response nulls both
        # priors. test_forecast_schema.py makes the same composition for the same reason.
        payload["model_prior"] = None
        payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return payload


def _response(heading: str = "Binary schema", **overrides: Any) -> ForecastResponse:
    payload = _payload(heading, **overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _problems(forecast: ForecastResponse, sources: tuple[str, ...] = PROMPT_SOURCES) -> list[str]:
    return attribution_problems(forecast, question_id=QUESTION_ID, source_ids=sources)


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SECRET in str(exc) or SECRET in rendered


@pytest.fixture()
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def golden_sources() -> tuple[str, ...]:
    data = json.loads(GOLDEN_SOURCES_PATH.read_text(encoding="utf-8"))
    return tuple(data["source_ids"])


# --- the acceptance criterion, positively ----------------------------------------


def test_the_golden_output_is_attributable_against_the_sources_it_cites(
    golden: dict[str, Any], golden_sources: tuple[str, ...]
) -> None:
    """M1-403's golden binary output, checked against the ids its packet would have
    minted. Every field the row names is present and every citation resolves."""
    forecast = validate_forecast_response(golden, BinaryForecastResponse)
    assert (
        attribution_problems(forecast, question_id=golden["question_id"], source_ids=golden_sources)
        == []
    )
    assert (
        validate_attribution_fields(
            forecast, question_id=golden["question_id"], source_ids=golden_sources
        )
        is forecast
    )


def test_the_golden_output_would_notice_a_source_it_was_never_given(
    golden: dict[str, Any], golden_sources: tuple[str, ...]
) -> None:
    """The test above passes for a reason rather than by construction.

    The golden cites five distinct ids across three fields; drop any one of them from
    the supplied set and the same fixture must be refused. A golden that cited nothing
    would satisfy the previous test while proving nothing at all.
    """
    forecast = validate_forecast_response(golden, BinaryForecastResponse)
    assert len(golden_sources) == 5
    for dropped in golden_sources:
        remaining = tuple(value for value in golden_sources if value != dropped)
        problems = attribution_problems(
            forecast, question_id=golden["question_id"], source_ids=remaining
        )
        assert problems, dropped


# --- "missing ... required fields" ------------------------------------------------


# The nine the response schema makes structurally required. Named here rather than
# derived from ``model_fields``, so a field losing its required-ness is a test failure
# instead of a test that quietly stops asking (M1-303's lesson about asserting against
# the implementation's own constant).
_STRUCTURALLY_REQUIRED = [
    "schema_version",
    "question_id",
    "question_type",
    "as_of_utc",
    "base_rate",
    "status_quo",
    "rationale_summary",
    "process_confidence",
    "final_prediction",
]

# The three M1-501's row names. Each parses today as an omitted key or an empty list.
_ATTRIBUTION_REQUIRED = ["evidence_adjustments", "load_bearing_facts", "failure_modes"]


@pytest.mark.parametrize("field", _STRUCTURALLY_REQUIRED)
def test_a_structurally_required_field_cannot_be_missing(field: str) -> None:
    payload = _payload()
    del payload[field]
    with pytest.raises(ForecastSchemaError):
        validate_forecast_response(payload, BinaryForecastResponse)


@pytest.mark.parametrize("field", _ATTRIBUTION_REQUIRED)
@pytest.mark.parametrize("how", ["omitted", "empty"])
def test_an_attribution_field_cannot_be_missing_or_empty(field: str, how: str) -> None:
    """These three are the gap. The schema accepts both spellings -- they carry
    ``default_factory=list`` -- so before M1-501 an unattributable forecast parsed."""
    payload = _payload()
    if how == "omitted":
        del payload[field]
    else:
        payload[field] = []
    forecast = validate_forecast_response(payload, BinaryForecastResponse)
    assert _problems(forecast) == [f"{field}: must not be empty"]


@pytest.mark.parametrize("field", _ATTRIBUTION_REQUIRED)
def test_the_schema_alone_still_accepts_an_empty_attribution_field(field: str) -> None:
    """The split stays visible rather than inferred from an absence, the idiom M1-402
    and M1-403 established. ``schema.py`` reads no question and no packet, so these
    three are M1-501's to require, and a test says so from the inside."""
    payload = _payload(**{field: []})
    assert validate_forecast_response(payload, BinaryForecastResponse) is not None


def test_an_unknown_field_is_refused() -> None:
    """The criterion's other half. ``_StrictModel`` is ``extra="forbid"``, so this is
    already true; it is asserted at M1-501's row because the criterion names it."""
    with pytest.raises(ForecastSchemaError):
        validate_forecast_response(_payload(unexpected_field=1), BinaryForecastResponse)


# --- "invalid source IDs" ---------------------------------------------------------


_CITATION_LOCATIONS = [
    ("base_rate", "base_rate.source_ids"),
    ("evidence_adjustments", "evidence_adjustments.0.source_ids"),
    ("load_bearing_facts", "load_bearing_facts.0.source_ids"),
]


def _cite(payload: dict[str, Any], field: str, ids: list[str]) -> dict[str, Any]:
    """Rewrite whichever ``source_ids`` list one of the three locations holds."""
    if field == "base_rate":
        payload["base_rate"] = {**payload["base_rate"], "source_ids": ids}
    else:
        entries = [dict(entry) for entry in payload[field]]
        entries[0]["source_ids"] = ids
        payload[field] = entries
    return payload


@pytest.mark.parametrize(("field", "location"), _CITATION_LOCATIONS)
def test_a_source_id_that_was_never_supplied_is_refused_at_its_own_location(
    field: str, location: str
) -> None:
    forecast = validate_forecast_response(
        _cite(_payload(), field, ["src-009"]), BinaryForecastResponse
    )
    problems = _problems(forecast)
    assert location in " ".join(problems)
    assert all("src-009" not in problem for problem in problems)


@pytest.mark.parametrize(("field", "location"), _CITATION_LOCATIONS)
def test_a_repeated_source_id_is_refused_at_its_own_location(field: str, location: str) -> None:
    forecast = validate_forecast_response(
        _cite(_payload(), field, ["src-001", "src-001"]), BinaryForecastResponse
    )
    assert f"{location}: must not repeat a source_id" in _problems(forecast)


@pytest.mark.parametrize("field", ["evidence_adjustments", "load_bearing_facts"])
def test_a_claim_that_cites_nothing_is_refused(field: str) -> None:
    """The row's "evidence links". A claim with no citation is a claim the ledger
    cannot stand behind, and this is the one place it can still be caught."""
    forecast = validate_forecast_response(_cite(_payload(), field, []), BinaryForecastResponse)
    assert f"{field}.0.source_ids: must cite at least one source_id" in " ".join(
        _problems(forecast)
    )


def test_a_base_rate_may_cite_nothing() -> None:
    """Deliberately *not* required, and this is the test that keeps it deliberate.

    ``prompts/forecaster.md`` Method step 1 says "if none is defensible, say so and use
    a broad prior" -- so a base rate with no reference class has nothing to cite, and
    requiring a citation would fail a reply that followed the prompt exactly. That is
    the same test M1-402 applied to ``source_disagreements`` and M1-403 to the priors.
    """
    forecast = validate_forecast_response(
        _cite(_payload(), "base_rate", []), BinaryForecastResponse
    )
    assert _problems(forecast) == []


@pytest.mark.parametrize("field", ["source_disagreements", "uncertainty_notes"])
def test_the_two_lists_the_row_does_not_name_stay_optional(field: str) -> None:
    """``source_disagreements`` because the prompt's own example prints it as ``[]``;
    ``uncertainty_notes`` because M1-501's row does not name it. Both are recorded
    decisions, so both get a test rather than an absence."""
    forecast = validate_forecast_response(_payload(**{field: []}), BinaryForecastResponse)
    assert _problems(forecast) == []


# --- the question the forecast answers --------------------------------------------


def test_a_forecast_for_another_question_is_refused() -> None:
    """``prompts/forecaster.md`` line 157: "the question ID and type match the input".
    The type is enforced by which response model the dispatch selected; nothing checked
    the id, so a record could carry a question the model invented."""
    forecast = _response(question_id=QUESTION_ID + 1)
    problems = _problems(forecast)
    assert problems == [
        "question_id: must be the question this forecast was requested for "
        "(offending input withheld)"
    ]
    assert str(QUESTION_ID + 1) not in problems[0]


def test_the_matching_question_id_is_accepted() -> None:
    assert _problems(_response()) == []


# --- the evidence rules stand down when there was nothing to cite -----------------


def _no_research_payload(**overrides: Any) -> dict[str, Any]:
    """The reply a model can actually give when it was handed no documents: no
    adjustments, no load-bearing facts, and a base rate resting on nothing citable."""
    payload = _payload(evidence_adjustments=[], load_bearing_facts=[], **overrides)
    return _cite(payload, "base_rate", [])


def test_with_no_documents_supplied_the_evidence_rules_stand_down() -> None:
    """A packet may hold no documents at all -- ``research/store.py`` names the state,
    "a question researched and found nothing" -- and **M1-504** owns the gate over it.

    Requiring a citation that cannot exist would fail every such forecast through the
    repair loop: two billed calls to reject something no model could have supplied.
    """
    forecast = validate_forecast_response(_no_research_payload(), BinaryForecastResponse)
    assert attribution_problems(forecast, question_id=QUESTION_ID, source_ids=()) == []


def test_with_no_documents_supplied_the_unconditional_rules_still_bite() -> None:
    """The other side of the conditional, and the reason it is not a hole: a citation
    naming evidence that does not exist is still refused, and ``failure_modes`` -- which
    needs no research to write -- is still required. Nothing passes silently."""
    citing = validate_forecast_response(_payload(), BinaryForecastResponse)
    assert attribution_problems(citing, question_id=QUESTION_ID, source_ids=())

    no_modes = validate_forecast_response(
        _no_research_payload(failure_modes=[]), BinaryForecastResponse
    )
    assert attribution_problems(no_modes, question_id=QUESTION_ID, source_ids=()) == [
        "failure_modes: must not be empty"
    ]


def test_one_supplied_document_is_enough_to_turn_the_evidence_rules_back_on() -> None:
    """The boundary is "any document at all", not a threshold. A count threshold is
    M1-504's to choose, with ``forecast.fail_on_stale_research`` behind it."""
    forecast = validate_forecast_response(
        _payload(evidence_adjustments=[], load_bearing_facts=[]), BinaryForecastResponse
    )
    problems = attribution_problems(forecast, question_id=QUESTION_ID, source_ids=("src-001",))
    assert "evidence_adjustments: must not be empty" in problems
    assert "load_bearing_facts: must not be empty" in problems


# --- every question type ----------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "model"),
    [
        ("Binary schema", BinaryForecastResponse),
        ("Multiple-choice schema", MultipleChoiceForecastResponse),
        ("Numeric schema", NumericForecastResponse),
    ],
)
def test_the_rules_are_cross_type(heading: str, model: type[ForecastResponse]) -> None:
    """M1-403's rules are binary-specific by construction; M1-501's are not. The row
    owns the attribution fields for all three question types, so all three are tested
    rather than binary tested and the rest assumed."""
    ok = _response(heading)
    assert isinstance(ok, model)
    assert _problems(ok) == []
    bad = _response(heading, failure_modes=[], question_id=QUESTION_ID + 1)
    assert sorted(_problems(bad)) == sorted(
        [
            "question_id: must be the question this forecast was requested for "
            "(offending input withheld)",
            "failure_modes: must not be empty",
        ]
    )


# --- the error boundary -----------------------------------------------------------


def _caller_mistakes() -> list[dict[str, Any]]:
    forecast = _response()
    return [
        {"forecast": object(), "question_id": QUESTION_ID, "source_ids": PROMPT_SOURCES},
        {"forecast": None, "question_id": QUESTION_ID, "source_ids": PROMPT_SOURCES},
        {"forecast": forecast, "question_id": "123", "source_ids": PROMPT_SOURCES},
        {"forecast": forecast, "question_id": True, "source_ids": PROMPT_SOURCES},
        # str satisfies Sequence[str], so this type-checks and would mean "the ids
        # s, r, c, -, 0, 0, 1" -- the M1-303 round-4 defect in its own module.
        {"forecast": forecast, "question_id": QUESTION_ID, "source_ids": "src-001"},
        {"forecast": forecast, "question_id": QUESTION_ID, "source_ids": b"src-001"},
        {"forecast": forecast, "question_id": QUESTION_ID, "source_ids": ["src-001", 2]},
        {
            "forecast": forecast,
            "question_id": QUESTION_ID,
            "source_ids": (value for value in ["src-001"]),
        },
    ]


@pytest.mark.parametrize("call", _caller_mistakes())
@pytest.mark.parametrize(
    "entry_point", [attribution_problems, validate_attribution_fields], ids=["problems", "validate"]
)
def test_every_refusal_path_raises_this_modules_own_type_exactly(
    call: dict[str, Any], entry_point: Any
) -> None:
    """Exact type, not ``isinstance``: the parent being raised directly on one path is
    exactly what M1-403 round 1 found, and an ``isinstance`` assertion cannot see it."""
    with pytest.raises(AttributionFieldError) as excinfo:
        entry_point(
            call["forecast"], question_id=call["question_id"], source_ids=call["source_ids"]
        )
    assert type(excinfo.value) is AttributionFieldError


def test_this_modules_errors_are_its_own_type_and_still_catch_as_the_packages() -> None:
    """The pair M1-403 round 1 settled: a caller handling the forecast package's
    response failures as one type keeps working unchanged."""
    forecast = _response(failure_modes=[])
    with pytest.raises(ForecastSchemaError) as excinfo:
        validate_attribution_fields(forecast, question_id=QUESTION_ID, source_ids=PROMPT_SOURCES)
    assert type(excinfo.value) is AttributionFieldError
    assert excinfo.value.problems == ["failure_modes: must not be empty"]


def test_a_caller_mistake_is_never_a_repair_turn() -> None:
    """``binary.py::_require_config``'s precedent. A dispatch bug of ours is not
    something to ask the model to fix, and asking would cost a billable call."""
    with pytest.raises(AttributionFieldError):
        attribution_problems(_response(), question_id=1.0, source_ids=PROMPT_SOURCES)  # type: ignore[arg-type]


def test_every_problem_is_reported_not_only_the_first() -> None:
    forecast = _response(
        question_id=QUESTION_ID + 1,
        failure_modes=[],
        evidence_adjustments=[],
        load_bearing_facts=[],
    )
    assert len(_problems(forecast)) == 4


def test_nothing_is_repaired_dropped_or_renumbered() -> None:
    """A forecast whose evidence list was quietly edited is precisely the record the
    ledger could not stand behind. The response comes back identical, or not at all."""
    forecast = _response()
    before = forecast.model_dump(mode="json")
    returned = validate_attribution_fields(
        forecast, question_id=QUESTION_ID, source_ids=PROMPT_SOURCES
    )
    assert returned is forecast
    assert returned.model_dump(mode="json") == before


# --- no value reaches a message ---------------------------------------------------


@pytest.mark.parametrize(
    "payload_overrides",
    [
        {"failure_modes": [SECRET]},
        {"status_quo": SECRET},
        {"rationale_summary": SECRET},
    ],
    ids=["failure-mode", "status-quo", "rationale"],
)
def test_model_text_reaches_no_problem_message(payload_overrides: dict[str, Any]) -> None:
    forecast = _response(question_id=QUESTION_ID + 1, **payload_overrides)
    problems = _problems(forecast)
    assert problems
    assert all(SECRET not in problem for problem in problems)


def test_a_cited_source_id_reaches_no_message_or_traceback() -> None:
    """The citation is the one model-authored string this module compares against a set
    it owns, so it is the value most likely to be interpolated by accident."""
    forecast = validate_forecast_response(
        _cite(_payload(), "evidence_adjustments", [SECRET]), BinaryForecastResponse
    )
    problems = _problems(forecast)
    assert problems
    assert all(SECRET not in problem for problem in problems)
    with pytest.raises(AttributionFieldError) as excinfo:
        validate_attribution_fields(forecast, question_id=QUESTION_ID, source_ids=PROMPT_SOURCES)
    assert not _leaks(excinfo.value)


def test_a_supplied_source_id_reaches_no_message_either() -> None:
    """Unlike ``binary.py``, which renders its configured bounds, this module renders
    neither side. The model already holds the whole id list in its own request under
    ``research_documents``, so naming them back buys nothing -- and the supplied ids
    reach this function from a caller, which makes not rendering them the cheaper rule
    to keep true."""
    forecast = validate_forecast_response(
        _cite(_payload(), "evidence_adjustments", ["src-009"]), BinaryForecastResponse
    )
    problems = attribution_problems(
        forecast, question_id=QUESTION_ID, source_ids=("src-001", SECRET)
    )
    assert problems
    assert all(SECRET not in problem for problem in problems)
