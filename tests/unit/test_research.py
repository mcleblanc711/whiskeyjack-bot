"""M1-301: the research-run/document schema round-trips, closes its vocabularies,
rejects unusable provenance, hashes content deterministically, withholds inputs
from validation errors, and is storable by the ledger that migration 002 upgrades."""

import hashlib
import json
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path

import pytest

from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect, initialize_ledger
from whiskeyjack_bot.research import (
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    content_sha256,
    validate_document,
    validate_run,
)

TS = "2026-07-17T00:00:00+00:00"
SHA = "a" * 64


def _document(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "retrieval_run_id": "run-1",
        "original_url": "https://example.org/a?utm_source=x",
        "canonical_url": "https://example.org/a",
        "title": "Payrolls rose in June",
        "publisher": "Example Wire",
        "author": "A. Reporter",
        "published_at_utc": TS,
        "retrieved_at_utc": TS,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": SHA,
        "snippet": "Nonfarm payrolls rose.",
    }
    data.update(overrides)
    return data


def _run(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "retrieval_run_id": "run-1",
        "question_id": 100,
        "provider": "asknews",
        "queries": ["june payrolls"],
        "started_at_utc": TS,
    }
    data.update(overrides)
    return data


def _checksum_of(migration: str) -> str:
    """The checksum ledger.py records, computed the same way it computes it."""
    return hashlib.sha256(
        files("whiskeyjack_bot.migrations").joinpath(migration).read_bytes()
    ).hexdigest()


def _insert_run(conn: sqlite3.Connection, **overrides: object) -> None:
    """Insert a research_runs row directly, bypassing the models.

    Direct SQL on purpose: these tests assert what the *database* refuses, so
    they must be able to write rows the Pydantic models would never produce.
    """
    row: dict[str, object] = {
        "retrieval_run_id": "run-1",
        "question_id": 100,
        "provider": "asknews",
        "started_at_utc": TS,
        "created_at_utc": TS,
        "agent_model": None,
        "posts_dropped_no_url": None,
        "cost_usd": None,
    }
    row.update(overrides)
    placeholders = ", ".join("?" * len(row))
    conn.execute(
        f"INSERT INTO research_runs ({', '.join(row)}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def _insert_document(conn: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "document_id": "doc-1",
        "retrieval_run_id": "run-1",
        "original_url": "https://example.org/a",
        "canonical_url": "https://example.org/a",
        "retrieved_at_utc": TS,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": SHA,
        "reliability_tag": None,
    }
    row.update(overrides)
    placeholders = ", ".join("?" * len(row))
    conn.execute(
        f"INSERT INTO research_documents ({', '.join(row)}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def test_document_round_trips() -> None:
    doc = validate_document(_document())
    assert validate_document(doc.model_dump()) == doc


def test_run_round_trips() -> None:
    run = validate_run(_run(completed_at_utc=TS, cost_usd=0.02))
    assert validate_run(run.model_dump()) == run


def test_original_url_is_retained_alongside_canonical_url() -> None:
    # The backlog acceptance criterion: the schema preserves the original URL.
    doc = validate_document(_document())
    assert doc.original_url != doc.canonical_url
    assert doc.original_url.endswith("utm_source=x")


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(reliability="high"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_type", "blog"),
        ("provenance", "scraped"),
        ("reliability_tag", "probably_fine"),
    ],
)
def test_closed_vocabularies_reject_off_list_values(field: str, value: str) -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(**{field: value}))


def test_provider_vocabulary_is_closed() -> None:
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(provider="tavily"))


def test_naive_timestamp_is_rejected() -> None:
    # A naive timestamp is not valid provenance: freshness windows compare these
    # across providers in different zones.
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(retrieved_at_utc="2026-07-17T00:00:00"))


def test_aware_timestamp_is_normalized_to_utc() -> None:
    doc = validate_document(_document(retrieved_at_utc="2026-07-17T02:00:00+02:00"))
    assert doc.retrieved_at_utc == datetime(2026, 7, 17, tzinfo=timezone.utc)
    assert doc.retrieved_at_utc.tzinfo == timezone.utc


def test_malformed_content_hash_is_rejected() -> None:
    for bad in ("not-a-hash", SHA.upper(), "a" * 63):
        with pytest.raises(ResearchSchemaError):
            validate_document(_document(content_sha256=bad))


