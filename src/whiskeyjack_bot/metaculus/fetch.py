"""Question fetching (M0-102) and tournament targeting (M0-104).

Two modes:

- **fixture** (default): load a saved snapshot; zero network access.
- **live**: the first — and in Milestone 0 the only — permitted network path
  in the codebase. Requires METACULUS_TOKEN; fails on credentials before any
  connection is attempted.

Tournament identity follows D31: the ID lives in config (or an explicit CLI
override for the bot-testing-area smoke path, M0-104); the SDK's rotating
``CURRENT_MINIBENCH_ID`` alias is checked at runtime and any mismatch is
logged loudly but never silently adopted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forecasting_tools.data_models.questions import MetaculusQuestion
from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.metaculus.client import build_client
from whiskeyjack_bot.metaculus.snapshots import SnapshotMeta, load_snapshot

logger = logging.getLogger(__name__)

TournamentOrigin = Literal["cli_override", "config", "sdk_current"]


@dataclass(frozen=True)
class ResolvedTournament:
    id: int | str
    origin: TournamentOrigin


def resolve_tournament_id(
    config: AppConfig, override: int | str | None = None
) -> ResolvedTournament:
    """Decide which tournament ID a fetch targets, per D31.

    Precedence: explicit override (smoke tests, M0-104) > SDK current alias
    when ``use_sdk_current_id`` is set > configured ID. A configured ID that
    differs from the SDK's alias is used as configured, with a warning — the
    SDK constant rotates and must never override deliberate config silently.
    """
    sdk_current: str = MetaculusClient.CURRENT_MINIBENCH_ID
    if override is not None:
        logger.info("tournament override active: targeting %r", override)
        return ResolvedTournament(id=override, origin="cli_override")
    configured = config.metaculus.tournament.id
    if config.metaculus.tournament.use_sdk_current_id:
        if sdk_current != configured:
            logger.warning(
                "use_sdk_current_id is set: SDK CURRENT_MINIBENCH_ID %r "
                "differs from configured %r; using the SDK value",
                sdk_current,
                configured,
            )
        return ResolvedTournament(id=sdk_current, origin="sdk_current")
    if configured != sdk_current:
        logger.warning(
            "configured tournament id %r differs from SDK CURRENT_MINIBENCH_ID %r; "
            "using the configured value (set use_sdk_current_id to adopt the SDK alias)",
            configured,
            sdk_current,
        )
    return ResolvedTournament(id=configured, origin="config")


def fetch_open_questions_live(
    config: AppConfig, override: int | str | None = None
) -> tuple[ResolvedTournament, list[MetaculusQuestion]]:
    """Fetch open questions from Metaculus (network; requires token).

    Group questions are handled per config (default ``unpack_subquestions``,
    the mode verified on the pinned SDK).
    """
    resolved = resolve_tournament_id(config, override)
    client = build_client(config)  # raises MissingCredentialError before any network use
    questions = client.get_all_open_questions_from_tournament(
        resolved.id, group_question_mode=config.metaculus.group_question_mode
    )
    logger.info("fetched %d open questions from tournament %r", len(questions), resolved.id)
    return resolved, questions


def fetch_open_questions_fixture(
    snapshot_path: Path,
) -> tuple[SnapshotMeta, list[MetaculusQuestion]]:
    """Load questions from a saved snapshot; zero network access."""
    meta, questions = load_snapshot(snapshot_path)
    logger.info(
        "loaded %d questions from snapshot %s (tournament %r, fetched %s)",
        len(questions),
        snapshot_path,
        meta.tournament_id,
        meta.fetched_at_utc.isoformat(),
    )
    return meta, questions
