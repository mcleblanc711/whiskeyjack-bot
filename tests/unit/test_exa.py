"""M1-303 acceptance: the Exa fallback records why it ran, preserves citations,
and cannot be invoked as a silent provider switch.

Every call goes through ``httpx.MockTransport``, so the suite exercises the real
request-building and response-parsing path with no network at all -- and the
three independent network guards (pytest-socket, the DNS refusal in
tests/conftest.py, the socket.connect refusal in tests/unit/conftest.py) enforce
that rather than leaving it asserted. The response bodies below are written in
the shape verified against https://api.exa.ai/openapi.json on 2026-07-27.
"""

import copy
import json
import logging
import traceback
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.logging_setup import SecretRedactionFilter, configure_logging
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.research import exa
from whiskeyjack_bot.research.canonical import canonicalize_url
from whiskeyjack_bot.research.exa import (
    ExaFallbackError,
    ExaRetrieval,
    build_exa_client,
    decide_fallback,
    retrieve_web,
)
from whiskeyjack_bot.research.hashing import content_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_KEY = "fakeEXAkey1234567890"
# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic
# secret shape here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
REASONS: tuple[exa.FallbackReason, ...] = ("primary_provider_failed",)


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    return validate_config_data(data)


def _result(**overrides: Any) -> dict[str, Any]:
    """Build a valid Exa search result; overrides replace individual fields."""
    result: dict[str, Any] = {
        "id": "https://example.org/june-payrolls",
        "title": "June payrolls beat expectations",
        "url": "https://example.org/june-payrolls",
        "publishedDate": "2026-07-20T09:30:00.000Z",
        "author": "A. Reporter",
        "text": "Nonfarm payrolls rose by 250,000 in June.",
        "image": "https://example.org/img.png",
        "favicon": "https://example.org/favicon.ico",
    }
    result.update(overrides)
    return result


def _body(*results: dict[str, Any], cost: Any = 0.005) -> dict[str, Any]:
    body: dict[str, Any] = {
        "requestId": "b5947044c4b78efa9552a7c89b306d95",
        "results": list(results),
    }
    if cost is not None:
        body["costDollars"] = {"total": cost}
    return body


