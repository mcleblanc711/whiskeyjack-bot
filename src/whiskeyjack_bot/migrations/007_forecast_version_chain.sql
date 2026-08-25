-- M1-602: the forecast version chain, enforced in the schema.
--
-- `forecast_records` has existed since `001_initial.sql` and has been read by
-- approval.py, submission.py and lifecycle.py ever since. Until this item nothing in
-- `src/` had ever written a row: every `INSERT INTO forecast_records` in the tree was
-- raw SQL inside a test fixture. This migration lands with that first writer, which is
-- the M1-607 precedent applied again -- guard the column as the writer for it arrives,
-- not afterwards.
--
-- WHAT `001` ALREADY GUARANTEES, AND WHAT IT DOES NOT
--
-- `001` gives `forecast_version INTEGER NOT NULL`, a self-referencing
-- `parent_record_id TEXT REFERENCES forecast_records (record_id)`, and
-- `UNIQUE (question_id, tournament_id, forecast_version)`. Together those forbid two
-- rows claiming the same version of the same question, and (with
-- `PRAGMA foreign_keys = ON`) forbid a parent pointer naming no row at all.
--
-- They do not forbid any of this:
--
--   * `forecast_version = 0`, or `-3`, or `2**63-1`;
--   * version 1 carrying a parent, so the chain has no root;
--   * version 4 carrying no parent, so the chain has a hole nothing records;
--   * version 2 of question 100 pointing at version 1 of question **200**, or at
--     version 1 of the same question in a *different tournament* -- a parent pointer
--     that satisfies the foreign key perfectly while linking two unrelated forecasts;
--   * version 5 pointing at version 1, skipping three versions that exist;
--   * a record pointing at itself, once its own row exists.
--
-- Every one of those is an attribution claim the ledger cannot stand behind, and
-- `003_lifecycle_events.sql` made this table append-only -- `forecast_records_block_update`
-- and `forecast_records_block_delete` -- so **none of them can be corrected afterwards**.
-- The linkage has to be right at INSERT or it is wrong permanently. That is the whole
-- argument for spending a migration here rather than trusting the writer.
--
-- WHY BOTH LAYERS
--
-- `forecast/store.py` computes the next version and the parent inside a single
-- `BEGIN IMMEDIATE` and would never construct one of the shapes above. That is not a
-- reason to leave the schema open. M1-603's round 5 is the case: a rule enforced in the
-- writer and *not* in the schema was defeated by a value the two layers disagreed about
-- (`str.strip()` versus SQL `trim()`), and a record reached `submitted` on two bytes of
-- nothing. A rule that lives in one layer is a rule that holds until someone writes a
-- second writer, a fixture, or a repair script.
--
-- THE CONTIGUITY ARGUMENT, WRITTEN OUT
--
-- The parent clause below requires `parent.forecast_version = NEW.forecast_version - 1`
-- for the *same* `(question_id, tournament_id)`. Contiguity of the whole chain follows by
-- induction and needs no separate clause: version 1 is the root, and version N cannot
-- exist unless version N-1 does. `001`'s UNIQUE constraint supplies the other half --
-- two children cannot share a parent, because they would have to share a version number.
-- So the chain is a path, not a tree and not a forest.
--
-- ONLY THE INSERT TRIGGER IS TOUCHED
--
-- A trigger's body cannot be ALTERed, so `forecast_records_require_draft_on_insert` is
-- dropped and recreated by the same name -- the pattern `004` established and `006`
-- followed. That touches the trigger's own definition and nothing else: not the table's
-- rows, not its append-only block triggers, and not the CHECK constraints, so this is not
-- the table-rebuild hazard `003`'s header describes. The body below is `006`'s
-- definition with the M1-602 clauses appended and **nothing else changed**.
--
-- NO BACKFILL PROBE, AND WHY
--
-- `006` opened with a temp-table probe that refuses to upgrade a ledger already holding a
-- violating row. There is nothing to probe here: this migration is the first thing in the
-- project's history that constrains a column no production writer has ever filled. A
-- pre-007 database holding a `forecast_records` row was written by raw SQL, and the
-- honest answer for one is the same as `003`'s -- it predates the guarantee. Adding a
-- probe would refuse to upgrade exactly the test fixtures this suite builds several
-- hundred of, for a population that is empty by construction.

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

    -- Everything above this line is 006's definition, unchanged. Everything below is
    -- M1-602's.

    -- A version number is a position in a chain, so it is a positive integer or it is
    -- nothing. `typeof()` is checked for the reason 006 gives about blobs: INTEGER
    -- affinity converts '2' to 2 on the way in, so what this actually catches is a blob
    -- or a real -- a `forecast_version` of 2.5 would otherwise sit permanently between
    -- two versions, and `submission.submission_key_for_record` reads this column into an
    -- idempotency key.
    SELECT RAISE(ABORT, 'forecast_records: forecast_version must be an integer of at least 1')
    WHERE NEW.forecast_version IS NULL
       OR typeof(NEW.forecast_version) <> 'integer'
       OR NEW.forecast_version < 1;

    -- Version 1 is the root of a chain and has nothing before it. A version 1 carrying a
    -- parent is either a mis-numbered later version or a link to an unrelated forecast;
    -- both are attribution claims, and neither can be withdrawn once written.
    SELECT RAISE(ABORT, 'forecast_records: forecast version 1 is the root of a chain and must have no parent')
    WHERE NEW.forecast_version = 1
      AND NEW.parent_record_id IS NOT NULL;

    -- The converse. "Repeat forecasts append a new version and reference the previous
    -- record" (CODEX_HANDOFF.md); a version above 1 with no parent is a chain with a hole
    -- in it, and the hole is unrecorded rather than merely unfilled.
    SELECT RAISE(ABORT, 'forecast_records: a forecast version above 1 must name the record it supersedes')
    WHERE NEW.forecast_version > 1
      AND NEW.parent_record_id IS NULL;

    -- What the foreign key cannot say. `REFERENCES forecast_records (record_id)` proves
    -- the parent row exists and nothing else: it is satisfied just as well by version 1 of
    -- a different question, by a record in another tournament, or by version 1 when the
    -- new row claims version 5. This clause is the one that makes `parent_record_id` mean
    -- "the immediately preceding version of this same forecast".
    --
    -- One EXISTS over all three conditions rather than three probes, because the failure
    -- being reported is one thing -- the pointer does not name this chain's previous
    -- version -- and splitting it would tempt a message that names which of the three
    -- failed, i.e. that echoes the stored row it compared against.
    --
    -- This also settles self-reference without a separate clause: a row's own record_id
    -- does not exist in this table yet at BEFORE INSERT time, so the EXISTS finds nothing
    -- and the insert is refused here.
    SELECT RAISE(ABORT, 'forecast_records: parent_record_id must name the immediately preceding version of the same question and tournament')
    WHERE NEW.parent_record_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM forecast_records
           WHERE record_id = NEW.parent_record_id
             AND question_id = NEW.question_id
             AND tournament_id = NEW.tournament_id
             AND forecast_version = NEW.forecast_version - 1
      );

    -- `record_json` is the canonical record -- the thing `forecast_sha256` digests and the
    -- thing M1-604 exports. `final_prediction_json` is the typed forecast on its own.
    -- `001` makes both NOT NULL and nothing more, so '' and 'not json at all' were both
    -- storable, permanently, on a row whose hash claims to attest to them.
    --
    -- `json_type(...) = 'object'` is the stricter reading of "the complete Pydantic record
    -- as canonical JSON" and subsumes `json_valid()`: it requires well-formed JSON *and*
    -- that it be an object. Both writers emit `model_dump(mode="json")` of a pydantic
    -- model, which is an object in every case, so nothing legitimate is excluded. A bare
    -- scalar or array in either column would be a shape no reader in this project knows
    -- how to interpret. Note the cost, since this trigger is immutable once merged:
    -- storing a non-object in either column later needs a migration to widen it.
    --
    -- typeof() first, because `json_type()` applied to a blob raises SQLITE_ERROR rather
    -- than returning NULL, and an aborted statement is not the same as a refusal carrying
    -- this schema's own message.
    SELECT RAISE(ABORT, 'forecast_records: record_json must be a JSON object')
    WHERE NEW.record_json IS NULL
       OR typeof(NEW.record_json) <> 'text'
       OR json_valid(NEW.record_json) = 0
       OR json_type(NEW.record_json) <> 'object';

    SELECT RAISE(ABORT, 'forecast_records: final_prediction_json must be a JSON object')
    WHERE NEW.final_prediction_json IS NULL
       OR typeof(NEW.final_prediction_json) <> 'text'
       OR json_valid(NEW.final_prediction_json) = 0
       OR json_type(NEW.final_prediction_json) <> 'object';

    -- The closed vocabulary from `config.SupportedQuestionType`, pinned here for the
    -- reason 003's header gives about its own CHECK vocabularies: this branch is the
    -- first writer of the column, and a vocabulary is cheapest to get right before the
    -- first row exists. `questions/events.py` already refuses to forecast an unsupported
    -- type -- it records a deferral instead -- so no reachable path produces a fourth
    -- value today; what this closes is the raw-SQL and future-writer path.
    --
    -- Not `config.supported_question_types`, which is the operator's *subset* of these
    -- three and can legitimately be narrower. Widening this list -- adding `discrete`,
    -- say -- costs one more DROP/CREATE of this trigger in a later migration, which is
    -- the same cheap escape hatch 004 and 006 used, and not a table rebuild.
    SELECT RAISE(ABORT, 'forecast_records: question_type must be one of the supported question types')
    WHERE NEW.question_type IS NULL
       OR typeof(NEW.question_type) <> 'text'
       OR NEW.question_type NOT IN ('binary', 'multiple_choice', 'numeric');
END;
