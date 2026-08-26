"""The canonical forecast record and its hash (M1-602).

Responses are composed from ``prompts/forecaster.md``'s own JSON examples rather than
hand-written, the ``test_forecast_attribution.py`` idiom: a record test that invents its
own response cannot notice the prompt and the record contract drifting apart.
"""

from __future__ import annotations

import copy
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.record import (
    RECORD_SCHEMA_VERSION,
    ForecastRecord,
    ForecastRecordDraft,
    ForecastRecordError,
    RecordedCommunityPrediction,
    assign_identity,
    build_forecast_record_draft,
    canonical_final_prediction_json,
    canonical_record_json,
    record_from_json,
    record_sha256,
)
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
    CanonicalQuestion,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic secret
# shape here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"

QUESTION_ID = 123
POST_ID = 456
TOURNAMENT = "minibench"
# What the prompt's own shared-fields example cites.
PROMPT_SOURCES = ("src-001", "src-002")
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

_HEADINGS = {
    "binary": "Binary schema",
    "multiple_choice": "Multiple-choice schema",
    "numeric": "Numeric schema",
}


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _response(question_type: str = "binary", **overrides: Any) -> ForecastResponse:
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block(_HEADINGS[question_type]) + "}"),
    }
    if payload["question_type"] != "binary":
        payload["model_prior"] = None
        payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _question(question_type: str = "binary", **overrides: Any) -> CanonicalQuestion:
    common: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": "Will the thing happen?",
        "resolution_criteria": "Resolves YES if the thing happens.",
    }
    common.update(overrides)
    if question_type == "binary":
        return CanonicalBinaryQuestion(**common)
    if question_type == "multiple_choice":
        options = [
            option["option"]
            for option in json.loads("{" + _json_block("Multiple-choice schema") + "}")[
                "final_prediction"
            ]["options"]
        ]
        return CanonicalMultipleChoiceQuestion(options=options, **common)
    return CanonicalNumericQuestion(
        lower_bound=0.0,
        upper_bound=100.0,
        open_lower_bound=False,
        open_upper_bound=False,
        cdf_size=201,
        **common,
    )


def _settings(**overrides: Any) -> ModelSettings:
    fields: dict[str, Any] = {
        "provider": "openrouter",
        "name": "openrouter/test-model",
        "temperature": 0.1,
        "max_output_tokens": 2048,
        "timeout_seconds": 60.0,
        "allowed_tries": 2,
        "prompt_version": "1.1.0",
        "prompt_sha256": "b" * 64,
    }
    fields.update(overrides)
    return ModelSettings(**fields)


def _sources(*source_ids: str, url_suffix: str = "") -> tuple[SourceReference, ...]:
    return tuple(
        SourceReference(
            source_id=source_id,
            document_id=None,
            canonical_url=f"https://example.test/{source_id}{url_suffix}",
            content_sha256="c" * 64,
        )
        for source_id in (source_ids or PROMPT_SOURCES)
    )


_DEFAULT = object()


def _generation(
    question_type: str = "binary",
    *,
    forecast: Any = _DEFAULT,
    sources: Any = _DEFAULT,
    settings: Any = _DEFAULT,
    **overrides: Any,
) -> ForecastGeneration:
    """A sentinel, not ``None``, for ``forecast``: ``None`` is the value under test.

    A default of ``None`` would have quietly substituted a real forecast in the one test
    that asks what happens when there is not one -- the test would then have passed for
    the wrong reason, which is the failure mode this project keeps meeting.
    """
    fields: dict[str, Any] = {
        "forecast": _response(question_type) if forecast is _DEFAULT else forecast,
        "settings": _settings() if settings is _DEFAULT else settings,
        "sources": _sources() if sources is _DEFAULT else sources,
        "request": "the rendered reasoning packet",
        "raw_responses": ("{}",),
        "invocations": 1,
        "repair_attempted": False,
        "cost_usd": None,
        "failure_code": None,
        "failure_problems": (),
    }
    fields.update(overrides)
    return ForecastGeneration(**fields)


