"""The mocked Metaculus platform shared by the T-902 integration tier.

**Named ``fake_platform`` and not ``platform``, which is not cosmetic.** ``tests/`` carries
no ``__init__.py``, so pytest's default ``prepend`` import mode puts this directory on
``sys.path`` -- and a module here called ``platform`` shadows the standard library's for the
whole process. ``uuid`` imports ``platform`` at its own import time, so the collision
surfaces as a circular-import ``AttributeError`` from inside ``pytest`` itself, nowhere near
this file. Found by execution while writing this module.

Two doubles at two different depths, and the split is deliberate.

**The read path is faked at our own seam.** ``FakeTournament`` replaces
``whiskeyjack_bot.metaculus.fetch.build_client``, so the composition under test is
``resolve_tournament_id`` -> ``build_client`` -> ``get_all_open_questions_from_tournament``
-> ``normalize_questions``. Going lower would mean stubbing the SDK's async pagination
(``get_all_open_questions_from_tournament`` runs ``asyncio.run(get_questions_matching_filter(...))``),
which tests the dependency rather than this project. What matters is preserved: the
questions handed back are **real SDK objects** parsed from the committed API-post fixtures
by ``DataOrganizer``, so normalization runs against the real types -- including the
``DiscreteQuestion``/``NumericQuestion`` inheritance CLAUDE.md's gotcha is about -- and not
against a ``SimpleNamespace`` shaped to match what normalization happens to read.

**The write path is faked at the transport.** ``CountingTransport`` replaces
``requests.post``/``requests.get``, so a real ``MetaculusClient`` and a real
``SingleAttemptPoster`` sit between the gateway and the counter. That is the only way to
make "exactly one POST" a *measurement* rather than a property of a hand-written
four-method double: ``tests/unit/test_submission_live.py`` counts calls into its own
``FakePoster``, which cannot see the SDK's blind retry at all because the retry lives
below it. Counting at ``requests`` is a witness outside the code under test.

``api_response`` is the T-902 generalization of ``tests/unit/test_metaculus_poster._response``,
which hardcodes a reason for each of its two statuses. Named here rather than imported so a
5xx gets an honest reason line; the two are not a silent duplicate of one another.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
import requests
from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import MetaculusQuestion
from forecasting_tools.helpers.metaculus_client import MetaculusClient

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
API_POSTS = FIXTURES / "api_posts"
GROUP_POST = API_POSTS / "group" / "minibench_group.json"

# The committed fixtures' own identifiers. Named rather than repeated so a test that means
# "the binary one" says so.
BINARY_QUESTION_ID = 91001
NUMERIC_QUESTION_ID = 91002
MULTIPLE_CHOICE_QUESTION_ID = 91003
BINARY_POST_ID = 90001
GROUP_POST_ID = 90004

TOURNAMENT = "minibench"

# Shared instants. They live here rather than in ``conftest.py`` because pytest registers
# every conftest in ``sys.modules`` under the bare name ``conftest`` -- the collision
# ``tests/unit/test_conftest_temproot.py`` documents -- so a test module importing
# ``from conftest import ...`` is importing whichever one got there first. This module's
# name is unique, so importing from it is unambiguous.
OCCURRED = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
RESEARCH_TIMESTAMP = "2026-08-22T00:00:00.000000+00:00"
RUN_ID = "run-1"


# ── raw fixtures ─────────────────────────────────────────────────────────────


def raw_post(name: str) -> dict[str, Any]:
    """One committed API post, as the platform would send it."""
    return json.loads((API_POSTS / f"{name}_post.json").read_text(encoding="utf-8"))


def raw_group_post() -> dict[str, Any]:
    return json.loads(GROUP_POST.read_text(encoding="utf-8"))


def flat_questions() -> list[MetaculusQuestion]:
    """The three v1 types, parsed by the SDK exactly as a live fetch would parse them."""
    return [
        DataOrganizer.get_question_from_post_json(raw_post(name))
        for name in ("binary", "numeric", "multiple_choice")
    ]


def unpacked_group_questions() -> list[MetaculusQuestion]:
    """The group post expanded the way a live fetch with ``unpack_subquestions`` expands it.

    Uses the **SDK's** expansion rather than this project's ``questions.groups`` mirror. A
    fetch-path test that expanded the post with our own code would be asserting that our
    unpacking agrees with itself; ``tests/unit/test_groups.py::test_our_unpacking_matches_
    the_pinned_sdk`` is where the two are held together.
    """
    return list(MetaculusClient._unpack_group_question(raw_group_post()))


# ── the read path ────────────────────────────────────────────────────────────


class FakeTournament:
    """Stands in for ``MetaculusClient`` on the fetch path, and records what it was asked.

    ``group_question_mode`` defaults to ``"exclude"`` -- the SDK's default on every
    overload *except* the tournament one -- so a caller that drops the keyword is caught
    rather than accidentally agreeing with the configured value. Same trick, and same
    reason, as ``tests/unit/test_fetch.py``'s local fake.
    """

    def __init__(self, questions: list[MetaculusQuestion] | None = None) -> None:
        self._questions = flat_questions() if questions is None else list(questions)
        self.calls: list[tuple[object, str]] = []

    def get_all_open_questions_from_tournament(
        self, tournament_id: object, group_question_mode: str = "exclude"
    ) -> list[MetaculusQuestion]:
        self.calls.append((tournament_id, group_question_mode))
        return list(self._questions)

    @property
    def tournament_ids(self) -> list[object]:
        return [call[0] for call in self.calls]

    @property
    def group_modes(self) -> list[str]:
        return [call[1] for call in self.calls]


def install_tournament(
    monkeypatch: pytest.MonkeyPatch, tournament: FakeTournament
) -> FakeTournament:
    """Replace the client factory ``fetch`` bound at import, not the SDK class.

    ``fetch.py`` does ``from ...client import build_client`` at module scope, so the name
    lives in ``fetch``'s namespace and patching ``metaculus.client.build_client`` would not
    reach it. Patching here also means no ``METACULUS_TOKEN`` is needed for a read test --
    ``build_client`` is what reads it, and it never runs.
    """
    monkeypatch.setattr("whiskeyjack_bot.metaculus.fetch.build_client", lambda _config: tournament)
    return tournament


# ── the write path ───────────────────────────────────────────────────────────


def api_response(
    status: int, body: bytes, headers: dict[str, str] | None = None
) -> requests.Response:
    """A real ``requests.Response``, so ``raise_for_status`` raises the real exception.

    The exception type is what matters: ``submission_live.classify_error`` matches on
    ``__module__ == "requests.exceptions"``, so a hand-rolled stand-in classifies as
    ``internal_error`` and never reaches the ``http_error`` branch these tests are about.
    """
    response = requests.Response()
    response.status_code = status
    try:
        response.reason = HTTPStatus(status).phrase
    except ValueError:  # a status the enum does not know; the reason is cosmetic
        response.reason = "Unknown"
    response.url = "https://example.invalid/api/questions/forecast/"
    response._content = body
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


class CountingTransport:
    """Counts every request the SDK really issues, and serves scripted answers.

    ``posts`` is the number the acceptance criterion is about. ``gets`` is its companion:
    the pair is what shows the line this project draws -- writes are not retried, reads
    are.

    ``post_outcomes``/``get_outcomes`` are consumed in order, the last one repeating, so a
    script shorter than the call sequence still answers every call. An outcome that is a
    ``BaseException`` is raised; anything else is returned.
    """

    def __init__(
        self,
        *,
        post_outcomes: list[Any] | None = None,
        get_outcomes: list[Any] | None = None,
    ) -> None:
        self._post_outcomes = list(post_outcomes or [api_response(200, b"{}")])
        self._get_outcomes = list(get_outcomes or [])
        self.posts = 0
        self.gets = 0
        self.post_urls: list[str] = []
        self.get_urls: list[str] = []

    @staticmethod
    def _serve(outcomes: list[Any], index: int) -> Any:
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def post(self, url: str, *_args: Any, **_kwargs: Any) -> Any:
        index = self.posts
        self.posts += 1
        self.post_urls.append(url)
        return self._serve(self._post_outcomes, index)

    def get(self, url: str, *_args: Any, **_kwargs: Any) -> Any:
        index = self.gets
        self.gets += 1
        self.get_urls.append(url)
        if not self._get_outcomes:
            raise AssertionError(f"unscripted GET to {url}")
        return self._serve(self._get_outcomes, index)


def install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: CountingTransport
) -> CountingTransport:
    """Stub both verbs and neutralize the SDK's own retry backoff.

    The backoff matters to the clock, not to the assertions: the decorator on
    ``get_question_by_post_id`` sleeps up to 75 seconds across its retries, and the refetch
    path deliberately keeps that decorator. Patched on the ``time`` module
    ``forecasting_tools.util.misc`` imported, the same place
    ``tests/unit/test_metaculus_poster.py`` patches it.
    """
    import forecasting_tools.util.misc as misc

    monkeypatch.setattr(requests, "post", transport.post)
    monkeypatch.setattr(requests, "get", transport.get)
    monkeypatch.setattr(misc.time, "sleep", lambda _seconds: None)
    return transport


def post_with_forecast_history(entries: list[dict[str, Any]], *, name: str = "binary") -> bytes:
    """A committed API post with a forecast history spliced in, as the refetch sees it.

    Built from the fixture rather than committed as a second file: the post shape then has
    exactly one source of truth, and the part this function adds -- the only part that is
    not evidence about a real post -- stays visible at the call site.
    """
    post = raw_post(name)
    post["question"]["my_forecasts"] = {
        "history": entries,
        "latest": entries[-1] if entries else None,
    }
    return json.dumps(post).encode("utf-8")


# ── the configuration that points at it ──────────────────────────────────────


def config_data(tmp_path: Path, **submission: Any) -> dict[str, Any]:
    """The committed example config, repointed at ``tmp_path`` and paced at zero.

    ``request_spacing_seconds`` and ``request_jitter_seconds`` are committed at 3.5 and 1.0
    and the SDK really sleeps them (``MetaculusClient._sleep_between_requests``), so a test
    driving a post plus three refetches would spend ~15 seconds of wall clock proving
    nothing. Both are ``ge=0`` in the schema, so zero is a value the real validator accepts
    rather than a monkeypatch around it.

    ``submission`` overrides land on the submission block, so a test asks for the live flags
    by name instead of restating all three.
    """
    import copy

    import yaml

    data: dict[str, Any] = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    data["metaculus"]["request_spacing_seconds"] = 0.0
    data["metaculus"]["request_jitter_seconds"] = 0.0
    data["submission"].update(submission)
    return data


LIVE_SUBMISSION_FLAGS: dict[str, Any] = {"enabled": True, "dry_run": False, "no_submit": False}
"""The three flags a live post needs, all committed ``false``/``true`` the safe way.

