"""Regression reproductions for PR 75, starting at 287c105; all providers are fake."""

from __future__ import annotations

import asyncio
import json
import signal
import sqlite3
from datetime import timedelta
from typing import Any

import httpx
import pytest
import yaml

from tests.unit.test_tournament import case as tournament_case, poll, _prepare_version
from whiskeyjack_bot.cli import main
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.ledger import connect
from whiskeyjack_bot.tournament_state import (
    Budget,
    StorageFailure,
    TournamentError,
    budget_context,
    events,
    spending,
    utcnow,
)


@pytest.fixture
def case(tmp_path: Any) -> Any:
    yield from tournament_case.__wrapped__(tmp_path)


@pytest.fixture(autouse=True)
def quiet_cli_logging(monkeypatch: Any) -> None:
    # CLI logging configuration otherwise leaves handlers pointing at pytest capture.
    monkeypatch.setattr("whiskeyjack_bot.logging_setup.configure_logging", lambda config: None)


def config_file(config: Any, tmp_path: Any) -> str:
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    return str(path)


@pytest.mark.parametrize("command", ["disable", "status", "run-once", "reconcile-restored"])
def test_missing_ledger_is_never_created(case: Any, tmp_path: Any, command: str) -> None:
    config = case[1]
    config = config.model_copy(
        update={
            "storage": config.storage.model_copy(
                update={"sqlite_path": tmp_path / "missing.sqlite"}
            )
        }
    )
    result = main(["tournament", command, "--config", config_file(config, tmp_path)])
    assert not config.storage.sqlite_path.exists()
    assert result != 0


@pytest.mark.parametrize("change", ["config", "prompt", "missing_prompt", "storage"])
def test_status_rejects_invalid_activation(
    case: Any, tmp_path: Any, capsys: Any, change: str
) -> None:
    conn, config, *_ = case
    if change == "config":
        config = config.model_copy(
            update={"run_limits": config.run_limits.model_copy(update={"max_questions": 2})}
        )
    elif change == "prompt":
        config.forecast.prompt_path.write_text("changed prompt")
    elif change == "missing_prompt":
        config.forecast.prompt_path.unlink()
    else:
        budget = Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000)
        budget.reserve("provider", 0.1, {})
        next((config.storage.artifact_root / "operations").glob("*.json")).unlink()
    result = main(["tournament", "status", "--config", config_file(config, tmp_path)])
    output = json.loads(capsys.readouterr().out)
    assert output["enabled"] is False
    assert output["refusal_reason"]
    assert result != 0