class _Exchange:
    """A MockTransport handler that replays canned responses and records requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "payload": json.loads(request.content.decode("utf-8")),
                "headers": dict(request.headers),
            }
        )
        if not self._responses:
            return httpx.Response(200, json=_body())
        return self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)


def _json_ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def _client(handler: _Exchange) -> httpx.Client:
    return httpx.Client(base_url="https://api.exa.ai", transport=httpx.MockTransport(handler))


def _retrieve(
    handler: _Exchange,
    config: AppConfig,
    *,
    queries: list[str] | None = None,
    reasons: tuple[exa.FallbackReason, ...] = REASONS,
    include_domains: tuple[str, ...] = (),
) -> ExaRetrieval:
    return retrieve_web(
        _client(handler),
        config,
        question_id=42,
        queries=["june payrolls"] if queries is None else queries,
        retrieval_run_id="run-1",
        now=NOW,
        fallback_reasons=reasons,
        include_domains=include_domains,
    )


# --- the fallback policy ----------------------------------------------------


@pytest.mark.parametrize(
    ("failed", "documents", "official", "expected"),
    [
        (False, 3, False, ()),
        (True, 3, False, ("primary_provider_failed",)),
        # Zero documents alone is not a trigger: AskNews succeeded, and no
        # official-source/web retrieval was requested (finding: PR #16 round 1).
        (False, 0, False, ()),
        (False, 3, True, ("official_source_required",)),
        (True, 0, False, ("primary_provider_failed", "primary_returned_no_documents")),
        (True, 3, True, ("primary_provider_failed", "official_source_required")),
        (False, 0, True, ("primary_returned_no_documents", "official_source_required")),
        (
            True,
            0,
            True,
            (
                "primary_provider_failed",
                "primary_returned_no_documents",
                "official_source_required",
            ),
        ),
    ],
)
def test_decide_fallback_reports_every_trigger(
    failed: bool, documents: int, official: bool, expected: tuple[str, ...]
) -> None:
    """Every fact is recorded once the fallback runs; zero documents alone never runs it."""
    decision = decide_fallback(
        primary_failed=failed,
        primary_documents=documents,
        official_source_required=official,
    )
    assert decision.reasons == expected
    assert decision.should_run is bool(expected)


def test_decide_fallback_does_not_run_when_the_primary_succeeded() -> None:
    decision = decide_fallback(
        primary_failed=False, primary_documents=5, official_source_required=False
    )
    assert decision.should_run is False
    assert decision.reasons == ()


def test_decide_fallback_does_not_run_on_empty_primary_alone() -> None:
    """A successful-but-empty AskNews run is not, by itself, an AskNews failure."""
    decision = decide_fallback(
        primary_failed=False, primary_documents=0, official_source_required=False
    )
    assert decision.should_run is False
    assert decision.reasons == ()


@pytest.mark.parametrize("documents", [-1, True, 1.5, "3", None])
def test_decide_fallback_rejects_a_count_that_is_not_a_count(documents: Any) -> None:
    """Including bool, which is an int subclass and would read as 1 document."""
    with pytest.raises(ExaFallbackError):
        decide_fallback(
            primary_failed=False, primary_documents=documents, official_source_required=False
        )


# --- no silent provider switching -------------------------------------------


def test_retrieve_web_refuses_a_call_with_no_recorded_reason(config: AppConfig) -> None:
    handler = _Exchange()
    with pytest.raises(ExaFallbackError):
        _retrieve(handler, config, reasons=())
    assert handler.requests == [], "refusal must happen before any billable call"


def test_retrieve_web_refuses_a_reason_outside_the_vocabulary(config: AppConfig) -> None:
    handler = _Exchange()
    with pytest.raises(ExaFallbackError):
        _retrieve(handler, config, reasons=("because_i_felt_like_it",))  # type: ignore[arg-type]
    assert handler.requests == []


def test_reasons_are_persisted_on_the_run(config: AppConfig) -> None:
    """The acceptance criterion: the ledger alone says why the fallback ran."""
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(
        handler,
        config,
        reasons=("official_source_required", "primary_provider_failed"),
    )
    assert result.run.provider_config is not None
    # Deduplicated and in vocabulary order, so identical triggers persist
    # identically regardless of how the caller assembled them.
    assert result.run.provider_config["fallback_reasons"] == [
        "primary_provider_failed",
        "official_source_required",
    ]
    assert result.fallback_reasons == ("primary_provider_failed", "official_source_required")


def test_repeated_reasons_are_collapsed(config: AppConfig) -> None:
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(
        handler,
        config,
        reasons=("primary_provider_failed", "primary_provider_failed"),
    )
    assert result.fallback_reasons == ("primary_provider_failed",)


def test_client_refuses_to_build_when_config_names_another_fallback(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running Exa while config says AskNews *is* the silent switch."""
    data = config.model_dump()
    data["retrieval"]["fallback"]["provider"] = "asknews"
    custom = validate_config_data(data)
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)
    with pytest.raises(ExaFallbackError):
        build_exa_client(custom)


