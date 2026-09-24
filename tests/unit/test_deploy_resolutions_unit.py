"""M4-805: the scheduled resolution unit, read as a contract and run as a measurement.

``deploy/systemd/whiskeyjack-resolutions.service`` is the only thing that makes ingestion and
scoring unattended, so its text is the product. Three kinds of test:

- **Parity with a witness outside this unit.** The pager, environment and interpreter are
  compared with ``whiskeyjack-tournament.service`` -- the unit that already pages in production
  -- rather than with constants written here, so the claim "the same alerting path" is a
  comparison, not a restatement.
- **The ExecStart contract.** Exactly two commands, ingestion then scoring, with no prefix that
  would make systemd ignore a failure, and nothing that turns a non-zero exit into success.
  systemd runs a later ExecStart only after the earlier one exited 0; that half is systemd's,
  and was verified by execution on systemd 255 (``docs/M4-NOTES.md`` § M4-805).
- **A measurement of what the unit's own command lines reach.** The argv is read out of the
  unit file and driven through ``cli.main`` in the unit's order, stopping at the first non-zero
  exit as systemd does, with every paid or posting entry point replaced by a refusal that
  records being reached. "No paid call, no submission path" is then a count of zero, not a
  reading of imports.
"""

from __future__ import annotations

import copy
import json
import math
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import litellm
import pytest
import requests
import yaml

from resolution_rows import kind_payload
from score_rows import seed_forecast, walk_to_submitted
from whiskeyjack_bot.cli import EXIT_REFUSED, build_parser, main
from whiskeyjack_bot.env_verify import EXIT_ENV_MISSING, EXIT_OK
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import current_status, latest_resolution, read_local_scores

REPO_ROOT = Path(__file__).resolve().parents[2]
UNITS = REPO_ROOT / "deploy" / "systemd"
SERVICE = UNITS / "whiskeyjack-resolutions.service"
TIMER = UNITS / "whiskeyjack-resolutions.timer"
# The unit that already pages in production: the witness the new unit is compared against.
TOURNAMENT = UNITS / "whiskeyjack-tournament.service"
EXPECTED_COMMANDS = ("ingest-resolutions", "score")


# ── a unit-file reader that keeps repeated keys ──────────────────────────────