def test_completion_may_not_precede_start() -> None:
    earlier = (datetime.fromisoformat(TS) - timedelta(minutes=1)).isoformat()
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(completed_at_utc=earlier))


def test_negative_counters_are_rejected() -> None:
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(cost_usd=-0.01))
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(posts_dropped_no_url=-1))


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_cost_usd_must_be_finite(bad: float) -> None:
    """ge=0 caught -inf and NaN by accident, which made +inf look covered.

    It was not: +inf validated and then serialized to null, so a run with an
    unbounded cost persisted as one with no recorded cost at all.
    """
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(cost_usd=bad))


def test_finite_cost_usd_still_round_trips() -> None:
    assert json.loads(validate_run(_run(cost_usd=0.02)).model_dump_json())["cost_usd"] == 0.02
    assert json.loads(validate_run(_run(cost_usd=0)).model_dump_json())["cost_usd"] == 0


def test_content_hash_is_stable_across_cosmetic_variation() -> None:
    base = content_sha256("Payrolls rose in June.")
    assert content_sha256("  Payrolls\n rose\tin   June.  ") == base
    # NFC: composed vs decomposed accents are the same content.
    assert content_sha256("resumé") == content_sha256("resumé")
    # But real content change, including case, must not collapse.
    assert content_sha256("Payrolls fell in June.") != base
    assert content_sha256("payrolls rose in june.") != base


def test_content_hash_pins_a_known_digest() -> None:
    # Regression guard on the normalization rule itself: changing it breaks
    # replay, so it may only change as a new versioned function.
    assert content_sha256("  hello   world  ") == content_sha256("hello world")
    assert content_sha256("hello world") == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_validation_error_never_echoes_retrieved_content() -> None:
    # A research document carries arbitrary provider text; a credential pasted
    # into a fixture must not surface through a diagnostic or its traceback.
    secret = "privateFAKE123456"
    with pytest.raises(ResearchSchemaError) as excinfo:
        validate_document(_document(source_type=secret))
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert secret not in str(excinfo.value)
    assert secret not in rendered
    assert excinfo.value.__cause__ is None  # a chained ValidationError would re-leak


def test_validation_error_never_echoes_an_input_controlled_field_name() -> None:
    # The companion to the test above: include_input=False withholds the offending
    # *value*, but under extra="forbid" the error's location IS the caller's key.
    # A credential pasted as a key must be withheld exactly as one pasted as a value.
    secret = "privateFAKE123456"
    with pytest.raises(ResearchSchemaError) as excinfo:
        validate_document(_document(**{secret: "x"}))
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert secret not in str(excinfo.value)
    assert secret not in rendered
    assert excinfo.value.__cause__ is None
    # Still diagnostic: the caller learns an unexpected key was rejected.
    assert "Extra inputs are not permitted" in str(excinfo.value)


def test_validation_error_never_echoes_a_provider_config_key() -> None:
    # provider_config keys are caller-supplied too, and land in the same loc.
    secret = "privateFAKE123456"
    with pytest.raises(ResearchSchemaError) as excinfo:
        validate_run(_run(provider_config={secret: object()}))
    assert secret not in str(excinfo.value)
    assert "provider_config" in str(excinfo.value)


SECRET = "privateFAKE123456"


def _leaks(exc: ResearchSchemaError) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SECRET in str(exc) or SECRET in rendered