def test_provider_mismatch_is_checked_before_the_credential(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = config.model_dump()
    data["retrieval"]["fallback"]["provider"] = "asknews"
    custom = validate_config_data(data)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(ExaFallbackError):
        build_exa_client(custom)


def test_client_refuses_when_the_primary_is_not_asknews(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exa naming itself as primary too would let it 'fall back' to itself."""
    data = config.model_dump()
    data["retrieval"]["primary"]["provider"] = "exa"
    custom = validate_config_data(data)
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)
    with pytest.raises(ExaFallbackError):
        build_exa_client(custom)


def test_primary_mismatch_is_checked_before_the_credential(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = config.model_dump()
    data["retrieval"]["primary"]["provider"] = "exa"
    custom = validate_config_data(data)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(ExaFallbackError):
        build_exa_client(custom)


def test_fallback_engagement_is_logged_without_provider_text(
    config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    handler = _Exchange(_json_ok(_body(_result())))
    with caplog.at_level(logging.INFO, logger="whiskeyjack_bot.research.exa"):
        _retrieve(handler, config, queries=["june payrolls"])
    messages = [record.getMessage() for record in caplog.records]
    assert any("exa fallback engaged" in m and "primary_provider_failed" in m for m in messages)
    # Constants and the question id only: no query text, no URLs.
    assert all("june payrolls" not in m for m in messages)
    assert all("example.org" not in m for m in messages)


def test_engagement_is_logged_even_when_every_call_fails(
    config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    handler = _Exchange(httpx.Response(500))
    with caplog.at_level(logging.INFO, logger="whiskeyjack_bot.research.exa"):
        result = _retrieve(handler, config)
    assert result.provider_failed is True
    assert any("exa fallback engaged" in r.getMessage() for r in caplog.records)


# --- credentials and transport ----------------------------------------------


def test_missing_credential_fails_before_any_call(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        build_exa_client(config)
    assert excinfo.value.env_var_name == "EXA_API_KEY"


def test_empty_credential_counts_as_missing(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "")
    with pytest.raises(MissingCredentialError):
        build_exa_client(config)


def test_custom_api_key_env_name_honored(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var name comes from config, not a hardcoded constant."""
    data = config.model_dump()
    data["retrieval"]["fallback"]["api_key_env"] = "OTHER_EXA_KEY"
    custom = validate_config_data(data)
    monkeypatch.delenv("OTHER_EXA_KEY", raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        build_exa_client(custom)
    assert excinfo.value.env_var_name == "OTHER_EXA_KEY"


def test_client_carries_the_key_and_the_configured_timeout(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)
    client = build_exa_client(config)
    assert client.headers["x-api-key"] == FAKE_KEY
    assert client.timeout.connect == config.retrieval.fallback.timeout_seconds
    assert str(client.base_url) == "https://api.exa.ai"


def test_retries_reach_the_actual_transport(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As in M1-302: assert where retrying happens, not where a value is stored."""
    data = config.model_dump()
    data["retrieval"]["fallback"]["retries"] = 7
    custom = validate_config_data(data)
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)

    client = build_exa_client(custom)

    transport = client._transport
    assert isinstance(transport, httpx.HTTPTransport)
    assert transport._pool._retries == 7


def test_retries_do_not_disable_env_proxy_routing(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applying retries must not cost HTTP(S)_PROXY routing (M1-302 round 2/3).

    Construction does no network I/O, so this stays under the socket guards;
    setting HTTPS_PROXY only wires up transports.
    """
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:8080")
    for var in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    client = build_exa_client(config)

    selected = client._transport_for_url(httpx.URL("https://api.exa.ai/search"))
    assert selected is not client._transport, "Exa traffic did not route through the proxy mount"


# --- the headline criterion: citations preserved ----------------------------


def test_search_returns_normalized_documents(config: AppConfig) -> None:
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(handler, config)

    assert len(result.documents) == 1
    doc = result.documents[0]
    assert doc.original_url == "https://example.org/june-payrolls"
    assert doc.canonical_url == canonicalize_url("https://example.org/june-payrolls")
    assert doc.title == "June payrolls beat expectations"
    assert doc.author == "A. Reporter"
    assert doc.published_at_utc == datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)
    assert doc.retrieved_at_utc == NOW
    assert doc.source_type == "web"
    assert doc.provenance == "direct_api"
    assert doc.snippet == "Nonfarm payrolls rose by 250,000 in June."
    # Exa returns no publisher, and deriving one from the host would put an
    # unasserted value in the ledger.
    assert doc.publisher is None
    assert doc.summary is None
    assert doc.reliability_tag is None
    assert doc.updated_at_utc is None
    # Writer-owned fields stay unset for M1-602 to fill in.
    assert doc.document_id is None
    assert doc.raw_artifact_path is None


def test_each_result_keeps_its_own_citation(config: AppConfig) -> None:
    """The point of per-document normalization: no flattened digest."""
    handler = _Exchange(
        _json_ok(
            _body(
                _result(),
                _result(
                    url="https://other.example/cpi",
                    title="CPI cooled",
                    author="B. Writer",
                    text="Consumer prices rose 0.1% in June.",
                ),
            )
        )
    )
    result = _retrieve(handler, config)
    assert [d.original_url for d in result.documents] == [
        "https://example.org/june-payrolls",
        "https://other.example/cpi",
    ]
    assert [d.title for d in result.documents] == ["June payrolls beat expectations", "CPI cooled"]
    assert [d.author for d in result.documents] == ["A. Reporter", "B. Writer"]


def test_canonical_url_is_derived_not_copied(config: AppConfig) -> None:
    """M1-305 owns canonicalization; the adapter calls it (canonical.py contract)."""
    tracked = "https://example.org/june-payrolls?utm_source=newsletter&id=42"
    handler = _Exchange(_json_ok(_body(_result(url=tracked))))
    doc = _retrieve(handler, config).documents[0]
    assert doc.original_url == tracked
    assert doc.canonical_url == "https://example.org/june-payrolls?id=42"


def test_content_hash_prefers_text_then_title(config: AppConfig) -> None:
    """The pinned rule is text > title; drifting from it changes document identity."""
    handler = _Exchange(_json_ok(_body(_result())))
    assert _retrieve(handler, config).documents[0].content_sha256 == content_sha256(
        "Nonfarm payrolls rose by 250,000 in June."
    )

    handler = _Exchange(_json_ok(_body(_result(text=""))))
    assert _retrieve(handler, config).documents[0].content_sha256 == content_sha256(
        "June payrolls beat expectations"
    )


def test_highlights_are_not_hashed(config: AppConfig) -> None:
    """Highlights are query-dependent, so hashing them would break dedup."""
    handler = _Exchange(_json_ok(_body(_result(text="", title="", highlights=["a highlight"]))))
    doc = _retrieve(handler, config).documents[0]
    assert doc.content_sha256 == content_sha256("")


def test_snippet_is_bounded(config: AppConfig) -> None:
    long_text = "x" * (exa._SNIPPET_CHARACTERS + 500)
    handler = _Exchange(_json_ok(_body(_result(text=long_text))))
    doc = _retrieve(handler, config).documents[0]
    assert doc.snippet is not None
    assert len(doc.snippet) == exa._SNIPPET_CHARACTERS
    # The hash still covers the full retrieved text, not the stored excerpt.
    assert doc.content_sha256 == content_sha256(long_text)


# --- request shape ----------------------------------------------------------


def test_request_matches_the_verified_contract(config: AppConfig) -> None:
    handler = _Exchange(_json_ok(_body(_result())))
    _retrieve(handler, config)

    assert len(handler.requests) == 1
    request = handler.requests[0]
    assert request["url"] == "https://api.exa.ai/search"
    payload = request["payload"]
    assert payload["query"] == "june payrolls"
    assert payload["type"] == "auto"
    assert payload["numResults"] == config.retrieval.max_documents_per_query
    assert (
        payload["startPublishedDate"]
        == (NOW - timedelta(days=config.retrieval.freshness_days_default)).isoformat()
    )
    assert payload["contents"] == {
        "text": {"maxCharacters": exa._TEXT_MAX_CHARACTERS},
        "maxAgeHours": exa._MAX_AGE_HOURS,
    }
    # livecrawl is deprecated in favour of maxAgeHours and must not be sent.
    assert "livecrawl" not in payload["contents"]
    assert "includeDomains" not in payload


def test_queries_are_capped_at_the_configured_maximum(config: AppConfig) -> None:
    handler = _Exchange(_json_ok(_body()))
    result = _retrieve(handler, config, queries=[f"q{i}" for i in range(20)])
    assert len(handler.requests) == config.retrieval.max_queries_per_question
    assert result.run.queries == [f"q{i}" for i in range(config.retrieval.max_queries_per_question)]


def test_over_returned_results_are_sliced(config: AppConfig) -> None:
    """numResults is the provider's promise; the ledger's cost is ours."""
    over = [_result(url=f"https://example.org/{i}", text=f"body {i}") for i in range(20)]
    handler = _Exchange(_json_ok(_body(*over)))
    result = _retrieve(handler, config)
    assert len(result.documents) == config.retrieval.max_documents_per_query


def test_num_results_is_capped_at_the_exa_maximum(config: AppConfig) -> None:
    """A configured value above Exa's documented ceiling must not reach the request."""
    data = config.model_dump()
    data["retrieval"]["max_documents_per_query"] = 250
    custom = validate_config_data(data)
    over = [_result(url=f"https://example.org/{i}", text=f"body {i}") for i in range(150)]
    handler = _Exchange(_json_ok(_body(*over)))

    result = retrieve_web(
        _client(handler),
        custom,
        question_id=42,
        queries=["june payrolls"],
        retrieval_run_id="run-1",
        now=NOW,
        fallback_reasons=REASONS,
    )

    assert handler.requests[0]["payload"]["numResults"] == 100
    assert len(result.documents) == 100
    assert result.run.provider_config is not None
    assert result.run.provider_config["num_results"] == 100


# --- official vs web --------------------------------------------------------


def test_domain_allowlist_promotes_documents_to_official(config: AppConfig) -> None:
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(handler, config, include_domains=("bls.gov",))
    assert handler.requests[0]["payload"]["includeDomains"] == ["bls.gov"]
    assert all(d.source_type == "official" for d in result.documents)
    assert result.run.provider_config is not None
    assert result.run.provider_config["include_domains"] == ["bls.gov"]


def test_official_reason_alone_does_not_make_a_document_official(config: AppConfig) -> None:
    """The allowlist earns the label; the reason does not."""
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(handler, config, reasons=("official_source_required",))
    assert all(d.source_type == "web" for d in result.documents)


@pytest.mark.parametrize("domains", [("",), ("   ",), (None,), (42,)])
def test_malformed_domain_allowlist_is_refused_before_any_call(
    config: AppConfig, domains: tuple[Any, ...]
) -> None:
    handler = _Exchange()
    with pytest.raises(ExaFallbackError):
        _retrieve(handler, config, include_domains=domains)
    assert handler.requests == []


# --- the run record ---------------------------------------------------------


def test_run_records_provider_and_configuration(config: AppConfig) -> None:
    handler = _Exchange(_json_ok(_body(_result())))
    run = _retrieve(handler, config).run

    assert run.provider == "exa"
    assert run.question_id == 42
    assert run.queries == ["june payrolls"]
    assert run.started_at_utc == NOW
    assert run.freshness_cutoff_utc == NOW - timedelta(days=config.retrieval.freshness_days_default)
    assert run.error_summary is None
    # Agent-only fields belong to the X adapter (M1-307).
    assert run.agent_model is None
    assert run.posts_dropped_no_url is None


def test_raw_responses_are_returned_not_persisted(config: AppConfig) -> None:
    """M1-306 owns disk persistence; this adapter writes nothing."""
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(handler, config)
    assert result.raw_responses
    assert result.raw_responses[0]["requestId"] == "b5947044c4b78efa9552a7c89b306d95"
    assert result.run.raw_response_path is None
    assert all(d.raw_artifact_path is None for d in result.documents)


def test_provider_config_survives_the_persisted_json_round_trip(config: AppConfig) -> None:
    """provider_config maps to a TEXT column; a value that cannot round-trip is a defect."""
    handler = _Exchange(_json_ok(_body(_result())))
    run = _retrieve(handler, config, include_domains=("bls.gov",)).run
    restored = json.loads(json.dumps(run.model_dump(mode="json"), ensure_ascii=True))
    assert restored["provider_config"]["fallback_reasons"] == ["primary_provider_failed"]
    assert restored["provider_config"]["include_domains"] == ["bls.gov"]


# --- cost -------------------------------------------------------------------


def test_cost_is_summed_across_calls(config: AppConfig) -> None:
    handler = _Exchange(
        _json_ok(_body(_result(), cost=0.005)),
        _json_ok(_body(_result(url="https://example.org/b", text="b"), cost=0.01)),
    )
    result = _retrieve(handler, config, queries=["a", "b"])
    assert result.run.cost_usd == pytest.approx(0.015)


def test_missing_cost_block_records_no_cost(config: AppConfig) -> None:
    handler = _Exchange(_json_ok(_body(_result(), cost=None)))
    assert _retrieve(handler, config).run.cost_usd is None


@pytest.mark.parametrize("bad", [True, "0.005", -1.0, [0.005], {"total": 0.005}])
def test_unusable_cost_values_are_ignored(config: AppConfig, bad: Any) -> None:
    """True is an int subclass and would otherwise be recorded as one dollar."""
    handler = _Exchange(_json_ok(_body(_result(), cost=bad)))
    assert _retrieve(handler, config).run.cost_usd is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_cost_literals_are_ignored(config: AppConfig, literal: str) -> None:
    """Reachable: these are not valid JSON, but ``json.loads`` accepts them.

    A stored non-finite cost would validate nowhere -- ``ResearchRun.cost_usd``
    requires a finite number and the ledger trigger rejects the rest -- so the
    run would pass validation here and fail at write time, after the money was
    spent. httpx cannot *encode* these, so the body is sent as raw content.
    """
    content = (
        '{"requestId": "x", "results": [], "costDollars": {"total": ' + literal + "}}"
    ).encode("utf-8")
    handler = _Exchange(
        httpx.Response(200, content=content, headers={"content-type": "application/json"})
    )
    result = _retrieve(handler, config)
    assert result.run.cost_usd is None
    assert result.provider_failed is False


def test_a_usable_cost_survives_an_unusable_one(config: AppConfig) -> None:
    handler = _Exchange(
        _json_ok(_body(_result(), cost="nonsense")),
        _json_ok(_body(_result(url="https://example.org/b", text="b"), cost=0.02)),
    )
    result = _retrieve(handler, config, queries=["a", "b"])
    assert result.run.cost_usd == pytest.approx(0.02)


@pytest.mark.parametrize("huge", [10**400, -(10**400)])
def test_oversized_integer_cost_is_ignored_not_a_crash(config: AppConfig, huge: int) -> None:
    """A JSON integer too large for a float must not crash an already-paid run.

    ``float(total)`` raises ``OverflowError`` for these; unrelated to the network
    call itself, so if this escaped it would crash the whole run, not just drop
    one result (PR #16 round-1 finding).
    """
    handler = _Exchange(_json_ok(_body(_result(), cost=huge)))
    result = _retrieve(handler, config)
    assert result.run.cost_usd is None
    assert result.provider_failed is False


def test_call_cost_usd_never_raises_on_an_oversized_integer() -> None:
    assert exa._call_cost_usd({"costDollars": {"total": 10**400}}) is None
    assert exa._call_cost_usd({"costDollars": {"total": -(10**400)}}) is None


# --- published dates --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-20T09:30:00.000Z", datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)),
        ("2026-07-20T09:30:00z", datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)),
        ("2026-07-20T09:30:00+05:30", datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)),
        # Date-only (the documented format) and offset-less values are pinned to
        # midnight UTC of the stated date.
        ("2026-07-20", datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)),
        ("2026-07-20T09:30:00", datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)),
        ("  2026-07-20  ", datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)),
        # Unparseable: undated, which M1-305 treats as stale -- the safe direction.
        ("not a date", None),
        ("", None),
        (None, None),
        (20260720, None),
        ({"date": "2026-07-20"}, None),
        # Syntactically valid boundary timestamps whose UTC conversion overflows
        # datetime's representable range (PR #16 round-1 finding): unusable, same
        # as any other bad date, not a crash of the whole run.
        ("0001-01-01T00:00:00+14:00", None),
        ("9999-12-31T23:59:59-14:00", None),
    ],
)
def test_published_date_parsing(config: AppConfig, raw: Any, expected: datetime | None) -> None:
    handler = _Exchange(_json_ok(_body(_result(publishedDate=raw))))
    result = _retrieve(handler, config)
    assert len(result.documents) == 1, "a bad date must not cost the citation"
    assert result.documents[0].published_at_utc == expected


