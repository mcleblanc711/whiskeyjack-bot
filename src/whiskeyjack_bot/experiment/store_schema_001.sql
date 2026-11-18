-- M5-806: the replay experiment store, schema version 1.
--
-- Its own file and its own version table. This file is NOT a ledger migration: it lives outside
-- src/whiskeyjack_bot/migrations/, which ledger._load_migrations would apply to the live ledger.
-- Every table is append-only: a stored arm forecast, a run and its report are never changed.
--
-- No table here is one the approval, submission or posting code reads. tournament_events is the
-- priced client's spend journal (the table name is hard-coded in tournament_state), copied from
-- migration 012 without 012's submission trigger, and restricted below to spend and model-call
-- kinds: no activation, intent or confirmation can be written to this store (owner decision on
-- AC7, 2026-11-17).

CREATE TABLE experiment_schema (
    version        INTEGER PRIMARY KEY NOT NULL,
    applied_at_utc TEXT NOT NULL,
    checksum       TEXT NOT NULL
);

CREATE TABLE arms (
    arm_sha256       TEXT PRIMARY KEY NOT NULL CHECK (length(arm_sha256) = 64),
    spec_json        TEXT NOT NULL CHECK (json_valid(spec_json)),
    settings_sha256  TEXT NOT NULL CHECK (length(settings_sha256) = 64),
    selection_json   TEXT NOT NULL CHECK (json_valid(selection_json)),
    selection_sha256 TEXT NOT NULL CHECK (length(selection_sha256) = 64),
    created_at_utc   TEXT NOT NULL
);

CREATE TABLE runs (
    run_id              TEXT PRIMARY KEY NOT NULL,
    arm_sha256          TEXT NOT NULL REFERENCES arms (arm_sha256),
    live_ledger_path    TEXT NOT NULL,
    live_schema_version INTEGER NOT NULL,
    ceiling_microusd    INTEGER NOT NULL CHECK (ceiling_microusd > 0),
    started_at_utc      TEXT NOT NULL
);

CREATE TABLE run_reports (
    run_id          TEXT PRIMARY KEY NOT NULL REFERENCES runs (run_id),
    stop_reason     TEXT NOT NULL,
    counts_json     TEXT NOT NULL CHECK (json_valid(counts_json)),
    finished_at_utc TEXT NOT NULL
);

-- One row per (run, control record) the gate judged. reason_code NULL means the record passed.
CREATE TABLE fidelity_outcomes (
    run_id            TEXT NOT NULL REFERENCES runs (run_id),
    control_record_id TEXT NOT NULL,
    reason_code       TEXT,
    control_effort    TEXT,
    PRIMARY KEY (run_id, control_record_id)
);

-- One row per (arm, control record, sample) that reached the model. The control is bound by id
-- and forecast_sha256 as text; there is no cross-file foreign key, so the writer re-reads the
-- control in the live ledger at write time.
CREATE TABLE arm_samples (
    sample_id               TEXT PRIMARY KEY NOT NULL,
    arm_sha256              TEXT NOT NULL REFERENCES arms (arm_sha256),
    run_id                  TEXT NOT NULL REFERENCES runs (run_id),
    control_record_id       TEXT NOT NULL,
    control_forecast_sha256 TEXT NOT NULL CHECK (length(control_forecast_sha256) = 64),
    sample_index            INTEGER NOT NULL CHECK (sample_index >= 0),
    attempt_id              TEXT NOT NULL UNIQUE,
    outcome                 TEXT NOT NULL CHECK (
        outcome IN ('forecast', 'generation_failed', 'outcome_unknown')
    ),
    failure_code            TEXT,
    kept_packet_sha256      TEXT NOT NULL CHECK (length(kept_packet_sha256) = 64),
    request_sha256          TEXT NOT NULL CHECK (length(request_sha256) = 64),
    record_json             TEXT,
    forecast_sha256         TEXT,
    raw_output_path         TEXT,
    cost_microusd           INTEGER CHECK (cost_microusd IS NULL OR cost_microusd >= 0),
    held_microusd           INTEGER NOT NULL CHECK (held_microusd >= 0),
    billed_calls            INTEGER NOT NULL CHECK (billed_calls >= 0),
    model_invocations       INTEGER CHECK (model_invocations IS NULL OR model_invocations >= 0),
    reported_models_json    TEXT NOT NULL CHECK (json_valid(reported_models_json)),
    reasoning_effort_sent   TEXT,
    created_at_utc          TEXT NOT NULL,
    UNIQUE (arm_sha256, control_record_id, sample_index),
    CHECK ((outcome = 'forecast') = (record_json IS NOT NULL)),
    CHECK ((record_json IS NULL) = (forecast_sha256 IS NULL)),
    CHECK (outcome <> 'forecast' OR failure_code IS NULL),
    CHECK (outcome <> 'generation_failed' OR failure_code IS NOT NULL)
);

