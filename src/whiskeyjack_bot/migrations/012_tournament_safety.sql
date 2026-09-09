-- Launch readiness: durable operations and cross-version exclusion.
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

-- One ledger is bound to one account by its activation history. This deliberately
-- also covers older records, which predate account IDs in the ledger.
CREATE TRIGGER submission_reserve_whole_question
BEFORE INSERT ON submission_key_reservations
BEGIN
    SELECT RAISE(ABORT, 'question already has a submission attempt or outstanding reservation')
    WHERE EXISTS (
        SELECT 1 FROM forecast_records old JOIN forecast_records new
          ON old.question_id = new.question_id AND old.tournament_id = new.tournament_id
        WHERE new.record_id = NEW.forecast_record_id AND old.record_id <> NEW.forecast_record_id AND (
          EXISTS (SELECT 1 FROM submission_attempts a WHERE a.forecast_record_id=old.record_id)
          OR EXISTS (SELECT 1 FROM submission_key_reservations r
            WHERE r.forecast_record_id=old.record_id AND NOT EXISTS (
              SELECT 1 FROM submission_key_releases x WHERE x.reservation_id=r.reservation_id))
        )
    );
END;