def test_published_at_utc_never_raises_on_a_boundary_overflow() -> None:
    assert exa._published_at_utc("0001-01-01T00:00:00+14:00") is None
    assert exa._published_at_utc("9999-12-31T23:59:59-14:00") is None


# --- failure paths that must not fail the run -------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(429),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["not", "a", "mapping"]),
        httpx.Response(200, json={"requestId": "x"}),
        httpx.Response(200, json={"requestId": "x", "results": "not a list"}),
    ],
    ids=["500", "429", "malformed-json", "non-mapping", "no-results-key", "results-not-a-list"],
)
def test_provider_failure_is_recorded_never_raised(
    config: AppConfig, response: httpx.Response
) -> None:
    result = _retrieve(_Exchange(response), config)
    assert result.provider_failed is True
    assert result.documents == ()
    assert result.run.provider == "exa"
    assert result.run.error_summary is not None
    assert "provider call failed" in result.run.error_summary


def test_a_failed_call_keeps_what_earlier_calls_paid_for(config: AppConfig) -> None:
    """Raising partway would discard the record of calls already billed."""
    handler = _Exchange(
        _json_ok(_body(_result(), cost=0.005)),
        httpx.Response(500),
        _json_ok(_body(_result(url="https://example.org/never", text="never"))),
    )
    result = _retrieve(handler, config, queries=["a", "b", "c"])

    assert result.provider_failed is True
    assert len(result.documents) == 1
    assert len(result.raw_responses) == 1
    assert result.run.cost_usd == pytest.approx(0.005)
    # Retrieval stopped rather than grinding through the remaining queries.
    assert len(handler.requests) == 2
    assert result.run.error_summary is not None
    assert "provider call failed" in result.run.error_summary