def test_valid_shutdown_and_inactive_status(case: Any, tmp_path: Any, capsys: Any) -> None:
    args = ["--config", config_file(case[1], tmp_path)]
    assert main(["tournament", "disable", *args]) == 0
    capsys.readouterr()
    assert main(["tournament", "status", *args]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False


@pytest.mark.parametrize("provider", ["asknews", "exa", "model"])
def test_phase_timeout_escapes_provider_handlers(
    case: Any, monkeypatch: Any, provider: str
) -> None:
    from whiskeyjack_bot.timeouts import phase_timeout
    from whiskeyjack_bot.research.asknews import retrieve_news
    from whiskeyjack_bot.research.exa import retrieve_web
    from whiskeyjack_bot.forecast.priced import PricedClient

    conn, config, platform, news, model = case

    def expire(*args: Any, **kwargs: Any) -> Any:
        signal.raise_signal(signal.SIGALRM)
        raise AssertionError("deadline was swallowed")

    previous = signal.getsignal(signal.SIGALRM)
    with pytest.raises(TournamentError, match="wall-clock timeout"):
        with phase_timeout(60):
            if provider == "asknews":
                monkeypatch.setattr(news, "search_news", expire)
                retrieve_news(
                    news,
                    config,
                    question_id=1,
                    queries=["a", "b"],
                    retrieval_run_id="timeout",
                    now=utcnow(),
                )
            elif provider == "exa":
                with httpx.Client(
                    base_url="https://api.exa.ai", transport=httpx.MockTransport(expire)
                ) as client:
                    retrieve_web(
                        client,
                        config,
                        question_id=1,
                        queries=["a", "b"],
                        retrieval_run_id="timeout",
                        now=utcnow(),
                        fallback_reasons=["primary_provider_failed"],
                    )
            else:
                monkeypatch.setenv(config.model.api_key_env, "fake")
                monkeypatch.setattr(httpx.AsyncClient, "post", expire)
                asyncio.run(PricedClient(config).invoke([]))
            pytest.fail("provider swallowed the deadline and allowed subsequent work")
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


@pytest.mark.parametrize("corrupt", ["{", '{"artifact_schema_version":"1.0.0","provider":" "}'])
def test_corrupt_research_blocks_post(case: Any, corrupt: str) -> None:
    from whiskeyjack_bot.submission_live import post_approved_forecast
    from whiskeyjack_bot.submission_payload import authorized_payload

    conn, config, platform, *_ = case
    record = read_forecast_record(conn, _prepare_version(case))
    path = conn.execute("SELECT raw_response_path FROM research_runs LIMIT 1").fetchone()[0]
    (config.storage.artifact_root / path).write_text(corrupt)
    with pytest.raises(StorageFailure, match="research.*artifact"):
        post_approved_forecast(
            conn,
            config=config,
            record_id=record.record_id,
            poster=platform,
            payload=authorized_payload(record, calibration=config.numeric_calibration).payload,
        )
    assert platform.posts == 0


def test_restore_spending_blocks_repeat_purchase(case: Any, tmp_path: Any) -> None:
    from whiskeyjack_bot.restore import reconcile_restored
    from whiskeyjack_bot.tournament_state import disable, enable

    conn, config, platform, news, model = case
    backup = tmp_path / "backup.sqlite"
    with sqlite3.connect(backup) as snapshot:
        conn.backup(snapshot)
    Budget(conn, config.storage.artifact_root, "42:32977", 20_000_000).reserve("exa", 0.05, {})
    # Retained guard is newer than both the restored ledger and artifact tree.
    for path in (config.storage.artifact_root / "operations").glob("*.json"):
        path.unlink()
    with connect(backup) as restored:
        assert reconcile_restored(restored, config, platform)["imported_witnesses"] == 1
        assert reconcile_restored(restored, config, platform)["imported_witnesses"] == 0
        assert spending(restored, "42:32977") == (0, 50_000)
        assert len(events(restored, "restored_spending_hold", "42:32977")) == 1
    with connect(backup) as restored:
        disable(restored)
        enable(
            restored,
            config,
            account_id=42,
            project_id=32977,
            starts=utcnow() - timedelta(minutes=1),
            ends=utcnow() + timedelta(days=1),
        )
        with pytest.raises(TournamentError, match="spending.*hold"):
            Budget(restored, config.storage.artifact_root, "42:32977", 20_000_000).reserve(
                "exa", 0.05, {}
            )
        result = poll((restored, config, platform, news, model))
        assert result["spending_held"] is True
        assert result["heartbeat"]["failures"] > 0
        assert news.calls == model.calls == platform.posts == platform.comment_posts == 0


@pytest.mark.parametrize("provider", ["exa", "asknews"])
def test_retrieval_cache_counts_only_new_requests(case: Any, provider: str) -> None:
    from whiskeyjack_bot.research.exa import retrieve_web
    from whiskeyjack_bot.research.asknews import retrieve_news

    conn, config, _, news, _ = case
    now = utcnow()
    calls = []

    def respond(request: Any) -> Any:
        calls.append(request)
        return httpx.Response(200, json={"results": [], "costDollars": {"total": 0.01}})

    budget = Budget(conn, config.storage.artifact_root, "42:32977", 20_000_000)
    with httpx.Client(
        base_url="https://api.exa.ai", transport=httpx.MockTransport(respond)
    ) as client:

        def retrieve(run: str, queries: list[str]) -> Any:
            kwargs = dict(question_id=1, queries=queries, retrieval_run_id=run, now=now)
            if provider == "exa":
                return retrieve_web(
                    client, config, fallback_reasons=["primary_provider_failed"], **kwargs
                )
            return retrieve_news(news, config, **kwargs)

        with budget_context(budget):
            first = retrieve("first", ["a"])
            cached = retrieve("cached", ["a"])
            mixed = retrieve("mixed", ["a", "b"])
    assert first.calls_attempted == (1 if provider == "exa" else 2)
    assert cached.calls_attempted == 0
    assert cached.run.cost_usd == 0
    assert mixed.calls_attempted == first.calls_attempted
    if provider == "exa":
        assert mixed.run.cost_usd == 0.01
        assert len(calls) == 2
    else:
        assert news.calls == 4


@pytest.mark.parametrize("provider", ["model", "retrieval"])
def test_completion_before_settlement_recovers_once(
    case: Any, monkeypatch: Any, provider: str
) -> None:
    from whiskeyjack_bot.forecast.priced import PricedClient
    from whiskeyjack_bot.research.durable import begin_call, complete_call

    conn, config, *_ = case
    budget = Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000)
    calls = []

    def respond(request: Any) -> Any:
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "saved"}}], "usage": {"cost": 0.01}}
        )

    original = httpx.AsyncClient
    monkeypatch.setenv(config.model.api_key_env, "fake")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )

    class Crash(BaseException):
        pass

    settle = Budget.settle

    def crash(*args: Any, **kwargs: Any) -> None:
        raise Crash

    monkeypatch.setattr(Budget, "settle", crash)

    def invoke() -> Any:
        if provider == "model":
            return asyncio.run(PricedClient(config).invoke([]))
        scope, cached = begin_call("exa", 0.05, {"query": "a"}, 1, "cutoff")
        if cached is None:
            complete_call(scope, {"results": [], "costDollars": {"total": 0.01}}, 0.01)
        return cached

    with budget_context(budget), pytest.raises(Crash):
        invoke()
    assert spending(conn, budget.scope)[1] > 0
    monkeypatch.setattr(Budget, "settle", settle)
    with connect(config.storage.sqlite_path) as restarted:
        with budget_context(Budget(restarted, budget.root, budget.scope, budget.ceiling)):
            # Retrieval closure reads its connection exclusively from budget_context.
            invoke()
            invoke()
    assert spending(conn, budget.scope) == (10_000, 0)
    assert len(events(conn, "cost_settled", budget.scope)) == 1
    assert len(calls) == (1 if provider == "model" else 0)


