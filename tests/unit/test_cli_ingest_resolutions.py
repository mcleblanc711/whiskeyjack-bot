"""M4-801: `ingest-resolutions` and the orchestration beneath it.

The orchestration is driven with an in-memory `fetch_post`. The command is driven end to end
through the real `build_client` and the pinned SDK, with `requests.get` and `requests.post`
replaced by counters -- so "reads Metaculus only" is a measurement of what went over the
wire (N GETs, zero POSTs), not a statement about which function was called.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests
import yaml

from resolution_rows import (
    FIXTURES,
    kind_payload,
    seed_record,
    seed_submitted,
    walk_to_submitted,
)
from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_ENV_MISSING, EXIT_OK
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import current_status, latest_resolution
from whiskeyjack_bot import notify
from whiskeyjack_bot.resolution_ingest import (
    ResolutionFetchError,
    ResolutionIngestError,
    WithheldRecord,
    ingest_resolutions,
    withheld_records,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)


def _clock() -> Iterator[datetime]:
    moment = T0
    while True:
        yield moment
        moment += timedelta(minutes=1)


def _group_post(resolutions: tuple[str | None, ...]) -> tuple[dict[str, Any], list[int]]:
    post = json.loads((FIXTURES / "group" / "minibench_group.json").read_text(encoding="utf-8"))
    members = post["group_of_questions"]["questions"]
    for member, resolution in zip(members, resolutions, strict=True):
        member["status"] = "resolved"
        member["resolution"] = resolution
    return post, [member["id"] for member in members]


class FakePlatform:
    """post_id -> payload, counting fetches; a missing post is a fetch failure."""

    def __init__(self, posts: dict[int, dict[str, Any]]) -> None:
        self.posts = posts
        self.fetches: list[int] = []

    def __call__(self, post_id: int) -> object:
        self.fetches.append(post_id)
        if post_id not in self.posts:
            raise ResolutionFetchError("the Metaculus post could not be fetched")
        return copy.deepcopy(self.posts[post_id])


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Any]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    connection = connect(db)
    try:
        yield connection
    finally:
        connection.close()


# ── orchestration ────────────────────────────────────────────────────────────


def test_every_submitted_record_is_resolved_with_one_fetch_per_post(conn: Any) -> None:
    group, (first, second, third) = _group_post(("yes", "annulled", None))
    group_post_id = group["id"]
    seed_submitted(conn, "rec-a", question_id=45747, post_id=45556)
    seed_submitted(conn, "rec-b", question_id=45748, post_id=45557, question_type="numeric")
    seed_submitted(conn, "rec-g1", question_id=first, post_id=group_post_id)
    seed_submitted(conn, "rec-g2", question_id=second, post_id=group_post_id)
    seed_submitted(conn, "rec-g3", question_id=third, post_id=group_post_id)
    seed_record(conn, "rec-draft", question_id=45749, post_id=45558)  # never posted
    platform = FakePlatform(
        {
            45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747),
            45557: kind_payload("numeric", "withheld", post_id=45557, question_id=45748),
            group_post_id: group,
        }
    )
    clock = _clock()
    results = ingest_resolutions(conn, platform, clock=lambda: next(clock))

    assert sorted(platform.fetches) == sorted([45556, 45557, group_post_id])
    by_record = {result.record_id: result for result in results}
    assert "rec-draft" not in by_record
    assert {
        r: (x.status, x.kind, x.scorable, x.moved_to_resolved) for r, x in by_record.items()
    } == {
        "rec-a": ("appended", "resolved", True, True),
        "rec-b": ("appended", "withheld", False, False),
        "rec-g1": ("appended", "resolved", True, True),
        "rec-g2": ("appended", "annulled", False, True),
        "rec-g3": ("appended", "withheld", False, False),
    }
    assert current_status(conn, "rec-b") == "submitted"
    assert current_status(conn, "rec-g2") == "resolved"
    # The group's records read one payload observed at one instant.
    g1, g2 = latest_resolution(conn, "rec-g1"), latest_resolution(conn, "rec-g2")
    assert g1 is not None and g2 is not None
    assert g1.observed_at_utc == g2.observed_at_utc
    assert g1.source_response_sha256 == g2.source_response_sha256


def test_a_second_run_changes_nothing(conn: Any) -> None:
    seed_submitted(conn, "rec-a", question_id=45747, post_id=45556)
    platform = FakePlatform(
        {45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747)}
    )
    clock = _clock()
    ingest_resolutions(conn, platform, clock=lambda: next(clock))
    again = ingest_resolutions(conn, platform, clock=lambda: next(clock))
    assert [result.status for result in again] == ["unchanged"]
    assert conn.execute("SELECT count(*) FROM resolution_events").fetchone()[0] == 1


def test_a_resolved_record_is_still_polled_so_a_retraction_is_seen(conn: Any) -> None:
    seed_submitted(conn, "rec-a", question_id=45747, post_id=45556)
    platform = FakePlatform(
        {45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747)}
    )
    clock = _clock()
    ingest_resolutions(conn, platform, clock=lambda: next(clock))
    platform.posts[45556] = kind_payload("binary", "unresolved", post_id=45556, question_id=45747)
    (result,) = ingest_resolutions(conn, platform, clock=lambda: next(clock))
    assert (result.status, result.kind, result.scorable) == ("appended", "unresolved", False)


def test_one_failed_post_does_not_stop_the_others(conn: Any) -> None:
    seed_submitted(conn, "rec-a", question_id=45747, post_id=45556)
    seed_submitted(conn, "rec-b", question_id=45748, post_id=45557)
    seed_submitted(conn, "rec-c", question_id=45749, post_id=45558)
    seed_submitted(conn, "rec-none", question_id=45750, post_id=None)
    malformed = kind_payload("binary", "resolved", post_id=45558, question_id=45749)
    malformed["question"]["resolution"] = "maybe"
    platform = FakePlatform(
        {
            45557: kind_payload("binary", "annulled", post_id=45557, question_id=45748),
            45558: malformed,
        }
    )
    clock = _clock()
    results = {
        r.record_id: r for r in ingest_resolutions(conn, platform, clock=lambda: next(clock))
    }
    assert results["rec-a"].status == "failed" and "could not be fetched" in str(
        results["rec-a"].detail
    )
    assert results["rec-b"].status == "appended"
    assert results["rec-c"].status == "failed" and "cannot be recorded" in str(
        results["rec-c"].detail
    )
    assert results["rec-none"].status == "failed" and "no post_id" in str(
        results["rec-none"].detail
    )
    assert 45556 in platform.fetches and None not in platform.fetches


def test_the_question_filter_fetches_only_that_question(conn: Any) -> None:
    seed_submitted(conn, "rec-a", question_id=45747, post_id=45556)
    seed_submitted(conn, "rec-b", question_id=45748, post_id=45557)
    platform = FakePlatform(
        {
            45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747),
            45557: kind_payload("binary", "resolved", post_id=45557, question_id=45748),
        }
    )
    results = ingest_resolutions(conn, platform, question_id=45748)
    assert platform.fetches == [45557]
    assert [result.record_id for result in results] == ["rec-b"]


@pytest.mark.parametrize("bad", [0, -1, True, "45747"])
def test_a_malformed_question_filter_is_refused(conn: Any, bad: object) -> None:
    with pytest.raises(ResolutionIngestError):
        ingest_resolutions(conn, FakePlatform({}), question_id=bad)  # type: ignore[arg-type]


# ── the command, through the real client and SDK ─────────────────────────────


class _Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status_code = status
        self.ok = status < 400
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()
        self.reason = "OK" if self.ok else "Not Found"

    def json(self) -> object:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.exceptions.HTTPError(str(self.status_code), response=self)  # type: ignore[arg-type]


class Wire:
    def __init__(self, posts: dict[int, dict[str, Any]]) -> None:
        self.posts = posts
        self.gets: list[str] = []
        self.posts_sent = 0

    def get(self, url: str, *args: Any, **kwargs: Any) -> _Response:
        self.gets.append(url)
        post_id = int(url.rstrip("/").rsplit("/", 1)[-1])
        if post_id not in self.posts:
            return _Response({"detail": "Not found."}, status=404)
        return _Response(self.posts[post_id])

    def post(self, *args: Any, **kwargs: Any) -> None:
        self.posts_sent += 1
        raise AssertionError("ingest-resolutions must never POST")


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    data = copy.deepcopy(
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
    data["metaculus"]["request_spacing_seconds"] = 0
    data["metaculus"]["request_jitter_seconds"] = 0
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _ledger(config_file: Path) -> Any:
    database = Path(
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"]
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(database)
    return connect(database)


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import forecasting_tools.util.misc as misc

    monkeypatch.setattr(misc.time, "sleep", lambda _seconds: None)


def _install(monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", "fake-token-for-tests")
    monkeypatch.setattr(requests, "get", wire.get)
    monkeypatch.setattr(requests, "post", wire.post)


def test_the_command_reads_and_records_and_never_posts(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _ledger(config_file)
    try:
        seed_submitted(connection, "rec-a", question_id=45747, post_id=45556)
        seed_submitted(connection, "rec-b", question_id=45748, post_id=45557)
    finally:
        connection.close()
    wire = Wire(
        {
            45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747),
            45557: kind_payload("binary", "ambiguous", post_id=45557, question_id=45748),
        }
    )
    _install(monkeypatch, wire)

    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "record rec-a  appended  kind resolved  scorable yes  -> resolved" in out
    assert "record rec-b  appended  kind ambiguous  scorable no  -> resolved" in out
    assert "records: 2  failed: 0" in out
    assert len(wire.gets) == 2 and wire.posts_sent == 0

    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert "unchanged" in capsys.readouterr().out
    assert len(wire.gets) == 4 and wire.posts_sent == 0


def test_the_command_exits_refused_when_any_record_failed(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _ledger(config_file)
    try:
        seed_submitted(connection, "rec-a", question_id=45747, post_id=45556)
        seed_submitted(connection, "rec-gone", question_id=45748, post_id=45557)
    finally:
        connection.close()
    wire = Wire({45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747)})
    _install(monkeypatch, wire)
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "record rec-gone  failed" in out and "records: 2  failed: 1" in out
    assert wire.posts_sent == 0
    connection = connect(
        Path(yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"])
    )
    try:
        assert current_status(connection, "rec-a") == "resolved", "the good record still landed"
    finally:
        connection.close()


def test_the_command_refuses_without_a_token_before_any_request(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _ledger(config_file).close()
    wire = Wire({})
    monkeypatch.setattr(requests, "get", wire.get)
    monkeypatch.setattr(requests, "post", wire.post)
    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_ENV_MISSING
    assert "METACULUS_TOKEN" in capsys.readouterr().out
    assert wire.gets == [] and wire.posts_sent == 0


def test_the_command_refuses_a_missing_ledger_without_creating_one(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, Wire({}))
    database = Path(
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"]
    )
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_REFUSED
    assert not database.exists()


def test_the_ingest_module_imports_nothing_that_can_post() -> None:
    """The module graph rather than the call path: no submission module, no poster."""
    import ast

    tree = ast.parse(
        (REPO_ROOT / "src/whiskeyjack_bot/resolution_ingest.py").read_text(encoding="utf-8")
    )
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported, "vacuity guard: the module does import things"
    assert not {name for name in imported if "submission" in name or "poster" in name.lower()}


# ── M4-807: a withheld resolution reaches the operator ───────────────────────


class Pager:
    """The ntfy side of a real :class:`notify.Notifier`, over ``httpx.MockTransport``.

    The notifier is the production one -- its throttle stamps, redaction and deadline all
    run -- and only the transport is fake. ``mode`` makes the push fail in each way the
    channel can: a rejection, a transport error, or a handler bug.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pushes: list[tuple[str, str, str]] = []  # (title, priority, body)
        self.built = 0
        self.mode = "ok"
        self.now = T0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "raise":
            raise RuntimeError("the push blew up")
        if self.mode == "transport":
            raise httpx.ConnectError("unreachable", request=request)
        self.pushes.append(
            (request.headers["Title"], request.headers["Priority"], request.content.decode())
        )
        return httpx.Response(500 if self.mode == "rejected" else 200)

    def build(self, config: Any) -> notify.Notifier:
        self.built += 1
        return notify.Notifier(
            client=httpx.Client(transport=httpx.MockTransport(self._handle)),
            topic_url="https://ntfy.invalid/topic-not-a-secret",
            state_root=config.storage.artifact_root,
            clock=lambda: self.now,
        )