@pytest.mark.parametrize(
    "url",
    [
        # urlsplit embeds the offending netloc in its own ValueError for this one
        # (the NFKC-normalization check), which leaked straight through the
        # sanitizer: the loc was clean but the *message* was not.
        f"https://{SECRET}／example.com/a",
        f"https://{SECRET}\x00/a",
        f"https://example.org:{SECRET}/a",
        f"https://user:{SECRET}@/a",
        f"not-a-url-{SECRET}",
        f" https://example.org/{SECRET} ",
    ],
)
def test_url_rejection_never_echoes_the_url(url: str) -> None:
    with pytest.raises(ResearchSchemaError) as excinfo:
        validate_document(_document(original_url=url))
    assert not _leaks(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_no_field_leaks_a_planted_secret_through_any_message() -> None:
    """Blanket net: plant the secret in every field of both models, one at a time.

    The per-field tests above pin the cases that actually leaked; this one exists
    so that a *new* field, or a validator whose message stops being constant,
    cannot open a fresh channel without failing a test. It is deliberately
    indiscriminate about which fields reject the value -- any that accept it are
    simply skipped.
    """
    for factory, validate, model in (
        (_document, validate_document, ResearchDocument),
        (_run, validate_run, ResearchRun),
    ):
        for field in model.model_fields:
            for planted in (SECRET, [SECRET], {SECRET: SECRET}, f"https://example.org/{SECRET}"):
                try:
                    validate(factory(**{field: planted}))
                except ResearchSchemaError as exc:
                    assert not _leaks(exc), f"{model.__name__}.{field} leaked {planted!r}"


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_model": None, "posts_dropped_no_url": 0},
        {"agent_model": "   ", "posts_dropped_no_url": 0},
        {"agent_model": "grok-4", "posts_dropped_no_url": None},
        {"agent_model": None, "posts_dropped_no_url": None},
    ],
)
def test_agent_runs_must_account_for_themselves(overrides: dict[str, object]) -> None:
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(provider="xai_x_search", **overrides))


def test_zero_dropped_posts_is_distinct_from_unmeasured() -> None:
    run = validate_run(
        _run(provider="xai_x_search", agent_model="grok-4-fast", posts_dropped_no_url=0)
    )
    assert run.posts_dropped_no_url == 0
    assert run.posts_dropped_no_url is not None


def test_non_agent_providers_need_no_agent_identity() -> None:
    # The requirement is scoped to the provider that actually runs a model.
    run = validate_run(_run(provider="asknews"))
    assert run.agent_model is None
    assert run.posts_dropped_no_url is None


def test_agent_runs_must_account_for_themselves_even_when_they_failed() -> None:
    # agent_model is config-supplied (D27), so a failed run still knows it, and a
    # run that gathered nothing dropped nothing.
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(provider="xai_x_search", error_summary="upstream 503"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"provenance": "direct_api", "reliability_tag": "journalist"},
        {"provenance": "llm_reported", "reliability_tag": None},
        {"provenance": "direct_api", "reliability_tag": None},
    ],
)
def test_social_documents_must_be_agent_reported_and_tagged(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(source_type="social", **overrides))


def test_a_well_formed_social_document_is_accepted() -> None:
    doc = validate_document(
        _document(
            source_type="social",
            provenance="llm_reported",
            reliability_tag="unverified_social",
            original_url="https://x.com/someone/status/1234567890",
            canonical_url="https://x.com/someone/status/1234567890",
        )
    )
    assert doc.reliability_tag == "unverified_social"


def test_non_social_documents_may_omit_a_reliability_tag() -> None:
    # The tag is conditionally required, never unconditionally: most providers
    # have no trust model of their own.
    assert validate_document(_document()).reliability_tag is None


def test_provider_config_must_be_json_persistable() -> None:
    # The column is provider_config_json TEXT: a config that cannot serialize is
    # not storable, and must fail here rather than inside the ledger write.
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(provider_config={"session": object()}))


def test_json_persistable_provider_config_round_trips() -> None:
    config = {"max_results": 10, "categories": ["Business"], "nested": {"strategy": None}}
    run = validate_run(_run(provider_config=config))
    assert json.loads(run.model_dump_json())["provider_config"] == config


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "wrap",
    [
        lambda v: {"threshold": v},
        lambda v: {"nested": {"threshold": v}},
        lambda v: {"weights": [1.0, v]},
        lambda v: {"deep": [{"weights": [v]}]},
    ],
    ids=["top", "nested-dict", "list", "deep"],
)
def test_non_finite_provider_config_values_are_rejected(bad: float, wrap: object) -> None:
    """NaN/Inf validate as floats but serialize to null -- silent config drift.

    A run that validated as {"threshold": nan} would persist as
    {"threshold": null}, so replay reconstructs the run against a configuration
    that was never the one used. Checked at every nesting depth, because the
    dict[str, ...] annotation only constrains the outermost layer.
    """
    with pytest.raises(ResearchSchemaError):
        validate_run(_run(provider_config=wrap(bad)))  # type: ignore[operator]