-- Migration 012's journal table, index and append-only triggers, verbatim.
CREATE TABLE tournament_events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    data TEXT NOT NULL CHECK(json_valid(data)),
    created_at_utc TEXT NOT NULL
);
CREATE INDEX tournament_events_scope ON tournament_events(scope, kind, seq);
CREATE TRIGGER tournament_events_no_update BEFORE UPDATE ON tournament_events
BEGIN SELECT RAISE(ABORT, 'tournament events are append-only'); END;
CREATE TRIGGER tournament_events_no_delete BEFORE DELETE ON tournament_events
BEGIN SELECT RAISE(ABORT, 'tournament events are append-only'); END;

CREATE TRIGGER tournament_events_spend_kinds_only BEFORE INSERT ON tournament_events
WHEN NEW.kind NOT IN (
    'cost_reserved', 'cost_settled', 'cost_settled_id', 'cost_corrected',
    'witness', 'model_started', 'model_completed', 'model_response'
)
BEGIN SELECT RAISE(ABORT, 'the experiment journal records spend and model calls only'); END;

CREATE TRIGGER experiment_schema_no_update BEFORE UPDATE ON experiment_schema
BEGIN SELECT RAISE(ABORT, 'experiment_schema is append-only'); END;
CREATE TRIGGER experiment_schema_no_delete BEFORE DELETE ON experiment_schema
BEGIN SELECT RAISE(ABORT, 'experiment_schema is append-only'); END;
CREATE TRIGGER arms_no_update BEFORE UPDATE ON arms
BEGIN SELECT RAISE(ABORT, 'arms is append-only'); END;
CREATE TRIGGER arms_no_delete BEFORE DELETE ON arms
BEGIN SELECT RAISE(ABORT, 'arms is append-only'); END;
CREATE TRIGGER runs_no_update BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'runs is append-only'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'runs is append-only'); END;
CREATE TRIGGER run_reports_no_update BEFORE UPDATE ON run_reports
BEGIN SELECT RAISE(ABORT, 'run_reports is append-only'); END;
CREATE TRIGGER run_reports_no_delete BEFORE DELETE ON run_reports
BEGIN SELECT RAISE(ABORT, 'run_reports is append-only'); END;
CREATE TRIGGER fidelity_outcomes_no_update BEFORE UPDATE ON fidelity_outcomes
BEGIN SELECT RAISE(ABORT, 'fidelity_outcomes is append-only'); END;
CREATE TRIGGER fidelity_outcomes_no_delete BEFORE DELETE ON fidelity_outcomes
BEGIN SELECT RAISE(ABORT, 'fidelity_outcomes is append-only'); END;
CREATE TRIGGER arm_samples_no_update BEFORE UPDATE ON arm_samples
BEGIN SELECT RAISE(ABORT, 'arm_samples is append-only: a stored arm forecast is never updated'); END;
CREATE TRIGGER arm_samples_no_delete BEFORE DELETE ON arm_samples
BEGIN SELECT RAISE(ABORT, 'arm_samples is append-only: a stored arm forecast is never deleted'); END;