def test_a_recorded_body_survives_a_missing_results_key(config: AppConfig) -> None:
    """The call was billed, so its body is kept even though the shape is wrong."""
    handler = _Exchange(
        httpx.Response(200, json={"requestId": "x", "costDollars": {"total": 0.005}})
    )
    result = _retrieve(handler, config)
    assert result.raw_responses and result.raw_responses[0]["requestId"] == "x"
    assert result.run.cost_usd == pytest.approx(0.005)
    assert result.provider_failed is True


def test_zero_documents_is_recorded_not_raised(config: AppConfig) -> None:
    result = _retrieve(_Exchange(_json_ok(_body())), config)
    assert result.documents == ()
    assert result.provider_failed is False
    assert result.run.error_summary is not None
    assert "no documents retained" in result.run.error_summary


@pytest.mark.parametrize(
    "bad",
    [
        {"title": "no url"},
        {"url": "not-a-url", "title": "t"},
        {"url": 42, "title": "t"},
        {"url": "https://exa mple.org/a", "title": "t"},
        "not even a mapping",
        None,
    ],
    ids=["no-url", "relative-url", "url-not-a-string", "url-with-space", "not-a-mapping", "null"],
)
def test_unusable_result_is_dropped_without_marking_the_run_failed(
    config: AppConfig, bad: Any
) -> None:
    handler = _Exchange(_json_ok({"requestId": "x", "results": [bad, _result()]}))
    result = _retrieve(handler, config)

    assert len(result.documents) == 1
    assert result.documents[0].original_url == "https://example.org/june-payrolls"
    assert result.documents_dropped == 1
    # One bad result among good ones is routine, not a run failure.
    assert result.provider_failed is False
    assert result.run.error_summary is None


