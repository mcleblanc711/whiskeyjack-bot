"""Raw provider responses on disk, as retrieval artifacts (M1-306).

Both retrieval adapters accumulate the provider bodies they normalized and hold them
**in memory only** -- ``AskNewsRetrieval.raw_responses`` and ``ExaRetrieval.raw_responses``
-- because persisting them, and the file layout and replay contract that implies, was
deferred to this item in both adapters' docstrings. This module is that layout.

What these files are *for* is worth stating precisely, because it is not what it
looks like. They are **not the replay substrate**: :func:`whiskeyjack_bot.research.store.replay_research`
reconstructs a packet from the ledger's normalized rows and never re-parses a body
here. Re-normalizing from raw would make a replayed packet depend on *adapter code
version*, so fixing a bug in a ``_to_document`` would silently re-derive every
historical forecast's evidence -- the ledger rewriting its own history on a refactor.
The rows are the record; these files are the evidence that those rows were derived
from something a provider really returned, and the material any later re-analysis
would work from.

The envelope is versioned and shaped after ``metaculus/snapshots.py``, the project's
existing on-disk replay substrate, rather than inventing a second convention.

Three rules the file layout enforces:

- **Never overwrite.** An artifact is the record that a paid run happened; replacing
  one destroys evidence. Creation is atomic against a concurrent writer (write a temp
  file, then ``os.link`` it into place, which fails rather than clobbers), so a crash
  leaves at most a stray temp file and never a half-written artifact.
- **The stored path is relative to the artifact root**, so a ledger stays readable
  after the artifact directory moves, or is opened on another machine. It is also why
  the path is excluded from the packet hash (see :mod:`whiskeyjack_bot.research.packet`).
- **Retention is the caller's flag** (``retrieval.retain_raw_responses`` /
  ``storage.retain_raw_research``), passed explicitly. This module reads no config and
  does not guess; retention off means no file written and no path recorded.

Secret hygiene: an envelope holds provider **response bodies only**. No request
headers and no request URL -- both carry the API key, which is also why both adapters
discard provider exceptions unread. :class:`ArtifactError` never echoes a body.
Filesystem paths *are* rendered, uniformly with ``config.py``/``ledger.py``/``snapshots.py``
under the settled M1-401 carve-out: a path is operator-supplied configuration, and an
unreadable-artifact error with no path cannot be acted on.

Purely local file I/O: no network access on any path through here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from whiskeyjack_bot.artifacts import (
    ArtifactError,
    require_int,
    require_safe_component,
    write_new_file,
)

ARTIFACT_SCHEMA_VERSION = "1.0.0"

# The subdirectory of storage.artifact_root that retrieval artifacts live under, so
# other artifact kinds (raw model output, M1-406) get their own namespace.
_RESEARCH_SUBDIR = "research"

# What this module's messages call its artifacts, passed to the shared writer so its
# "never overwritten" message names the kind. A literal, never content.
_WHAT = "retrieval artifact"

# `ArtifactError`, the safe-path-component rule, the int guard and the atomic
# never-overwrite writer moved to `whiskeyjack_bot.artifacts` when M1-406 added a second
# artifact kind. `ArtifactError` is re-exported here rather than renamed: it is this
# module's public error type and every caller imports it from here.
__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactError",
    "artifact_relative_path",
    "read_raw_responses",
    "write_raw_responses",
]


def _require_safe_run_id(retrieval_run_id: object) -> str:
    return require_safe_component(retrieval_run_id, field="retrieval_run_id")


def _require_int(value: object, field: str) -> int:
    return require_int(value, field)


def artifact_relative_path(*, question_id: int, retrieval_run_id: str) -> str:
    """The path an artifact is stored at, relative to ``storage.artifact_root``.

    Exposed so a reader can resolve a stored path without restating the layout, and
    so the layout has exactly one definition.
    """
    run_id = _require_safe_run_id(retrieval_run_id)
    question = _require_int(question_id, "question_id")
    return f"{_RESEARCH_SUBDIR}/{question}/{run_id}.json"


def write_raw_responses(
    artifact_root: Path,
    *,
    retrieval_run_id: str,
    question_id: int,
    provider: str,
    raw_responses: Sequence[dict[str, Any]],
    written_at_utc: datetime,
    retain: bool,
) -> str | None:
    """Write one run's raw provider bodies; return the path to record, or ``None``.

    Returns the **relative** path for ``research_runs.raw_response_path``. Returns
    ``None`` -- writing nothing -- when ``retain`` is false, which is the configured
    default's meaning and not a failure.

    Raises :class:`ArtifactError` rather than swallowing a write failure. Callers on
    the paid path must not let that lose the run: write the artifact first, and if it
    fails, persist the ledger row anyway with ``raw_response_path=None``. A run that
    cost money and produced no artifact is still a run that must be recorded.

    **That composition is** :func:`whiskeyjack_bot.research.persist.persist_paid_run`
    (M1-312), which catches the ``ArtifactError`` this function raises, commits the run
    and its documents regardless, and reports the audit loss to its caller. Nothing in
    this module or in the store performs it: both are primitives, and each takes or
    returns the path without an opinion about how the other went. An earlier version of
    this docstring claimed ``store.persist_retrieval`` did it, which was simply false
    (M1-306 round 1, non-blocking observation), and the correction stands -- it takes an
    already-computed path. Use this function directly only when you are *not* on the paid
    path; on it, use the composition, which is the one place the ordering rule is executed
    rather than described.
    """
    run_id = _require_safe_run_id(retrieval_run_id)
    question = _require_int(question_id, "question_id")
    if type(provider) is not str or not provider.strip():
        raise ArtifactError("provider must be a non-blank string")
    if not retain:
        return None
    if isinstance(raw_responses, (str, bytes)) or not isinstance(raw_responses, Sequence):
        # A bare str satisfies Sequence and would be persisted one character per
        # "body". The same class of caller mistake M1-303's round-4 preflight closed
        # for `queries`, where it cost billable calls.
        raise ArtifactError("raw_responses must be a sequence of JSON objects")
    bodies = list(raw_responses)
    if any(not isinstance(body, dict) for body in bodies):
        raise ArtifactError("raw_responses must be a sequence of JSON objects")
    if not isinstance(written_at_utc, datetime) or written_at_utc.tzinfo is None:
        raise ArtifactError("written_at_utc must be a timezone-aware datetime")

    envelope = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "retrieval_run_id": run_id,
        "question_id": question,
        "provider": provider,
        "written_at_utc": written_at_utc.astimezone(timezone.utc).isoformat(),
        "raw_responses": bodies,
    }
    try:
        # allow_nan=False for the reason model.py rejects non-finite provider config:
        # `NaN`/`Infinity` are not JSON, so a body carrying one would be written as
        # something no other tool can read back. json.loads *accepts* them, so this
        # is reachable from a provider body, not hypothetical.
        payload = json.dumps(envelope, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        # from None and a constant message: json.dumps names the offending value.
        raise ArtifactError(
            "raw responses could not be rendered as JSON "
            "(detail withheld: a provider body is untrusted content)"
        ) from None

    relative = artifact_relative_path(question_id=question, retrieval_run_id=run_id)
    destination = artifact_root / relative
    write_new_file(destination, payload, what=_WHAT)
    return relative


def _reject_json_constant(token: str) -> object:
    """Refuse ``NaN``/``Infinity``/``-Infinity`` while parsing an artifact.

    ``json.loads`` accepts all three by default, so the reader was accepting bodies
    the writer refuses to produce -- an envelope holding ``NaN`` came back as a
    Python float and would have flowed on as though it had round-tripped (round 1,
    finding 7). A reader that admits more than its writer can emit is not reading
    the format it documents.
    """
    raise ArtifactError(
        "retrieval artifact contains a non-finite JSON constant, which this format does not permit"
    )


def _require_envelope_text(envelope: dict[str, Any], key: str, path: Path) -> str:
    """Require a non-blank string, matching the rule the writer applies.

    ``not value.strip()`` rather than ``not value``: the writer refuses a
    whitespace-only ``provider``, and a reader that accepts one accepts an envelope
    the writer cannot emit -- an effectively unattributed artifact reported as valid
    audit evidence (review round 2, finding 1). The reader's job is to admit exactly
    what the writer can produce, which means sharing the rule rather than
    approximating it.
    """
    value = envelope.get(key)
    if type(value) is not str or not value.strip():
        raise ArtifactError(f"retrieval artifact {key} is missing or malformed: {path}")
    return value


def read_raw_responses(artifact_root: Path, relative_path: str) -> tuple[dict[str, Any], ...]:
    """Read back one run's raw provider bodies, for audit.

    Not on the replay path -- replay reads the ledger. A missing or corrupt artifact
    therefore costs an audit trail, not a replay, which is stated as a standing risk
    in ``docs/M1-NOTES.md`` rather than left to be discovered.

    Every required envelope field is validated, not just the schema version. The
    first cut checked the version and the bodies and returned; an envelope carrying
    neither run id, question, provider nor timestamp was accepted as a valid
    artifact, which makes it an unattributable blob rather than a retrieval record
    (round 1, finding 7).
    """
    if type(relative_path) is not str or not relative_path:
        raise ArtifactError("relative_path must be a non-empty string")
    path = artifact_root / relative_path
    try:
        raw = path.read_bytes()
    except OSError:
        raise ArtifactError(f"cannot read retrieval artifact {path}") from None
    except ValueError:
        # `UnicodeEncodeError` (a ValueError) rather than an OSError: a lone surrogate in
        # the path cannot be encoded for the syscall, and `open()` raises before any I/O
        # happens. An `except OSError` here reads as exhaustive and is not (M1-314; the
        # analogous fix in `forecast/artifacts.py::read_raw_model_output`).
        #
        # Reachable without a hostile operator: `raw_response_path` is a TEXT column
        # whose shape nothing constrains, and this is a public entry point that takes a
        # string from a caller. The message must not render the path here, since
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
        raise ArtifactError(f"retrieval artifact is not valid JSON: {path}") from None
    if not isinstance(envelope, dict):
        raise ArtifactError(f"retrieval artifact is not a JSON object: {path}")
    version = envelope.get("artifact_schema_version")
    if version != ARTIFACT_SCHEMA_VERSION:
        # The expected version is this module's own literal; the found one is file
        # content and is withheld, exactly as load_snapshot does.
        raise ArtifactError(
            f"retrieval artifact schema version is not {ARTIFACT_SCHEMA_VERSION} "
            f"(found value withheld): {path}"
        )
    # Provenance, not decoration: these are what say which run and which question the
    # bodies below belong to. An artifact that cannot answer that is not evidence.
    _require_safe_run_id(_require_envelope_text(envelope, "retrieval_run_id", path))
    _require_envelope_text(envelope, "provider", path)
    written_at = _require_envelope_text(envelope, "written_at_utc", path)
    try:
        parsed = datetime.fromisoformat(written_at)
    except ValueError:
        # from None: fromisoformat quotes the offending string.
        raise ArtifactError(
            f"retrieval artifact written_at_utc is not an ISO-8601 timestamp: {path}"
        ) from None
    if parsed.tzinfo is None:
        raise ArtifactError(f"retrieval artifact written_at_utc has no offset: {path}")
    if type(envelope.get("question_id")) is not int:
        raise ArtifactError(f"retrieval artifact question_id is missing or malformed: {path}")
    bodies = envelope.get("raw_responses")
    if not isinstance(bodies, list) or any(not isinstance(body, dict) for body in bodies):
        raise ArtifactError(f"retrieval artifact raw_responses is malformed: {path}")
    return tuple(bodies)
