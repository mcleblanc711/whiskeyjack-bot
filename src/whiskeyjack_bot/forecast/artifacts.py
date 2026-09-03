"""Raw model output on disk, as forecast artifacts (M1-406).

``generate_forecast`` returns a :class:`~whiskeyjack_bot.forecast.parse.ForecastGeneration`
carrying the rendered request, every raw provider reply, the invocation count and the cost,
and until this item nothing persisted any of them -- ``docs/M1-NOTES.md`` lists all four
under *"Deferred → M1-406"*, and ``forecast_records`` had no column for one. This module is
the file layout; :mod:`whiskeyjack_bot.forecast.persist` is what composes it with the
ledger write.

**What these files are for, and it is not what the research ones are for.**
``research/artifacts.py`` is explicit that its files are *not* the replay substrate: replay
reads the ledger's normalized rows, because re-normalizing from raw would make a replayed
packet depend on adapter code version. These files are different, and deliberately so.
M1-406's acceptance criterion is *"model replay makes zero API calls and reproduces the
parsed forecast hash"*, which is only a claim about anything if the parsed forecast is
**re-derived** from the stored text and compared. So :mod:`whiskeyjack_bot.forecast.replay`
does re-parse what is stored here -- *in order to compare against the stored hash*, never
to replace it. The record stays authoritative; a mismatch is a loud failure, not a new
record. Re-parsing to verify and re-parsing to re-derive are different acts, and only the
second one lets the ledger rewrite its own history on a refactor.

**Keyed on ``attempt_id``, not ``record_id``.** An ``attempt_id`` is minted once per
campaign, before the call is made; it is `UNIQUE` on ``forecast_records`` since ``004``;
and it is the only key a *failed* generation has, since a generation that produced no
forecast produces no record. A call that cost money and returned unusable text is still a
call whose evidence must survive, so the artifact is written either way -- see
``forecast/persist.py`` on why a file with no row pointing at it is the benign direction.

The envelope is versioned and shaped after ``research/artifacts.py``, which is itself
shaped after ``metaculus/snapshots.py``, rather than inventing a third convention. The
three file-layout rules (never overwrite, atomic creation, relative stored path) are
:mod:`whiskeyjack_bot.artifacts`', shared with the retrieval writer so the two cannot
drift.

**Secret hygiene, and one deliberate difference from the retrieval envelope.** That one
excludes the request outright, because a retrieval request's URL and headers carry the API
key. A forecaster request carries none: it is the prompt this project rendered, from
``forecast.inputs``, and the API key travels in a litellm header this module never sees. So
``request`` **is** stored, and without it the artifact could not show what the model was
actually asked -- which is most of what makes it evidence. The model settings are
``ModelSettings``' eight fields and are built from config; ``GeneralLlm.to_dict()`` is never
called anywhere in this project because it dumps ``litellm_kwargs`` wholesale, API key
included.

That closes the *known* channel; it does not close every one. ``request`` and
``raw_responses`` are otherwise stored verbatim, and a provider's own text -- an HTTP error
body, a model reply -- can echo back a credential this project sent, the same class of leak
``logging_setup.ProviderResponseTextFilter`` already guards against in logs. M1-605 closes it
here too: :func:`write_raw_model_output` redacts every configured credential's value out of
``request`` and each ``raw_responses`` entry before the envelope is written.

**D24 is not touched.** What is stored is the provider's returned text -- the reply the
parser was handed -- not a reasoning trace, and this module has no access to one. The
canonical record is unaffected: ``record_json`` still carries no raw response, which
``test_the_record_stores_no_hidden_reasoning_and_no_raw_response`` asserts on the rendered
bytes.

Purely local file I/O: no network access on any path through here, and no import that
reaches a provider client -- which is what makes replay's zero-calls claim structural.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard, get_args

from whiskeyjack_bot.artifacts import (
    ArtifactError,
    require_int,
    require_safe_component,
    write_new_file,
)
from whiskeyjack_bot.config import MAX_MODEL_INVOCATIONS
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.lifecycle import PreForecastFailureCode
from whiskeyjack_bot.redaction import redact_secrets

# The version of *this* envelope. Not `RESPONSE_SCHEMA_VERSION` (the model's output
# contract), not `RECORD_SCHEMA_VERSION` (the shape of the stored row), and not
# `ARTIFACT_SCHEMA_VERSION` (the retrieval envelope). Four versions travel with one
# forecast and none of them is the others; bumping one does not bump the rest.
MODEL_OUTPUT_SCHEMA_VERSION = "1.0.0"

# The subdirectory of storage.artifact_root raw model output lives under, beside
# `research/`. Namespacing was `research/artifacts.py`'s stated reason for having a
# subdirectory at all.
_FORECAST_SUBDIR = "forecast"

_WHAT = "raw model output artifact"

_FAILURE_CODES: frozenset[str] = frozenset(get_args(PreForecastFailureCode))

# The `ModelSettings` fields, in one list, used to write the envelope and to read it back.
# One definition rather than two, for `forecast/store.py::_projection`'s reason: a reader
# that enumerates a different set than the writer is a reader that silently drops a field
# or invents a default for one that was never stored.
_SETTINGS_TEXT_FIELDS = ("provider", "name", "prompt_version", "prompt_sha256")
_SETTINGS_INT_FIELDS = ("max_output_tokens", "allowed_tries")
_SETTINGS_FLOAT_FIELDS = ("temperature", "timeout_seconds")

# The envelope's own top-level field set, for the same reason and enforced the same way as
# `model_settings`' (see `_settings`). The reader checked each field with `.get()`, which
# made a *missing* nullable field read back as `None` -- indistinguishable from a cost that
# was explicitly recorded as unknown -- and let an unknown key through untouched. Round 1
# finding 1. `test_the_writer_emits_exactly_the_envelope_fields` pins this against what the
# writer actually renders, since a constant compared only against itself pins nothing.
_ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {
        "artifact_schema_version",
        "attempt_id",
        "question_id",
        "model_settings",
        "request",
        "raw_responses",
        "invocations",
        "repair_attempted",
        "cost_usd",
        "failure_code",
        "failure_problems",
        "written_at_utc",
    }
)


@dataclass(frozen=True)
class StoredModelOutput:
    """One raw-output artifact, read back.

    Carries what :class:`ForecastGeneration` carried, minus the parsed forecast -- which is
    exactly the point: the parsed forecast is what replay re-derives from ``raw_responses``
    and compares against the ledger, so storing it here too would let a replay verify a
    copy of the answer against itself.
    """

    attempt_id: str
    question_id: int
    settings: ModelSettings
    request: str
    raw_responses: tuple[str, ...]
    invocations: int
    repair_attempted: bool
    cost_usd: float | None
    failure_code: str | None
    failure_problems: tuple[str, ...]
    written_at_utc: datetime


def artifact_relative_path(*, question_id: int, attempt_id: str) -> str:
    """The path an artifact is stored at, relative to ``storage.artifact_root``.

    Exposed so a reader can resolve a stored path without restating the layout, and so the
    layout has exactly one definition.
    """
    attempt = require_safe_component(attempt_id, field="attempt_id")
    question = require_int(question_id, "question_id")
    return f"{_FORECAST_SUBDIR}/{question}/{attempt}.json"


def _finite(value: object) -> TypeGuard[float]:
    """Exactly a ``float``, and not NaN or an infinity.

    A ``TypeGuard`` rather than a plain ``bool`` so the callers below narrow: `type() is
    float` already excludes ``bool`` (which subclasses ``int``, not ``float``) and ``int``,
    but mypy cannot see that through a helper without it.
    """
    return type(value) is float and value == value and value not in (float("inf"), float("-inf"))


def _writer_cost(value: object) -> float | None:
    """``None`` means *unknown*, not free -- the M1-303 rule, kept at the storage layer.

    ``generate_forecast`` publishes a cost only when every attempted call reported a usable
    figure; anything less is a subtotal, and a subtotal stored as a total looks exactly like
    a complete one.

    An ``int`` from a caller is normalized to ``float`` here, which is why the reader below
    can be strict: after this, the envelope holds a JSON float or ``null`` and nothing else.
    """
    if value is None:
        return None
    if type(value) is int:
        value = float(value)
    if not _finite(value):
        raise ArtifactError("cost_usd must be None or a finite number")
    if value < 0.0:
        raise ArtifactError("cost_usd must not be negative")
    return float(value)


def _reader_number(value: object, what: str) -> float:
    """A stored number must be a JSON **float**, not an int that happens to be equal.

    Found by ``tests/property/test_model_artifact_properties.py`` at the full 200-draw
    profile (the `fast` profile missed it, which is the whole reason `fast` is never a
    gate): the reader used to coerce ``0`` to ``0.0`` and hand it back as though it had
    round-tripped. ``json.dumps`` renders every float this writer stores with a decimal
    point, so a bare int in one of these positions is a value the writer could not have
    produced -- and a reader that admits more than its writer can emit is not reading the
    format it documents (M1-306 round 1, finding 7).

    The coercion was harmless arithmetically and that is exactly what made it worth
    closing: a silent coercion on an audited value is indistinguishable from a value that
    really was stored that way.
    """
    if not _finite(value):
        raise ArtifactError(f"{what} must be a finite JSON number with a decimal point")
    return float(value)


def _settings_payload(settings: object) -> dict[str, Any]:
    """The eight recorded settings fields, validated off whatever the caller passed.

    Deliberately duck-typed and broad, the ``record.py`` rule: what arrives is a caller's
    object, and an ``AttributeError`` escaping here would be a raw exception out of a public
    boundary. Every message below is this module's own literal.
    """
    payload: dict[str, Any] = {}
    for name in _SETTINGS_TEXT_FIELDS:
        value = getattr(settings, name, None)
        if type(value) is not str or not value.strip():
            raise ArtifactError(f"model settings {name} must be a non-blank string")
        payload[name] = value
    for name in _SETTINGS_INT_FIELDS:
        value = getattr(settings, name, None)
        if type(value) is not int:
            raise ArtifactError(f"model settings {name} must be an int")
        payload[name] = value
    for name in _SETTINGS_FLOAT_FIELDS:
        value = getattr(settings, name, None)
        if type(value) is int:
            value = float(value)
        if type(value) is not float or value != value or value in (float("inf"), float("-inf")):
            raise ArtifactError(f"model settings {name} must be a finite number")
        payload[name] = value
    return payload


def write_raw_model_output(
    artifact_root: Path,
    *,
    attempt_id: str,
    question_id: int,
    generation: ForecastGeneration,
    written_at_utc: datetime,
    retain: bool,
    secret_env_var_names: Sequence[str],
) -> str | None:
    """Write one attempt's raw model output; return the path to record, or ``None``.

    Returns the **relative** path for ``forecast_records.raw_output_path``. Returns ``None``
    -- writing nothing -- when ``retain`` is false, which is the meaning of
    ``storage.retain_raw_model_output: false`` and not a failure.

    ``secret_env_var_names`` is redacted out of ``request`` and every ``raw_responses`` entry
    before they are written (M1-605) -- see this module's docstring. Required rather than
    defaulted, so a caller cannot silently skip redaction by omission.

    Raises :class:`ArtifactError` rather than swallowing a write failure. Callers on the
    paid path must not let that lose the forecast: write the artifact first, and if it
    fails, persist the ledger row anyway with ``raw_output_path=None``. A call that cost
    money and produced no artifact is still a call that must be recorded. That composition
    is :func:`whiskeyjack_bot.forecast.persist.persist_generation`, which is the one place
    the ordering rule is executed rather than described.
    """
    attempt = require_safe_component(attempt_id, field="attempt_id")
    question = require_int(question_id, "question_id")
    if not retain:
        return None
    if not isinstance(written_at_utc, datetime) or written_at_utc.tzinfo is None:
        raise ArtifactError("written_at_utc must be a timezone-aware datetime")

    request = getattr(generation, "request", None)
    if type(request) is not str:
        raise ArtifactError("generation.request must be a string")
    replies = getattr(generation, "raw_responses", None)
    if isinstance(replies, (str, bytes)) or not isinstance(replies, Sequence):
        # A bare str satisfies Sequence and would be persisted one character per reply --
        # the caller mistake M1-303's round-4 preflight closed for `queries`, where it cost
        # billable calls.
        raise ArtifactError("generation.raw_responses must be a sequence of strings")
    bodies = list(replies)
    if any(type(body) is not str for body in bodies):
        raise ArtifactError("generation.raw_responses must be a sequence of strings")
    invocations = getattr(generation, "invocations", None)
    if type(invocations) is not int or not 0 <= invocations <= MAX_MODEL_INVOCATIONS:
        raise ArtifactError(
            f"generation.invocations must be an int between 0 and {MAX_MODEL_INVOCATIONS}"
        )
    repair_attempted = getattr(generation, "repair_attempted", None)
    if type(repair_attempted) is not bool:
        raise ArtifactError("generation.repair_attempted must be a bool")
    failure_code = getattr(generation, "failure_code", None)
    # `type() is not str` before the membership test, not for tidiness: `x not in
    # frozenset` calls `hash(x)`, so an unhashable value raised a raw `TypeError` out of a
    # public boundary. Found by the property suite, and the same shape in the reader below.
    if failure_code is not None and (
        type(failure_code) is not str or failure_code not in _FAILURE_CODES
    ):
        raise ArtifactError("generation.failure_code is not a known pre-forecast failure code")
    problems = getattr(generation, "failure_problems", None)
    if isinstance(problems, (str, bytes)) or not isinstance(problems, Sequence):
        raise ArtifactError("generation.failure_problems must be a sequence of strings")
    problem_list = list(problems)
    if any(type(problem) is not str for problem in problem_list):
        raise ArtifactError("generation.failure_problems must be a sequence of strings")

    request = redact_secrets(request, secret_env_var_names)
    bodies = [redact_secrets(body, secret_env_var_names) for body in bodies]

    envelope = {
        "artifact_schema_version": MODEL_OUTPUT_SCHEMA_VERSION,
        "attempt_id": attempt,
        "question_id": question,
        "model_settings": _settings_payload(getattr(generation, "settings", None)),
        "request": request,
        "raw_responses": bodies,
        "invocations": invocations,
        "repair_attempted": repair_attempted,
        "cost_usd": _writer_cost(getattr(generation, "cost_usd", None)),
        "failure_code": failure_code,
        "failure_problems": problem_list,
        "written_at_utc": written_at_utc.astimezone(timezone.utc).isoformat(),
    }
    try:
        # `ensure_ascii=True` for M1-305's reason, and it is load-bearing here rather than
        # tidy: a lone surrogate reaches provider text, `model_dump_json()`/`str.encode()`
        # would raise on one, and escaping it is what lets the same bytes come back.
        # `allow_nan=False` for `research/artifacts.py`'s reason: NaN/Infinity are not JSON,
        # and `json.loads` *accepts* them, so a body carrying one would be written as
        # something no other tool can read back.
        payload = json.dumps(envelope, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        # from None and a constant message: json.dumps names the offending value.
        raise ArtifactError(
            "raw model output could not be rendered as JSON "
            "(detail withheld: a model reply is untrusted content)"
        ) from None

    relative = artifact_relative_path(question_id=question, attempt_id=attempt)
    write_new_file(artifact_root / relative, payload, what=_WHAT)
    return relative


def _reject_json_constant(token: str) -> object:
    """Refuse ``NaN``/``Infinity``/``-Infinity`` while parsing an artifact.

    ``json.loads`` accepts all three by default, so a reader without this accepts envelopes
    the writer refuses to produce. M1-306 round 1, finding 7: a reader that admits more than
    its writer can emit is not reading the format it documents.
    """
    raise ArtifactError(
        "raw model output artifact contains a non-finite JSON constant, "
        "which this format does not permit"
    )


def _text(envelope: dict[str, Any], key: str, path: Path) -> str:
    value = envelope.get(key)
    if type(value) is not str or not value.strip():
        raise ArtifactError(f"raw model output artifact {key} is missing or malformed: {path}")
    return value


def _settings_from(envelope: dict[str, Any], path: Path) -> ModelSettings:
    """Rebuild ``ModelSettings`` from the envelope, applying the writer's own rules.

    Every field is required. The first cut of the retrieval reader checked the version and
    the bodies and returned, which made an envelope carrying no provenance a valid artifact
    (M1-306 round 1, finding 7); the settings are this envelope's provenance -- they are
    what says which model, at which temperature, under which prompt, produced the text
    below them.
    """
    payload = envelope.get("model_settings")
    if not isinstance(payload, dict):
        raise ArtifactError(
            f"raw model output artifact model_settings is missing or malformed: {path}"
        )
    expected = set(_SETTINGS_TEXT_FIELDS) | set(_SETTINGS_INT_FIELDS) | set(_SETTINGS_FLOAT_FIELDS)
    if set(payload) != expected:
        # Set equality, not a subset check: an extra key is as much a shape this writer
        # cannot emit as a missing one, and reading one back as valid would hide a version
        # skew the schema version exists to make visible (M1-501's lesson about one-sided
        # assertions).
        raise ArtifactError(
            f"raw model output artifact model_settings does not carry exactly the "
            f"recorded settings fields: {path}"
        )
    for name in _SETTINGS_TEXT_FIELDS:
        _text(payload, name, path)
    for name in _SETTINGS_INT_FIELDS:
        if type(payload[name]) is not int:
            raise ArtifactError(f"raw model output artifact model_settings {name} is not an int")
    for name in _SETTINGS_FLOAT_FIELDS:
        # Strict for `_reader_number`'s reason: `_settings_payload` normalizes an int to a
        # float before the envelope is rendered, so a bare int here is a shape the writer
        # cannot emit.
        _reader_number(payload[name], f"raw model output artifact model_settings {name}")
    return ModelSettings(
        provider=payload["provider"],
        name=payload["name"],
        temperature=payload["temperature"],
        max_output_tokens=payload["max_output_tokens"],
        timeout_seconds=payload["timeout_seconds"],
        allowed_tries=payload["allowed_tries"],
        prompt_version=payload["prompt_version"],
        prompt_sha256=payload["prompt_sha256"],
    )


def read_raw_model_output(artifact_root: Path, relative_path: str) -> StoredModelOutput:
    """Read back one attempt's raw model output.

    Unlike ``research/artifacts.py``'s reader this one *is* on a replay path -- see this
    module's header on why the two artifact kinds differ there. A missing or corrupt
    artifact therefore costs a replay as well as an audit trail, and both are refusals
    rather than best-effort returns: a replay that silently skipped a damaged reply would
    report a hash match for a comparison it never made.

    Every envelope field is validated, not just the schema version.
    """
    if type(relative_path) is not str or not relative_path:
        raise ArtifactError("relative_path must be a non-empty string")
    path = artifact_root / relative_path
    try:
        raw = path.read_bytes()
    except OSError:
        raise ArtifactError(f"cannot read raw model output artifact {path}") from None
    except ValueError:
        # `UnicodeEncodeError` (a ValueError) rather than an OSError: a lone surrogate in
        # the path cannot be encoded for the syscall, and `open()` raises before any I/O
        # happens. Found by `tests/property/test_model_artifact_properties.py`, which is
        # the whole reason the property exists -- an `except OSError` reads as exhaustive
        # and is not.
        #
        # Reachable without a hostile operator: `raw_output_path` is a TEXT column, `008`
        # constrains its shape and not its encoding, and this is a public entry point that
        # takes a string from a caller. The message must not render the path here, since
        # interpolating it is itself the failing operation.
        raise ArtifactError(
            "the recorded artifact path is not a usable filename "
            "(path withheld: it cannot be rendered)"
        ) from None
    try:
        envelope = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except ArtifactError:
        raise
    except (UnicodeDecodeError, ValueError):
        # from None: a JSONDecodeError quotes the surrounding document text.
        raise ArtifactError(f"raw model output artifact is not valid JSON: {path}") from None
    if not isinstance(envelope, dict):
        raise ArtifactError(f"raw model output artifact is not a JSON object: {path}")
    version = envelope.get("artifact_schema_version")
    if version != MODEL_OUTPUT_SCHEMA_VERSION:
        # The expected version is this module's own literal; the found one is file content
        # and is withheld, exactly as load_snapshot does.
        raise ArtifactError(
            f"raw model output artifact schema version is not {MODEL_OUTPUT_SCHEMA_VERSION} "
            f"(found value withheld): {path}"
        )
    if set(envelope) != _ENVELOPE_FIELDS:
        # After the version check, so a genuine version skew still reports as one. Set
        # equality rather than a subset: within a declared schema version, a missing field
        # and an extra field are equally shapes this writer cannot emit, and accepting
        # either would hide the skew the version exists to make visible. The key names are
        # this module's own constant; the found ones are file content and are withheld.
        raise ArtifactError(
            f"raw model output artifact does not carry exactly the recorded envelope fields: {path}"
        )
    attempt_id = require_safe_component(_text(envelope, "attempt_id", path), field="attempt_id")
    if type(envelope.get("question_id")) is not int:
        raise ArtifactError(
            f"raw model output artifact question_id is missing or malformed: {path}"
        )
    written_at = _text(envelope, "written_at_utc", path)
    try:
        parsed = datetime.fromisoformat(written_at)
    except ValueError:
        # from None: fromisoformat quotes the offending string.
        raise ArtifactError(
            f"raw model output artifact written_at_utc is not an ISO-8601 timestamp: {path}"
        ) from None
    if parsed.tzinfo is None:
        raise ArtifactError(f"raw model output artifact written_at_utc has no offset: {path}")
    if parsed.astimezone(timezone.utc).isoformat() != written_at:
        # The writer emits `written_at_utc.astimezone(timezone.utc).isoformat()` and nothing
        # else, so any other rendering of the same instant -- a `+02:00` offset, a trailing
        # `Z`, a `.000000` the writer would have omitted -- is a shape it cannot produce.
        # Checking the offset alone would accept the first of those. The value is file
        # content and is withheld; only the rule is named.
        raise ArtifactError(
            f"raw model output artifact written_at_utc is not the canonical UTC "
            f"form the writer emits: {path}"
        )

    # `request` is the one text field the writer stores without a non-blank rule: it is
    # `render_model_input`'s output, and refusing an empty one here would be a rule the
    # writer does not apply. `_text` is not used for that reason.
    request = envelope.get("request")
    if type(request) is not str:
        raise ArtifactError(f"raw model output artifact request is missing or malformed: {path}")
    bodies = envelope.get("raw_responses")
    if not isinstance(bodies, list) or any(type(body) is not str for body in bodies):
        raise ArtifactError(f"raw model output artifact raw_responses is malformed: {path}")
    invocations = envelope.get("invocations")
    if type(invocations) is not int or not 0 <= invocations <= MAX_MODEL_INVOCATIONS:
        raise ArtifactError(f"raw model output artifact invocations is malformed: {path}")
    repair_attempted = envelope.get("repair_attempted")
    if type(repair_attempted) is not bool:
        raise ArtifactError(f"raw model output artifact repair_attempted is malformed: {path}")
    failure_code = envelope.get("failure_code")
    if failure_code is not None and (
        type(failure_code) is not str or failure_code not in _FAILURE_CODES
    ):
        raise ArtifactError(f"raw model output artifact failure_code is malformed: {path}")
    problems = envelope.get("failure_problems")
    if not isinstance(problems, list) or any(type(problem) is not str for problem in problems):
        raise ArtifactError(f"raw model output artifact failure_problems is malformed: {path}")
    raw_cost = envelope.get("cost_usd")
    if raw_cost is None:
        cost_usd: float | None = None
    else:
        try:
            cost_usd = _reader_number(raw_cost, "cost_usd")
            if cost_usd < 0.0:
                raise ArtifactError("cost_usd must not be negative")
        except ArtifactError:
            raise ArtifactError(
                f"raw model output artifact cost_usd is malformed: {path}"
            ) from None

    return StoredModelOutput(
        attempt_id=attempt_id,
        question_id=envelope["question_id"],
        settings=_settings_from(envelope, path),
        request=request,
        raw_responses=tuple(bodies),
        invocations=invocations,
        repair_attempted=repair_attempted,
        cost_usd=cost_usd,
        failure_code=failure_code,
        failure_problems=tuple(problems),
        written_at_utc=parsed,
    )