def test_intra_run_duplicates_are_collapsed_without_marking_the_run_failed(
    config: AppConfig,
) -> None:
    """Two queries can surface one page; UNIQUE(run, url, hash) would reject the pair."""
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(handler, config, queries=["a", "b"])

    assert len(result.documents) == 1
    assert result.duplicates_collapsed == 1
    assert result.documents_dropped == 0
    assert result.provider_failed is False
    assert result.run.error_summary is None


def test_tracking_parameters_do_not_defeat_the_collapse(config: AppConfig) -> None:
    """Canonicalization is what makes two reports of one page collapse."""
    handler = _Exchange(
        _json_ok(_body(_result())),
        _json_ok(_body(_result(url="https://example.org/june-payrolls?utm_source=x"))),
    )
    result = _retrieve(handler, config, queries=["a", "b"])
    assert len(result.documents) == 1
    assert result.duplicates_collapsed == 1


# --- secret hygiene ---------------------------------------------------------


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for needle in (SECRET, FAKE_KEY):
        if needle in str(exc) or needle in rendered:
            return True
    return False


@pytest.mark.parametrize(
    "field",
    ["url", "title", "author", "text", "publishedDate", "id", "image", "favicon"],
    ids=lambda f: f"planted-in-{f}",
)
def test_planted_secret_never_reaches_any_egress_channel(config: AppConfig, field: str) -> None:
    """Provider text is untrusted: no field of it may surface in a message.

    Warnings are watched as well as exceptions, because pydantic's serializer
    warnings embed the offending *value* in their text and go to stderr and to
    captured logs -- a distinct egress channel from a raise (M1-302 round 1).
    """
    handler = _Exchange(_json_ok(_body(_result(**{field: SECRET}))))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = _retrieve(handler, config)
        except Exception as exc:  # noqa: BLE001 - the assertion is about any raise
            assert not _leaks(exc), f"{field} leaked through a raised error"
            return

    for warning in caught:
        text = str(warning.message)
        assert SECRET not in text, f"{field} leaked through a {warning.category.__name__}"
        assert FAKE_KEY not in text

    assert SECRET not in str(result.run.provider_config)
    assert SECRET not in (result.run.error_summary or "")