def _draft(question_type: str = "binary", **overrides: Any) -> ForecastRecordDraft:
    fields: dict[str, Any] = {
        "question": _question(question_type),
        "generation": _generation(question_type),
        "tournament_id": TOURNAMENT,
        "attempt_id": "attempt-1",
        "retrieval_run_id": "run-1",
        "research_packet_sha256": "d" * 64,
        "generated_at": GENERATED_AT,
    }
    fields.update(overrides)
    return build_forecast_record_draft(**fields)


_IDENTITY_FIELDS = ("record_id", "forecast_version", "parent_record_id")


def _record(question_type: str = "binary", **overrides: Any) -> ForecastRecord:
    identity: dict[str, Any] = {
        "record_id": "01a02000-0000-7000-8000-000000000001",
        "forecast_version": 1,
        "parent_record_id": None,
    }
    identity.update({k: overrides.pop(k) for k in _IDENTITY_FIELDS if k in overrides})
    return assign_identity(_draft(question_type, **overrides), **identity)


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SECRET in str(exc) or SECRET in rendered


# --------------------------------------------------------------------------------------
# The record contract
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("question_type", sorted(_HEADINGS))
def test_a_draft_assembles_for_every_supported_question_type(question_type: str) -> None:
    draft = _draft(question_type)
    assert draft.question_type == question_type
    assert draft.record_schema_version == RECORD_SCHEMA_VERSION


def test_the_record_carries_exactly_the_contracted_fields() -> None:
    """Set equality, not membership.

    M1-501's round-1 lesson: a one-sided assertion is vacuous against the failure that
    matters. ``assert "forecast" in keys`` passes just as well on a record that also
    carries a raw chain-of-thought field or that quietly dropped ``sources``. The whole
    key set is pinned, so *adding* a field to the record is as much a test failure as
    removing one -- which is what a persisted contract needs, since every stored record
    written under the old set keeps its old hash.
    """
    expected = {
        "record_schema_version",
        "record_id",
        "forecast_version",
        "parent_record_id",
        "question_id",
        "post_id",
        "tournament_id",
        "question_type",
        "question_domain",
        "question",
        "attempt_id",
        "model_settings",
        "retrieval_run_id",
        "research_packet_sha256",
        "sources",
        "community_prediction",
        "forecast",
        "generated_at_utc",
    }
    assert set(json.loads(canonical_record_json(_record())).keys()) == expected


def test_the_record_stores_no_hidden_reasoning_and_no_raw_response() -> None:
    """The two CLAUDE.md hard constraints that a record could breach by accident.

    ``ForecastGeneration`` carries ``request`` and ``raw_responses`` -- the whole rendered
    packet and every raw model reply -- because M1-406 needs them. Neither belongs in
    ``record_json``: raw output is M1-406's own storage, and "never persist hidden
    chain-of-thought" means the record holds the concise auditable fields and nothing else.
    Asserted on the rendered bytes rather than on the field list, because the failure this
    guards against is a value reaching the column, not a name appearing in a schema.
    """
    rendered = canonical_record_json(_record())
    assert "the rendered reasoning packet" not in rendered
    assert "raw_responses" not in rendered
    assert "request" not in rendered


def test_community_prediction_cannot_claim_it_was_a_model_input() -> None:
    """The v1 hard constraint, made unrepresentable rather than merely documented."""
    with pytest.raises(Exception):
        RecordedCommunityPrediction(used_as_model_input=True)
    with pytest.raises(Exception):
        RecordedCommunityPrediction(snapshot={"community": 0.4})
    assert _record().community_prediction.used_as_model_input is False


def test_a_failed_generation_has_no_record_to_persist() -> None:
    generation = _generation(forecast=None, failure_code="model_output_invalid")
    with pytest.raises(ForecastRecordError) as excinfo:
        _draft(generation=generation)
    assert "no forecast" in str(excinfo.value)


