-- M1-607: the non-blank identifier guard, applied to the identifier columns that
-- shipped before 004 invented it.
--
-- 004 built a two-layer guard for `attempt_id`: `lifecycle._require_identifier` on the
-- writer side and a trigger clause here that spells out the 29 codepoints where Python's
-- `str.isspace()` is true. It spelled the set out rather than calling one-argument
-- `trim()`, which strips U+0020 and nothing else -- that is M1-603's round 5, where a
-- '\n\t' value was refused by the writer, accepted by the schema, and carried a record to
-- `submitted` on two bytes of nothing.
--
-- 004 deliberately scoped that guard to its own writer instead of widening the shared
-- `_require_text`, because widening it would have changed what already-shipped,
-- already-reviewed writers accept -- a behaviour change to merged code smuggled in under
-- a different item. It filed M1-607 instead (docs/M1-NOTES.md, "M1-606 deviation"). This
-- is that widening, reviewed on its own.
--
-- What was unguarded until now, and why each one matters:
--
--   * forecast_records.record_id      -- the ledger's primary identity. M1-602 is the
--                                        item that starts writing it, and it is not on
--                                        master yet, so this guard lands ahead of its
--                                        first writer rather than behind it.
--   * forecast_records.tournament_id  -- 004 already guards
--                                        pipeline_failure_events.tournament_id, and 004's
--                                        identity-stability probe compares the two columns
--                                        directly. One end guarded and the other not is
--                                        the same both-ends-of-a-join asymmetry 004 exists
--                                        to close, so this column is included even though
--                                        the backlog row does not name it.
--   * submission_attempts.attempt_id
--   * submission_attempts.idempotency_key
--   * research_runs.retrieval_run_id
--
-- A BLOB is worse than a blank one, which is why `typeof()` is checked and not only
-- emptiness: `_stored_text` refuses a blob on the way *out*, so a blob identifier is an
-- append-only row that this schema's own reader can never read back. On a TEXT-affinity
-- column `42` arrives already converted to '42' and is accepted -- correctly, since what
-- lands is genuine text -- so what the clause actually catches is what affinity cannot
-- convert, which is a blob. (004 learned this the hard way: a test feeding '1' and
-- expecting a refusal passes against a trigger with no typeof() clause at all.)
--
-- WHAT IS **NOT** GUARDED HERE, AND WHY IT IS STATED RATHER THAN HALF-FIXED
--
-- Every remaining identifier column in the schema is a foreign key into one of the
-- primary keys guarded above -- approval_events/submission_attempts/resolution_events/
-- score_events.forecast_record_id into forecast_records.record_id, and
-- forecast_records/research_documents.retrieval_run_id into research_runs.retrieval_run_id
-- -- and research_documents.document_id is minted by the writer as uuid4().hex. So they
-- are covered transitively, and duplicating the clause onto each would be a second copy
-- of a rule with nothing keeping the copies in agreement.
--
-- That transitivity rests on `PRAGMA foreign_keys = ON`, which ledger.py sets but -- unlike
-- `journal_mode` and `recursive_triggers` -- does not read back. An unknown or ignored
-- PRAGMA is a silent no-op in SQLite, so this is an assumption, not a verified property.
-- Recorded here deliberately: reading it back is a change to ledger.py's connection
-- contract and belongs to its own item, and a stated residual is worth more than a
-- guarantee that half holds.
--
-- ONLY THE INSERT TRIGGERS ARE TOUCHED
--
-- forecast_records and submission_attempts carry unconditional append-only block triggers
-- from 003, so they have no UPDATE path to guard. research_runs is updatable -- M1-306
-- completes a run in a second phase -- but 003's `research_runs_block_identity_update`
-- pins `retrieval_run_id` against any change, so an UPDATE cannot reintroduce a blank one.
-- The completion columns 002's update trigger validates are unaffected by this migration.
--
-- 001/002/003/004 are not edited. ledger.py records each migration's sha256 when it is
-- applied and refuses to run against a database whose stored checksum no longer matches,
-- so a guard added to an already-applied file would break every existing ledger.

