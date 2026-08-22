"""The canonical forecast record and the hash approval binds to (M1-602).

``forecast_records`` has existed since ``001_initial.sql`` and has been *read* by
:mod:`whiskeyjack_bot.approval`, :mod:`whiskeyjack_bot.submission` and
:mod:`whiskeyjack_bot.lifecycle` ever since. Nothing in ``src/`` had ever written a row.
This module is the record; :mod:`whiskeyjack_bot.forecast.store` is the writer.

**What this module is for.** ``CODEX_HANDOFF.md`` § "Canonical forecast record" lists what
``record_json`` must contain, and ``001`` stores it as one TEXT column. A TEXT column with
a documented list is a contract nothing enforces, so the list is transcribed here as a
pydantic model instead: a record that is missing a field, or carries one the contract does
not name, fails validation rather than reaching an append-only table.

**The hash rule is the project's, not a new one.** ``record_sha256`` digests
``model_dump(mode="json")`` -> ``json.dumps(ensure_ascii=True, sort_keys=True,
separators=(",", ":"), allow_nan=False)`` -> UTF-8 -> SHA-256. That is
:func:`whiskeyjack_bot.research.packet.packet_sha256`'s rule verbatim, and M1-305 paid five
review rounds to establish why it is not any of the near-misses:

- ``model_dump_json()`` raises on a lone surrogate, which provider text can reach;
- ``repr``/``model_dump()`` carry ``datetime.fold`` and astral/surrogate distinctions that
  JSON drops, so a record would not replay to its own hash across the ledger round trip;
- ``ensure_ascii=True`` escapes a lone surrogate rather than failing to encode it;
- ``allow_nan=False`` refuses NaN/Infinity, which ``json.dumps`` would otherwise emit as
  bare tokens that are not JSON and that SQLite reads back as NULL.

**Changing anything about that rendering breaks every stored record**, in the way
``research/hashing.py``'s header describes: previously stored records keep their old
digests, so the approval bound to one no longer verifies. If the rule must change it
changes as a new ``RECORD_SCHEMA_VERSION`` alongside this one, never as an edit to it.

**What is deliberately absent from the record**, and is not an omission:

- *Approval state and history, submission attempts, resolution and score events.* The
  handoff lists them; ``docs/M1-NOTES.md`` (M1-603, "Deferred") settles that they are
  joined at read/export time by M1-604 and ``show``, never written back -- writing them
  back means updating a stored forecast version, which is what D25 forbids.
- *Validation results and the generated numeric CDF.* M1-502 and M1-503, both Not Started.
- *The raw provider response, invocation count and cost.* M1-406 owns raw-output
  persistence, and ``forecast_records`` has no column for any of them.

Local objects and hashing only: this module opens no database, reads no file and makes no
network call.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from whiskeyjack_bot.config import SupportedQuestionType, _StrictModel
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    UtcDatetime,
    _schema_field_names,
    _WITHHELD,
)
from whiskeyjack_bot.questions.model import CanonicalQuestion

if TYPE_CHECKING:
    from whiskeyjack_bot.forecast.generate import ForecastGeneration

# The version of the *record* contract. Three versions travel with one forecast and none
# of them is the others: ``prompts/forecaster.md``'s H1 is the prompt (M1-401),
# ``schema.RESPONSE_SCHEMA_VERSION`` is the model's output contract (M1-402), and this is
# the shape of the row this project stores. ``forecast/schema.py`` makes the same warning
# about its own pair; bumping one does not bump the others.
RECORD_SCHEMA_VERSION = "1.0.0"

_SHA256_LENGTH = 64
_MAX_IDENTIFIER = 200

# Discriminated so a serialized record round-trips back to the right response subclass.
# ``schema.ForecastResponse`` is a bare union -- the discriminator is added here rather
# than there because M1-402 uses the members directly and never needs the tag.
_DiscriminatedForecast = Annotated[ForecastResponse, Field(discriminator="question_type")]


class ForecastRecordError(Exception):
    """A record that could not be built, rendered or read back.

    The project-wide hygiene rule (``ConfigError``, ``StoreError``, ``LifecycleError``):
    the message is a constant and never echoes a question, a document, a model response or
    a stored value. Sanitizing raises use ``from None`` so an underlying exception cannot
    reprint a value through its text or a rendered traceback.

    :mod:`whiskeyjack_bot.forecast.store` raises this type too rather than minting a
    second one, following :mod:`whiskeyjack_bot.research.persist`'s reuse of ``StoreError``:
    the writer adds no failure mode this module does not already own, and a caller
    handling "the record is wrong" separately from "the record could not be stored" would
    be handling a distinction neither module makes.
    """


class RecordedModelSettings(_StrictModel):
    """What the call was made with, as stored.

    Transcribed from :class:`whiskeyjack_bot.forecast.generate.ModelSettings`, which is a
    frozen dataclass rather than a model. Restated here so the persisted shape is fixed by
    this contract: a field added to ``ModelSettings`` for the call's benefit does not
    silently change what every stored record contains.

    Never built from ``GeneralLlm.to_dict()``, which dumps the API key verbatim -- the
    reason ``ModelSettings`` exists at all.
    """

    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    temperature: float = Field(allow_inf_nan=False)
    max_output_tokens: int
    timeout_seconds: float = Field(allow_inf_nan=False)
    allowed_tries: int
    prompt_version: str = Field(min_length=1)
    prompt_sha256: str = Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH)


class RecordedSource(_StrictModel):
    """What one ``source_id`` in the model's output resolves back to.

    The persisted form of :class:`whiskeyjack_bot.forecast.inputs.SourceReference`. This
    is the handoff's "normalized source references", and it is what makes a citation
    auditable: ``content_sha256`` ties the cited text to a ``research_documents`` row, so a
    reader can check that the document the forecast cited is the document that is stored.

    ``document_id`` is nullable because adapters build documents before the ledger mints
    an id (``research/model.py``); a source that was never persisted still has a URL and a
    content hash.
    """

    source_id: str = Field(min_length=1)
    document_id: str | None = None
    canonical_url: str = Field(min_length=1)
    content_sha256: str = Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH)


class RecordedCommunityPrediction(_StrictModel):
    """The handoff's community-prediction bullet, in the only honest shape v1 has.

    The bullet reads "community prediction snapshot and timestamp **when technically
    available**, plus ``used_as_model_input=false`` for the v1 baseline". It is not
    available: ``questions/normalize.py`` is the single place SDK fields are read, and it
    deliberately never carries the parent post's payload precisely because that payload
    holds community-prediction aggregations (``questions/model.py``, ``group_parent_title``).
    So there is no snapshot to store, and the record says so rather than omitting the
    field and leaving a reader to guess which of "absent" and "unavailable" it meant.

    Both snapshot fields are typed ``None`` -- not ``str | None`` -- and
    ``used_as_model_input`` is ``Literal[False]``. That is deliberate and is the M1-312
    rule about a result that cannot represent a lie: "community prediction is never a
    forecaster input in v1" is a CLAUDE.md hard constraint, and a record able to claim
    otherwise would be a place for it to be breached quietly. Making it available later is
    a ``RECORD_SCHEMA_VERSION`` bump, which is the visible change it should be.
    """

    snapshot: None = None
    snapshot_at_utc: None = None
    used_as_model_input: Literal[False] = False


class ForecastRecordDraft(_StrictModel):
    """Everything known about a forecast *before* the ledger assigns it an identity.

    Split from :class:`ForecastRecord` rather than carrying three optional fields, so that
    "not yet persisted" and "persisted" are different types. A writer cannot hand back a
    half-filled record and a caller cannot construct one claiming a version it was never
    given: ``forecast_version`` and ``parent_record_id`` are decided by
    :func:`whiskeyjack_bot.forecast.store.append_forecast_version` inside the transaction
    that reads the current chain, and there is no honest value for them before that.

    The cross-field validators below are the ones that would otherwise be enforced nowhere:
    three objects assembled here each carry their own copy of the question's identity, and
    a record whose three copies disagree is an attribution claim about a question it may
    not be answering.
    """

    record_schema_version: str
    question_id: int
    post_id: int
    tournament_id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER)
    question_type: SupportedQuestionType
    # Free-form and caller-supplied, never derived from ``question.source_categories``.
    # ``docs/M1-NOTES.md`` (M1-201) records that no mechanical mapping exists from
    # Metaculus categories to this project's ``config/x_accounts.yaml`` taxonomy, and
    # M1-308 settled that domains stay free-form. Deriving one here would invent a
    # taxonomy mapping under an item whose acceptance criterion is about version chains.
    question_domain: str | None = None
    question: CanonicalQuestion
    attempt_id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER)
    model_settings: RecordedModelSettings
    retrieval_run_id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER)
    # The packet the forecast *saw*, stamped rather than stored. M1-306 decided against a
    # ``research_packets`` table because a stored hash that no longer matches the rows it
    # summarizes is an attribution claim the evidence contradicts; the truth lives in the
    # evidence rows and is recomputed. This stamp is the other half of that design -- it is
    # what a later audit compares the recomputed hash against.
    research_packet_sha256: str = Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH)
    sources: list[RecordedSource] = Field(default_factory=list)
    community_prediction: RecordedCommunityPrediction = Field(
        default_factory=RecordedCommunityPrediction
    )
    forecast: _DiscriminatedForecast
    generated_at_utc: UtcDatetime

    @model_validator(mode="after")
    def _schema_version_matches(self) -> ForecastRecordDraft:
        if self.record_schema_version != RECORD_SCHEMA_VERSION:
            raise ValueError(
                f"record_schema_version must be {RECORD_SCHEMA_VERSION} "
                "(offending input withheld from this message)"
            )
        return self

    @model_validator(mode="after")
    def _one_question(self) -> ForecastRecordDraft:
        """The question, the response and the row's own columns must name one question.

        ``question_id`` and ``question_type`` are ``forecast_records`` columns *and*
        fields on both nested objects. Nothing else compares them: M1-501's
        ``attribution_problems`` checks the response against a ``question_id`` its caller
        supplies, which is this same value, so it cannot catch a caller that supplied the
        wrong one.
        """
        if self.question.question_id != self.question_id or self.forecast.question_id != (
            self.question_id
        ):
            raise ValueError(
                "question_id must agree across the record, the question and the forecast"
            )
        if self.question.post_id != self.post_id:
            raise ValueError("post_id must agree between the record and the question")
        if self.question.qtype != self.question_type or self.forecast.question_type != (
            self.question_type
        ):
            raise ValueError(
                "question_type must agree across the record, the question and the forecast"
            )
        return self

    @model_validator(mode="after")
    def _source_ids_are_distinct(self) -> ForecastRecordDraft:
        """A repeated ``source_id`` makes a citation ambiguous rather than merely untidy.

        The response cites sources by id; two ``RecordedSource`` entries sharing one id
        mean a stored citation resolves to two different documents, so the record cannot
        be replayed against the evidence it claims. Constant message: which id repeated is
        provider-derived text.
        """
        ids = [source.source_id for source in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("sources must not repeat a source_id")
        return self


class ForecastRecord(ForecastRecordDraft):
    """A draft plus the identity the ledger assigned it.

    This is what ``record_json`` holds and what ``forecast_sha256`` digests. Every scalar
    column of the ``forecast_records`` row is also a field here, so the hash attests to
    the whole row rather than to a summary of it -- a column that disagreed with the
    record would otherwise be undetectable.
    """

    record_id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER)
    forecast_version: int = Field(ge=1)
    parent_record_id: str | None = None

    @model_validator(mode="after")
    def _version_one_is_the_root(self) -> ForecastRecord:
        """The Python half of migration ``007``'s parent clause.

        Both layers, for the reason M1-603's round 5 records: a rule enforced only in the
        writer was defeated by a value the writer and the schema disagreed about. This
        cannot check that the parent is the *previous* version -- that needs the ledger --
        but the root/non-root split needs nothing but the record itself, so it is checked
        where a caller building a record by hand will meet it.
        """
        if (self.forecast_version == 1) != (self.parent_record_id is None):
            raise ValueError(
                "forecast version 1 is the root of a chain and has no parent; "
                "every later version names the record it supersedes"
            )
        if self.parent_record_id is not None and self.parent_record_id == self.record_id:
            raise ValueError("a forecast record cannot be its own parent")
        return self


_RecordAdapter: TypeAdapter[ForecastRecord] = TypeAdapter(ForecastRecord)


def _canonical_json(payload: Any, what: str) -> str:
    """Render ``payload`` under the pinned rule, or fail as this module's error.

    ``from None`` and a constant message: ``json.dumps`` names the offending value in both
    the circular-reference and the out-of-range-float cases, and everything reaching here
    is question text, provider text or model output.
    """
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ForecastRecordError(
            f"{what} could not be rendered as canonical JSON "
            "(detail withheld: it can echo question, document or model content)"
        ) from None


# A UTF-16 high surrogate immediately followed by a low one. Two Python code points that
# `json.dumps(ensure_ascii=True)` escapes to the same two \uXXXX units an astral scalar
# escapes to -- and that `json.loads` then **recombines into the single scalar**. So the
# pair is the one input that does not survive this project's persisted form: what comes
# back out of the ledger is a different Python string than what went in.
_SURROGATE_PAIR_RE = re.compile("[\ud800-\udbff][\udc00-\udfff]")


def _require_replayable(payload: Any, path: str = "<record>") -> None:
    """Refuse a value that would come back out of the ledger as a different value.

    Measured, not assumed. Of everything a record can hold, exactly one shape fails to
    survive ``model_dump(mode="json")`` -> ``json.dumps(ensure_ascii=True)`` ->
    ``json.loads``: a **surrogate pair**. ``"\ud83d\ude00"`` is two code points going in
    and one scalar coming back, so a record holding one is stored as something the
    forecaster did not produce, and a replay reproduces the ledger's version rather than
    the model's. That is an attribution loss of exactly the kind this ledger exists to
    prevent, and ``003`` makes the row uncorrectable.

    A **lone** surrogate is deliberately *not* refused *here*, and the distinction is the
    point. It escapes to ``"\ud800"`` and ``json.loads`` hands back the same lone
    surrogate, so it round-trips exactly -- ``forecast/inputs.render_model_input`` says as
    much about the same rendering rule, and refusing it in ``record_json`` would contradict
    a sibling in this subpackage over an input that is provably safe.

    That holds for ``record_json`` and **only** for ``record_json``. Round 1's finding B2 is
    that the writer also copies a dozen scalars into their own bare TEXT columns, which hold
    the value as written and cannot carry a lone surrogate at all --
    :func:`_require_storable_in_a_text_column` is that half of the rule. So this project
    ends up in the same place ``research/store.py`` is for the fields it stores twice, and a
    step less strict for the fields it stores only inside canonical ASCII JSON.

    Reachable rather than theoretical: this text is a Metaculus question title and raw
    model output, both untrusted under CLAUDE.md's threat boundary.

    ``path`` is a field path this module authored; the *value* is never named.
    """
    if isinstance(payload, str):
        if _SURROGATE_PAIR_RE.search(payload):
            raise ForecastRecordError(
                f"{path} holds a UTF-16 surrogate pair, which the ledger cannot store "
                "without changing it (offending input withheld from this message)"
            )
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            _require_replayable(key, f"{path}.<key>")
            _require_replayable(value, f"{path}.{key}")
        return
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            _require_replayable(item, f"{path}[{index}]")


# The record fields `forecast.store` projects into a **bare TEXT** column of
# `forecast_records`, as dotted paths into the dumped payload.
#
# Round 1, finding B2. `record_json` holds `ensure_ascii` output and is therefore pure
# ASCII, which is why a lone surrogate is storable *there* -- but the writer also copies a
# dozen scalars into their own columns, and those hold the value as written. sqlite3
# encodes a TEXT parameter as UTF-8 at bind time, so a lone surrogate in one of them raises
# a raw `UnicodeEncodeError` that quotes the character. The transaction rolls back and
# nothing is written, so this is an exception-contract and leak defect rather than a
# corruption one -- but it is still a raw exception escaping a public boundary, and its
# text names the offending character.
#
# `final_prediction_json` and `record_json` are absent deliberately: both are
# `ensure_ascii` output and cannot contain an unencodable character. `status` is the
# literal `'draft'` and `created_at_utc` is `strftime` output.
#
# `test_every_bare_text_column_is_probed` pins this list against the writer's own column
# set, so a column added to one and not the other fails rather than silently reopening this.
_BARE_TEXT_PROJECTIONS = (
    "record_id",
    "parent_record_id",
    "tournament_id",
    "question_type",
    "question_domain",
    "retrieval_run_id",
    "attempt_id",
    "generated_at_utc",
    "model_settings.provider",
    "model_settings.name",
    "model_settings.prompt_version",
    "model_settings.prompt_sha256",
)


def _require_storable_in_a_text_column(payload: dict[str, Any]) -> None:
    """Refuse a record whose scalar columns cannot be bound as SQLite TEXT.

    The narrow companion to :func:`_require_replayable`. That one refuses what does not
    survive the JSON round trip; this one refuses what cannot be *written* at all. Kept as
    a separate rule with its own field list because the two have different domains: a lone
    surrogate passes the first and fails this one, which is exactly the case round 1 found.
    """
    for path in _BARE_TEXT_PROJECTIONS:
        value: Any = payload
        for part in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if not isinstance(value, str):
            continue
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # from None and no value in the message: the UnicodeEncodeError names the
            # offending character, which is the leak half of this finding.
            raise ForecastRecordError(
                "a record field cannot be stored in a text column of the ledger: "
                f"{path} (offending input withheld from this message)"
            ) from None


def _dumped(record: ForecastRecord) -> dict[str, Any]:
    """``model_dump(mode="json")``, with pydantic's value-echoing warning suppressed.

    ``warnings=False`` is not noise control. A pydantic serializer warning embeds the
    offending *value* in its text and reaches stderr and captured logs, so it is an egress
    channel (M1-302 round 1); ``pyproject.toml`` makes any such warning a suite failure for
    that reason.
    """
    if not isinstance(record, ForecastRecord):
        raise ForecastRecordError("record must be a ForecastRecord")
    try:
        dumped: dict[str, Any] = record.model_dump(mode="json", warnings=False)
    except Exception:
        raise ForecastRecordError(
            "the forecast record could not be serialized "
            "(detail withheld: it can echo question, document or model content)"
        ) from None
    # Checked on every rendering rather than only at build time, so no path to the stored
    # bytes can skip it -- `canonical_record_json`, `record_sha256` and
    # `canonical_final_prediction_json` all come through here, and so does every write.
    _require_replayable(dumped)
    _require_storable_in_a_text_column(dumped)
    return dumped


def canonical_record_json(record: ForecastRecord) -> str:
    """Return the exact bytes stored in ``forecast_records.record_json``."""
    return _canonical_json(_dumped(record), "the forecast record")


def canonical_final_prediction_json(record: ForecastRecord) -> str:
    """Return the exact bytes stored in ``forecast_records.final_prediction_json``.

    The typed forecast on its own, taken from the same dump the record hash is computed
    over rather than re-dumped from ``record.forecast.final_prediction``. Two dumps of one
    object are two chances to disagree, and this column is the one M1-604 exports as *the*
    prediction: it must be the same bytes that are nested inside ``record_json``.
    """
    dumped = _dumped(record)
    try:
        prediction = dumped["forecast"]["final_prediction"]
    except (KeyError, TypeError):
        raise ForecastRecordError("the forecast record carries no final prediction") from None
    return _canonical_json(prediction, "the final prediction")


def record_sha256(record: ForecastRecord) -> str:
    """Return the lowercase hex SHA-256 of ``record`` under the pinned rule.

    This is ``forecast_records.forecast_sha256`` -- the value approval binds to (D12/D23),
    which is why it digests the canonical JSON of the *whole* record: any content change,
    anywhere in it, invalidates an approval rather than only a change to the prediction.
    """
    return hashlib.sha256(canonical_record_json(record).encode("utf-8")).hexdigest()


def record_from_json(text: str) -> ForecastRecord:
    """Parse a stored ``record_json`` back into a record, refusing any drift.

    Two checks, and the second is the load-bearing one.

    ``_StrictModel`` is ``extra="forbid"`` but **not** ``strict``: pydantic will coerce
    ``"42"`` to ``42`` and ``1`` to ``1.0``, which M1-303 round 4 found the hard way. A
    reader that coerced would hand back a record that is not what the ledger holds, and
    would then hash to something other than the stored ``forecast_sha256`` -- silently, as
    a "successful" read.

    So the parsed record is re-rendered and compared to the text it came from, byte for
    byte. That catches coercion, key reordering, dropped defaults and any future drift in
    the rendering rule, and it is the exact property the ledger needs: what is read back
    equals what was written. Stored values are untrusted under CLAUDE.md's threat boundary,
    and this is the check that makes reading one safe.
    """
    if type(text) is not str:
        raise ForecastRecordError("stored record_json is not text")
    try:
        parsed = json.loads(text)
    except ValueError:
        # from None: a JSONDecodeError quotes the surrounding document text.
        raise ForecastRecordError(
            "stored record_json is not valid JSON (detail withheld: it can echo a stored value)"
        ) from None
    if not isinstance(parsed, dict):
        raise ForecastRecordError("stored record_json is not a JSON object")
    try:
        record = _RecordAdapter.validate_python(parsed)
    except ValidationError as exc:
        raise _sanitized(exc) from None
    if canonical_record_json(record) != text:
        raise ForecastRecordError(
            "stored record_json does not round-trip to itself; the stored record is not "
            "in canonical form or does not match this record schema"
        )
    return record


def _sanitized(exc: ValidationError) -> ForecastRecordError:
    """Rebuild a ``ValidationError`` as this module's error with no input echoed.

    ``include_input=False, include_url=False`` is the project-wide rule, and **it is not
    sufficient on its own**. Round 1, finding B1: those two flags suppress pydantic's
    ``input`` field, but several of its ``msg`` strings interpolate the offending value
    into the message text itself. The discriminated union is the reachable case --
    a stored ``forecast.question_type`` of ``"WJLEAKMARKER-secret"`` produces
    ``Input tag 'WJLEAKMARKER-secret' found using 'question_type' does not match ...``,
    which reached ``ForecastRecordError`` verbatim. Reproduced by execution before this fix.

    So the message is rebuilt from ``loc`` and ``type`` only. Both are ours or pydantic's
    own fixed vocabulary -- ``loc`` is a field path declared in this module, ``type`` is a
    slug from pydantic's error catalogue (``union_tag_invalid``, ``missing``,
    ``string_type``) that never carries an input.

    ``msg`` is dropped **entirely**, including for ``value_error`` entries raised by this
    project's own validators, whose texts happen to be value-free today. That is the
    stricter reading and it is deliberate: an allowlist keyed on error type would make
    every future validator's wording part of the leak surface, silently, and this rule has
    already been breached once by a message nobody wrote.

    **``loc`` is not automatically safe either**, and that half was still open after the
    first pass at this fix. Under ``extra="forbid"`` the location of an unexpected key *is*
    that key, and the keys of a stored ``record_json`` are untrusted: a stored object with a
    key named ``WJLEAKMARKER-extra-key`` produced
    ``stored record_json does not match the record schema (WJLEAKMARKER-extra-key:
    extra_forbidden)``. So a location part survives only if this schema authored it -- an
    integer list index, or a field name declared somewhere in the model tree.

    ``_schema_field_names`` and ``_WITHHELD`` are imported from
    :mod:`whiskeyjack_bot.forecast.schema` rather than reimplemented. It is a private name
    from a sibling in the same subpackage, which is deliberate: this is one rule, the
    traversal is non-trivial (the model tree is five levels deep here), and M1-607's note
    about a rule written twice is what round 1's finding B4 turned out to be.

    Integer ``loc`` entries are list indices and render as ``[i]`` rather than being
    dropped, so the path stays readable.
    """
    known = _schema_field_names(ForecastRecord)
    paths = []
    for error in exc.errors(include_input=False, include_url=False):
        parts = []
        for part in error["loc"]:
            if isinstance(part, int):
                parts.append(f"[{part}]")
            elif part in known:
                parts.append(str(part))
            else:
                parts.append(_WITHHELD)
        location = ".".join(parts)
        paths.append(f"{location or '<record>'}: {error['type']}")
    joined = "; ".join(sorted(set(paths)))
    return ForecastRecordError(f"stored record_json does not match the record schema ({joined})")


def build_forecast_record_draft(
    *,
    question: CanonicalQuestion,
    generation: ForecastGeneration,
    tournament_id: str,
    attempt_id: str,
    retrieval_run_id: str,
    research_packet_sha256: str,
    generated_at: datetime,
    question_domain: str | None = None,
) -> ForecastRecordDraft:
    """Assemble a draft from the objects the pipeline already holds.

    ``generation`` is what :func:`whiskeyjack_bot.forecast.generate.generate_forecast`
    returned. A failed generation is refused here rather than persisted as an empty
    record: ``ForecastGeneration.forecast`` is ``None`` exactly when ``failure_code`` is
    set, and that case has its own ledger home -- ``pipeline_failure_events``, scoped to
    this same ``attempt_id`` (M1-606). Writing a forecast row for it would be the second
    record of one failure, in the table that is supposed to hold successes.

    ``generated_at`` is caller-supplied rather than read from the clock, matching
    ``lifecycle``'s ``occurred_at`` parameters: a replayed run has to be able to reproduce
    the timestamp it stored. ``forecast.as_of_utc`` is *not* it -- that is the model's
    claim about the world, not the pipeline's record of when it ran.
    """
    if not isinstance(tournament_id, str):
        raise ForecastRecordError("tournament_id must be a string")
    if not isinstance(attempt_id, str):
        raise ForecastRecordError("attempt_id must be a string")
    if not isinstance(retrieval_run_id, str):
        raise ForecastRecordError("retrieval_run_id must be a string")
    if not isinstance(research_packet_sha256, str):
        raise ForecastRecordError("research_packet_sha256 must be a string")
    if question_domain is not None and not isinstance(question_domain, str):
        raise ForecastRecordError("question_domain must be a string or None")
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise ForecastRecordError("generated_at must be a timezone-aware datetime")

    forecast = getattr(generation, "forecast", None)
    if forecast is None:
        raise ForecastRecordError(
            "a generation that produced no forecast has no record to persist; "
            "record it as a pipeline failure instead"
        )
    settings = getattr(generation, "settings", None)
    sources = getattr(generation, "sources", None)
    if settings is None or sources is None:
        raise ForecastRecordError("generation must be a ForecastGeneration")

    try:
        recorded_settings = RecordedModelSettings(
            provider=settings.provider,
            name=settings.name,
            temperature=settings.temperature,
            max_output_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            allowed_tries=settings.allowed_tries,
            prompt_version=settings.prompt_version,
            prompt_sha256=settings.prompt_sha256,
        )
        recorded_sources = [
            RecordedSource(
                source_id=source.source_id,
                document_id=source.document_id,
                canonical_url=source.canonical_url,
                content_sha256=source.content_sha256,
            )
            for source in sources
        ]
    except (AttributeError, TypeError, ValidationError):
        # Deliberately broad and scoped to this one block, the M1-308 round-7 rule: what
        # arrives here is a caller's object, and a pydantic or attribute failure would
        # otherwise echo a model name, a URL or a prompt hash through its message.
        raise ForecastRecordError(
            "generation must be a ForecastGeneration carrying model settings and "
            "resolvable sources (detail withheld: it can echo a stored value)"
        ) from None

    try:
        draft = ForecastRecordDraft(
            record_schema_version=RECORD_SCHEMA_VERSION,
            question_id=question.question_id,
            post_id=question.post_id,
            tournament_id=tournament_id,
            question_type=question.qtype,
            question_domain=question_domain,
            question=question,
            attempt_id=attempt_id,
            model_settings=recorded_settings,
            retrieval_run_id=retrieval_run_id,
            research_packet_sha256=research_packet_sha256,
            sources=recorded_sources,
            community_prediction=RecordedCommunityPrediction(),
            forecast=forecast,
            generated_at_utc=generated_at,
        )
    except (AttributeError, ValidationError):
        raise ForecastRecordError(
            "the forecast record could not be assembled "
            "(detail withheld: it can echo question, document or model content)"
        ) from None
    # Checked here too, so a caller learns the record is unstorable at build time rather
    # than from inside the writer's transaction. `_dumped` is the binding check; this one
    # is the early one, the same shape as M1-303's "refuse before you spend" rule. The
    # identity fields are absent from a draft, so the column probe simply skips them here
    # and catches them on the record.
    dumped = draft.model_dump(mode="json", warnings=False)
    _require_replayable(dumped)
    _require_storable_in_a_text_column(dumped)
    return draft


def require_unassigned_draft(draft: object) -> ForecastRecordDraft:
    """Return ``draft`` if it is a draft that has not been given an identity yet.

    Exact type rather than ``isinstance``, because :class:`ForecastRecord` subclasses
    :class:`ForecastRecordDraft`: an already-appended record would otherwise pass the gate
    and be minted a second identity for a forecast that already has one -- the duplicate
    ``001``'s UNIQUE constraint exists to prevent. One helper shared by
    :func:`assign_identity` and :mod:`whiskeyjack_bot.forecast.store`, so the rule cannot
    hold in one place and not the other (round 1, finding B4).
    """
    if type(draft) is not ForecastRecordDraft:
        raise ForecastRecordError(
            "draft must be a ForecastRecordDraft; a record that already has an identity "
            "cannot be given another one"
        )
    return draft


def assign_identity(
    draft: ForecastRecordDraft,
    *,
    record_id: str,
    forecast_version: int,
    parent_record_id: str | None,
) -> ForecastRecord:
    """Promote a draft to a record by attaching the identity the ledger assigned.

    Called only by :mod:`whiskeyjack_bot.forecast.store`, inside the transaction that read
    the current chain. Exposed rather than made private so a test can build a record
    without a database, and so the draft/record split is a real boundary rather than a
    convention.

    Refuses by **exact type**, not ``isinstance``. Round 1, finding B4: ``ForecastRecord``
    subclasses ``ForecastRecordDraft``, so an already-assigned record passed an
    ``isinstance`` gate and then hit ``ForecastRecord() got multiple values for keyword
    argument 'record_id'`` -- a raw ``TypeError`` escaping a public boundary. The writer
    already made this distinction; the shared helper is here so the two cannot disagree
    about it.
    """
    require_unassigned_draft(draft)
    try:
        return ForecastRecord(
            **draft.model_dump(warnings=False),
            record_id=record_id,
            forecast_version=forecast_version,
            parent_record_id=parent_record_id,
        )
    except ValidationError as exc:
        raise _sanitized(exc) from None