def test_a_question_that_answers_another_question_is_refused() -> None:
    with pytest.raises(ForecastRecordError):
        _draft(question=_question(question_id=999))


@pytest.mark.parametrize("field", ["question_id", "post_id", "question_type"])
def test_a_stored_record_whose_copies_of_the_identity_disagree_is_refused(field: str) -> None:
    """The identity fields are checked where a *stored* record can still be wrong.

    ``build_forecast_record_draft`` derives ``post_id`` and ``question_type`` from the
    question, so a builder-level test of those two cannot fail -- it would be asserting
    against an assignment. The reachable path is the other one: a ``record_json`` read back
    out of the ledger, which CLAUDE.md classes as untrusted. Mangled at the top level only,
    so the nested question and forecast keep the original value and the three copies really
    do disagree.
    """
    payload = json.loads(canonical_record_json(_record()))
    payload[field] = 999 if field != "question_type" else "numeric"
    with pytest.raises(ForecastRecordError):
        record_from_json(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )


def test_a_forecast_for_another_question_is_refused() -> None:
    with pytest.raises(ForecastRecordError):
        _draft(generation=_generation(forecast=_response(question_id=999)))


def test_a_forecast_of_another_type_than_the_question_is_refused() -> None:
    """The type policy cannot be tested by flipping a type string (M1-202's trap).

    A numeric response against a binary question is refused because the two
    ``question_type`` tags disagree -- and the response is a genuine
    ``NumericForecastResponse``, not a binary one with its tag edited, which pydantic's
    discriminator would have rejected for an unrelated reason.
    """
    with pytest.raises(ForecastRecordError):
        _draft("binary", generation=_generation("numeric"))


def test_a_repeated_source_id_is_refused() -> None:
    with pytest.raises(ForecastRecordError):
        _draft(generation=_generation(sources=_sources("src-001", "src-001")))


def test_two_sources_may_share_a_url_but_not_an_id() -> None:
    """The converse of the test above, so it is not passing for the wrong reason."""
    duplicated_url = _sources("src-001", "src-002", url_suffix="")
    same_url = tuple(
        SourceReference(
            source_id=source.source_id,
            document_id=None,
            canonical_url="https://example.test/same",
            content_sha256="c" * 64,
        )
        for source in duplicated_url
    )
    draft = _draft(generation=_generation(sources=same_url))
    assert [source.source_id for source in draft.sources] == ["src-001", "src-002"]


# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------


def test_version_one_is_the_root_and_later_versions_name_a_parent() -> None:
    assert _record(forecast_version=1, parent_record_id=None).parent_record_id is None
    with pytest.raises(ForecastRecordError):
        _record(forecast_version=1, parent_record_id="01a02000-0000-7000-8000-000000000000")
    with pytest.raises(ForecastRecordError):
        _record(forecast_version=2, parent_record_id=None)
    with pytest.raises(ForecastRecordError):
        _record(forecast_version=0, parent_record_id=None)


def test_a_record_cannot_be_its_own_parent() -> None:
    with pytest.raises(ForecastRecordError):
        _record(
            record_id="01a02000-0000-7000-8000-000000000001",
            forecast_version=2,
            parent_record_id="01a02000-0000-7000-8000-000000000001",
        )


# --------------------------------------------------------------------------------------
# The canonical rendering and the hash
# --------------------------------------------------------------------------------------


