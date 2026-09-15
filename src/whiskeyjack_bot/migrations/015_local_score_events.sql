-- M4-802: local Brier and log scores.
--
-- 001 created `score_events` (metric, value, implementation_version, comparison_baseline,
-- computed_at_utc), 003 made it append-only, and 014 refused any row unless the record's
-- latest resolution is scorable. Nothing wrote to it, and nothing constrained what a row
-- may claim beyond that. This migration is the write contract for M4-802's local scores,
-- added the cheap way: one ADD COLUMN, one new BEFORE INSERT trigger and one UNIQUE index.
-- No existing trigger body is touched, and no table is rebuilt.
--
-- WHY A COLUMN
--
-- A score is computed against one resolution observation, and the platform can retract or
-- re-resolve after a record is scored (M4-801). Without naming the observation, a stored
-- score cannot say which outcome it measured, a re-resolution cannot be scored as a new
-- row without guessing which old rows it supersedes, and "scoring twice writes nothing" is
-- a property of the writer alone. `resolution_event_id` makes all three structural: the
-- UNIQUE index below is the idempotency rule, enforced where it can be witnessed.
--
-- WHAT THIS DOES NOT OWN
--
-- 014's `score_events_require_scorable_resolution` still decides *whether* a record may be
-- scored (its latest resolution must be `resolved`). This trigger decides what a local
-- score row must say. It does not recompute the value: SQLite's `ln` depends on build
-- flags, and a second implementation of the score in SQL would be a second source of
-- truth for the number. `lifecycle.read_local_scores` recomputes every row on the way out.
--
-- The metric vocabulary is a trigger clause rather than a CHECK so that M4-803 can widen
-- it with a DROP/CREATE, the 004-014 escape hatch, not a rebuild of an append-only table.

ALTER TABLE score_events ADD COLUMN resolution_event_id INTEGER REFERENCES resolution_events (event_id);

-- ---------------------------------------------------------------------------
-- Upgrade precondition: no row exists that predates the column.
-- ---------------------------------------------------------------------------
--
-- 006/014's mechanism: RAISE() is only legal inside a trigger, so the refusal is a CHECK
-- the offending row violates. No writer existed before this item, so a row present was
-- written by hand and cannot be attributed to an observation after the fact. The live
-- ledger holds none.

CREATE TEMP TABLE migration_015_requires_no_unattributed_score_rows (
    violation TEXT NOT NULL CHECK (violation = 'none')
);

INSERT INTO migration_015_requires_no_unattributed_score_rows (violation)
SELECT 'score_row_predates_015' FROM score_events LIMIT 1;

DROP TABLE migration_015_requires_no_unattributed_score_rows;

-- ---------------------------------------------------------------------------
-- score_events: what an inserted local score row may claim.
-- ---------------------------------------------------------------------------

CREATE TRIGGER score_events_validate_local_score_on_insert
BEFORE INSERT ON score_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'score_events: resolution_event_id must name a resolution row of this forecast record')
    WHERE typeof(NEW.resolution_event_id) <> 'integer'
       OR NOT EXISTS (
          SELECT 1 FROM resolution_events
           WHERE event_id = NEW.resolution_event_id
             AND forecast_record_id = NEW.forecast_record_id
       );

    -- The latest observation, not merely one of them. 014 refuses a score when the latest
    -- row is not scorable; this refuses a score that cites a superseded row, so a score
    -- computed against a retracted outcome cannot be written after the retraction.
    SELECT RAISE(ABORT, 'score_events: resolution_event_id must be the latest resolution row for the forecast record')
    WHERE NEW.resolution_event_id IS NOT (
        SELECT max(event_id) FROM resolution_events
         WHERE forecast_record_id = NEW.forecast_record_id
    );

    -- Every name carries `local_`: nothing this table holds from M4-802 can be read as a
    -- Metaculus score (D30 by analogy; D36).
    SELECT RAISE(ABORT, 'score_events: metric is not a recognized local score metric')
    WHERE typeof(NEW.metric) <> 'text'
       OR NEW.metric NOT IN (
          'local_brier_binary', 'local_log_binary',
          'local_brier_multiclass', 'local_log_multiclass'
       );

    SELECT RAISE(ABORT, 'score_events: metric does not apply to the forecast record question type')
    WHERE (SELECT question_type FROM forecast_records WHERE record_id = NEW.forecast_record_id)
       IS NOT CASE
          WHEN NEW.metric IN ('local_brier_binary', 'local_log_binary') THEN 'binary'
          WHEN NEW.metric IN ('local_brier_multiclass', 'local_log_multiclass') THEN 'multiple_choice'
       END;

    -- SQLite stores an IEEE infinity in a REAL column, and a NaN bound from Python arrives as
    -- NULL. Neither is a score, and M1-604's export refuses both.
    SELECT RAISE(ABORT, 'score_events: value must be a finite real number')
    WHERE typeof(NEW.value) <> 'real'
       OR NEW.value NOT BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308;

    -- The bounds that follow from the definitions alone. A multiclass Brier upper bound is
    -- deliberately absent: it depends on the Python sum tolerance, and a copy of that here
    -- would be a second, divergent spelling of it.
    SELECT RAISE(ABORT, 'score_events: value is outside the range its metric can take')
    WHERE (NEW.metric IN ('local_brier_binary', 'local_brier_multiclass') AND NEW.value < 0)
       OR (NEW.metric = 'local_brier_binary' AND NEW.value > 1)
       OR (NEW.metric IN ('local_log_binary', 'local_log_multiclass') AND NEW.value > 0);

    -- A version is named for the metric it implements, so a row cannot pair one metric's
    -- value with another metric's implementation.
    SELECT RAISE(ABORT, 'score_events: implementation_version must name its metric')
    WHERE typeof(NEW.implementation_version) <> 'text'
       OR length(NEW.implementation_version) > 200
       OR length(NEW.implementation_version) <= length(NEW.metric) + 1
       OR substr(NEW.implementation_version, 1, length(NEW.metric) + 1) IS NOT NEW.metric || '/';

    -- A local score is measured against nothing but the outcome. The baseline column is where
    -- a "baseline" or "peer" label would go, and no local score may carry one.
    SELECT RAISE(ABORT, 'score_events: a local score has no comparison baseline')
    WHERE NEW.comparison_baseline IS NOT NULL;

    SELECT RAISE(ABORT, 'score_events: computed_at_utc must be a canonical UTC timestamp no earlier than the observation it scores')
    WHERE typeof(NEW.computed_at_utc) <> 'text'
       OR length(NEW.computed_at_utc) <> 32
       OR NEW.computed_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
       OR NEW.computed_at_utc < (
          SELECT observed_at_utc FROM resolution_events WHERE event_id = NEW.resolution_event_id
       );
END;

-- Idempotency: one value per record, observation, metric and implementation. Scoring the
-- same observation again writes nothing; a new observation (a re-resolution) or a new
-- implementation version is a new row, never an UPDATE.
CREATE UNIQUE INDEX score_events_one_per_observation_metric_version
    ON score_events (forecast_record_id, resolution_event_id, metric, implementation_version);
