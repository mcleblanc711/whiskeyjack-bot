-- M4-801: resolution ingestion.
--
-- 001 created `resolution_events` and 003 made it append-only (unconditional UPDATE and
-- DELETE blocks) and gave `lifecycle_events` a `resolved` event that must cite one of its
-- rows. Nothing ever wrote to it, and nothing constrained what a row may claim. This
-- migration is the write contract, added the cheap way: ADD COLUMN plus two new BEFORE
-- INSERT triggers and one AFTER INSERT trigger. No existing trigger body is touched, and
-- no table is rebuilt.
--
-- WHY THIS TABLE AND NOT A NEW ONE
--
-- `lifecycle_events.resolution_event_id REFERENCES resolution_events (event_id)` and 003's
-- two ownership probes already point here. A new table would leave that column pointing
-- at a table nothing writes, and the `resolved` transition unreachable without a rewrite
-- of `lifecycle_events_validate_on_insert`. `resolution_events` carries no column CHECK
-- vocabulary to widen, so the rebuild 003's header warns about never arises.
--
-- WHAT A ROW IS
--
-- One row is one *observation* of one question's resolution state, recorded against one
-- forecast record of that question. `resolution_kind` is the classification:
--
--   resolved    the platform shows a definite outcome; the only scorable kind
--   annulled    the platform annulled the question; never scorable
--   ambiguous   the platform resolved it ambiguous; never scorable
--   withheld    status `resolved` with a null resolution -- the API masks the value from an
--               account that did not predict on the question (Metaculus docs/openapi.yml);
--               never scorable, and never moves a record to `resolved`
--   unresolved  a question with a prior observation whose platform status is no longer
--               `resolved` -- a retraction (the SDK exposes `unresolve_question`); never
--               scorable, and refused as a first observation, since it retracts nothing
--
-- The vocabulary is a trigger clause rather than a column CHECK so that widening it later
-- is a DROP/CREATE, the 004-013 escape hatch, not a rebuild of an append-only table.
--
-- The current resolution of a record is its **latest** row (highest `event_id`). A later
-- observation that differs is appended, never written over the earlier one: the platform
-- can re-resolve or retract, and the attribution record keeps every state it showed.

ALTER TABLE resolution_events ADD COLUMN post_id INTEGER;
ALTER TABLE resolution_events ADD COLUMN question_type TEXT;
ALTER TABLE resolution_events ADD COLUMN resolution_kind TEXT;
ALTER TABLE resolution_events ADD COLUMN scorable INTEGER;
ALTER TABLE resolution_events ADD COLUMN observation_sha256 TEXT;
ALTER TABLE resolution_events ADD COLUMN source_response_sha256 TEXT;
ALTER TABLE resolution_events ADD COLUMN observed_at_utc TEXT;

-- ---------------------------------------------------------------------------
-- Upgrade precondition: no row exists that the new rules could not account for.
-- ---------------------------------------------------------------------------
--
-- 006's mechanism: RAISE() is only legal inside a trigger, so the refusal is a CHECK the
-- offending row violates, and the temp table's name carries the reason. ledger.py applies
-- the migration inside BEGIN/COMMIT and rolls back on error.
--
-- The rule is "no rows at all" rather than "no violating rows" for both tables. No writer
-- for either existed before this item, so any row present was written by hand, carries
-- none of the columns added above, and cannot be classified after the fact -- and a
-- `score_events` row with no scorable resolution behind it is exactly the row the second
-- trigger below exists to make impossible. The live ledger holds zero of each.

CREATE TEMP TABLE migration_014_requires_no_unclassified_resolution_rows (
    violation TEXT NOT NULL CHECK (violation = 'none')
);

INSERT INTO migration_014_requires_no_unclassified_resolution_rows (violation)
SELECT 'resolution_row_predates_014' FROM resolution_events LIMIT 1;

INSERT INTO migration_014_requires_no_unclassified_resolution_rows (violation)
SELECT 'score_row_predates_014' FROM score_events LIMIT 1;

DROP TABLE migration_014_requires_no_unclassified_resolution_rows;

-- ---------------------------------------------------------------------------
-- resolution_events: what an inserted row may claim.
-- ---------------------------------------------------------------------------

