"""Question snapshot persistence (M0-103).

A snapshot is a versioned JSON envelope holding typed question records
exactly as the pinned forecasting-tools models serialize them (their
``to_json()`` includes the raw ``api_json`` payload, so nothing from the API
response is lost). Snapshots are the replay substrate (D12): fixture-mode
fetches read them instead of the network, and tests round-trip them offline.

Snapshots may contain whatever the Metaculus API returned — including
community-prediction aggregates when present. Exclusion of the community
prediction is enforced where the forecaster input packet is assembled (M1),
not by lossy storage here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typing import get_args

from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import MetaculusQuestion

from whiskeyjack_bot.config import GroupQuestionMode

SNAPSHOT_SCHEMA_VERSION = "1.0.0"

_VALID_SOURCES = ("live", "fixture")


class SnapshotError(Exception):
    """A snapshot file is unreadable, malformed, or from an unknown schema.

    Same hygiene rule as ``ConfigError``: the message never echoes values read
    from the snapshot file (a mistakenly pasted credential must not surface
    through CLI output), and sanitizing raise sites use ``from None`` so the
    cause chain cannot reprint the raw value through a traceback.
    """


@dataclass(frozen=True)
class SnapshotMeta:
    tournament_id: int | str
    group_question_mode: str
    fetched_at_utc: datetime
    source: str  # "live" or "fixture"
    question_count: int


def _question_class_registry() -> dict[str, type[MetaculusQuestion]]:
    return {cls.__name__: cls for cls in DataOrganizer.get_all_question_types()}


def save_snapshot(
    path: Path,
    questions: list[MetaculusQuestion],
    *,
    tournament_id: int | str,
    group_question_mode: str,
    source: str,
    fetched_at_utc: datetime | None = None,
) -> SnapshotMeta:
    """Write a snapshot envelope; returns the metadata that was written."""
    meta = SnapshotMeta(
        tournament_id=tournament_id,
        group_question_mode=group_question_mode,
        fetched_at_utc=fetched_at_utc or datetime.now(tz=timezone.utc),
        source=source,
        question_count=len(questions),
    )
    envelope = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "tournament_id": meta.tournament_id,
        "group_question_mode": meta.group_question_mode,
        "fetched_at_utc": meta.fetched_at_utc.isoformat(),
        "source": meta.source,
        "question_count": meta.question_count,
        "questions": [{"question_class": type(q).__name__, "data": q.to_json()} for q in questions],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return meta


def load_snapshot(path: Path) -> tuple[SnapshotMeta, list[MetaculusQuestion]]:
    """Read a snapshot envelope back into typed question objects.

    Purely local file I/O: no network access on any path through here.
    """
    try:
        envelope: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SnapshotError(f"cannot read snapshot {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot {path} is not valid JSON: {exc}") from exc

    if not isinstance(envelope, dict):
        raise SnapshotError(f"snapshot {path} must contain a JSON object")
    version = envelope.get("snapshot_schema_version")
    if version != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotError(
            f"snapshot {path} has an unsupported schema version "
            f"(value withheld; this build reads {SNAPSHOT_SCHEMA_VERSION!r})"
        )

    entries = envelope.get("questions")
    if not isinstance(entries, list):
        raise SnapshotError(f"snapshot {path} must hold its questions in a list")

    registry = _question_class_registry()
    questions: list[MetaculusQuestion] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SnapshotError(f"snapshot {path} entry {i} must be a JSON object")
        class_name = entry.get("question_class")
        if not isinstance(class_name, str):
            raise SnapshotError(f"snapshot {path} entry {i} is missing question_class")
        question_cls = registry.get(class_name)
        if question_cls is None:
            raise SnapshotError(
                f"snapshot {path} entry {i} has an unrecognized question_class "
                "(name withheld; it is not in the pinned model registry)"
            )
        if "data" not in entry:
            raise SnapshotError(f"snapshot {path} entry {i} is missing its data payload")
        try:
            questions.append(question_cls.from_json(entry["data"]))
        except Exception:  # noqa: BLE001 - pinned models raise several shapes
            # The validation error interpolates payload values, so neither its
            # text nor its cause chain may reach the SnapshotError the CLI
            # prints. class_name is safe: it matched the registry above.
            raise SnapshotError(
                f"snapshot {path} entry {i} does not deserialize as {class_name} "
                "(validation detail withheld: it can echo snapshot contents)"
            ) from None

    declared = envelope.get("question_count")
    if not isinstance(declared, int) or isinstance(declared, bool):
        raise SnapshotError(f"snapshot {path} question_count must be an integer")
    if declared != len(questions):
        raise SnapshotError(
            f"snapshot {path} declares {declared} questions but contains {len(questions)}"
        )

    missing = [
        key
        for key in ("tournament_id", "group_question_mode", "fetched_at_utc", "source")
        if key not in envelope
    ]
    if missing:
        raise SnapshotError(f"snapshot {path} is missing metadata: {', '.join(missing)}")

    # Metadata carries replay provenance; presence alone is not enough
    # (re-review finding 2). Invalid values are described but never echoed.
    tournament_id = envelope["tournament_id"]
    if isinstance(tournament_id, bool) or not isinstance(tournament_id, int | str):
        raise SnapshotError(
            f"snapshot {path} tournament_id must be an integer or a non-empty string"
        )
    if tournament_id == "":
        raise SnapshotError(f"snapshot {path} tournament_id must not be an empty string")

    group_question_mode = envelope["group_question_mode"]
    valid_modes = get_args(GroupQuestionMode)
    if group_question_mode not in valid_modes:
        raise SnapshotError(
            f"snapshot {path} group_question_mode must be one of {sorted(valid_modes)}"
        )

    source = envelope["source"]
    if source not in _VALID_SOURCES:
        raise SnapshotError(f"snapshot {path} source must be one of {sorted(_VALID_SOURCES)}")

    try:
        fetched_at = datetime.fromisoformat(envelope["fetched_at_utc"])
    except (TypeError, ValueError):
        # from None: fromisoformat's ValueError echoes the raw input string.
        raise SnapshotError(
            f"snapshot {path} has an invalid fetched_at_utc timestamp "
            "(value withheld: it can echo snapshot contents)"
        ) from None
    if fetched_at.utcoffset() is None:
        raise SnapshotError(
            f"snapshot {path} fetched_at_utc must be timezone-aware; "
            "naive timestamps are not valid provenance"
        )

    meta = SnapshotMeta(
        tournament_id=tournament_id,
        group_question_mode=group_question_mode,
        fetched_at_utc=fetched_at.astimezone(timezone.utc),
        source=source,
        question_count=len(questions),
    )
    return meta, questions