def read_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Section -> ``(key, value)`` pairs in file order.

    Not ``configparser``: systemd repeats keys (two ``ExecStart=`` lines are the design), and
    ``configparser`` either refuses a duplicate or keeps the last one, which would hide
    exactly the line order this suite exists to pin.
    """
    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        assert not line.endswith("\\"), f"{path.name}: line continuations are not read here"
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        assert current is not None, f"{path.name}: a key outside any section"
        key, separator, value = line.partition("=")
        assert separator, f"{path.name}: a line that is not key=value"
        current.append((key.strip(), value.strip()))
    return sections


def values(unit: dict[str, list[tuple[str, str]]], section: str, key: str) -> list[str]:
    return [value for name, value in unit.get(section, []) if name == key]


def only(unit: dict[str, list[tuple[str, str]]], section: str, key: str) -> str:
    found = values(unit, section, key)
    assert len(found) == 1, f"expected exactly one {section}.{key}, found {len(found)}"
    return found[0]


def cli_argv(exec_start: str) -> list[str]:
    """The arguments ``cli.main`` receives from one ``ExecStart=`` line."""
    words = shlex.split(exec_start)
    assert words[1:3] == ["-m", "whiskeyjack_bot.cli"], "the unit must run the package's CLI"
    return words[3:]


def config_of(argv: list[str]) -> str:
    assert argv.count("--config") == 1
    return argv[argv.index("--config") + 1]


@pytest.fixture(scope="module")
def service() -> dict[str, list[tuple[str, str]]]:
    return read_unit(SERVICE)


@pytest.fixture(scope="module")
def tournament() -> dict[str, list[tuple[str, str]]]:
    return read_unit(TOURNAMENT)


# ── parity with the unit that already pages ──────────────────────────────────


def test_a_failure_starts_the_same_pager_the_tournament_poll_uses(
    service: dict[str, list[tuple[str, str]]], tournament: dict[str, list[tuple[str, str]]]
) -> None:
    pager = only(service, "Unit", "OnFailure")
    assert pager == only(tournament, "Unit", "OnFailure")
    assert pager == "whiskeyjack-notify@%N.service"
    assert (UNITS / "whiskeyjack-notify@.service").is_file()
    # A failure only reaches OnFailure if systemd sees one. Each of these would swallow it:
    # a non-zero exit declared success, a restart loop that never settles into "failed", or a
    # oneshot replaced by a type whose ExecStart failure semantics differ.
    for silencer in ("SuccessExitStatus", "Restart", "RestartForceExitStatus"):
        assert values(service, "Service", silencer) == [], silencer
    assert only(service, "Service", "Type") == "oneshot"
    assert values(service, "Service", "TimeoutStartSec"), "a hang must end as a timeout failure"


def test_the_environment_matches_the_tournament_poll(
    service: dict[str, list[tuple[str, str]]], tournament: dict[str, list[tuple[str, str]]]
) -> None:
    for key in ("WorkingDirectory", "EnvironmentFile"):
        assert only(service, "Service", key) == only(tournament, "Service", key), key
    # Includes LITELLM_LOCAL_MODEL_COST_MAP: without it, importing the SDK fetches litellm's
    # cost map over the network on every run.
    assert sorted(values(service, "Service", "Environment")) == sorted(
        values(tournament, "Service", "Environment")
    )
    assert "LITELLM_LOCAL_MODEL_COST_MAP=True" in values(service, "Service", "Environment")
    tournament_python = shlex.split(only(tournament, "Service", "ExecStart"))[0]
    for line in values(service, "Service", "ExecStart"):
        assert shlex.split(line)[0] == tournament_python


# ── the ExecStart contract ───────────────────────────────────────────────────


def test_exactly_ingestion_then_scoring_and_nothing_else_runs(
    service: dict[str, list[tuple[str, str]]], tournament: dict[str, list[tuple[str, str]]]
) -> None:
    lines = values(service, "Service", "ExecStart")
    assert len(lines) == 2
    # systemd.service(5): "-" ignores the command's failure, "+"/"!"/"!!" change privileges,
    # "@" and ":" change argv[0] and expansion. None belongs here; "-" would let `score` run
    # after a failed ingestion and hide the failure from the pager.
    for line in lines:
        assert line[0] not in "-+!@:|", line
    parser = build_parser()
    commands = []
    for line in lines:
        args = parser.parse_args(cli_argv(line))
        commands.append(args.command)
    assert tuple(commands) == EXPECTED_COMMANDS
    # Every record, not one: neither line narrows its scope.
    assert parser.parse_args(cli_argv(lines[0])).question_id is None
    assert parser.parse_args(cli_argv(lines[1])).record_id is None
    # The profile the live poll forecasts and posts from, for both lines.
    tournament_config = config_of(cli_argv(only(tournament, "Service", "ExecStart")))
    assert [config_of(cli_argv(line)) for line in lines] == [tournament_config] * 2
    for key, _ in service["Service"]:
        if key.startswith("Exec"):
            assert key == "ExecStart", f"{key} would run a command outside the contract"


def test_the_timer_fires_the_service_every_six_hours_and_survives_downtime() -> None:
    timer = read_unit(TIMER)
    assert only(timer, "Timer", "Unit") == SERVICE.name
    assert only(timer, "Timer", "OnCalendar") == "*-*-* 00/6:23:00"
    assert only(timer, "Timer", "Persistent") == "true"
    assert only(timer, "Install", "WantedBy") == "timers.target"


# ── what the unit's own command lines reach ──────────────────────────────────


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


@dataclass
class Wire:
    """Metaculus GETs by post id; everything else a refusal that records being reached."""

    posts: dict[int, dict[str, Any]]
    gets: list[str]
    reached: list[str]

    def get(self, url: str, *args: Any, **kwargs: Any) -> _Response:
        self.gets.append(url)
        post_id = int(url.rstrip("/").rsplit("/", 1)[-1])
        if post_id not in self.posts:
            return _Response({"detail": "Not found."}, status=404)
        return _Response(copy.deepcopy(self.posts[post_id]))

    def refuse(self, name: str) -> Any:
        def call(*args: object, **kwargs: object) -> Any:
            self.reached.append(name)
            raise AssertionError(f"the scheduled unit reached {name}")

        return call


def install_wire(monkeypatch: pytest.MonkeyPatch, posts: dict[int, dict[str, Any]]) -> Wire:
    import forecasting_tools.util.misc as misc

    import whiskeyjack_bot.forecast.generate as generate
    import whiskeyjack_bot.forecast.priced as priced
    import whiskeyjack_bot.metaculus.client as metaculus_client
    import whiskeyjack_bot.notify as notify
    import whiskeyjack_bot.pipeline_live as pipeline_live
    import whiskeyjack_bot.research.asknews as asknews
    import whiskeyjack_bot.research.exa as exa
    import whiskeyjack_bot.submission_live as submission_live

    wire = Wire(posts=posts, gets=[], reached=[])
    monkeypatch.setattr(misc.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(requests, "get", wire.get)
    for verb in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(requests, verb, wire.refuse(f"requests.{verb}"))
    monkeypatch.setattr(requests.Session, "request", wire.refuse("requests.Session.request"))
    # The submission path, by its entry points and by its one constructor.
    monkeypatch.setattr(metaculus_client, "build_poster", wire.refuse("build_poster"))
    monkeypatch.setattr(
        submission_live, "post_approved_forecast", wire.refuse("post_approved_forecast")
    )
    # Every paid provider: the live pipeline, retrieval, the model, and litellm beneath the SDK.
    monkeypatch.setattr(pipeline_live, "run_live", wire.refuse("run_live"))
    monkeypatch.setattr(asknews, "build_asknews_client", wire.refuse("build_asknews_client"))
    monkeypatch.setattr(exa, "build_exa_client", wire.refuse("build_exa_client"))
    monkeypatch.setattr(generate, "build_forecaster_client", wire.refuse("build_forecaster_client"))
    monkeypatch.setattr(priced.PricedClient, "__init__", wire.refuse("PricedClient"))
    monkeypatch.setattr(litellm, "completion", wire.refuse("litellm.completion"))
    monkeypatch.setattr(litellm, "acompletion", wire.refuse("litellm.acompletion"))
    # Anything else that would leave the machine: httpx (AskNews, Exa, ntfy) and name lookup,
    # which the Metaculus GETs above never need because they never reach a socket.
    monkeypatch.setattr(notify, "build_notify_client", wire.refuse("build_notify_client"))
    monkeypatch.setattr(httpx.Client, "send", wire.refuse("httpx.Client.send"))
    monkeypatch.setattr(socket, "getaddrinfo", wire.refuse("socket.getaddrinfo"))
    return wire


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


def ledger_path(config_file: Path) -> Path:
    return Path(yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"])


def run_unit(config_file: Path) -> list[tuple[str, int]]:
    """Each ExecStart line of the unit, in order, against ``config_file``.

    Stops at the first non-zero exit, as systemd does for a oneshot whose lines carry no "-"
    prefix -- the test above pins that they carry none.
    """
    ran: list[tuple[str, int]] = []
    for line in values(read_unit(SERVICE), "Service", "ExecStart"):
        argv = cli_argv(line)
        argv[argv.index("--config") + 1] = str(config_file)
        code = main(argv)
        ran.append((argv[0], code))
        if code != 0:
            break
    return ran


def seed_posted_binary(
    config_file: Path, record_id: str, *, question_id: int, post_id: int
) -> None:
    database = ledger_path(config_file)
    database.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(database)
    connection = connect(database)
    try:
        digest = seed_forecast(
            connection, record_id, question_id=question_id, post_id=post_id, probability_yes=0.7
        )
        walk_to_submitted(connection, record_id, digest)
    finally:
        connection.close()


def test_the_unit_ingests_then_scores_and_reaches_nothing_paid_or_posting(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_posted_binary(config_file, "rec-a", question_id=45747, post_id=45556)
    seed_posted_binary(config_file, "rec-b", question_id=45748, post_id=45557)
    monkeypatch.setenv("METACULUS_TOKEN", "fake-token-for-tests")
    wire = install_wire(
        monkeypatch,
        {
            45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747),
            45557: kind_payload("binary", "resolved", post_id=45557, question_id=45748),
        },
    )

    assert run_unit(config_file) == [("ingest-resolutions", EXIT_OK), ("score", EXIT_OK)]
    out = capsys.readouterr().out
    assert "record rec-a  appended  kind resolved  scorable yes  -> resolved" in out
    assert "record rec-a  binary  appended  rows 2  platform appended  rows 4  -> scored" in out
    assert len(wire.gets) == 2
    assert wire.reached == []

    connection = connect(ledger_path(config_file))
    try:
        for record_id in ("rec-a", "rec-b"):
            assert current_status(connection, record_id) == "scored"
            scores = {row.metric: row.value for row in read_local_scores(connection, record_id)}
            resolution = latest_resolution(connection, record_id)
            assert resolution is not None and resolution.kind == "resolved"
            # Resolved "yes" against 0.7, by hand: (0.7 - 1)^2 = 0.09; ln(0.7) by `bc -l`.
            assert math.isclose(scores["local_brier_binary"], 0.09, rel_tol=1e-12)
            assert math.isclose(scores["local_log_binary"], -0.35667494393873237891, rel_tol=1e-12)
    finally:
        connection.close()

    # A second scheduled run writes nothing and still reaches nothing it must not.
    assert run_unit(config_file) == [("ingest-resolutions", EXIT_OK), ("score", EXIT_OK)]
    out = capsys.readouterr().out
    assert "unchanged" in out and "appended" not in out
    assert wire.reached == []


def test_scoring_does_not_run_after_an_ingestion_that_could_not_open_the_ledger(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", "fake-token-for-tests")
    wire = install_wire(monkeypatch, {})

    assert run_unit(config_file) == [("ingest-resolutions", EXIT_REFUSED)]
    assert not ledger_path(config_file).exists()

    # A file that is not a ledger at the configured path: refused, and not rewritten.
    database = ledger_path(config_file)
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"not a sqlite database, only its path")
    assert run_unit(config_file) == [("ingest-resolutions", EXIT_REFUSED)]
    assert database.read_bytes() == b"not a sqlite database, only its path"
    assert wire.gets == [] and wire.reached == []


def test_a_failed_record_fails_the_unit_and_scoring_waits(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One post the platform cannot return: the rest is recorded, the unit fails, score waits."""
    seed_posted_binary(config_file, "rec-a", question_id=45747, post_id=45556)
    seed_posted_binary(config_file, "rec-b", question_id=45748, post_id=45557)
    monkeypatch.setenv("METACULUS_TOKEN", "fake-token-for-tests")
    wire = install_wire(
        monkeypatch,
        {45556: kind_payload("binary", "resolved", post_id=45556, question_id=45747)},
    )

    assert run_unit(config_file) == [("ingest-resolutions", EXIT_REFUSED)]
    connection = connect(ledger_path(config_file))
    try:
        assert current_status(connection, "rec-a") == "resolved"
        assert read_local_scores(connection, "rec-a") == ()
    finally:
        connection.close()
    assert wire.reached == []


def test_a_missing_token_fails_the_unit_before_any_request(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_posted_binary(config_file, "rec-a", question_id=45747, post_id=45556)
    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    wire = install_wire(monkeypatch, {})

    assert run_unit(config_file) == [("ingest-resolutions", EXIT_ENV_MISSING)]
    assert wire.gets == [] and wire.reached == []
