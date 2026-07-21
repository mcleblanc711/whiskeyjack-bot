"""M1-302 acceptance: a mocked AskNews call returns normalized documents with
article-level provenance, and missing credentials fail before any paid call.

The suite runs under three independent network guards (pytest-socket, the DNS
refusal in tests/conftest.py, and the socket.connect refusal in
tests/unit/conftest.py), so "no network was reached" is enforced rather than
asserted. Real ``SearchResponseDictItem`` objects are used as fixtures so this
breaks if the pinned asknews DTO shape drifts.
"""

import copy
import logging
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from asknews_sdk import AskNewsSDK
from pydantic import AnyUrl
from asknews_sdk.dto.base import Author, Entities
from asknews_sdk.dto.news import SearchResponse, SearchResponseDictItem

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.logging_setup import SecretRedactionFilter, configure_logging
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.research.asknews import (
    AskNewsRetrievalError,
    build_asknews_client,
    retrieve_news,
)
from whiskeyjack_bot.research.hashing import content_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_KEY = "fakeASKNEWSkey123456"
# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic
# secret shape here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    return validate_config_data(data)


def _article(**overrides: Any) -> SearchResponseDictItem:
    """Build a valid AskNews article; overrides replace individual fields."""
    data: dict[str, Any] = {
        "article_url": AnyUrl("https://example.org/june-payrolls"),
        "article_id": uuid.UUID(int=1),
        "classification": ["Business"],
        "country": "US",
        "source_id": "Example Wire",
        "page_rank": 3,
        "domain_url": "example.org",
        "eng_title": "June payrolls beat expectations",
        "entities": Entities(),
        "keywords": ["payrolls"],
        "language": "en",
        "pub_date": datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc),
        "summary": "Nonfarm payrolls rose more than forecast.",
        "title": "June payrolls beat expectations",
        "sentiment": 0,
        "as_string_key": "k1",
        "crawl_date": datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
        "full_text": "Nonfarm payrolls rose by 250,000 in June.",
        "authors": [Author(name="A. Reporter", email=None, url=None)],
    }
    data.update(overrides)
    return SearchResponseDictItem.model_construct(**data)