-- ---------------------------------------------------------------------------
-- Upgrade precondition: no row already violates the rule.
-- ---------------------------------------------------------------------------
--
-- BEFORE INSERT triggers bind new rows only, so without this the guarantee would quietly
-- become "every row written after 006" -- and on append-only tables a violating row can
-- never be corrected afterwards. A blank or blob identifier is exactly the row that is
-- unjoinable and unreadable for good, which is the condition this item exists to make
-- impossible, so the upgrade refuses rather than shipping a rule with a grandfathered
-- exception.
--
-- Same mechanism 003 uses for its non-draft-record precondition: RAISE() is legal only
-- inside a trigger body, so the refusal is a CHECK that the offending row violates and
-- the table's NAME carries the reason. ledger.py applies each migration inside
-- BEGIN/COMMIT with a ROLLBACK on error and wraps the failure without echoing any stored
-- value, so a refused upgrade leaves the database exactly at version 5 and the temp table
-- does not survive either outcome.
--
-- Each probe is the *same predicate* as the trigger clause it corresponds to, on purpose.
-- Two definitions of "blank" that nobody compared is precisely the defect this whole
-- family of guards descends from; a third layer with its own wording would reopen it.
--
-- The realistic population is empty today -- forecast_records has no writer until M1-602,
-- submission is disabled until M2, and research_runs ids are short caller-supplied
-- strings -- so this is a formality that stays honest, not a live data migration.

CREATE TEMP TABLE migration_006_requires_non_blank_identifiers (
    violation TEXT NOT NULL CHECK (violation = 'none')
);

INSERT INTO migration_006_requires_non_blank_identifiers (violation)
SELECT 'blank_identifier' FROM forecast_records
 WHERE record_id IS NULL
    OR typeof(record_id) <> 'text'
    OR length(record_id) > 200
    OR instr(record_id, char(0)) > 0
    OR trim(record_id,
            char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                 8232, 8233, 8239, 8287, 12288)) = ''
 LIMIT 1;

INSERT INTO migration_006_requires_non_blank_identifiers (violation)
SELECT 'blank_identifier' FROM forecast_records
 WHERE tournament_id IS NULL
    OR typeof(tournament_id) <> 'text'
    OR length(tournament_id) > 200
    OR instr(tournament_id, char(0)) > 0
    OR trim(tournament_id,
            char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                 8232, 8233, 8239, 8287, 12288)) = ''
 LIMIT 1;

INSERT INTO migration_006_requires_non_blank_identifiers (violation)
SELECT 'blank_identifier' FROM submission_attempts
 WHERE attempt_id IS NULL
    OR typeof(attempt_id) <> 'text'
    OR length(attempt_id) > 200
    OR instr(attempt_id, char(0)) > 0
    OR trim(attempt_id,
            char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                 8232, 8233, 8239, 8287, 12288)) = ''
 LIMIT 1;

INSERT INTO migration_006_requires_non_blank_identifiers (violation)
SELECT 'blank_identifier' FROM submission_attempts
 WHERE idempotency_key IS NULL
    OR typeof(idempotency_key) <> 'text'
    OR length(idempotency_key) > 200
    OR instr(idempotency_key, char(0)) > 0
    OR trim(idempotency_key,
            char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                 8232, 8233, 8239, 8287, 12288)) = ''
 LIMIT 1;

-- No length probe, matching the trigger below. See its comment for why.
INSERT INTO migration_006_requires_non_blank_identifiers (violation)
SELECT 'blank_identifier' FROM research_runs
 WHERE retrieval_run_id IS NULL
    OR typeof(retrieval_run_id) <> 'text'
    OR instr(retrieval_run_id, char(0)) > 0
    OR trim(retrieval_run_id,
            char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                 8232, 8233, 8239, 8287, 12288)) = ''
 LIMIT 1;

DROP TABLE migration_006_requires_non_blank_identifiers;

-- ---------------------------------------------------------------------------
-- The three insert triggers, redefined.
-- ---------------------------------------------------------------------------
--
-- A trigger's body cannot be ALTERed, so each is dropped and recreated by the same name.
-- That touches only the trigger's own definition -- not the table's rows and not its
-- append-only block triggers -- which is why this is not the table-rebuild hazard 003's
-- header describes for widening a CHECK. 004 established the pattern when it extended
-- this same forecast_records trigger; the bodies below are those definitions with the
-- identifier clauses added and nothing else changed.

DROP TRIGGER forecast_records_require_draft_on_insert;