def test_a_query_never_reaches_the_run_error_or_the_log(
    config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """A query is caller content and can carry anything, including a pasted key."""
    handler = _Exchange(httpx.Response(500))
    with caplog.at_level(logging.DEBUG):
        result = _retrieve(handler, config, queries=[SECRET])
    assert SECRET not in (result.run.error_summary or "")
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    # It does reach `queries`, which is the field that exists to record it.
    assert result.run.queries == [SECRET]


def test_api_key_never_reaches_the_run_or_documents(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)
    handler = _Exchange(_json_ok(_body(_result())))
    result = _retrieve(handler, config)
    assert FAKE_KEY not in result.run.model_dump_json()
    assert all(FAKE_KEY not in d.model_dump_json() for d in result.documents)


def test_configured_logging_redacts_the_exa_key(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)
    configure_logging(config)
    logging.getLogger("httpx").warning("x-api-key: %s", FAKE_KEY)
    captured = capsys.readouterr()
    file_text = config.logging.file.read_text(encoding="utf-8")
    assert FAKE_KEY not in captured.err
    assert FAKE_KEY not in file_text
    assert "<redacted:EXA_API_KEY>" in file_text


def test_redaction_filter_covers_the_exa_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", FAKE_KEY)
    record = logging.LogRecord("any", logging.INFO, __file__, 1, "key is %s", (FAKE_KEY,), None)
    SecretRedactionFilter(["EXA_API_KEY"]).filter(record)
    assert FAKE_KEY not in record.getMessage()