class _FakeNewsAPI:
    """Returns the nth canned response for the nth call, repeating the last one.

    Repeating matters: with a single canned response both the current and the
    historical pass return the same article, which is the real overlap the
    adapter has to collapse. Pass an explicit second entry to avoid that.
    """

    def __init__(self, responses: list[list[SearchResponseDictItem]]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def search_news(self, **kwargs: Any) -> SearchResponse:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return SearchResponse.model_construct(as_dicts=self._responses[index])


class _FakeSDK:
    """Stand-in for AskNewsSDK; only the .news seam is exercised."""

    def __init__(self, responses: list[list[SearchResponseDictItem]]) -> None:
        self.news = _FakeNewsAPI(responses)


class _ExplodingNewsAPI:
    def search_news(self, **kwargs: Any) -> SearchResponse:
        raise RuntimeError(f"upstream said no; auth header Bearer {SECRET}")


class _ExplodingSDK:
    def __init__(self) -> None:
        self.news = _ExplodingNewsAPI()


def _retrieve(sdk: Any, config: AppConfig, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "question_id": 42,
        "queries": ["june payrolls"],
        "retrieval_run_id": "run-1",
        "now": NOW,
    }
    kwargs.update(overrides)
    return retrieve_news(sdk, config, **kwargs)


# --- credential boundary: the item's safety property ------------------------


def test_missing_api_key_raises_before_any_network(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASKNEWS_API_KEY", raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        build_asknews_client(config)
    assert excinfo.value.env_var_name == "ASKNEWS_API_KEY"


def test_empty_api_key_counts_as_missing(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASKNEWS_API_KEY", "")
    with pytest.raises(MissingCredentialError):
        build_asknews_client(config)


def test_client_construction_is_io_free(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing the SDK must not touch the network; the guards are the assertion."""
    monkeypatch.setenv("ASKNEWS_API_KEY", FAKE_KEY)
    client = build_asknews_client(config)
    assert isinstance(client, AskNewsSDK)


def test_api_key_absent_from_repr_and_str(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASKNEWS_API_KEY", FAKE_KEY)
    client = build_asknews_client(config)
    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in str(client)


def test_custom_api_key_env_name_honored(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var name comes from config, not a hardcoded constant."""
    data = config.model_dump()
    data["retrieval"]["primary"]["api_key_env"] = "OTHER_ASKNEWS_KEY"
    custom = validate_config_data(data)
    monkeypatch.delenv("OTHER_ASKNEWS_KEY", raising=False)
    with pytest.raises(MissingCredentialError) as excinfo:
        build_asknews_client(custom)
    assert excinfo.value.env_var_name == "OTHER_ASKNEWS_KEY"


def test_timeout_and_retries_reach_the_sdk(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASKNEWS_API_KEY", FAKE_KEY)
    client = build_asknews_client(config)
    assert client.client.timeout == config.retrieval.primary.timeout_seconds
    assert client.client.retries == config.retrieval.primary.retries


# --- the headline criterion: normalized documents ---------------------------


def test_mocked_search_returns_normalized_documents(config: AppConfig) -> None:
    sdk = _FakeSDK([[_article()]])
    result = _retrieve(sdk, config)

    assert len(result.documents) == 1
    doc = result.documents[0]
    assert doc.original_url == "https://example.org/june-payrolls"
    assert doc.canonical_url == doc.original_url  # M1-305 derives the real one
    assert doc.title == "June payrolls beat expectations"
    assert doc.publisher == "Example Wire"
    assert doc.author == "A. Reporter"
    assert doc.published_at_utc == datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)
    assert doc.updated_at_utc == datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    assert doc.retrieved_at_utc == NOW
    assert doc.source_type == "news"
    assert doc.provenance == "direct_api"
    assert doc.snippet == "Nonfarm payrolls rose more than forecast."
    assert doc.summary is None
    assert doc.reliability_tag is None
    # Writer-owned fields stay unset for the adapter to fill in later.
    assert doc.document_id is None
    assert doc.raw_artifact_path is None


def test_content_hash_prefers_full_text(config: AppConfig) -> None:
    """The pinned rule is full_text > summary > title; drifting changes identity."""
    sdk = _FakeSDK([[_article()]])
    doc = _retrieve(sdk, config).documents[0]
    assert doc.content_sha256 == content_sha256("Nonfarm payrolls rose by 250,000 in June.")


def test_content_hash_falls_back_through_summary_to_title(config: AppConfig) -> None:
    sdk = _FakeSDK([[_article(full_text=None)]])
    assert _retrieve(sdk, config).documents[0].content_sha256 == content_sha256(
        "Nonfarm payrolls rose more than forecast."
    )

    sdk = _FakeSDK([[_article(full_text=None, summary="")]])
    assert _retrieve(sdk, config).documents[0].content_sha256 == content_sha256(
        "June payrolls beat expectations"
    )


def test_run_records_provenance_and_no_agent_identity(config: AppConfig) -> None:
    # One article on the current pass, nothing on the historical pass, so this
    # run has no drops and no collapses and error_summary must stay None.
    sdk = _FakeSDK([[_article()], []])
    run = _retrieve(sdk, config).run

    assert run.provider == "asknews"
    assert run.question_id == 42
    assert run.queries == ["june payrolls"]
    assert run.started_at_utc == NOW
    assert run.freshness_cutoff_utc == NOW - timedelta(days=config.retrieval.freshness_days_default)
    assert run.error_summary is None
    # AskNews reports credits, not currency; an invented USD figure would be an
    # unearned number in the ledger.
    assert run.cost_usd is None
    # Agent-only fields belong to the X adapter (M1-307).
    assert run.agent_model is None
    assert run.posts_dropped_no_url is None


def test_raw_responses_are_returned_not_persisted(config: AppConfig, tmp_path: Path) -> None:
    """M1-306 owns disk persistence; this adapter writes nothing."""
    sdk = _FakeSDK([[_article()]])
    result = _retrieve(sdk, config)
    assert result.raw_responses
    assert result.run.raw_response_path is None
    assert all(d.raw_artifact_path is None for d in result.documents)


# --- current + historical passes --------------------------------------------


def test_both_current_and_historical_strategies_are_queried(config: AppConfig) -> None:
    sdk = _FakeSDK([[_article()]])
    _retrieve(sdk, config)
    strategies = [c["strategy"] for c in sdk.news.calls]
    assert strategies == ["latest news", "news knowledge"]
    assert [c["historical"] for c in sdk.news.calls] == [False, True]


def test_config_parameters_plumb_through(config: AppConfig) -> None:
    sdk = _FakeSDK([[_article()]])
    _retrieve(sdk, config, queries=["a", "b", "c", "d", "e", "f", "g", "h"])

    # Queries capped, two strategy passes each.
    assert len(sdk.news.calls) == config.retrieval.max_queries_per_question * 2
    call = sdk.news.calls[0]
    assert call["n_articles"] == config.retrieval.max_documents_per_query
    assert call["hours_back"] == config.retrieval.freshness_days_default * 24
    assert call["return_type"] == "dicts"


# --- failure paths that must not fail the run -------------------------------


def test_intra_run_duplicates_are_collapsed(config: AppConfig) -> None:
    """The two passes overlap by design; UNIQUE(run, url, hash) would reject the pair."""
    sdk = _FakeSDK([[_article()], [_article()]])
    result = _retrieve(sdk, config)
    assert len(result.documents) == 1
    assert result.run.error_summary is not None
    assert "duplicate" in result.run.error_summary


def test_unusable_article_is_dropped_not_fatal(config: AppConfig) -> None:
    good = _article()
    bad = _article(article_url="not-a-url", as_string_key="k2")
    sdk = _FakeSDK([[bad, good]])

    result = _retrieve(sdk, config)

    assert len(result.documents) == 1
    assert result.documents[0].original_url == "https://example.org/june-payrolls"
    assert result.run.error_summary is not None
    assert "dropped" in result.run.error_summary


def test_zero_documents_is_recorded_not_raised(config: AppConfig) -> None:
    sdk = _FakeSDK([[]])
    result = _retrieve(sdk, config)
    assert result.documents == ()
    assert result.run.error_summary is not None
    assert "no documents retained" in result.run.error_summary


def test_provider_failure_raises_sanitized_error(config: AppConfig) -> None:
    with pytest.raises(AskNewsRetrievalError) as excinfo:
        _retrieve(_ExplodingSDK(), config)
    assert not _leaks(excinfo.value)
    assert excinfo.value.__cause__ is None


# --- secret hygiene ---------------------------------------------------------


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for needle in (SECRET, FAKE_KEY):
        if needle in str(exc) or needle in rendered:
            return True
    return False


@pytest.mark.parametrize(
    "field",
    ["article_url", "eng_title", "title", "summary", "full_text", "source_id", "domain_url"],
    ids=lambda f: f"planted-in-{f}",
)
def test_planted_secret_never_reaches_an_error_or_the_run(config: AppConfig, field: str) -> None:
    """Provider text is untrusted: no field of it may surface in a message.

    Fields that validate successfully are fine -- the point is that the failing
    ones do not echo, and that nothing lands in provider_config either.
    """
    sdk = _FakeSDK([[_article(**{field: SECRET})]])
    try:
        result = _retrieve(sdk, config)
    except Exception as exc:  # noqa: BLE001 - the assertion is about any raise
        assert not _leaks(exc), f"{field} leaked through a raised error"
        return
    assert SECRET not in str(result.run.provider_config)
    assert SECRET not in (result.run.error_summary or "")


def test_api_key_never_reaches_the_run_or_documents(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASKNEWS_API_KEY", FAKE_KEY)
    sdk = _FakeSDK([[_article()]])
    result = _retrieve(sdk, config)
    assert FAKE_KEY not in result.run.model_dump_json()
    assert all(FAKE_KEY not in d.model_dump_json() for d in result.documents)


def test_configured_logging_redacts_the_asknews_key(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ASKNEWS_API_KEY", FAKE_KEY)
    configure_logging(config)
    # Simulate the third-party SDK logger leaking the key.
    logging.getLogger("asknews_sdk.client").warning("auth header: Bearer %s", FAKE_KEY)
    captured = capsys.readouterr()
    file_text = config.logging.file.read_text(encoding="utf-8")
    assert FAKE_KEY not in captured.err
    assert FAKE_KEY not in file_text
    assert "<redacted:ASKNEWS_API_KEY>" in file_text


def test_redaction_filter_covers_the_asknews_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASKNEWS_API_KEY", FAKE_KEY)
    record = logging.LogRecord("any", logging.INFO, __file__, 1, "key is %s", (FAKE_KEY,), None)
    SecretRedactionFilter(["ASKNEWS_API_KEY"]).filter(record)
    assert FAKE_KEY not in record.getMessage()
