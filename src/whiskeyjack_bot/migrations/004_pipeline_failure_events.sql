-- M1-606: pre-forecast pipeline failures, attempt-scoped.
--
-- 003 could not record a research or generation failure: `lifecycle_events` is scoped to
-- a `forecast_records` row, and 001 requires a non-null `final_prediction_json`,
-- `record_json` and `retrieval_run_id` on that table, so no row exists until generation
-- has already succeeded. 003's own header names this migration and this item; the reasons
-- for the shape below are in `docs/M1-NOTES.md`'s "M1-606" section, written before this
-- file per CLAUDE.md.
--
-- `lifecycle_events.event_type` is a closed CHECK and does not include
-- `research_failed`/`generation_failed` (003 round 1 removed them as unreachable).
-- SQLite cannot widen a CHECK without rebuilding the table -- CREATE TABLE ... AS SELECT
-- plus a rename -- which means dropping and recreating every trigger on it, including the
-- append-only block pair. That is exactly the operation the ledger exists to make
-- impossible, so this migration adds a new table instead of touching `lifecycle_events`.
--
-- `pipeline_failure_events` is scoped to an `attempt_id` (caller-minted TEXT) rather than
-- a forecast record. An attempt is the end-to-end campaign toward one forecast version for
-- one question: it may fail more than once before either succeeding or being abandoned, so
-- `UNIQUE (attempt_id, event_seq)` with a contiguous-next-value trigger probe is
-- `lifecycle_events`' own `UNIQUE (forecast_record_id, event_seq)` pattern, reused rather
-- than reinvented. `question_id`/`tournament_id` are stored directly on the row, the same
-- choice 001 made for `resolution_events`: the event may have no forecast record to join
-- through, ever, so its own identity cannot depend on one existing.
--
-- The other half of the acceptance criterion -- "the failed attempt is linked to the
-- forecast version that later succeeds for the same question" -- is `forecast_records`
-- getting its own `attempt_id` column, written at INSERT time. `forecast_records` is
-- UPDATE-blocked (003, D25), so nothing can annotate a stored row after the fact; the two
-- sides can only agree by both being given the same value up front. See the migration's
-- second half below.
--
-- Timestamps are TEXT ISO-8601 UTC, matching 001/003. Trigger messages name fields and
-- never interpolate row values, per the project-wide error-hygiene rule.

-- One event per pre-forecast failure. `event_id` is a bare rowid alias, the same shape as
-- `lifecycle_events.event_id`.
--
-- `detail_code` is `lifecycle.FailureCode` minus `refetch_mismatch`/`refetch_missing`:
-- both describe what a refetch saw of an *already-posted* forecast (M2-704's contract on
-- `submission_verifications`), which cannot occur before generation has even succeeded
-- once. Spelled out rather than referencing the wider vocabulary, so a future addition to
-- `FailureCode` does not silently become reachable here before anyone decides it belongs.
--
-- `retrieval_run_id` is nullable: required by trigger for `generation_failed` (generation
-- only runs once research has completed, so there is always a run to cite) and optional for
-- `research_failed` (a failure can occur before any `research_runs` row exists at all, e.g.
-- the provider call was never made).
CREATE TABLE pipeline_failure_events (
    event_id         INTEGER PRIMARY KEY,
    attempt_id       TEXT NOT NULL,
    event_seq        INTEGER NOT NULL,
    question_id      INTEGER NOT NULL,
    tournament_id    TEXT NOT NULL,
    event_type       TEXT NOT NULL CHECK (event_type IN ('research_failed', 'generation_failed')),
    detail_code      TEXT NOT NULL CHECK (
        detail_code IN (
            'provider_error', 'provider_unavailable', 'no_evidence', 'stale_evidence',
            'malformed_response', 'schema_invalid', 'calibration_invalid',
            'http_error', 'timeout', 'internal_error'
        )
    ),
    retrieval_run_id TEXT REFERENCES research_runs (retrieval_run_id),
    occurred_at_utc  TEXT NOT NULL,
    created_at_utc   TEXT NOT NULL,
    UNIQUE (attempt_id, event_seq)
);

-- No separate index on attempt_id: the UNIQUE above already indexes this table with
-- attempt_id leftmost, the same reasoning 003 gives for omitting one on
-- lifecycle_events.forecast_record_id.
CREATE INDEX idx_pipeline_failure_events_question_id
    ON pipeline_failure_events (question_id);

-- SQLite does not auto-index a plain REFERENCES column (001's reason for its explicit
-- foreign-key indexes), and M1-604's export is expected to join failures back to the
-- research run they happened during.
CREATE INDEX idx_pipeline_failure_events_retrieval_run_id
    ON pipeline_failure_events (retrieval_run_id);