@pytest.fixture()
def pager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Pager:
    installed = Pager(tmp_path)
    # The command imports `build_notifier` from the module at call time.
    monkeypatch.setattr(notify, "build_notifier", installed.build)
    return installed


def _withheld_population(config_file: Path) -> dict[int, dict[str, Any]]:
    connection = _ledger(config_file)
    try:
        seed_submitted(connection, "rec-w", question_id=45748, post_id=45557)
        seed_submitted(connection, "rec-a", question_id=45747, post_id=45556)
    finally:
        connection.close()
    withheld = kind_payload("binary", "withheld", post_id=45557, question_id=45748)
    # Payload text the alert must never carry.
    withheld["title"] = "SENTINEL-post-title"
    withheld["question"]["title"] = "SENTINEL-question-title"
    withheld["question"]["description"] = "SENTINEL-description"
    return {
        45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747),
        45557: withheld,
    }


def test_a_withheld_record_pages_once_per_window_while_it_stays_withheld(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pager: Pager,
) -> None:
    _install(monkeypatch, Wire(_withheld_population(config_file)))

    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "record rec-w  appended  kind withheld  scorable no" in out
    assert "records: 2  failed: 0  withheld: 1" in out
    assert len(pager.pushes) == 1
    title, priority, body = pager.pushes[0]
    assert title == "whiskeyjack: a posted forecast's resolution is withheld"
    assert priority == "default"
    assert "record=rec-w" in body and "question=45748" in body
    assert "rec-a" not in body and "45747" not in body, "only the withheld record pages"
    assert "SENTINEL" not in title + body, "the alert carries no payload value"

    # The next scheduled run, same day: the record reads `unchanged` (no kind), but it is
    # still withheld -- the throttle, not the transition, is what keeps it quiet.
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "record rec-w  unchanged" in out and "withheld: 1" in out
    assert len(pager.pushes) == 1

    # Later the same UTC day -- the window is a tumbling one, floor(epoch / 86400), and T0 is
    # 18:00 -- the day-long window still holds it.
    pager.now = T0 + timedelta(hours=5, minutes=59)
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert len(pager.pushes) == 1

    # A day later it is still true, so it reminds again.
    pager.now = T0 + timedelta(days=1)
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert len(pager.pushes) == 2 and "record=rec-w" in pager.pushes[1][2]


