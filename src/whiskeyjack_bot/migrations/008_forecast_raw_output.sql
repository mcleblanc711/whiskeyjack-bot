-- M1-406: raw model output, its cost, and what it took to get it.
--
-- `generate_forecast` returns a `ForecastGeneration` carrying the rendered request, every
-- raw provider reply, the invocation count and the cost. M1-602 shipped the first writer
-- of `forecast_records` and persisted none of them -- its own notes list all four under
-- "Deferred -> M1-406", because this table had no column for one. These three columns are
-- that, and they are what make the acceptance criterion checkable: *"model replay makes
-- zero API calls and reproduces the parsed forecast hash"* needs the ledger to say where
-- the stored provider text is.
--
-- WHY COLUMNS AND NOT A `forecast_model_calls` TABLE
--
-- `research_runs` already carries `raw_response_path` and `cost_usd` on the run row, and a
-- forecast is one attempt with at most two invocations of one model -- not a collection.
-- A per-invocation table would be the right shape for a variable number of calls; here it
-- would add a join to every reader in exchange for a row count that is always 1 or 2, and
-- the repair turn's own text is already in the artifact this path names. (Owner decision at
-- plan time.)
--
-- WHY NOT IN `record_json`
--
-- Because `RECORD_SCHEMA_VERSION` is a promise about bytes already written.
-- `forecast/record.py` digests the record's canonical JSON into `forecast_sha256`, and
-- approval binds to that hash; adding a field changes every future digest while stored
-- records keep their old ones, so an approval bound to one stops verifying. Cost and
-- invocation count are *facts about the call*, not part of the forecast's content, and
-- keeping them out of the hashed record is what lets them be recorded at all.
--
-- NULLABILITY
--
-- All three are NULLable for 002/003/004's reason: `ADD COLUMN` cannot add NOT NULL
-- without a default, and no default is honest for a pre-existing row nobody measured. New
-- rows are constrained by the BEFORE INSERT trigger below instead. NULL is also a real
-- value on the write path and not merely a legacy artifact -- see the `raw_output_path`
-- clause on why a lost or unretained artifact must not cost the row.
--
-- ONLY THE INSERT TRIGGER IS TOUCHED
--
-- A trigger's body cannot be ALTERed, so `forecast_records_require_draft_on_insert` is
-- dropped and recreated by the same name -- the pattern 004, 006 and 007 established. That
-- touches the trigger's own definition and nothing else: not the table's rows, not its
-- append-only block triggers, and not the CHECK constraints, so this is not the
-- table-rebuild hazard 003's header describes. The body below is 007's definition with
-- M1-406's clauses appended and NOTHING ELSE CHANGED.
--
-- NO BACKFILL PROBE
--
-- 007's reason, verbatim: this migration is the first thing in the project's history that
-- constrains columns no writer has ever filled. They do not exist before this file runs, so
-- every pre-008 row holds NULL for all three, which every clause below permits.

ALTER TABLE forecast_records ADD COLUMN raw_output_path   TEXT;
ALTER TABLE forecast_records ADD COLUMN cost_usd          REAL;
ALTER TABLE forecast_records ADD COLUMN model_invocations INTEGER;

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

    -- Everything above this line is 007's definition, unchanged. Everything below is
    -- M1-406's.

    -- `raw_output_path` names the artifact holding the provider text this forecast was
    -- parsed from, and it is stored **relative to storage.artifact_root** so the ledger
    -- stays readable after the artifact directory moves or is opened on another machine
    -- (`research/artifacts.py` gives the same rule for `research_runs.raw_response_path`).
    -- An absolute path defeats that silently: it resolves, it reads, and it keeps
    -- resolving to the wrong machine's tree after a move. A `..` segment defeats the other
    -- half -- the guarantee that a recorded path points inside the artifact root at all.
    --
    -- The `..` test is on whole segments (`'/' || path || '/'` containing `'/../'`) rather
    -- than `instr(path, '..')`, because `require_safe_component` permits `.` inside an
    -- identifier, so an attempt_id of `run..2` is legitimate and names no parent directory.
    -- A substring test would refuse a valid path, and this trigger is immutable once merged.
    --
    -- NULL is permitted and is not a gap in the record: it means the artifact was not
    -- retained (`storage.retain_raw_model_output: false`) or its write failed, and
    -- `forecast/persist.py` commits the row either way because a call that cost money must
    -- stay recorded. Which of the two it was is reported to the caller, not stored here.
    SELECT RAISE(ABORT, 'forecast_records: raw_output_path must be a non-blank relative path of at most 200 characters and no NUL')
    WHERE NEW.raw_output_path IS NOT NULL
      AND (typeof(NEW.raw_output_path) <> 'text'
        OR length(NEW.raw_output_path) > 200
        OR instr(NEW.raw_output_path, char(0)) > 0
        OR NEW.raw_output_path GLOB '/*'
        OR instr('/' || NEW.raw_output_path || '/', '/../') > 0
        OR trim(NEW.raw_output_path,
                char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                     8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                     8232, 8233, 8239, 8287, 12288)) = '');

    -- `cost_usd` NULL means **unknown, not free** -- the rule M1-303 settled and
    -- `generate.py` implements: a total is published only when every attempted call
    -- reported a usable figure, and anything less is a subtotal that would look exactly
    -- like a complete one. So NULL is permitted and a negative number is not: a negative
    -- spend is not a measurement, and this row can never be corrected.
    --
    -- `typeof() IN ('real','integer')` because REAL affinity leaves an integer-valued bind
    -- stored as `integer`; both are numbers and both are accepted. A NaN cannot reach here
    -- -- SQLite stores one as NULL -- and the writer refuses one before binding anyway.
    SELECT RAISE(ABORT, 'forecast_records: cost_usd must be NULL or a non-negative number')
    WHERE NEW.cost_usd IS NOT NULL
      AND (typeof(NEW.cost_usd) NOT IN ('real', 'integer') OR NEW.cost_usd < 0);

    -- `model_invocations` is how many billable calls produced this forecast: 1, or 2 when
    -- the one budgeted repair was spent. The ceiling is `config.MAX_MODEL_INVOCATIONS`,
    -- pinned here as a literal for the reason 007 pins the question-type vocabulary: this
    -- branch is the first writer of the column and a bound is cheapest to get right before
    -- the first row exists. It is not a tunable -- "at most one bounded repair attempt" is
    -- M1-402's acceptance criterion and `ModelConfig` refuses a config that would lift it,
    -- so a value of 3 here is a record of something the pipeline is not allowed to do.
    -- Widening it later costs one more DROP/CREATE of this trigger, the same cheap escape
    -- hatch 004, 006 and 007 used.
    --
    -- A row may carry NULL for the same reason `attempt_id` may: `ADD COLUMN` cannot add
    -- NOT NULL without a default, and no default is honest for a count nobody recorded.
    SELECT RAISE(ABORT, 'forecast_records: model_invocations must be NULL or an integer between 1 and 2')
    WHERE NEW.model_invocations IS NOT NULL
      AND (typeof(NEW.model_invocations) <> 'integer'
        OR NEW.model_invocations < 1
        OR NEW.model_invocations > 2);

END;
