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

from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import MetaculusQuestion

SNAPSHOT_SCHEMA_VERSION = "1.0.0"


class SnapshotError(Exception):
    """A snapshot file is unreadable, malformed, or from an unknown schema."""


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
        "questions": [
            {"question_class": type(q).__name__, "data": q.to_json()} for q in questions
        ],
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
            f"snapshot {path} has schema version {version!r}; "
            f"this build reads {SNAPSHOT_SCHEMA_VERSION!r}"
        )

    registry = _question_class_registry()
    questions: list[MetaculusQuestion] = []
    for i, entry in enumerate(envelope.get("questions", [])):
        class_name = entry.get("question_class")
        question_cls = registry.get(class_name)
        if question_cls is None:
            raise SnapshotError(
                f"snapshot {path} entry {i} has unknown question_class {class_name!r}"
            )
        questions.append(question_cls.from_json(entry["data"]))

    declared = envelope.get("question_count")
    if declared != len(questions):
        raise SnapshotError(
            f"snapshot {path} declares {declared} questions but contains {len(questions)}"
        )

    meta = SnapshotMeta(
        tournament_id=envelope["tournament_id"],
        group_question_mode=envelope["group_question_mode"],
        fetched_at_utc=datetime.fromisoformat(envelope["fetched_at_utc"]),
        source=envelope["source"],
        question_count=len(questions),
    )
    return meta, questions