CREATE TRIGGER resolution_events_validate_on_insert
BEFORE INSERT ON resolution_events
FOR EACH ROW
BEGIN
    -- 001 left `forecast_record_id` nullable. 003's `resolved` event already requires the
    -- row it cites to belong to its record; this makes every row belong to one, because an
    -- observation nobody forecast has nothing to attribute and the API masks it anyway.
    SELECT RAISE(ABORT, 'resolution_events: forecast_record_id must name a stored forecast record')
    WHERE NEW.forecast_record_id IS NULL
       OR NOT EXISTS (SELECT 1 FROM forecast_records WHERE record_id = NEW.forecast_record_id);

    -- Identity must agree with the record it is attached to, column by column. A row that
    -- names the right record and the wrong question would be scored against another
    -- question's outcome (003 guards the lifecycle link the same way).
    SELECT RAISE(ABORT, 'resolution_events: question_id, post_id and question_type must match the forecast record')
    WHERE typeof(NEW.question_id) <> 'integer'
       OR typeof(NEW.post_id) <> 'integer'
       OR typeof(NEW.question_type) <> 'text'
       OR NOT EXISTS (
          SELECT 1 FROM forecast_records f
           WHERE f.record_id = NEW.forecast_record_id
             AND f.question_id = NEW.question_id
             AND f.post_id = NEW.post_id
             AND f.question_type = NEW.question_type
       );

    SELECT RAISE(ABORT, 'resolution_events: resolution_kind is not a recognized kind')
    WHERE NEW.resolution_kind IS NULL
       OR typeof(NEW.resolution_kind) <> 'text'
       OR NEW.resolution_kind NOT IN ('resolved', 'annulled', 'ambiguous', 'withheld', 'unresolved');

    -- The three flags are derived from the kind and stored so a reader never has to know
    -- the vocabulary to answer "may this be scored". `IS NOT` rather than `<>` so a NULL
    -- flag fails instead of comparing as unknown.
    SELECT RAISE(ABORT, 'resolution_events: scorable, annulled and ambiguous must follow from resolution_kind')
    WHERE typeof(NEW.scorable) <> 'integer'
       OR NEW.scorable IS NOT (NEW.resolution_kind = 'resolved')
       OR NEW.annulled IS NOT (NEW.resolution_kind = 'annulled')
       OR NEW.ambiguous IS NOT (NEW.resolution_kind = 'ambiguous');

    SELECT RAISE(ABORT, 'resolution_events: outcome is required for a resolved kind and forbidden otherwise')
    WHERE (NEW.resolution_kind = 'resolved'
           AND (typeof(NEW.outcome) <> 'text' OR length(NEW.outcome) = 0))
       OR (NEW.resolution_kind <> 'resolved' AND NEW.outcome IS NOT NULL);

    SELECT RAISE(ABORT, 'resolution_events: resolution_snapshot_json and source_response must be JSON objects')
    WHERE typeof(NEW.resolution_snapshot_json) <> 'text'
       OR NOT json_valid(NEW.resolution_snapshot_json)
       OR json_type(NEW.resolution_snapshot_json) <> 'object'
       OR typeof(NEW.source_response) <> 'text'
       OR NOT json_valid(NEW.source_response)
       OR json_type(NEW.source_response) <> 'object';

    -- The columns are a second spelling of the snapshot, so they must agree with it. The
    -- snapshot is what `observation_sha256` hashes and what replay re-validates; a column
    -- that disagreed would let a reader score one thing while the hash attested another.
    SELECT RAISE(ABORT, 'resolution_events: indexed columns must agree with resolution_snapshot_json')
    WHERE json_extract(NEW.resolution_snapshot_json, '$.kind') IS NOT NEW.resolution_kind
       OR json_extract(NEW.resolution_snapshot_json, '$.question_id') IS NOT NEW.question_id
       OR json_extract(NEW.resolution_snapshot_json, '$.post_id') IS NOT NEW.post_id
       OR json_extract(NEW.resolution_snapshot_json, '$.question_type') IS NOT NEW.question_type
       OR json_extract(NEW.resolution_snapshot_json, '$.outcome') IS NOT NEW.outcome;

    -- SQLite has no sha256, so the digests cannot be recomputed here; they are shape-checked
    -- and the reader (lifecycle.latest_resolution) recomputes both on the way out.
    SELECT RAISE(ABORT, 'resolution_events: observation_sha256 and source_response_sha256 must be 64 lowercase hex characters')
    WHERE typeof(NEW.observation_sha256) <> 'text'
       OR length(NEW.observation_sha256) <> 64
       OR NEW.observation_sha256 GLOB '*[^0-9a-f]*'
       OR typeof(NEW.source_response_sha256) <> 'text'
       OR length(NEW.source_response_sha256) <> 64
       OR NEW.source_response_sha256 GLOB '*[^0-9a-f]*';

    SELECT RAISE(ABORT, 'resolution_events: observed_at_utc and ingested_at_utc must be canonical UTC timestamps')
    WHERE typeof(NEW.observed_at_utc) <> 'text'
       OR length(NEW.observed_at_utc) <> 32
       OR NEW.observed_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
       OR typeof(NEW.ingested_at_utc) <> 'text'
       OR length(NEW.ingested_at_utc) <> 32
       OR NEW.ingested_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00';

    -- Idempotency, enforced where it can be witnessed. A repeated poll that sees what the
    -- latest row already records appends nothing; a changed observation appends, including
    -- a return to an earlier state (A, B, A is three rows). That is why this compares with
    -- the latest row only and is not a UNIQUE index: a unique digest would refuse the
    -- second A and lose the retraction in between.
    SELECT RAISE(ABORT, 'resolution_events: this observation is already the latest one recorded for the forecast record')
    WHERE NEW.observation_sha256 IS (
        SELECT observation_sha256 FROM resolution_events
         WHERE forecast_record_id = NEW.forecast_record_id
         ORDER BY event_id DESC LIMIT 1
    );

    -- A retraction with nothing before it retracts nothing.
    SELECT RAISE(ABORT, 'resolution_events: an unresolved observation must follow an earlier observation')
    WHERE NEW.resolution_kind = 'unresolved'
      AND NOT EXISTS (
          SELECT 1 FROM resolution_events WHERE forecast_record_id = NEW.forecast_record_id
      );

    -- Observations are appended in the order they were made, so "latest row" and "latest
    -- observation" are the same claim.
    SELECT RAISE(ABORT, 'resolution_events: observed_at_utc is earlier than the latest observation for the forecast record')
    WHERE NEW.observed_at_utc < (
        SELECT max(observed_at_utc) FROM resolution_events
         WHERE forecast_record_id = NEW.forecast_record_id
    );