def test_a_record_the_platform_unmasks_stops_paging(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pager: Pager,
) -> None:
    posts = _withheld_population(config_file)
    _install(monkeypatch, Wire(posts))
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert len(pager.pushes) == 1
    posts[45557] = kind_payload("binary", "resolved", post_id=45557, question_id=45748)
    pager.now = T0 + timedelta(days=1)
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert "withheld: 0" in capsys.readouterr().out
    assert len(pager.pushes) == 1


@pytest.mark.parametrize("kind", ["resolved", "annulled", "ambiguous", "unresolved"])
def test_no_other_kind_pages_or_builds_a_notifier(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, pager: Pager, kind: str
) -> None:
    connection = _ledger(config_file)
    try:
        seed_submitted(connection, "rec-a", question_id=45747, post_id=45556)
    finally:
        connection.close()
    posts = {45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747)}
    _install(monkeypatch, Wire(posts))
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    # Then the observation `kind` is recorded on top (`unresolved` is a retraction).
    posts[45556] = kind_payload("binary", kind, post_id=45556, question_id=45747)
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert pager.pushes == [] and pager.built == 0


@pytest.mark.parametrize("mode", ["rejected", "transport", "raise", "no_notifier"])
@pytest.mark.parametrize("also_failed", [False, True])
def test_the_alert_never_changes_the_exit_code(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pager: Pager,
    mode: str,
    also_failed: bool,
) -> None:
    posts = _withheld_population(config_file)
    if also_failed:
        del posts[45556]  # a 404: rec-a fails, which is what decides the exit code
    if mode == "no_notifier":
        monkeypatch.setattr(notify, "build_notifier", lambda config: None)
    else:
        pager.mode = mode
    _install(monkeypatch, Wire(posts))
    expected = EXIT_REFUSED if also_failed else EXIT_OK
    assert main(["ingest-resolutions", "--config", str(config_file)]) == expected
    assert f"failed: {1 if also_failed else 0}  withheld: 1" in capsys.readouterr().out