CREATE TRIGGER pipeline_failure_events_validate_on_insert
BEFORE INSERT ON pipeline_failure_events
FOR EACH ROW
BEGIN
    -- typeof() because INTEGER is affinity, not a type: 003 documents the same trap for
    -- lifecycle_events.event_seq.
    SELECT RAISE(ABORT, 'pipeline_failure_events: event_seq must be a positive integer')
    WHERE typeof(NEW.event_seq) <> 'integer' OR NEW.event_seq < 1;

    SELECT RAISE(ABORT, 'pipeline_failure_events: event_seq must be the next sequence number for this attempt_id')
    WHERE NEW.event_seq <> COALESCE(
        (SELECT max(event_seq) FROM pipeline_failure_events WHERE attempt_id = NEW.attempt_id),
        0
    ) + 1;

    -- An attempt that already produced a successful forecast cannot also fail: success and
    -- failure are both terminal for a given attempt_id.
    SELECT RAISE(ABORT, 'pipeline_failure_events: this attempt_id already produced a successful forecast record')
    WHERE EXISTS (SELECT 1 FROM forecast_records WHERE attempt_id = NEW.attempt_id);

    -- Identity stability: an attempt_id names one campaign toward one forecast version for
    -- one question, so every event under it must agree on which question and tournament
    -- that is. Same reasoning 003 applies to a resolution_events row citing the wrong
    -- question.
    SELECT RAISE(ABORT, 'pipeline_failure_events: this attempt_id was previously used for a different question or tournament')
    WHERE EXISTS (
        SELECT 1 FROM pipeline_failure_events
         WHERE attempt_id = NEW.attempt_id
           AND (question_id <> NEW.question_id OR tournament_id <> NEW.tournament_id)
    );

    SELECT RAISE(ABORT, 'pipeline_failure_events: a generation failure requires retrieval_run_id')
    WHERE NEW.event_type = 'generation_failed' AND NEW.retrieval_run_id IS NULL;

    -- A BEFORE INSERT trigger runs ahead of the foreign-key check, so an unknown run fails
    -- here with this schema's own message rather than a generic FK one (003's reasoning for
    -- its own forecast_record_id probe).
    SELECT RAISE(ABORT, 'pipeline_failure_events: retrieval_run_id does not name a stored research run')
    WHERE NEW.retrieval_run_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM research_runs WHERE retrieval_run_id = NEW.retrieval_run_id);

    -- Ownership: a cited run must actually be about this event's question. Same shape as
    -- 003's approval/resolution ownership probes.
    SELECT RAISE(ABORT, 'pipeline_failure_events: the linked research run is for another question')
    WHERE NEW.retrieval_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM research_runs
           WHERE retrieval_run_id = NEW.retrieval_run_id
             AND question_id = NEW.question_id
      );
END;

-- Append-only (D25), unconditional -- there is no legitimate UPDATE or DELETE against a
-- ledger row. Same shape as every block trigger in 003. PRAGMA recursive_triggers
-- (ledger.py, set ON since 003) is what keeps INSERT OR REPLACE from bypassing these by
-- deleting the conflicting row without firing BEFORE DELETE; 003 found that the hard way in
-- its own round 1 and this table inherits the same connection-level setting.

CREATE TRIGGER pipeline_failure_events_block_update
BEFORE UPDATE ON pipeline_failure_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'pipeline_failure_events is append-only: a recorded pipeline failure is never updated (D25)');
END;

CREATE TRIGGER pipeline_failure_events_block_delete
BEFORE DELETE ON pipeline_failure_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'pipeline_failure_events is append-only: a recorded pipeline failure is never deleted (D25)');
END;

-- The link from a failed attempt to the forecast version that later succeeds for the same
-- question: `attempt_id`, minted once per campaign and reused across every retry, stamped
-- onto the forecast_records row if and when the campaign succeeds.
--
-- NULLable for 002/003's reason: ADD COLUMN cannot add NOT NULL without a default, and no
-- default is honest for an identifier nobody minted for a pre-existing row. New rows are
-- made to carry it by the BEFORE INSERT trigger below instead. M1-602 (the writer of this
-- table) is Not Started, so the population this affects is empty today -- the carve-out is
-- a formality, not a live migration.
ALTER TABLE forecast_records ADD COLUMN attempt_id TEXT;

-- One attempt succeeds at most once. An index rather than a trigger probe for the reason
-- 003 gives for its own partial unique indexes: the message names the column, never a
-- value, and the index is also the join path M1-604 uses.
CREATE UNIQUE INDEX idx_forecast_records_attempt_id
    ON forecast_records (attempt_id) WHERE attempt_id IS NOT NULL;

-- 003's forecast_records_require_draft_on_insert, extended. A trigger's body cannot be
-- ALTERed; dropping and recreating a trigger by the same name touches only its own
-- definition, not the table's rows or its append-only block triggers, so this is not the
-- rebuild hazard the header above describes for lifecycle_events -- that hazard is about
-- widening a CHECK, which requires recreating the table itself, not about redefining one
-- trigger.
DROP TRIGGER forecast_records_require_draft_on_insert;

CREATE TRIGGER forecast_records_require_draft_on_insert
BEFORE INSERT ON forecast_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'forecast_records: a new record must be created with status draft; every later state is reachable only through lifecycle_events')
    WHERE NEW.status <> 'draft';

    SELECT RAISE(ABORT, 'forecast_records: forecast_sha256 is required and must be 64 lowercase hex characters')
    WHERE NEW.forecast_sha256 IS NULL
       OR typeof(NEW.forecast_sha256) <> 'text'
       OR length(NEW.forecast_sha256) <> 64
       OR NEW.forecast_sha256 GLOB '*[^0-9a-f]*';

    SELECT RAISE(ABORT, 'forecast_records: attempt_id is required')
    WHERE NEW.attempt_id IS NULL OR typeof(NEW.attempt_id) <> 'text' OR trim(NEW.attempt_id) = '';

    -- The other half of pipeline_failure_events_validate_on_insert's identity-stability
    -- probe: a successful record cannot claim an attempt_id that pipeline_failure_events
    -- already recorded against a different question or tournament.
    SELECT RAISE(ABORT, 'forecast_records: attempt_id was used for a different question or tournament')
    WHERE EXISTS (
        SELECT 1 FROM pipeline_failure_events
         WHERE attempt_id = NEW.attempt_id
           AND (question_id <> NEW.question_id OR tournament_id <> NEW.tournament_id)
    );
END;
