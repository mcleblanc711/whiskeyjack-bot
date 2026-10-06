-- M4-808 (D52, amends D42): a platform score may be an integer-valued JSON number.
--
-- 017's value clause required `json_type(...) = 'real'`, on the evidence that all 140 live
-- `score_data` values seen 2026-09-23 were floats. On 2026-10-03 questions 45978 and 45971
-- carried `baseline_score` and `spot_baseline_score` as the JSON integer 0, so the writer
-- refused them, `score` exited 4, and the resolutions unit has been red since. The Python
-- reader (`platform_scores._read`) now converts an exact-type int to the float of the same
-- value; this migration makes the same widening in the schema, because otherwise the ledger
-- would refuse the INSERT the writer is now willing to make.
--
-- WHAT CHANGES
--
-- `score_events_validate_on_insert` is dropped and recreated as 017 wrote it, with exactly two
-- edits: the two `json_type(...) = 'real'` tests (top-level question and group member) become
-- `IN ('real', 'integer')`, and one comment. Nothing else is touched: `= NEW.value` still
-- demands the stored number equal the stored REAL, and SQLite compares integer to REAL
-- exactly, so an integer a double cannot hold (|n| > 2**53, say) is refused here as well as in
-- the writer. `typeof(NEW.value) <> 'real'` is untouched: what is STORED is still a REAL.
--
-- NO PRECONDITION SCAN
--
-- A trigger validates rows as they are inserted, and this one only admits more than 017 did.

DROP TRIGGER score_events_validate_on_insert;

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
    -- `resolution.select_question`'s rule), as a JSON real or a JSON integer (018, D52).
    -- SQLite's `=` compares an integer with a REAL exactly, so an integer the writer could not
    -- convert without loss is still refused here.
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
                   AND json_type(r.source_response, '$.question.my_forecasts.score_data.' || k.name) IN ('real', 'integer')
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
                          AND json_type(g.value, '$.my_forecasts.score_data.' || k.name) IN ('real', 'integer')
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
