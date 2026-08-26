"""Re-deriving a stored forecast from the model text that produced it (M1-406).

The acceptance criterion is one sentence: **"model replay makes zero API calls and
reproduces the parsed forecast hash."** This module is that sentence.

**Zero API calls is structural, not a mock count.** M1-306 settled that for retrieval and
``tests/unit/test_forecast_generate.py::test_the_response_schema_reaches_no_provider_client``
made it an assertion: nothing on this import path may reach a provider SDK or an HTTP
client, so there is no call to make. That is why ``forecast/parse.py`` exists as a module
separate from ``forecast/generate.py`` -- the parse must be *the same code* the generating
call ran, and reaching it inside ``generate.py`` would pull litellm into the replay process.
The import-graph test pins this module alongside the others.

**Reproducing the hash means re-deriving it, and that is a deliberate difference from
retrieval replay.** ``research/artifacts.py`` is explicit that its files are not the replay
substrate: re-normalizing from raw would make a replayed packet depend on adapter code
version, so a bug fix in a ``_to_document`` would silently re-derive every historical
forecast's evidence. Here the re-derivation is the *point*, because it is a comparison and
not a substitution:

- the stored record stays authoritative -- nothing here writes, and ``003`` blocks UPDATE
  and DELETE on ``forecast_records`` anyway;
- the re-parsed forecast is rebuilt into a record carrying the **stored** identity and
  hashed, and the two hashes are reported;
- a mismatch is an answer, not an exception, and never a new row.

So this is a verification instrument. What it verifies is that the record the ledger holds
is what the stored provider text actually parses to under the code and configuration in
force right now -- which is the question an auditor asks, and the one nothing in the project
could answer before this item.

**A configuration change is visible here, and that is intended.** ``_parse`` runs the
config-dependent output checks (``forecast.min_probability``/``max_probability``, the
attribution rules) exactly as generation ran them. An operator who narrows the probability
bounds after a forecast was stored gets a replay that reports the record no longer
re-derives. That is a true statement about the ledger under the current configuration, and
suppressing it would make the instrument agree with itself by construction.

**Refusals rather than best-effort answers.** ``forecast.replay_saved_model_output`` must be
enabled -- ``research/store.py::replay_research``'s rule, and for its reason: replay is not
a fallback a caller drifts into, the committed default is ``false``, and honouring it is
what keeps "we replayed" from being something that happened by accident. A row with no
recorded artifact, or an artifact that belongs to another attempt, is refused rather than
reported as a non-match: "we could not check" and "we checked and it differs" are different
findings and only one of them is about the record.

Errors are :class:`~whiskeyjack_bot.forecast.record.ForecastRecordError`, reused rather than
a fifth type -- ``forecast/store.py`` and ``forecast/persist.py`` reuse it, and
``research/store.py`` raises its own ``StoreError`` from ``replay_research`` for the same
reason: a caller handling "the record is wrong" separately from "the replay is disabled"
would be handling a distinction the pipeline does not make.

Purely local file I/O and SQLite: no network access on any path through here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from whiskeyjack_bot.artifacts import ArtifactError
from whiskeyjack_bot.forecast.artifacts import StoredModelOutput, read_raw_model_output
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.parse import ForecastGeneration, _parse
from whiskeyjack_bot.forecast.record import (
    ForecastRecord,
    ForecastRecordError,
    assign_identity,
    build_forecast_record_draft,
    record_sha256,
)
from whiskeyjack_bot.forecast.schema import ForecastSchemaError, response_model_for
from whiskeyjack_bot.forecast.store import ModelCall, read_forecast_record, read_model_call

if TYPE_CHECKING:
    from whiskeyjack_bot.config import AppConfig


@dataclass(frozen=True)
class ForecastReplay:
    """The outcome of one replay: what was stored, what re-derived, and whether they agree.

    Cannot be constructed in a shape that claims a match it did not compute (M1-312's rule
    for :class:`~whiskeyjack_bot.forecast.persist.GenerationPersistence`, applied to the
    thing this branch exists to assert). ``replayed_sha256`` is ``None`` exactly when the
    stored text no longer parses, and ``problems`` is non-empty exactly then -- the same
    invariant ``_parse`` itself maintains, carried out to the caller.

    ``matches`` is not an independent field a caller could set: it is derived, and the
    constructor refuses any value but the derived one.
    """

    record_id: str
    stored_sha256: str
    replayed_sha256: str | None
    matches: bool
    problems: tuple[str, ...]
    call: ModelCall
    raw_response_count: int

    def __post_init__(self) -> None:
        derived = self.replayed_sha256 is not None and self.replayed_sha256 == self.stored_sha256
        if self.matches is not derived:
            raise ForecastRecordError("a replay matches exactly when it re-derived the stored hash")
        if (self.replayed_sha256 is None) != bool(self.problems):
            raise ForecastRecordError(
                "a replay that could not re-parse the stored reply reports why, and one "
                "that could reports nothing"
            )


def _require_enabled(config: AppConfig) -> Path:
    try:
        enabled = config.forecast.replay_saved_model_output
        root = config.storage.artifact_root
    except AttributeError:
        raise ForecastRecordError("config must be an AppConfig") from None
    if not isinstance(root, Path) or type(enabled) is not bool:
        raise ForecastRecordError("config must be an AppConfig")
    if not enabled:
        raise ForecastRecordError(
            "forecast.replay_saved_model_output is disabled; refusing to replay saved model output"
        )
    return root


def _require_matching_artifact(stored: StoredModelOutput, record: ForecastRecord) -> None:
    """Refuse an artifact that is not this record's.

    ``forecast_records.raw_output_path`` is a free-text column: ``008`` constrains its
    *shape*, not which file it names, and it is written once and never corrected. So the
    envelope's own provenance is compared against the row rather than trusted -- an
    artifact carrying another attempt's text would otherwise be re-parsed and its hash
    reported as this record's, which is the single worst thing this module could do.

    ``read_raw_model_output`` already refused an envelope that carries no provenance at
    all; this is the other half, and neither substitutes for the other.
    """
    if stored.attempt_id != record.attempt_id or stored.question_id != record.question_id:
        raise ForecastRecordError(
            "the artifact this record names belongs to a different attempt or question; "
            "refusing to replay it (offending values withheld)"
        )
    if not stored.raw_responses:
        raise ForecastRecordError(
            "the artifact this record names carries no model reply; there is nothing to "
            "replay, which is not the same as a replay that did not match"
        )


def replay_forecast(
    conn: sqlite3.Connection, config: AppConfig, *, record_id: str
) -> ForecastReplay:
    """Re-derive one stored forecast from its stored model output. Makes no API call.

    Reads the record, reads the artifact its row names, re-runs the identical parse over the
    reply that produced the forecast, rebuilds the record with the **stored** identity, and
    reports both hashes.

    The reply re-parsed is ``raw_responses[-1]``. ``generate_forecast`` appends every reply
    in order and returns as soon as one parses, so the last one is the one the record came
    from; an earlier one is a malformed reply that a repair turn replaced, and re-parsing it
    would deliberately reproduce a failure.

    Raises :class:`ForecastRecordError` when the replay could not be *attempted* -- replay
    disabled, unknown record, no recorded artifact, an artifact belonging to something else.
    A replay that was attempted always returns, including when it does not match: a mismatch
    is a finding about the ledger, and raising would make it indistinguishable from a
    mechanical failure to look.
    """
    root = _require_enabled(config)
    if type(record_id) is not str:
        raise ForecastRecordError("record_id must be a string")

    record = read_forecast_record(conn, record_id)
    call = read_model_call(conn, record_id)
    if call.raw_output_path is None:
        raise ForecastRecordError(
            "this record has no recorded raw model output; it cannot be replayed, which is "
            "not the same as a replay that did not match"
        )
    try:
        stored = read_raw_model_output(root, call.raw_output_path)
    except ArtifactError as exc:
        # Message preserved rather than replaced, for approval.py's rule: an ArtifactError
        # names no content, and the path it does name is the one thing that makes an
        # unreadable artifact actionable (the settled M1-401 carve-out).
        raise ForecastRecordError(str(exc)) from None
    _require_matching_artifact(stored, record)

    # `read_forecast_record` has already refused any row whose stored `forecast_sha256`
    # is not the hash of its own `record_json`, so this value provably equals the stored
    # column. Recomputing it rather than selecting it keeps the comparison between two
    # things this process derived the same way.
    stored_sha256 = record_sha256(record)

    try:
        model = response_model_for(record.question_type)
    except ForecastSchemaError as exc:
        # Reachable without a hostile operator: `007` pins the three supported types in the
        # schema, but a record stored under a type this build no longer dispatches would
        # arrive here, and a raw ForecastSchemaError out of a public boundary is a review
        # finding in this project (it has been, twice).
        raise ForecastRecordError(str(exc)) from None

    source_ids = tuple(source.source_id for source in record.sources)
    try:
        forecast, problems = _parse(
            stored.raw_responses[-1],
            model,
            config.forecast,
            question=record.question,
            source_ids=source_ids,
        )
    except ForecastSchemaError as exc:
        # The same translation as ``response_model_for`` above, for the same reason, and
        # round 1 found the path: ``_parse`` reaches the type-specific checkers, and those
        # refuse a *question* they cannot check a forecast against -- ``numeric``'s is a
        # ``zero_point`` at or above ``lower_bound``, which ``CanonicalNumericQuestion``
        # and ``ForecastRecordDraft`` both accept and which every writer accepted before
        # M1-405 registered a numeric checker at all. So the state is ordinary, already in
        # the ledger, and not something a replay may crash on with another module's
        # exception type.
        #
        # The generating path needs no such handler: ``generate_forecast`` refuses that
        # question in its preflight before anything is spent. Replay has no preflight --
        # it is reading a row that already exists -- which is exactly why the boundary is
        # here. ``from None`` because the chained cause would re-render the problem list.
        raise ForecastRecordError(str(exc)) from None
    if forecast is None:
        return ForecastReplay(
            record_id=record.record_id,
            stored_sha256=stored_sha256,
            replayed_sha256=None,
            matches=False,
            problems=tuple(problems),
            call=call,
            raw_response_count=len(stored.raw_responses),
        )

    replayed = ForecastGeneration(
        forecast=forecast,
        settings=stored.settings,
        # Rebuilt as real `SourceReference`s rather than passed as the record's own
        # `RecordedSource`s. The two carry the same four fields and
        # `build_forecast_record_draft` reads them by name, so duck-typing would work and
        # would also be the kind of accidental coupling that survives until one of the two
        # models gains a field. `forecast.inputs` imports no provider SDK, which the
        # import-graph test pins, so naming its type costs nothing here.
        sources=tuple(
            SourceReference(
                source_id=source.source_id,
                document_id=source.document_id,
                canonical_url=source.canonical_url,
                content_sha256=source.content_sha256,
            )
            for source in record.sources
        ),
        request=stored.request,
        raw_responses=stored.raw_responses,
        invocations=stored.invocations,
        repair_attempted=stored.repair_attempted,
        cost_usd=stored.cost_usd,
        failure_code=None,
        failure_problems=(),
    )
    draft = build_forecast_record_draft(
        question=record.question,
        generation=replayed,
        tournament_id=record.tournament_id,
        attempt_id=record.attempt_id,
        retrieval_run_id=record.retrieval_run_id,
        research_packet_sha256=record.research_packet_sha256,
        generated_at=record.generated_at_utc,
        question_domain=record.question_domain,
    )
    rebuilt = assign_identity(
        draft,
        record_id=record.record_id,
        forecast_version=record.forecast_version,
        parent_record_id=record.parent_record_id,
    )
    replayed_sha256 = record_sha256(rebuilt)
    return ForecastReplay(
        record_id=record.record_id,
        stored_sha256=stored_sha256,
        replayed_sha256=replayed_sha256,
        matches=replayed_sha256 == stored_sha256,
        problems=(),
        call=call,
        raw_response_count=len(stored.raw_responses),
    )