# The quiet-branch table. A payload the classifier cannot read must never be stored as
# `withheld` -- which would page daily and look like routine access -- nor pass silently as
# no alert: each is a `failed` record, a non-zero exit, and the schedule's OnFailure page.
def _no_resolution_key(post: dict[str, Any]) -> None:
    del post["question"]["resolution"]


def _numeric_resolution(post: dict[str, Any]) -> None:
    post["question"]["resolution"] = 5


def _unknown_status(post: dict[str, Any]) -> None:
    post["question"]["status"] = "resolvedish"


def _other_question(post: dict[str, Any]) -> None:
    post["question"]["id"] = 99999


def _other_type(post: dict[str, Any]) -> None:
    post["question"]["type"] = "numeric"


def _not_a_post(post: dict[str, Any]) -> None:
    post.clear()


@pytest.mark.parametrize(
    "malform",
    [
        _no_resolution_key,
        _numeric_resolution,
        _unknown_status,
        _other_question,
        _other_type,
        _not_a_post,
    ],
)
def test_a_malformed_withheld_shape_fails_loudly_and_never_reads_as_withheld(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pager: Pager,
    malform: Any,
) -> None:
    posts = _withheld_population(config_file)
    malform(posts[45557])
    _install(monkeypatch, Wire(posts))
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "record rec-w  failed" in out and "withheld: 0" in out
    assert pager.pushes == []
    connection = _ledger(config_file)
    try:
        assert latest_resolution(connection, "rec-w") is None
    finally:
        connection.close()