def test_mixed_evidence_is_filtered_before_generation(case: Any, monkeypatch: Any) -> None:
    from whiskeyjack_bot.research.store import load_packet
    from whiskeyjack_bot.research.quality import usable

    conn, config, platform, news, model = case
    search = news.search_news

    def mixed(**kwargs: Any) -> Any:
        response = search(**kwargs)
        fresh = response.as_dicts[0]
        stale = fresh.model_copy(deep=True)
        stale.article_url = "https://example.org/stale"
        stale.pub_date = stale.crawl_date = utcnow() - timedelta(days=365)
        irrelevant = fresh.model_copy(deep=True)
        irrelevant.article_url = "https://example.org/irrelevant"
        irrelevant.eng_title = irrelevant.title = "Unrelated sports"
        irrelevant.summary = "Football tournament result"
        future = fresh.model_copy(deep=True)
        future.article_url = "https://example.org/future"
        future.pub_date = future.crawl_date = utcnow() + timedelta(days=2)
        response.as_dicts = [fresh, response.as_dicts[1], stale, irrelevant, future]
        return response

    monkeypatch.setattr(news, "search_news", mixed)
    import whiskeyjack_bot.pipeline_live as pipeline

    generate = pipeline.generate_forecast
    seen = []

    def checked(**kwargs: Any) -> Any:
        packet = kwargs["packet"]
        seen.extend(packet.documents)
        assert all(
            usable(d, kwargs["question"], kwargs["now"], config.retrieval.freshness_days_default)
            for d in packet.documents
        )
        return generate(**kwargs)

    monkeypatch.setattr(pipeline, "generate_forecast", checked)
    result = poll(case)
    assert seen
    assert result["heartbeat"]["failures"] == 0
    assert platform.posts == 1
    record = read_forecast_record(
        conn, conn.execute("SELECT record_id FROM forecast_records").fetchone()[0]
    )
    runs = tuple(r[0] for r in conn.execute("SELECT retrieval_run_id FROM research_runs"))
    original = load_packet(conn, question_id=record.question_id, retrieval_run_ids=runs)
    assert len(original.documents) > len(seen)
    from whiskeyjack_bot.research.packet import packet_sha256
    from whiskeyjack_bot.research.quality import without_future, usable_packet
    from whiskeyjack_bot.submission_policy import require_research_artifacts

    hashes = set()
    for packet in (
        original,
        without_future(original, record.generated_at_utc),
        usable_packet(
            original,
            record.question,
            record.generated_at_utc,
            config.retrieval.freshness_days_default,
        ),
    ):
        saved_hash = packet_sha256(packet)
        hashes.add(saved_hash)
        require_research_artifacts(
            conn, config, record.model_copy(update={"research_packet_sha256": saved_hash})
        )
    assert len(hashes) == 3
    assert record.research_packet_sha256 == packet_sha256(
        usable_packet(
            original,
            record.question,
            record.generated_at_utc,
            config.retrieval.freshness_days_default,
        )
    )


