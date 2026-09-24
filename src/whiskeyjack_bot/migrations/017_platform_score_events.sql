-- M4-803: Metaculus's own scores, read from the observation they came with.
--
-- Every resolution observation M4-801 stores keeps the raw post payload as `source_response`,
-- and for a question this account predicted on that payload carries the account's own scores
-- at `question.my_forecasts.score_data`. `platform_scores.py` copies four of them into
-- `score_events` as `platform_*` rows; nothing is computed (D30 forbids a local replica).
-- This migration is the write contract for those rows, added the way 015's header said it
-- would be: one DROP/CREATE of 015's validate trigger. No column is added, no table is
-- rebuilt, and 015's UNIQUE index -- (record, resolution, metric, version) -- is the
-- idempotency rule for platform rows unchanged.
--
-- WHAT CHANGES
--
-- `score_events_validate_local_score_on_insert` becomes `score_events_validate_on_insert`.
-- The clauses that do not depend on the metric are 015's verbatim: the cited resolution is
-- this record's and its latest; the value is a finite real; the version begins with the
-- metric's name; computed_at_utc is canonical and not before the observation. The
-- metric-dependent clauses split in two:
--
-- - local_*: exactly 015. The four names, each bound to its question type (so a local score
--   cannot land on a numeric or discrete record), the ranges that follow from the
--   definitions, and no comparison baseline.
-- - platform_*: any supported question type; the comparison baseline 001 created the column
--   for, fixed per metric; the one version shape `platform_scores.py` writes; and the value
--   must be the one the cited observation's stored response holds for this record's question,
--   exactly.
--
-- WHY THE VALUE IS CHECKED HERE, WHEN 015 DECLINED TO
--
-- 015 would not recompute a local score in SQL: `ln` depends on build flags, and a second
-- implementation of a formula is a second source of truth for the number. A platform score
-- has no formula. The check is a lookup of the stored text, so it is the same source of truth
-- read twice, and it makes "a platform row is the platform's number" a property of the schema
-- rather than of the writer. It relies on SQLite parsing a JSON number to the same double
-- Python's `json` does. Measured 2026-09-23 on the SQLite the deployed venv ships (3.53.1):
-- exact for ~700,000 doubles and all 140 live `score_data` values. An older SQLite (3.45.1)
-- is not exact for about 1 value in 60,000, and there the clause *refuses* a genuine score --
-- a loud `failed`, never a wrong value admitted (docs/M4-NOTES.md, M4-803 Standing risk).
--
-- NO PRECONDITION SCAN
--
-- A trigger validates rows as they are inserted; every existing row is a local score that
-- 015's trigger already admitted under the clauses kept here, so there is nothing to check.

DROP TRIGGER score_events_validate_local_score_on_insert;