def test_finite_floats_still_round_trip() -> None:
    # The guard rejects non-finite values only; ordinary floats are untouched.
    config = {"threshold": 0.75, "weights": [-1.5, 0.0, 1e300]}
    run = validate_run(_run(provider_config=config))
    assert json.loads(run.model_dump_json())["provider_config"] == config


@pytest.mark.parametrize(
    "url",
    [
        " ",
        "  https://example.org/a  ",
        "not a url",
        "example.org/a",
        "/relative/path",
        "ftp://example.org/a",
        "javascript:alert(1)",
        "https://",
        # netloc is non-empty but there is no host in it: userinfo only, or a
        # port only. Checking netloc alone accepted both.
        "https://:443/a",
        "https://user@/a",
        "https://user:pw@/a",
        # urlsplit silently deletes these, so the parsed host looks clean while
        # the string we would store still carries them.
        "https://exa\nmple.org/a",
        "https://exa\tmple.org/a",
        "https://exa\rmple.org/a",
        "https://example.org/\x00a",
        # Ports that cannot be dialled.
        "https://example.org:99999/a",
        "https://example.org:0/a",
        "https://example.org:-1/a",
        "https://example.org:notaport/a",
        # Whitespace *inside* the URL. A raw space is not a valid URI character,
        # and checking only the ends let these through. Written as escapes on
        # purpose: every case here is invisible or near-invisible in a source
        # listing, which is exactly why it is worth pinning.
        "https://exa mple.org/a",
        "https://example.org/a b",
        "https://exa\u00a0mple.org/a",  # NBSP -- Zs, not a control character
        # C1 controls (U+0080-U+009F). The hand-rolled set covered C0 and DEL
        # only, while its comment claimed to cover both ranges.
        "https://exa\u0085mple.org/a",  # NEL
        "https://exa\u009fmple.org/a",
    ],
)
def test_urls_must_be_absolute_http(url: str) -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(original_url=url))
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(canonical_url=url))


@pytest.mark.parametrize(
    "url",
    [
        # U+200C/U+200D are *required* between certain scripts' letters, so IDNA
        # accepts these labels and so must the schema. A blanket Cf ban did not.
        "https://\u0646\u0627\u0645\u0647\u200c\u0627\u06cc.ir/a",  # ZWNJ, Persian
        "https://\u0915\u094d\u200d\u0937.com/a",  # ZWJ, Devanagari
        "https://münchen.de/a",
        "https://例え.jp/a",
        "https://example.org/a",
    ],
)
def test_standards_valid_international_hostnames_are_accepted(url: str) -> None:
    assert validate_document(_document(original_url=url)).original_url == url


@pytest.mark.parametrize(
    "url",
    [
        # urlsplit strips the brackets, so an IPv6 authority reaches the check as
        # a bare "::1" -- which IDNA refuses. IPv4 had been passing only because
        # dotted digits happen to be acceptable IDNA labels, not because anything
        # checked it.
        "https://[::1]/a",
        "https://[2001:db8::1]:443/a",
        "https://[fe80::1]/a",
        "https://192.168.1.1/a",
        "https://8.8.8.8:8080/a",
    ],
)
def test_ip_literal_hosts_are_accepted(url: str) -> None:
    assert validate_document(_document(original_url=url)).original_url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1/a",  # unclosed bracket -- urlsplit itself refuses
        "https://[gg::1]/a",  # bracketed but not an address
        "https://[::1]:99999/a",  # IP literal does not exempt the port check
    ],
)
def test_malformed_ip_literal_hosts_are_rejected(url: str) -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(original_url=url))


@pytest.mark.parametrize(
    "url",
    [
        # Cf that IDNA rejects wherever it appears: valid in no context.
        "https://exa\u200bmple.org/a",  # zero-width space
        "https://ex\u202eample.org/a",  # right-to-left override
        "https://ex\u200emple.org/a",  # left-to-right mark
        # ZWNJ in a context the contextual rules do not permit.
        "https://ab\u200ccd.com/a",
        # Not a hostname at all once encoded.
        "https://-leading-hyphen.com/a",
        "https://exam ple.org/a",
    ],
)
def test_hostnames_idna_refuses_are_rejected(url: str) -> None:
    with pytest.raises(ResearchSchemaError):
        validate_document(_document(original_url=url))