def test_restore_hold_commits_before_witness_and_survives_interruption(
    case: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    import whiskeyjack_bot.restore as restore

    conn, config, platform, *_ = case
    backup = tmp_path / "backup.sqlite"
    with sqlite3.connect(backup) as snapshot:
        conn.backup(snapshot)
    Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000).reserve("exa", 0.05, {})
    write = restore.write_new_file

    def crash(*args: Any, **kwargs: Any) -> None:
        raise StorageFailure("interrupted artifact write")

    monkeypatch.setattr(restore, "write_new_file", crash)
    with connect(backup) as restored:
        with pytest.raises(StorageFailure, match="interrupted"):
            restore.reconcile_restored(restored, config, platform)
        assert len(events(restored, "restored_spending_hold", "42:32977")) == 1
        assert spending(restored, "42:32977") == (0, 50_000)
        assert (
            restored.execute(
                "SELECT count(*) FROM tournament_events WHERE kind='witness'"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(restore, "write_new_file", write)
    with connect(backup) as restored:
        restore.reconcile_restored(restored, config, platform)
        restore.reconcile_restored(restored, config, platform)
        assert len(events(restored, "restored_spending_hold", "42:32977")) == 1
        assert spending(restored, "42:32977") == (0, 50_000)


def test_spending_hold_allows_read_only_external_recovery(case: Any) -> None:
    from whiskeyjack_bot.tournament_state import append
    from whiskeyjack_bot.tournament import comment_text

    conn, _, platform, news, model = case
    platform.hide_forecast = True
    poll(case)
    assert platform.posts == 1 and platform.comment_posts == 0
    rid = conn.execute("SELECT record_id FROM forecast_records").fetchone()[0]
    append(conn, "restored_spending_hold", "42:32977", {"reservation_id": "uncertain"})
    calls = news.calls, model.calls
    platform.hide_forecast = False
    result = poll(case)
    assert result["forecast_confirmed"] == 1
    assert result["heartbeat"]["failures"] > 0
    assert platform.comment_posts == 0
    platform.comments.append(
        {
            "id": 1,
            "on_post": platform.raw["id"],
            "author": 42,
            "is_private": True,
            "text": comment_text(conn, rid),
        }
    )
    result = poll(case)
    assert result["comment_completed"] == 1
    assert result["spending_held"] is True
    assert platform.posts == 1 and platform.comment_posts == 0
    assert (news.calls, model.calls) == calls


def test_concurrent_settlement_records_one_charge(case: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    conn, config, *_ = case
    budget = Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000)
    reservation = budget.reserve("exa", 0.05, {})
    ready = Barrier(2)

    def recover() -> None:
        with connect(config.storage.sqlite_path) as worker:
            ready.wait(timeout=10)
            Budget(worker, budget.root, budget.scope, budget.ceiling).settle(reservation, 0.01)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    assert spending(conn, budget.scope) == (10_000, 0)
    assert len(events(conn, "cost_settled", budget.scope)) == 1


@pytest.mark.parametrize("provider", ["asknews", "exa", "model"])
def test_timeout_records_question_failure_without_subsequent_paid_work(
    case: Any, monkeypatch: Any, provider: str
) -> None:
    from whiskeyjack_bot.tournament import run_once

    conn, config, platform, news, model = case
    calls = []

    def expire(*args: Any, **kwargs: Any) -> Any:
        calls.append(provider)
        signal.raise_signal(signal.SIGALRM)

    if provider == "asknews":
        monkeypatch.setattr(news, "search_news", expire)
    elif provider == "exa":
        news.stale = True
    else:
        monkeypatch.setattr(model, "invoke", expire)
    with httpx.Client(
        base_url="https://api.exa.ai", transport=httpx.MockTransport(expire)
    ) as client:
        result = run_once(
            conn,
            config,
            client=platform,
            poster=platform,
            news_client=news,
            web_client=client,
            forecaster=model,
        )
    assert calls == [provider]
    assert model.calls == platform.posts == platform.comment_posts == 0
    assert result["heartbeat"]["failures"] == 1
    failures = events(conn, "question_failure", f"32977:{platform.raw['question']['id']}")
    assert failures[-1]["error_type"] == "TournamentError"