CREATE TRIGGER score_events_validate_on_insert
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

    -- The latest observation, not merely one of them (015).
    SELECT RAISE(ABORT, 'score_events: resolution_event_id must be the latest resolution row for the forecast record')
    WHERE NEW.resolution_event_id IS NOT (
        SELECT max(event_id) FROM resolution_events
         WHERE forecast_record_id = NEW.forecast_record_id
    );

    -- Every name carries its provenance: `local_` for M4-802's own arithmetic, `platform_` for
    -- a number copied from Metaculus. Nothing else may be written (D30; D36).
    SELECT RAISE(ABORT, 'score_events: metric is not a recognized score metric')
    WHERE typeof(NEW.metric) <> 'text'
       OR NEW.metric NOT IN (
          'local_brier_binary', 'local_log_binary',
          'local_brier_multiclass', 'local_log_multiclass',
          'platform_spot_peer_score', 'platform_spot_baseline_score',
          'platform_peer_score', 'platform_baseline_score'
       );

    -- A local score applies to one question type; a platform score to every supported one.
    SELECT RAISE(ABORT, 'score_events: metric does not apply to the forecast record question type')
    WHERE (SELECT question_type FROM forecast_records WHERE record_id = NEW.forecast_record_id)
       IS NOT CASE
          WHEN NEW.metric IN ('local_brier_binary', 'local_log_binary') THEN 'binary'
          WHEN NEW.metric IN ('local_brier_multiclass', 'local_log_multiclass') THEN 'multiple_choice'
          WHEN NEW.metric GLOB 'platform_*'
           AND (SELECT question_type FROM forecast_records WHERE record_id = NEW.forecast_record_id)
               IN ('binary', 'multiple_choice', 'numeric', 'discrete')
          THEN (SELECT question_type FROM forecast_records WHERE record_id = NEW.forecast_record_id)
       END;

    SELECT RAISE(ABORT, 'score_events: value must be a finite real number')
    WHERE typeof(NEW.value) <> 'real'
       OR NEW.value NOT BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308;

    -- 015's ranges, for local scores only: a platform score's range is the platform's.
    SELECT RAISE(ABORT, 'score_events: value is outside the range its metric can take')
    WHERE (NEW.metric IN ('local_brier_binary', 'local_brier_multiclass') AND NEW.value < 0)
       OR (NEW.metric = 'local_brier_binary' AND NEW.value > 1)
       OR (NEW.metric IN ('local_log_binary', 'local_log_multiclass') AND NEW.value > 0);

    SELECT RAISE(ABORT, 'score_events: implementation_version must name its metric')
    WHERE typeof(NEW.implementation_version) <> 'text'
       OR length(NEW.implementation_version) > 200
       OR length(NEW.implementation_version) <= length(NEW.metric) + 1
       OR substr(NEW.implementation_version, 1, length(NEW.metric) + 1) IS NOT NEW.metric || '/';

    -- A local score is measured against nothing but the outcome (015).
    SELECT RAISE(ABORT, 'score_events: a local score has no comparison baseline')
    WHERE NEW.metric GLOB 'local_*'
      AND NEW.comparison_baseline IS NOT NULL;

    -- A platform score always names what it is measured against, and it is fixed per metric:
    -- a peer score is not a baseline score, whatever the writer says.
    SELECT RAISE(ABORT, 'score_events: a platform score must carry its comparison baseline')
    WHERE NEW.metric GLOB 'platform_*'
      AND NEW.comparison_baseline IS NOT CASE NEW.metric
          WHEN 'platform_spot_peer_score' THEN 'peer'
          WHEN 'platform_peer_score' THEN 'peer'
          WHEN 'platform_spot_baseline_score' THEN 'baseline'
          WHEN 'platform_baseline_score' THEN 'baseline'
       END;

    -- The source, named: the version is the path the value was read from.
    SELECT RAISE(ABORT, 'score_events: a platform score version must name the score_data it was read from')
    WHERE NEW.metric GLOB 'platform_*'
      AND (typeof(NEW.implementation_version) <> 'text'
           OR substr(NEW.implementation_version, length(NEW.metric) + 2)
              NOT GLOB 'metaculus_score_data/[1-9]*'
           OR substr(NEW.implementation_version, length(NEW.metric) + 23)
              GLOB '*[^0-9]*');

    -- The value is the platform's: exactly the number the cited observation's stored response
    -- holds for this record's question (top-level, or the one group member with its id --
    -- `resolution.select_question`'s rule), and a JSON real rather than an integer, which
    -- SQLite's `=` would otherwise admit against a REAL.
    SELECT RAISE(ABORT, 'score_events: a platform score must be the value its cited observation holds')
    WHERE NEW.metric GLOB 'platform_*'
      AND NOT EXISTS (
          SELECT 1
            FROM resolution_events r
            JOIN forecast_records f ON f.record_id = NEW.forecast_record_id
            JOIN (SELECT CASE NEW.metric
                         WHEN 'platform_spot_peer_score' THEN 'spot_peer_score'
                         WHEN 'platform_spot_baseline_score' THEN 'spot_baseline_score'
                         WHEN 'platform_peer_score' THEN 'peer_score'
                         WHEN 'platform_baseline_score' THEN 'baseline_score'
                         END AS name) k
           WHERE r.event_id = NEW.resolution_event_id
             AND json_valid(r.source_response)
             AND (
                  (json_type(r.source_response, '$.question.id') = 'integer'
                   AND json_extract(r.source_response, '$.question.id') = f.question_id
                   AND json_type(r.source_response, '$.question.my_forecasts.score_data.' || k.name) = 'real'
                   AND json_extract(r.source_response, '$.question.my_forecasts.score_data.' || k.name) = NEW.value)
                  OR
                  (NOT (json_type(r.source_response, '$.question.id') IS 'integer'
                        AND json_extract(r.source_response, '$.question.id') = f.question_id)
                   AND json_type(r.source_response, '$.group_of_questions.questions') = 'array'
                   AND (SELECT count(*) FROM json_each(r.source_response, '$.group_of_questions.questions') g
                         WHERE json_type(g.value, '$.id') = 'integer'
                           AND json_extract(g.value, '$.id') = f.question_id) = 1
                   AND EXISTS (
                       SELECT 1 FROM json_each(r.source_response, '$.group_of_questions.questions') g
                        WHERE json_type(g.value, '$.id') = 'integer'
                          AND json_extract(g.value, '$.id') = f.question_id
                          AND json_type(g.value, '$.my_forecasts.score_data.' || k.name) = 'real'
                          AND json_extract(g.value, '$.my_forecasts.score_data.' || k.name) = NEW.value))
          )
      );

    SELECT RAISE(ABORT, 'score_events: computed_at_utc must be a canonical UTC timestamp no earlier than the observation it scores')
    WHERE typeof(NEW.computed_at_utc) <> 'text'
       OR length(NEW.computed_at_utc) <> 32
       OR NEW.computed_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
       OR NEW.computed_at_utc < (
          SELECT observed_at_utc FROM resolution_events WHERE event_id = NEW.resolution_event_id
       );
END;
