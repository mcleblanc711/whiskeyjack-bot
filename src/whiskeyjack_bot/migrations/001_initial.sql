-- M1-601: initial attribution-ledger schema.
--
-- SQLite is the v1 source of truth (decision D16). The ledger is append-only
-- and replayable: forecasts, their evidence, approvals, submission attempts,
-- resolutions and scores (decision D25 -- never overwrite history).
--
-- Connection-level settings (WAL journal, foreign_keys, busy_timeout) are set
-- per connection in whiskeyjack_bot.ledger, not here: PRAGMAs are not part of
-- the persisted schema and several are ignored inside a transaction.
--
-- Timestamps are TEXT ISO-8601 UTC, matching the snapshot convention.

CREATE TABLE forecast_records (
    record_id             TEXT PRIMARY KEY,
    question_id           INTEGER NOT NULL,
    post_id               INTEGER,
    tournament_id         TEXT NOT NULL,
    forecast_version      INTEGER NOT NULL,
    parent_record_id      TEXT REFERENCES forecast_records (record_id),
    question_type         TEXT NOT NULL,
    question_domain       TEXT,
    status                TEXT NOT NULL CHECK (
        status IN ('draft', 'validated', 'approved', 'submitted', 'failed', 'resolved', 'scored')
    ),
    model_provider        TEXT NOT NULL,
    model_name            TEXT NOT NULL,
    prompt_version        TEXT NOT NULL,
    prompt_sha256         TEXT NOT NULL,
    retrieval_run_id      TEXT NOT NULL REFERENCES research_runs (retrieval_run_id),
    generated_at_utc      TEXT NOT NULL,
    final_prediction_json TEXT NOT NULL,
    record_json           TEXT NOT NULL,
    created_at_utc        TEXT NOT NULL,
    UNIQUE (question_id, tournament_id, forecast_version)
);

CREATE TABLE research_runs (
    retrieval_run_id     TEXT PRIMARY KEY,
    provider             TEXT NOT NULL,
    provider_config_json TEXT,
    queries_json         TEXT,
    started_at_utc       TEXT NOT NULL,
    completed_at_utc     TEXT,
    freshness_cutoff_utc TEXT,
    raw_response_path    TEXT,
    error_summary        TEXT,
    cost_usd             REAL,
    created_at_utc       TEXT NOT NULL
);

CREATE TABLE research_documents (
    document_id       TEXT PRIMARY KEY,
    retrieval_run_id  TEXT NOT NULL REFERENCES research_runs (retrieval_run_id),
    canonical_url     TEXT NOT NULL,
    title             TEXT,
    publisher         TEXT,
    author            TEXT,
    published_at_utc  TEXT,
    updated_at_utc    TEXT,
    retrieved_at_utc  TEXT NOT NULL,
    source_type       TEXT,
    content_sha256    TEXT NOT NULL,
    snippet           TEXT,
    summary           TEXT,
    raw_artifact_path TEXT,
    reliability_tag   TEXT,
    UNIQUE (retrieval_run_id, canonical_url, content_sha256)
);

CREATE TABLE approval_events (
    event_id           INTEGER PRIMARY KEY,
    forecast_record_id TEXT NOT NULL REFERENCES forecast_records (record_id),
    decision           TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    actor              TEXT NOT NULL,
    forecast_sha256    TEXT NOT NULL,
    note               TEXT,
    created_at_utc     TEXT NOT NULL
);

CREATE TABLE submission_attempts (
    attempt_id                  TEXT PRIMARY KEY,
    forecast_record_id          TEXT NOT NULL REFERENCES forecast_records (record_id),
    idempotency_key             TEXT NOT NULL UNIQUE,
    requested_at_utc            TEXT NOT NULL,
    completed_at_utc            TEXT,
    request_payload_sha256      TEXT NOT NULL,
    http_status                 INTEGER,
    response_body               TEXT,
    response_headers            TEXT,
    success                     INTEGER NOT NULL CHECK (success IN (0, 1)),
    error_type                  TEXT,
    error_message               TEXT,
    verified_by_refetch         INTEGER NOT NULL CHECK (verified_by_refetch IN (0, 1)),
    refetched_forecast_snapshot TEXT,
    created_at_utc              TEXT NOT NULL
);

CREATE TABLE resolution_events (
    event_id                 INTEGER PRIMARY KEY,
    question_id              INTEGER NOT NULL,
    forecast_record_id       TEXT REFERENCES forecast_records (record_id),
    resolution_snapshot_json TEXT,
    outcome                  TEXT,
    annulled                 INTEGER NOT NULL DEFAULT 0 CHECK (annulled IN (0, 1)),
    ambiguous                INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
    source_response          TEXT,
    ingested_at_utc          TEXT NOT NULL
);

CREATE TABLE score_events (
    event_id               INTEGER PRIMARY KEY,
    forecast_record_id     TEXT NOT NULL REFERENCES forecast_records (record_id),
    metric                 TEXT NOT NULL,
    value                  REAL NOT NULL,
    implementation_version TEXT NOT NULL,
    comparison_baseline    TEXT,
    computed_at_utc        TEXT NOT NULL
);

CREATE TABLE schema_migrations (
    version        INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL,
    checksum       TEXT NOT NULL
);