Named because ``submission_live.require_live_submission_enabled`` needs all three flipped
together and a test that flips two is testing the refusal it did not mean to.
"""


# ── the real adapter, over the counted transport ─────────────────────────────


def build_real_poster(transport: CountingTransport | None = None) -> Any:
    """A **real** ``SingleAttemptPoster`` over a **real** ``MetaculusClient``.

    Deliberately not a double. ``tests/unit/test_submission_live.py`` counts calls into its
    own four-method ``FakePoster``, which structurally cannot observe the SDK's blind POST
    retry because that retry lives *below* the protocol. Putting the real adapter in the
    chain and counting at ``requests`` is what makes "exactly one POST" a measurement.

    ``transport`` is accepted only to keep the call sites honest about needing one; it is
    installed by :func:`install_transport`.
    """
    from whiskeyjack_bot.metaculus.client import SingleAttemptPoster

    del transport  # installed separately; named here so a caller cannot forget it exists
    client = MetaculusClient(token="fake-token-for-integration")
    client.sleep_time_between_requests_min = 0.0
    client.sleep_jitter_seconds = 0.0
    return SingleAttemptPoster(client)


def forecast_entry(start_time: float, values: list[float]) -> dict[str, Any]:
    """One ``my_forecasts.history`` entry, in the platform's own shape."""
    return {"start_time": start_time, "end_time": None, "forecast_values": values}


def binary_values(probability: float) -> list[float]:
    """What the platform stores for a binary forecast: ``[P(no), P(yes)]``."""
    return [1.0 - probability, probability]


PROBABILITY_YES = 0.37
"""The probability the shared approved record carries, mirrored from the unit tier."""

CONFIRMING_HISTORY_ENTRIES: list[dict[str, Any]] = [
    forecast_entry(1_000_100.0, binary_values(PROBABILITY_YES))
]
"""What the platform shows after an honest post: one entry newer than an empty baseline."""