def test_the_rendering_is_sorted_compact_and_ascii_only() -> None:
    rendered = canonical_record_json(_record())
    assert rendered == json.dumps(
        json.loads(rendered), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    assert rendered.isascii()


def test_a_lone_surrogate_survives_the_rendering() -> None:
    """``model_dump_json()`` raises on one; ``ensure_ascii=True`` escapes it.

    Reachable rather than theoretical: a lone surrogate reaches a schema text field from
    provider JSON. Asserted on a field that reaches no column of its own -- see
    ``test_a_lone_surrogate_in_a_column_backed_field_is_refused`` for the other half.
    M1-305 round 2.
    """
    record = _record(question=_question(resolution_criteria="Resolves if econ\ud800data."))
    rendered = canonical_record_json(record)
    assert "\\ud800" in rendered
    assert record_from_json(rendered) == record


def test_a_surrogate_pair_is_refused_rather_than_silently_rewritten() -> None:
    """The one input that does not survive this project's persisted form.

    ``"\\ud83d\\ude00"`` is two Python code points; ``json.dumps(ensure_ascii=True)``
    escapes them to the two ``\\uXXXX`` units an astral scalar escapes to, and
    ``json.loads`` recombines them into ``U+1F600``. Without the guard the record is
    *accepted*, stored, and read back as a different string -- the failure is silent, which
    is why it is asserted on the round trip rather than only on the refusal.
    """
    pair = "\ud83d" + "\ude00"
    assert len(pair) == 2
    # Which fields can actually hold one, checked rather than assumed. A pydantic *
    # constrained* string (`Field(min_length=...)`) refuses a surrogate pair on its own,
    # so `question.title` is not a way in; a bare `str` and a `NonBlankStr` (a bare str
    # with an AfterValidator) both accept one. `resolution_criteria` is untrusted
    # Metaculus text and `rationale_summary` is untrusted model output, so both of the
    # cases below are reachable.
    with pytest.raises(ForecastRecordError, match="surrogate pair"):
        _record(question_domain=pair)
    with pytest.raises(ForecastRecordError, match="surrogate pair"):
        _draft(question=_question(resolution_criteria=f"Resolves YES if {pair} happens."))
    with pytest.raises(ForecastRecordError, match="surrogate pair"):
        _draft(generation=_generation(forecast=_response(rationale_summary=f"Because {pair}.")))
    # The corruption the guard prevents, demonstrated on the rendering rule itself so the
    # test still means something if the guard is ever moved.
    rendered = json.dumps({"t": pair}, ensure_ascii=True, separators=(",", ":"))
    assert json.loads(rendered)["t"] != pair


def test_a_lone_surrogate_is_accepted_in_a_field_that_only_record_json_holds() -> None:
    """The converse, so the guard is narrow rather than merely strict.

    A lone surrogate escapes to ``"\\ud800"`` and comes back as the same lone surrogate.
    Refusing it in ``record_json`` would contradict ``forecast/inputs.render_model_input``,
    which documents the same rendering rule accepting one.

    ``resolution_criteria`` lives inside the stored question and reaches no column of its
    own, which is what makes it the right field for this assertion. Round 1's finding B2 is
    that the same value in a field the writer *also* copies into a bare TEXT column is a
    different case entirely -- see the test below.
    """
    record = _record(question=_question(resolution_criteria="Resolves if econ\ud800data."))
    assert record_from_json(canonical_record_json(record)) == record


def test_a_lone_surrogate_in_a_column_backed_field_is_refused() -> None:
    """Round 1, finding B2, as a regression.

    `record_json` is `ensure_ascii` output and is pure ASCII, so it can carry a lone
    surrogate as an escape. The writer also copies a dozen scalars into their own bare TEXT
    columns, and sqlite3 encodes a TEXT parameter as UTF-8 at bind time -- so the same value
    there raised a raw `UnicodeEncodeError` quoting the character, escaping the module's
    exception contract *and* leaking. Refused at build time now, before any transaction.

    `question_domain` is the field the finding actually reached, and the test below says
    why it is the only one.
    """
    with pytest.raises(ForecastRecordError, match="text column"):
        _record(question_domain="a\ud800b")


@pytest.mark.parametrize("field", ["tournament_id", "attempt_id", "retrieval_run_id"])
def test_the_other_column_backed_fields_were_never_reachable(field: str) -> None:
    """Why `question_domain` was the hole, checked rather than asserted in a comment.

    Every other field the writer projects into a bare TEXT column is a pydantic
    *constrained* string (`Field(min_length=..., max_length=...)`), and a constrained string
    refuses a value that cannot be encoded as UTF-8 on its own -- the same behaviour that
    keeps a surrogate *pair* out of `question.title`. `question_domain` is a bare
    `str | None`, which accepts one.

    So the refusal still arrives as this module's error, from one layer up. The probe is the
    fix for `question_domain` and defence in depth for the rest -- and this test is what
    would notice if a later change relaxed one of those constraints, since it would then
    start failing on the *message*, not on the type.
    """
    with pytest.raises(ForecastRecordError, match="could not be assembled"):
        _draft(**{field: "a\ud800b"})


def test_the_column_probe_covers_every_bare_text_column_the_writer_writes() -> None:
    """The list of probed fields against the writer's own column set.

    The failure mode this guards is a column added to `forecast.store._projection` and not
    to `record._BARE_TEXT_PROJECTIONS`, which would silently reopen finding B2 for that
    column. Compared as **set equality** over the columns that are neither writer-owned nor
    `ensure_ascii` output, so an addition on either side fails.
    """
    from whiskeyjack_bot.forecast import record as record_module
    from whiskeyjack_bot.forecast import store as store_module

    projected = set(store_module._PROJECTED_COLUMNS)
    # The two JSON columns are `ensure_ascii` output and cannot hold an unencodable
    # character; `question_id`, `post_id` and `forecast_version` are INTEGER columns.
    ascii_json = {"record_json", "final_prediction_json", "forecast_sha256"}
    integers = {"question_id", "post_id", "forecast_version"}
    needs_probe = projected - ascii_json - integers

    probed = {path.split(".")[0] for path in record_module._BARE_TEXT_PROJECTIONS}
    # `model_settings.*` fans out into four columns under different names; map them.
    probed_columns = {
        path.rsplit(".", 1)[-1] if "." in path else path
        for path in record_module._BARE_TEXT_PROJECTIONS
    }
    probed_columns = {
        {"provider": "model_provider", "name": "model_name"}.get(name, name)
        for name in probed_columns
    }
    assert probed_columns == needs_probe, (probed_columns ^ needs_probe, probed)


def test_a_pydantic_discriminator_failure_does_not_echo_the_stored_tag() -> None:
    """Round 1, finding B1, as a regression.

    `include_input=False, include_url=False` suppresses pydantic's `input` field but not
    the values several of its `msg` strings interpolate. The discriminated union is the
    reachable case: a stored `forecast.question_type` reached `ForecastRecordError` verbatim
    through `Input tag '...' found using 'question_type' does not match ...`.
    """
    payload = json.loads(canonical_record_json(_record()))
    payload["forecast"]["question_type"] = SECRET
    with pytest.raises(ForecastRecordError) as excinfo:
        record_from_json(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    assert not _leaks(excinfo.value)
    # Still actionable: the field path and pydantic's value-free error slug survive.
    assert "forecast" in str(excinfo.value)
    assert "union_tag_invalid" in str(excinfo.value)


@pytest.mark.parametrize("where", ["top_level", "nested"])
def test_an_unauthored_field_name_is_withheld_from_the_refusal(where: str) -> None:
    """The other half of finding B1, found while fixing the first half.

    `include_input=False` suppresses the offending *value*; under `extra="forbid"` the
    offending **key** is the error's `loc`, and the keys of a stored `record_json` are
    untrusted. Dropping `msg` did nothing about that: the refusal still read
    `(WJLEAKMARKER-extra-key: extra_forbidden)`.

    A location part now survives only if this schema declared it. Asserted at both depths,
    because the nested case is the one a top-level-only field-name set would miss.
    """
    payload = json.loads(canonical_record_json(_record()))
    if where == "top_level":
        payload[SECRET] = 1
    else:
        payload["question"][SECRET] = 1
    with pytest.raises(ForecastRecordError) as excinfo:
        record_from_json(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    assert not _leaks(excinfo.value)
    assert "<withheld>" in str(excinfo.value)


def test_a_field_name_this_schema_authored_still_survives() -> None:
    """The converse, so the withholding is narrow rather than blanket.

    A refusal rendered as `<withheld>.<withheld>` is one nobody can act on, which is its own
    failure mode -- the same argument the M1-401 path carve-out makes. `_schema_field_names`
    walks the whole model tree for that reason.
    """
    payload = json.loads(canonical_record_json(_record()))
    del payload["question"]["title"]
    with pytest.raises(ForecastRecordError) as excinfo:
        record_from_json(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    assert "title" in str(excinfo.value)
    assert "missing" in str(excinfo.value)


def test_a_record_that_already_has_an_identity_cannot_be_given_another() -> None:
    """Round 1, finding B4, as a regression.

    `ForecastRecord` subclasses `ForecastRecordDraft`, so the old `isinstance` gate accepted
    one and then raised a raw `TypeError` about duplicate keyword arguments.
    """
    record = _record()
    with pytest.raises(ForecastRecordError, match="already has an identity"):
        assign_identity(
            record,
            record_id="01a02000-0000-7000-8000-00000000000f",
            forecast_version=2,
            parent_record_id=record.record_id,
        )


def test_the_hash_moves_with_any_content_change() -> None:
    base = _record()
    assert record_sha256(base) == record_sha256(_record())
    assert record_sha256(base) != record_sha256(_record(question_domain="econ_data"))
    assert record_sha256(base) != record_sha256(
        _record(record_id="01a02000-0000-7000-8000-00000000ffff")
    )


def test_the_final_prediction_column_is_the_bytes_nested_in_the_record() -> None:
    """Two dumps of one object are two chances to disagree.

    ``final_prediction_json`` is what M1-604 exports as *the* prediction, and
    ``record_json`` holds the same object nested inside. This asserts they are the same
    bytes -- a re-dump from ``record.forecast.final_prediction`` would pass a test that
    only compared parsed values while still being able to drift.
    """
    record = _record()
    nested = json.loads(canonical_record_json(record))["forecast"]["final_prediction"]
    assert canonical_final_prediction_json(record) == json.dumps(
        nested, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


# --------------------------------------------------------------------------------------
# Reading a stored record back
# --------------------------------------------------------------------------------------


def test_a_stored_record_round_trips() -> None:
    record = _record()
    assert record_from_json(canonical_record_json(record)) == record


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda text: json.dumps(json.loads(text)), id="pretty_printed"),
        pytest.param(lambda text: text + " ", id="trailing_space"),
        pytest.param(
            # Genuinely reordered. `sort_keys=False` alone re-emits the same bytes, because
            # json.loads preserves the canonical order it was given -- a mangler that
            # always produced canonical output would skip on every run and test nothing.
            lambda text: json.dumps(
                dict(reversed(list(json.loads(text).items()))),
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            id="reordered_keys",
        ),
    ],
)
def test_a_stored_record_not_in_canonical_form_is_refused(mangle: Any) -> None:
    """Non-canonical bytes cannot be read back as if nothing were wrong.

    The stored bytes are what ``forecast_sha256`` digests. A reader that accepted a
    re-rendered equivalent would hand back a record whose hash is not the stored one --
    silently, as a successful read -- and every approval bound to it would then fail to
    verify with no explanation.
    """
    text = canonical_record_json(_record())
    mangled = mangle(text)
    if mangled == text:  # a rendering that happens to already be canonical proves nothing
        pytest.skip("the mangler produced canonical bytes")
    with pytest.raises(ForecastRecordError):
        record_from_json(mangled)


def test_a_coerced_scalar_is_refused_rather_than_rewritten() -> None:
    """``_StrictModel`` is ``extra="forbid"``, not ``strict`` (M1-303 round 4).

    Pydantic would coerce ``"123"`` to ``123`` and hand back a record that is not what the
    ledger holds. The round-trip comparison is what catches it: the re-rendered record
    carries ``123`` and the stored text carries ``"123"``.
    """
    payload = json.loads(canonical_record_json(_record()))
    payload["question_id"] = str(payload["question_id"])
    payload["question"]["question_id"] = str(payload["question"]["question_id"])
    payload["forecast"]["question_id"] = str(payload["forecast"]["question_id"])
    with pytest.raises(ForecastRecordError):
        record_from_json(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[]", '"a string"', "null", '{"record_schema_version":"1.0.0"}'],
)
def test_a_malformed_stored_record_arrives_as_this_modules_error(text: str) -> None:
    with pytest.raises(ForecastRecordError):
        record_from_json(text)


def test_an_unknown_field_in_a_stored_record_is_refused() -> None:
    payload = json.loads(canonical_record_json(_record()))
    payload["hidden_chain_of_thought"] = "step 1..."
    with pytest.raises(ForecastRecordError):
        record_from_json(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )


def test_a_stored_record_of_another_schema_version_is_refused() -> None:
    payload = json.loads(canonical_record_json(_record()))
    payload["record_schema_version"] = "2.0.0"
    with pytest.raises(ForecastRecordError):
        record_from_json(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )


# --------------------------------------------------------------------------------------
# Error hygiene
# --------------------------------------------------------------------------------------


def test_no_refusal_echoes_the_content_it_refused() -> None:
    """Every failure path this module owns, checked against one planted value.

    Checked on the rendered traceback as well as on ``str(exc)``: M1-302's lesson is that
    a value can reach a log through a chained exception's text even when the message this
    module wrote is clean, which is why every sanitizing raise here uses ``from None``.
    """
    poisoned = json.loads(canonical_record_json(_record()))
    poisoned["question"]["title"] = SECRET
    poisoned["question_domain"] = SECRET
    canonical = json.dumps(poisoned, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    attempts: list[Any] = [
        lambda: record_from_json(
            canonical.replace('"record_schema_version":"1.0.0"', '"record_schema_version":"9.9.9"')
        ),
        lambda: record_from_json(json.dumps(poisoned)),
        lambda: record_from_json(canonical[:-1]),
        lambda: _draft(question=_question(title=SECRET, question_id=999)),
        lambda: _draft(generation=_generation(sources=_sources("src-001", "src-001"))),
        lambda: _draft(tournament_id=SECRET, question=_question(question_id=999)),
        lambda: _draft(generation=object()),
        lambda: assign_identity(
            _draft(), record_id=SECRET, forecast_version=2, parent_record_id=None
        ),
    ]
    raised = 0
    for attempt in attempts:
        try:
            attempt()
        except Exception as exc:  # noqa: BLE001 - the point is that nothing leaks, whatever it is
            raised += 1
            assert isinstance(exc, ForecastRecordError), attempt
            assert not _leaks(exc), attempt
    assert raised == len(attempts), "a refusal path stopped refusing; the guard is now vacuous"


def test_the_leak_guard_would_notice_a_leak() -> None:
    """The vacuity check M1-308 shipped alongside its own truth-table guards.

    A no-leak assertion is only worth what its detector is worth. This proves the detector
    fires on an exception that really does carry the planted value, so a green
    ``test_no_refusal_echoes_the_content_it_refused`` means the messages are clean rather
    than that ``_leaks`` never returns True.
    """
    assert _leaks(ForecastRecordError(f"echoing {SECRET}"))
    assert not _leaks(ForecastRecordError("clean"))


def test_a_draft_is_not_mutated_by_being_given_an_identity() -> None:
    """``assign_identity`` builds a new record; the draft stays appendable elsewhere."""
    draft = _draft()
    before = draft.model_dump(mode="json", warnings=False)
    assign_identity(
        draft,
        record_id="01a02000-0000-7000-8000-000000000009",
        forecast_version=1,
        parent_record_id=None,
    )
    assert draft.model_dump(mode="json", warnings=False) == before
    assert copy.deepcopy(before) == before