def test_url_validation_does_not_rewrite_the_url() -> None:
    # Validation is not canonicalization (that is M1-305): the stored URL is the
    # one the provider returned, byte for byte, tracking parameters and all.
    messy = "https://example.org:443/a/b?utm_source=x&q=1#frag"
    doc = validate_document(_document(original_url=messy))
    assert doc.original_url == messy


def test_migration_002_makes_the_document_storable(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 2

    doc = validate_document(_document(document_id="doc-1"))
    run = validate_run(_run())
    conn = connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(research_documents)")}
        assert {"original_url", "provenance"} <= columns
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(research_runs)")}
        assert {"agent_model", "posts_dropped_no_url", "question_id"} <= run_columns

        _insert_run(
            conn,
            retrieval_run_id=run.retrieval_run_id,
            question_id=run.question_id,
            provider=run.provider,
        )
        _insert_document(
            conn,
            document_id=doc.document_id,
            retrieval_run_id=doc.retrieval_run_id,
            original_url=doc.original_url,
            canonical_url=doc.canonical_url,
            retrieved_at_utc=doc.retrieved_at_utc.isoformat(),
            source_type=doc.source_type,
            provenance=doc.provenance,
            content_sha256=doc.content_sha256,
        )
        stored = conn.execute(
            "SELECT original_url, provenance FROM research_documents WHERE document_id = 'doc-1'"
        ).fetchone()
        assert stored[0] == doc.original_url
        assert stored[1] == "direct_api"
    finally:
        conn.close()


def test_database_rejects_off_list_provenance(tmp_path: Path) -> None:
    # The CHECK is real, not merely a Pydantic-level convention.
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _insert_run(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(conn, provenance="scraped")
    finally:
        conn.close()


@pytest.mark.parametrize("missing", ["original_url", "provenance", "source_type"])
def test_database_rejects_provenance_less_documents(tmp_path: Path, missing: str) -> None:
    # The columns are NULLable so that pre-002 rows keep their honest NULLs, but a
    # *new* row may not be written without the fields that make it attributable.
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _insert_run(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(conn, **{missing: None})
    finally:
        conn.close()


def test_database_rejects_runs_without_a_question(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, question_id=None)
    finally:
        conn.close()


def test_database_rejects_unaccountable_agent_runs(tmp_path: Path) -> None:
    # Storage-level mirror of the D27 model invariant: an agent run names its
    # model and reports its dropped-citation count, or it is not written.
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        for overrides in (
            {"agent_model": None, "posts_dropped_no_url": 0},
            {"agent_model": "   ", "posts_dropped_no_url": 0},
            {"agent_model": "grok-4", "posts_dropped_no_url": None},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_run(conn, provider="xai_x_search", **overrides)
        _insert_run(conn, provider="xai_x_search", agent_model="grok-4", posts_dropped_no_url=0)
    finally:
        conn.close()


def test_database_rejects_impossible_counts(tmp_path: Path) -> None:
    """A negative dropped-citation count is an unfalsifiable accountability claim.

    The presence of the counter was already enforced; its value was not, so
    direct SQL could store -1 and make the run's citation hygiene unauditable in
    the other direction. cost_usd is guarded alongside it.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(
                conn, provider="xai_x_search", agent_model="grok-4", posts_dropped_no_url=-1
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, cost_usd=-0.01)
        # The boundary is allowed: zero dropped posts and a free run are real.
        _insert_run(
            conn,
            retrieval_run_id="run-ok",
            provider="xai_x_search",
            agent_model="grok-4",
            posts_dropped_no_url=0,
            cost_usd=0,
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        # SQLite column types are *affinity*, not constraints. A REAL that cannot
        # be losslessly narrowed stays REAL, and a non-numeric string stays TEXT
        # -- and TEXT sorts above every number, so `>= 0` is satisfied by
        # 'garbage'. Only typeof() actually pins the storage class.
        ("posts_dropped_no_url", 1.5),
        ("posts_dropped_no_url", "garbage"),
        ("cost_usd", "free"),
        ("cost_usd", float("inf")),
    ],
)
def test_database_rejects_wrongly_typed_counters(
    tmp_path: Path, column: str, value: object
) -> None:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, provider="xai_x_search", agent_model="grok-4", **{column: value})
    finally:
        conn.close()


@pytest.mark.parametrize("cost", [0, 0.02, 1.1e308, 1.7976931348623157e308])
def test_model_and_database_agree_on_valid_costs(tmp_path: Path, cost: float) -> None:
    """Anything the model accepts must be storable, up to the largest float.

    An earlier trigger used `> 1e308` as a stand-in for "infinite", which
    rejected finite costs the model had just validated -- validation passing and
    persistence failing is the model/table fidelity gap this schema exists to
    close. The trigger now tests SQLite's infinity sentinel instead, so the two
    definitions of "finite" are the same definition.
    """
    run = validate_run(_run(cost_usd=cost))
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _insert_run(conn, cost_usd=run.cost_usd)
        assert conn.execute("SELECT cost_usd FROM research_runs").fetchone()[0] == cost
    finally:
        conn.close()


def test_database_stores_counters_with_their_declared_type(tmp_path: Path) -> None:
    """The typeof() guards must not reject what affinity legitimately converts.

    Affinity conversion is only rejected where it would be *lossy*: SQLite
    narrows the numeric string '3' to integer 3, and widens integer 1 to real 1.0
    for a REAL column, both losslessly. Pinning this stops the guards from being
    tightened into something that refuses ordinary driver round-trips.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _insert_run(
            conn,
            provider="xai_x_search",
            agent_model="grok-4",
            posts_dropped_no_url="3",
            cost_usd=1,
        )
        row = conn.execute(
            "SELECT posts_dropped_no_url, typeof(posts_dropped_no_url), "
            "cost_usd, typeof(cost_usd) FROM research_runs"
        ).fetchone()
        assert tuple(row) == (3, "integer", 1.0, "real")
    finally:
        conn.close()


def test_database_enforces_the_social_trust_contract(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _insert_run(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(
                conn, source_type="social", provenance="direct_api", reliability_tag="journalist"
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(
                conn, source_type="social", provenance="llm_reported", reliability_tag=None
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(conn, reliability_tag="trustworthy")
        _insert_document(
            conn,
            source_type="social",
            provenance="llm_reported",
            reliability_tag="unverified_social",
        )
    finally:
        conn.close()


def test_database_rejects_nulling_out_provenance_by_update(tmp_path: Path) -> None:
    # Insert-only enforcement would let a valid row be hollowed out afterwards.
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _insert_run(conn)
        _insert_document(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE research_documents SET provenance = NULL")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE research_runs SET question_id = NULL")
    finally:
        conn.close()


def test_rows_written_before_migration_002_survive_it(tmp_path: Path) -> None:
    """The triggers must not retroactively invalidate the 001-era ledger.

    Defaulting the new columns would have stamped a false provenance claim onto
    these rows; rejecting them would have made the migration undeployable. They
    keep their NULLs and stay readable, and only *new* rows must be complete.
    """
    db = tmp_path / "ledger.sqlite3"
    conn = connect(db)
    try:
        # Apply 001 alone, then write the kind of row it permitted.
        conn.executescript(
            files("whiskeyjack_bot.migrations").joinpath("001_initial.sql").read_text()
        )
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, started_at_utc, "
            "created_at_utc) VALUES ('legacy-run', 'asknews', ?, ?)",
            (TS, TS),
        )
        conn.execute(
            "INSERT INTO research_documents (document_id, retrieval_run_id, canonical_url, "
            "retrieved_at_utc, content_sha256) "
            "VALUES ('legacy-doc', 'legacy-run', 'https://example.org/old', ?, ?)",
            (TS, SHA),
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at_utc, checksum) VALUES (1, ?, ?)",
            (TS, _checksum_of("001_initial.sql")),
        )
    finally:
        conn.close()

    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION

    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT canonical_url, original_url, provenance FROM research_documents "
            "WHERE document_id = 'legacy-doc'"
        ).fetchone()
        assert tuple(row) == ("https://example.org/old", None, None)
        # And the same row still cannot be created from here on.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(conn, retrieval_run_id="legacy-run", provenance=None)
    finally:
        conn.close()


def test_reapplying_migrations_is_a_no_op(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    conn = connect(db)
    try:
        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert applied == LEDGER_SCHEMA_VERSION
    finally:
        conn.close()


def test_models_are_importable_without_the_questions_package() -> None:
    # M1-301 stays decoupled from M1-201: a run references its question by id.
    assert ResearchRun.model_fields["question_id"].annotation is int
    assert "document_id" in ResearchDocument.model_fields
