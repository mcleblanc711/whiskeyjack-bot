-- M1-359 (D53 Phase 2, D54): the shadow experiment arm.
--
-- WHY A TABLE OF ITS OWN AND NOT A forecast_records ROW
--
-- A shadow forecast is generated in the same worker pass as the forecast the worker submits
-- (the control), from the control's baseline research plus the local-source lens's runs, with a
-- lens-aware prompt. It exists to be scored against the control on the same question
-- (M5-805), and for no other reason. Every path that can approve, reserve an idempotency key
-- for, or post a forecast starts from a `forecast_records` row: `approval.approve` reads one,
-- `submission.submission_key_for_record` reads one, and `approval_events`, `lifecycle_events`,
-- `submission_key_reservations`, `submission_attempts` and `submission_reconciliations` each
-- carry a foreign key to one. A shadow forecast is therefore kept out of that table entirely,
-- so none of those paths can be pointed at it: the foreign keys refuse a shadow id, and the
-- trigger at the bottom of this file refuses the id ever becoming a forecast record.
--
-- WHAT A ROW CLAIMS
--
--   control_record_id -- the forecast this one is paired with: the control recorded in the
--   same poll, for the same question, from the research the lens followed.
--
--   local_lens_checkpoint_id -- the `local_lens_checkpoint` journal event whose runs this
--   forecast read. UNIQUE: one shadow per lens checkpoint, so the arm is bought at most once
--   per bought research, as the lens is.
--
--   research_packet_sha256, packet_schema_version, retrieval_run_ids_json -- the combined
--   packet the model was shown (baseline plus lens runs, after the control's own
--   freshness filter), by the same rule `forecast_records.record_json` stamps for the control.
--   Lens documents carry locale rows, so this is a 1.1.0 packet hash.
--
--   prompt_version, prompt_sha256 -- the lens-aware prompt, by its own hash. It is not the
--   activation's prompt and is not bound to the activation (docs/M1-NOTES.md, M1-359).
--
--   record_json, record_sha256 -- the forecast itself, in the control's canonical record form
--   (`forecast.record.canonical_record_json`), with this row's id as its record_id. Same shape,
--   so the paired report reads both arms with one parser.
--
--   raw_output_path, cost_usd, model_invocations -- what the model call returned and cost,
--   as `forecast_records` holds them for the control.
--
-- APPEND-ONLY
--
-- Like every evidence table (003): no UPDATE, no DELETE. A failed or skipped shadow writes no
-- row here; the `shadow_forecast_checkpoint` journal event says what happened instead.

CREATE TABLE shadow_forecasts (
    shadow_forecast_id TEXT PRIMARY KEY,
    control_record_id TEXT NOT NULL REFERENCES forecast_records (record_id),
    question_id INTEGER NOT NULL,
    tournament_id TEXT NOT NULL,
    local_lens_checkpoint_id TEXT NOT NULL UNIQUE REFERENCES tournament_events (event_id),
    research_packet_sha256 TEXT NOT NULL,
    packet_schema_version TEXT NOT NULL,
    retrieval_run_ids_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    raw_output_path TEXT,
    cost_usd REAL,
    model_invocations INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE INDEX shadow_forecasts_control ON shadow_forecasts (control_record_id);

-- The row must describe the pairing it claims. Clauses rather than CHECKs, so a later item
-- can widen one with a DROP/CREATE (the 004-019 escape hatch) instead of rebuilding an
-- append-only table.
CREATE TRIGGER shadow_forecasts_validate_on_insert
BEFORE INSERT ON shadow_forecasts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'shadow_forecasts: a shadow id must never also be a forecast record id')
    WHERE EXISTS (
        SELECT 1 FROM forecast_records WHERE record_id = NEW.shadow_forecast_id
    );
    SELECT RAISE(ABORT, 'shadow_forecasts: control_record_id must name a forecast record of the same question and tournament')
    WHERE NOT EXISTS (
        SELECT 1 FROM forecast_records
        WHERE record_id = NEW.control_record_id
          AND question_id = NEW.question_id
          AND tournament_id = NEW.tournament_id
    );
    SELECT RAISE(ABORT, 'shadow_forecasts: local_lens_checkpoint_id must name a local_lens_checkpoint event')
    WHERE NOT EXISTS (
        SELECT 1 FROM tournament_events
        WHERE event_id = NEW.local_lens_checkpoint_id AND kind = 'local_lens_checkpoint'
    );
    SELECT RAISE(ABORT, 'shadow_forecasts: record_json must be the canonical record of this row')
    WHERE NOT json_valid(NEW.record_json)
       OR json_extract(NEW.record_json, '$.record_id') IS NOT NEW.shadow_forecast_id
       OR json_extract(NEW.record_json, '$.question_id') IS NOT NEW.question_id
       OR json_extract(NEW.record_json, '$.tournament_id') IS NOT NEW.tournament_id
       OR json_extract(NEW.record_json, '$.research_packet_sha256')
          IS NOT NEW.research_packet_sha256
       OR json_extract(NEW.record_json, '$.model_settings.prompt_sha256')
          IS NOT NEW.prompt_sha256;
    SELECT RAISE(ABORT, 'shadow_forecasts: retrieval_run_ids_json must be a non-empty JSON array')
    WHERE NOT json_valid(NEW.retrieval_run_ids_json)
       OR json_type(NEW.retrieval_run_ids_json) IS NOT 'array'
       OR json_array_length(NEW.retrieval_run_ids_json) = 0;
    SELECT RAISE(ABORT, 'shadow_forecasts: model_invocations must be at least 1')
    WHERE typeof(NEW.model_invocations) <> 'integer' OR NEW.model_invocations < 1;
END;

CREATE TRIGGER shadow_forecasts_no_update
BEFORE UPDATE ON shadow_forecasts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'shadow_forecasts is append-only: a shadow forecast is never updated');
END;

CREATE TRIGGER shadow_forecasts_no_delete
BEFORE DELETE ON shadow_forecasts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'shadow_forecasts is append-only: a shadow forecast is never deleted');
END;

-- The other direction, and the guard the whole design rests on: a shadow id can never become
-- a forecast record, so nothing that starts from `forecast_records` can ever reach one. A new
-- trigger rather than a sixth rewrite of `forecast_records_require_draft_on_insert`: this
-- touches no existing trigger body.
CREATE TRIGGER forecast_records_refuse_shadow_id
BEFORE INSERT ON forecast_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'forecast_records: a shadow forecast id can never become a forecast record')
    WHERE EXISTS (
        SELECT 1 FROM shadow_forecasts WHERE shadow_forecast_id = NEW.record_id
    );
END;