def test_a_withheld_row_that_no_longer_matches_its_digest_is_refused_not_quiet(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pager: Pager,
) -> None:
    """The condition is read through the verifying reader, so a stored row whose content was
    changed after hashing refuses the run rather than dropping out of the withheld set."""
    posts = _withheld_population(config_file)
    _install(monkeypatch, Wire(posts))
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert len(pager.pushes) == 1
    connection = _ledger(config_file)
    try:
        connection.execute("DROP TRIGGER resolution_events_block_update")
        connection.execute(
            "UPDATE resolution_events SET source_response = '{}' WHERE forecast_record_id = 'rec-w'"
        )
        connection.commit()
    finally:
        connection.close()
    pager.now = T0 + timedelta(days=1)
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused: a record's latest resolution could not be read" in out
    assert "SENTINEL" not in out
    assert len(pager.pushes) == 1


def test_withheld_records_reads_the_condition_once_per_record(conn: Any) -> None:
    post, question_ids = _group_post((None, "yes", "annulled"))
    post_id = post["id"]
    for index, question_id in enumerate(question_ids):
        seed_submitted(conn, f"rec-g{index}", question_id=question_id, post_id=post_id)
    results = ingest_resolutions(conn, FakePlatform({post_id: post}), clock=_clock().__next__)
    doubled = results + results
    assert withheld_records(conn, doubled) == (
        WithheldRecord(record_id="rec-g0", question_id=question_ids[0]),
    )


def test_two_withheld_records_on_one_question_each_page(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, pager: Pager
) -> None:
    """The throttle is keyed on the record, not the question: two forecast versions posted on
    the same question are two records the ledger cannot score, and each is reported."""
    connection = _ledger(config_file)
    try:
        seed_submitted(connection, "rec-w1", question_id=45748, post_id=45557)
        # The same question under another project id (`forecast_records` is unique per
        # question, tournament and version), as across the 33122 -> 33125 rollover.
        seed_record(connection, "rec-w2", question_id=45748, post_id=45557, tournament_id="33125")
        walk_to_submitted(connection, "rec-w2")
    finally:
        connection.close()
    posts = {45557: kind_payload("binary", "withheld", post_id=45557, question_id=45748)}
    _install(monkeypatch, Wire(posts))
    assert main(["ingest-resolutions", "--config", str(config_file)]) == EXIT_OK
    assert sorted(body.split("record=")[1].split()[0] for _, _, body in pager.pushes) == [
        "rec-w1",
        "rec-w2",
    ]