CREATE TRIGGER forecast_records_require_draft_on_insert
BEFORE INSERT ON forecast_records
FOR EACH ROW
BEGIN
    -- M1-607. `record_id` is the value every other table in this ledger points at:
    -- approval_events, submission_attempts, resolution_events and score_events all
    -- reference it, and lifecycle.py and approval.py both look a record up by it. A blank
    -- one is a record nothing can cite meaningfully; a blob one is a record their
    -- `_stored_text` refuses to hand back, so the row is append-only and unreadable at the
    -- same time. The 200-character ceiling is the readers': every public entry point in
    -- lifecycle.py and approval.py caps `record_id` at `_MAX_IDENTIFIER`, so a longer one
    -- written through raw SQL could never be looked up again (004's finding B1, on this
    -- column). The NUL check is the other half of that finding: SQLite's `length()` stops
    -- counting at an embedded NUL, so a 202-character id with a NUL early in it reads as
    -- `length() == 1` here and passes, while the readers' Python `len()` sees 202 and
    -- refuses it -- the unreadable row the ceiling exists to prevent, reopened through the
    -- one input the two counting functions disagree about.
    SELECT RAISE(ABORT, 'forecast_records: record_id must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.record_id IS NULL
       OR typeof(NEW.record_id) <> 'text'
       OR length(NEW.record_id) > 200
       OR instr(NEW.record_id, char(0)) > 0
       OR trim(NEW.record_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    -- M1-607. The same clause pipeline_failure_events.tournament_id already carries, on
    -- the column 004's identity-stability probe compares it against: that probe refuses a
    -- forecast record whose attempt_id was recorded under a *different* tournament, and it
    -- can only be as trustworthy as the weaker of the two columns it joins. 004 guarded
    -- one end; this guards the other. The ceiling matches `_require_identifier`, which is
    -- what the failure-table writer already applies to its own copy of this value.
    SELECT RAISE(ABORT, 'forecast_records: tournament_id must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.tournament_id IS NULL
       OR typeof(NEW.tournament_id) <> 'text'
       OR length(NEW.tournament_id) > 200
       OR instr(NEW.tournament_id, char(0)) > 0
       OR trim(NEW.tournament_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    -- Everything below this line is 004's definition, unchanged.

    SELECT RAISE(ABORT, 'forecast_records: a new record must be created with status draft; every later state is reachable only through lifecycle_events')
    WHERE NEW.status <> 'draft';

    SELECT RAISE(ABORT, 'forecast_records: forecast_sha256 is required and must be 64 lowercase hex characters')
    WHERE NEW.forecast_sha256 IS NULL
       OR typeof(NEW.forecast_sha256) <> 'text'
       OR length(NEW.forecast_sha256) <> 64
       OR NEW.forecast_sha256 GLOB '*[^0-9a-f]*';

    SELECT RAISE(ABORT, 'forecast_records: attempt_id is required and must be at most 200 characters and no NUL')
    WHERE NEW.attempt_id IS NULL
       OR typeof(NEW.attempt_id) <> 'text'
       OR length(NEW.attempt_id) > 200
       OR instr(NEW.attempt_id, char(0)) > 0
       OR trim(NEW.attempt_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    SELECT RAISE(ABORT, 'forecast_records: attempt_id was used for a different question or tournament')
    WHERE EXISTS (
        SELECT 1 FROM pipeline_failure_events
         WHERE attempt_id = NEW.attempt_id
           AND (question_id <> NEW.question_id OR tournament_id <> NEW.tournament_id)
    );
END;

DROP TRIGGER submission_attempts_require_receipt_on_insert;

CREATE TRIGGER submission_attempts_require_receipt_on_insert
BEFORE INSERT ON submission_attempts
FOR EACH ROW
BEGIN
    -- M1-607. `attempt_id` is this table's primary key and the value
    -- submission_verifications joins on to decide whether an uncertain submission becomes
    -- `submitted` or `failed`; `idempotency_key` is what makes a retry provably the same
    -- request rather than a second one. A blank or blob value in either is a receipt that
    -- cannot be cited or compared -- on a table 003 made append-only, so it cannot be
    -- corrected. Both ceilings are the writer's `_MAX_IDENTIFIER`, and the NUL check
    -- keeps SQLite's `length()` from disagreeing with the writer's `len()` about the same
    -- string (004, finding B1, rounds 1 and 2).
    SELECT RAISE(ABORT, 'submission_attempts: attempt_id must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.attempt_id IS NULL
       OR typeof(NEW.attempt_id) <> 'text'
       OR length(NEW.attempt_id) > 200
       OR instr(NEW.attempt_id, char(0)) > 0
       OR trim(NEW.attempt_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    SELECT RAISE(ABORT, 'submission_attempts: idempotency_key must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.idempotency_key IS NULL
       OR typeof(NEW.idempotency_key) <> 'text'
       OR length(NEW.idempotency_key) > 200
       OR instr(NEW.idempotency_key, char(0)) > 0
       OR trim(NEW.idempotency_key,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    -- Everything below this line is 003's definition, unchanged.

    SELECT RAISE(ABORT, 'submission_attempts: completed_at_utc is required; an attempt is recorded once, after it has finished')
    WHERE NEW.completed_at_utc IS NULL;

    SELECT RAISE(ABORT, 'submission_attempts: requested_at_utc and completed_at_utc must be UTC timestamps of the form YYYY-MM-DDTHH:MM:SS.ffffff+00:00')
    WHERE typeof(NEW.requested_at_utc) <> 'text'
       OR typeof(NEW.completed_at_utc) <> 'text'
       OR length(NEW.requested_at_utc) <> 32
       OR length(NEW.completed_at_utc) <> 32
       OR NEW.requested_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
       OR NEW.completed_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00';

    SELECT RAISE(ABORT, 'submission_attempts: completed_at_utc is earlier than requested_at_utc')
    WHERE NEW.completed_at_utc < NEW.requested_at_utc;

    SELECT RAISE(ABORT, 'submission_attempts: http_status must be an integer HTTP status code between 100 and 599')
    WHERE NEW.http_status IS NOT NULL
      AND (typeof(NEW.http_status) <> 'integer'
           OR NEW.http_status < 100
           OR NEW.http_status > 599);
END;

DROP TRIGGER research_runs_require_question_on_insert;

CREATE TRIGGER research_runs_require_question_on_insert
BEFORE INSERT ON research_runs
FOR EACH ROW
BEGIN
    -- M1-607. `retrieval_run_id` is the evidence pointer: research_documents and
    -- forecast_records both reference it, and it is part of M1-305's dedup key and of
    -- M1-306's research packet. A blank one detaches a forecast from the evidence it was
    -- made on, which is the attribution claim this ledger exists to support.
    --
    -- **No 200-character ceiling here, unlike the four columns above, and that asymmetry
    -- is deliberate.** The ceiling in 004 closes one specific defect: raw SQL writing an
    -- identifier longer than the *reader* accepts, producing an append-only row that can
    -- never be read back. Every reader of the columns above caps at `_MAX_IDENTIFIER`;
    -- `store.load_run` and `store.load_documents` impose no length limit at all, so that
    -- defect does not exist on this column -- and inventing a ceiling would refuse input
    -- the shipped M1-306 writer accepts, which is the "behaviour change to merged code"
    -- this whole item was filed to avoid doing by accident.
    --
    -- The NUL check stays, because it is justified independently of any ceiling: a NUL is
    -- the one input where SQLite's length()/trim() and Python's len()/strip() disagree
    -- about the *same* string, so a NUL-bearing identifier is one the schema and the
    -- writer cannot both reason about.
    SELECT RAISE(ABORT, 'research_runs: retrieval_run_id must be non-blank text and no NUL')
    WHERE NEW.retrieval_run_id IS NULL
       OR typeof(NEW.retrieval_run_id) <> 'text'
       OR instr(NEW.retrieval_run_id, char(0)) > 0
       OR trim(NEW.retrieval_run_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    -- Everything below this line is 002's definition, unchanged.

    SELECT RAISE(ABORT, 'research_runs: question_id is required')
    WHERE NEW.question_id IS NULL;

    SELECT RAISE(ABORT, 'research_runs: provider is not in the schema vocabulary')
    WHERE NEW.provider NOT IN ('asknews', 'exa', 'structured', 'xai_x_search');

    SELECT RAISE(ABORT, 'research_runs: provider xai_x_search requires agent_model and posts_dropped_no_url')
    WHERE NEW.provider = 'xai_x_search'
      AND (NEW.agent_model IS NULL
           OR trim(NEW.agent_model) = ''
           OR NEW.posts_dropped_no_url IS NULL);

    SELECT RAISE(ABORT, 'research_runs: cost_usd must be a finite non-negative number')
    WHERE NEW.cost_usd IS NOT NULL
      AND (typeof(NEW.cost_usd) NOT IN ('integer', 'real')
           OR NEW.cost_usd < 0
           OR NEW.cost_usd = 9e999);
END;