END;

-- "Latest" is the highest event_id, so an explicit event_id below an existing one would
-- let a row become older than rows written before it. The check runs AFTER the insert
-- because in a BEFORE trigger an auto-assigned INTEGER PRIMARY KEY reads as -1, which an
-- explicit -1 cannot be told apart from. RAISE(ABORT) here still undoes the statement.
CREATE TRIGGER resolution_events_append_in_order
AFTER INSERT ON resolution_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'resolution_events: event_id must be greater than every existing event_id')
    WHERE EXISTS (SELECT 1 FROM resolution_events WHERE event_id > NEW.event_id);
END;

-- ---------------------------------------------------------------------------
-- score_events: annulled, ambiguous, withheld and retracted outcomes are not scored.
-- ---------------------------------------------------------------------------
--
-- M4-801's acceptance criterion, stated where a scorer cannot route around it. M4-802
-- (local Brier/log) and M4-803 (platform scores) both write here; neither needs its own
-- migration to be bound by this. The rule is the latest observation, not the one the
-- `resolved` lifecycle event cites: a record resolved `yes` and later retracted is not
-- scorable until the platform resolves it again.
CREATE TRIGGER score_events_require_scorable_resolution
BEFORE INSERT ON score_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'score_events: the latest resolution for this forecast record is not scorable')
    WHERE (
        SELECT scorable FROM resolution_events
         WHERE forecast_record_id = NEW.forecast_record_id
         ORDER BY event_id DESC LIMIT 1
    ) IS NOT 1;
END;
