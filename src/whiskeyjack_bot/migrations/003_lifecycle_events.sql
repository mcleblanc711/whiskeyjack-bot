-- M1-603: lifecycle events, and the append-only enforcement 001 and 002 deferred.
--
-- 001 and 002 both say the same thing in their headers: "M1-602/M1-603 add the
-- triggers that forbid UPDATE outright." This is that migration, and it settles the
-- question docs/M1-NOTES.md left open -- how `forecast_records.status` relates to
-- immutability -- in the strictest of the available readings (owner decision):
--
--   A forecast record is written once, as a `draft`, and is never updated. Every
--   later state is reachable ONLY by appending a `lifecycle_events` row. The record's
--   current status is therefore *derived*: the `to_status` of its highest `event_seq`,
--   falling back to `forecast_records.status` for a record with no events yet.
--
-- That is what makes M1-603's acceptance criterion structural rather than a promise
-- the write path keeps. "Injected failures cannot leave an approved/submitted state
-- without its event record" holds because in this schema the approved/submitted state
-- *is* the event record: there is no second place for it to be written, and no
-- statement that can put it there without the event.
--
-- The alternative -- a narrowly constrained UPDATE of `status` alongside its event --
-- was considered and rejected. It keeps one obvious column to read, but it makes a
-- stored record's bytes change after creation, which is exactly the guarantee M1-602
-- ("v1 remains byte-identical") and D25 exist to provide.
--
-- WHAT IS NOT PROTECTED HERE, and why:
--
-- * `research_runs` / `research_documents` get a DELETE block and a *partial* UPDATE
--   block, not a total one. D25's wording is "append forecast versions and lifecycle
--   events", and the handoff describes a research run as carrying started *and*
--   completed timestamps, an error summary and a cost -- i.e. a row M1-306 starts and
--   later finishes. Blocking UPDATE outright would decide that unstarted item's write
--   shape from outside it. So the rule is narrower and says what it means: evidence may
--   be completed and annotated, but never re-identified and never erased. The identity
--   and provenance columns are pinned at the bottom of this file; everything M1-306 has
--   to fill in stays writable. 002's completeness triggers stay live and remain the
--   enforcement on the rest of that pair (migrations are immutable, so they cannot be
--   removed even where they overlap).
-- * `schema_migrations` is untouched. It is the migration runner's own bookkeeping,
--   not ledger content, and ledger.py's schema-drift detection is tested by corrupting
--   it deliberately.
--
-- WHAT THIS MIGRATION CANNOT RECORD, and who owns it:
--
-- Every event here is scoped to a forecast record, and 001 requires a forecast record
-- to carry a non-null final_prediction_json, record_json and retrieval_run_id. So no
-- record exists until generation has already succeeded, and a research or generation
-- failure -- which happens before that -- has nothing to attach to. The first draft
-- shipped `research_failed`/`generation_failed` event types anyway; they were
-- unreachable, and are removed rather than left as a promise the schema cannot keep
-- (GPT review round 1, finding 2). M1-606 owns pre-forecast failures and the
-- attempt-scoped identity they need, in migration 004. `forecast_record_id` is declared
-- nullable-with-a-trigger below so that item does not have to rebuild an append-only
-- table to get there.
--
-- There is no `phase` column. Every event type names its own pipeline phase already
-- (`submission_failed` happens at submission), so a phase column would be a second
-- spelling of `event_type` that has to be kept in agreement with it. The *reason* a
-- phase failed is `detail_code`, which is not derivable and so is stored.
--
-- Timestamps are TEXT ISO-8601 UTC, matching 001. Trigger messages name fields and
-- never interpolate row values, per the project-wide error-hygiene rule.

-- The content hash a forecast record's approval binds to (CLAUDE.md: "approval binds
-- to an exact forecast hash; any content change invalidates it"). Without a stored
-- hash that rule can only ever be a Python-side convention, and M2-701's "changed
-- forecast invalidates prior approval" has nothing to compare against.
--
-- NULLable for 002's reason: SQLite requires a non-null default on an added NOT NULL
-- column, and there is no honest default for a hash -- any value would be a false
-- content claim about a row nobody hashed. New rows are made to carry it by the
-- BEFORE INSERT trigger below instead, which separates the two populations a column
-- default cannot. A pre-003 row keeps its NULL, stays readable, and cannot be
-- approved.
ALTER TABLE forecast_records ADD COLUMN forecast_sha256 TEXT;

-- The lifecycle spine: one row per state transition, ordered per record.
--
-- `event_seq` is per record and contiguous from 1, which is a stronger claim than the
-- rowid's global order: it makes a *missing* event detectable. It is also the
-- concurrency guard. Two writers that both read "current status = validated" and
-- append seq 2 cannot both win -- UNIQUE(forecast_record_id, event_seq) turns the loser
-- into a loud IntegrityError instead of a silently reordered history. (The writers
-- take the write lock up front with BEGIN IMMEDIATE, so this is defence in depth.)
--
-- The detail row is referenced through four typed, nullable foreign keys rather than a
-- polymorphic (related_table, related_id) pair. A polymorphic pair cannot be a real
-- foreign key, and comparing it would mean CASTing across SQLite's affinity rules --
-- the same trap 002 documents for posts_dropped_no_url. Exactly one is set, and which
-- one is fixed by event_type; the trigger enforces both.
--
-- `detail_code` is a CLOSED vocabulary, not free text, and there is deliberately no
-- free-text column on this table at all. A failure's provider-supplied text lives in
-- `submission_attempts.error_message` / `response_body`, which this row *points at* --
-- so the lifecycle log can be read, exported and logged without carrying untrusted
-- content. Same reasoning as the derived `DeferralReason` in questions/events.py.
CREATE TABLE lifecycle_events (
    event_id              INTEGER PRIMARY KEY,
    -- Nullable in the DDL, mandatory in the trigger. Every event type defined today is
    -- scoped to a forecast record and the trigger's first probe rejects a NULL, so the
    -- constraint in force is exactly NOT NULL. The column is declared nullable so that
    -- M1-606 can add pre-forecast, attempt-scoped events without rebuilding this table:
    -- SQLite's ALTER TABLE cannot relax NOT NULL, and rebuilding an append-only table
    -- means dropping and recreating its block triggers, which is precisely the operation
    -- the ledger exists to make impossible. Cheap insurance, no change in behaviour.
    --
    -- For M1-606: UNIQUE (forecast_record_id, event_seq) below treats NULLs as distinct,
    -- so an attempt-scoped event will need its own uniqueness over its own identity.
    forecast_record_id    TEXT REFERENCES forecast_records (record_id),
    event_seq             INTEGER NOT NULL,
    -- Kept in step with whiskeyjack_bot.lifecycle.LifecycleEventType, which explains why
    -- `research_failed` and `generation_failed` are not here: no forecast_records row
    -- exists until generation has succeeded, so those events had nothing to attach to.
    event_type            TEXT NOT NULL CHECK (
        event_type IN (
            'validated', 'validation_failed', 'rejected', 'approved',
            'submitted', 'submission_failed', 'resolved', 'scored'
        )
    ),
    from_status           TEXT NOT NULL CHECK (
        from_status IN ('draft', 'validated', 'approved', 'submitted', 'failed', 'resolved', 'scored')
    ),
    to_status             TEXT NOT NULL CHECK (
        to_status IN ('draft', 'validated', 'approved', 'submitted', 'failed', 'resolved', 'scored')
    ),
    detail_code           TEXT CHECK (
        detail_code IS NULL OR detail_code IN (
            'provider_error', 'provider_unavailable', 'no_evidence', 'stale_evidence',
            'malformed_response', 'schema_invalid', 'calibration_invalid',
            'rejected_by_reviewer', 'http_error', 'timeout', 'refetch_mismatch',
            'refetch_missing', 'internal_error'
        )
    ),
    approval_event_id     INTEGER REFERENCES approval_events (event_id),
    submission_attempt_id TEXT REFERENCES submission_attempts (attempt_id),
    resolution_event_id   INTEGER REFERENCES resolution_events (event_id),
    score_event_id        INTEGER REFERENCES score_events (event_id),
    -- When the thing being recorded happened (caller-supplied, so a replayed run can
    -- reproduce it) versus when the ledger stored the row (writer-owned).
    occurred_at_utc       TEXT NOT NULL,
    created_at_utc        TEXT NOT NULL,
    UNIQUE (forecast_record_id, event_seq)
);

-- No separate index on forecast_record_id: 001 added those because SQLite does not
-- auto-index a plain REFERENCES column, and the UNIQUE above already indexes this one
-- with forecast_record_id leftmost.

-- The state machine. `to_status = 'failed'` is terminal by omission -- there is no
-- transition out of it, because a retry is a new forecast *version* (M1-602), not a
-- resurrected record. `rejected` is deliberately validated -> validated: the seven
-- states in 001's CHECK have no 'rejected' member, and the handoff requires a rejected
-- approval to "leave the last valid record intact". Rejection records a decision; it
-- does not move the record.
--
-- The same table is spelled out in whiskeyjack_bot.lifecycle._LEGAL_TRANSITIONS. The
-- duplication is deliberate -- the database is the enforcement, the Python table is the
-- writer -- and tests/unit/test_lifecycle.py drives every possible triple through this
-- trigger and asserts the two agree exactly, so they cannot drift.
CREATE TRIGGER lifecycle_events_validate_on_insert
BEFORE INSERT ON lifecycle_events
FOR EACH ROW
BEGIN
    -- The column is nullable in the DDL and mandatory here; see the note above it. This
    -- probe runs first so a NULL gets its own message rather than falling through to the
    -- record-exists probe, whose NOT EXISTS would also be true.
    SELECT RAISE(ABORT, 'lifecycle_events: forecast_record_id is required')
    WHERE NEW.forecast_record_id IS NULL;

    -- A BEFORE INSERT trigger runs ahead of the foreign-key check, so an unknown record
    -- fails here and carries this schema's own message rather than a generic FK one.
    SELECT RAISE(ABORT, 'lifecycle_events: forecast_record_id does not name a stored forecast record')
    WHERE NOT EXISTS (SELECT 1 FROM forecast_records WHERE record_id = NEW.forecast_record_id);

    -- typeof() because INTEGER is affinity, not a type: without it '2' and 2.5 both
    -- satisfy the arithmetic below and are stored as-is (002 documents the same trap).
    SELECT RAISE(ABORT, 'lifecycle_events: event_seq must be a positive integer')
    WHERE typeof(NEW.event_seq) <> 'integer' OR NEW.event_seq < 1;

    SELECT RAISE(ABORT, 'lifecycle_events: event_seq must be the next sequence number for this record')
    WHERE NEW.event_seq <> COALESCE(
        (SELECT max(event_seq) FROM lifecycle_events
          WHERE forecast_record_id = NEW.forecast_record_id),
        0
    ) + 1;

    -- The appended event must start where the record actually is. This is what stops a
    -- caller from asserting its own starting point and skipping a state.
    SELECT RAISE(ABORT, 'lifecycle_events: from_status does not match the record''s current status')
    WHERE NEW.from_status <> COALESCE(
        (SELECT to_status FROM lifecycle_events
          WHERE forecast_record_id = NEW.forecast_record_id
          ORDER BY event_seq DESC LIMIT 1),
        (SELECT status FROM forecast_records WHERE record_id = NEW.forecast_record_id)
    );

    SELECT RAISE(ABORT, 'lifecycle_events: (event_type, from_status, to_status) is not a legal transition')
    WHERE NOT (
           (NEW.event_type = 'validated'         AND NEW.from_status = 'draft'     AND NEW.to_status = 'validated')
        OR (NEW.event_type = 'validation_failed' AND NEW.from_status = 'draft'     AND NEW.to_status = 'failed')
        OR (NEW.event_type = 'validation_failed' AND NEW.from_status = 'validated' AND NEW.to_status = 'failed')
        OR (NEW.event_type = 'rejected'          AND NEW.from_status = 'validated' AND NEW.to_status = 'validated')
        OR (NEW.event_type = 'approved'          AND NEW.from_status = 'validated' AND NEW.to_status = 'approved')
        OR (NEW.event_type = 'submitted'         AND NEW.from_status = 'approved'  AND NEW.to_status = 'submitted')
        OR (NEW.event_type = 'submission_failed' AND NEW.from_status = 'approved'  AND NEW.to_status = 'failed')
        OR (NEW.event_type = 'resolved'          AND NEW.from_status = 'submitted' AND NEW.to_status = 'resolved')
        OR (NEW.event_type = 'scored'            AND NEW.from_status = 'resolved'  AND NEW.to_status = 'scored')
    );

    -- A failure that does not say why is an unfalsifiable claim about the pipeline,
    -- which is the same objection 002 raised against an unconstrained accountability
    -- counter.
    SELECT RAISE(ABORT, 'lifecycle_events: an event ending in failed requires detail_code')
    WHERE NEW.to_status = 'failed' AND NEW.detail_code IS NULL;

    -- Exactly one detail foreign key, and the right one for the event type.
    SELECT RAISE(ABORT, 'lifecycle_events: an approval event must link exactly one approval_events row')
    WHERE NEW.event_type IN ('approved', 'rejected')
      AND (NEW.approval_event_id IS NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: a submission event must link exactly one submission_attempts row')
    WHERE NEW.event_type IN ('submitted', 'submission_failed')
      AND (NEW.submission_attempt_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: a resolution event must link exactly one resolution_events row')
    WHERE NEW.event_type = 'resolved'
      AND (NEW.resolution_event_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: a score event must link exactly one score_events row')
    WHERE NEW.event_type = 'scored'
      AND (NEW.score_event_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: this event type carries no detail row')
    WHERE NEW.event_type IN ('validated', 'validation_failed')
      AND (NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    -- A detail row belonging to another forecast record would make the lifecycle log
    -- cite evidence that is not about this forecast.
    SELECT RAISE(ABORT, 'lifecycle_events: the linked approval_events row is for another forecast record or records a different decision')
    WHERE NEW.approval_event_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM approval_events
           WHERE event_id = NEW.approval_event_id
             AND forecast_record_id = NEW.forecast_record_id
             AND decision = NEW.event_type
      );

    SELECT RAISE(ABORT, 'lifecycle_events: the linked submission_attempts row is for another forecast record')
    WHERE NEW.submission_attempt_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND forecast_record_id = NEW.forecast_record_id
      );

    -- M2-704's "success requires refetch confirmation", made a constraint instead of a
    -- convention. A `submitted` state that no refetch confirmed is precisely the
    -- unverified claim the ledger exists to prevent; an attempt that timed out or whose
    -- refetch disagreed is a failure with a detail_code, not a submission.
    SELECT RAISE(ABORT, 'lifecycle_events: a submitted event requires a successful, refetch-verified attempt')
    WHERE NEW.event_type = 'submitted'
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND success = 1
             AND verified_by_refetch = 1
      );

    -- The exact complement of the probe above, and it has to be the complement rather
    -- than `success = 0`: an attempt that posted successfully but whose refetch did not
    -- confirm it (M2-704's uncertain timeout) is neither a verified success nor an
    -- outright failure. Requiring success = 0 here would leave that state unable to
    -- record any lifecycle event at all -- a verification mismatch with no ledger
    -- event, which the handoff's failure-boundary rule forbids. Complementary probes
    -- mean every attempt has exactly one legal event, and `detail_code`
    -- (refetch_mismatch / refetch_missing / timeout / http_error) carries which case.
    SELECT RAISE(ABORT, 'lifecycle_events: a submission_failed event requires an attempt that is not a refetch-verified success')
    WHERE NEW.event_type = 'submission_failed'
      AND EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND success = 1
             AND verified_by_refetch = 1
      );

    SELECT RAISE(ABORT, 'lifecycle_events: the linked resolution_events row is for another forecast record')
    WHERE NEW.resolution_event_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM resolution_events
           WHERE event_id = NEW.resolution_event_id
             AND forecast_record_id = NEW.forecast_record_id
      );

    SELECT RAISE(ABORT, 'lifecycle_events: the linked score_events row is for another forecast record')
    WHERE NEW.score_event_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM score_events
           WHERE event_id = NEW.score_event_id
             AND forecast_record_id = NEW.forecast_record_id
      );
END;

-- A record is born a draft. Without this, a writer could INSERT a row already claiming
-- `approved` and satisfy every other constraint in the schema -- an approved state with
-- no approval event, which is the exact failure M1-603 is accepted against.
--
-- The hash probe is here rather than as a column CHECK because ADD COLUMN cannot
-- retrofit NOT NULL, and because the distinction being drawn is between rows written
-- before this migration and rows written after it.
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
END;

-- Approval binds to an exact forecast hash. The COALESCE to '' is what makes a
-- pre-003 record (NULL hash) unapprovable rather than approvable-by-any-hash: no
-- 64-character hex string equals ''. A missing record fails here too, ahead of the
-- foreign key, so the message is this schema's own.
CREATE TRIGGER approval_events_bind_forecast_hash_on_insert
BEFORE INSERT ON approval_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'approval_events: forecast_sha256 must be 64 lowercase hex characters')
    WHERE NEW.forecast_sha256 IS NULL
       OR typeof(NEW.forecast_sha256) <> 'text'
       OR length(NEW.forecast_sha256) <> 64
       OR NEW.forecast_sha256 GLOB '*[^0-9a-f]*';

    SELECT RAISE(ABORT, 'approval_events: forecast_sha256 does not match the stored hash of this forecast record')
    WHERE NEW.forecast_sha256 <> COALESCE(
        (SELECT forecast_sha256 FROM forecast_records WHERE record_id = NEW.forecast_record_id),
        ''
    );
END;

-- Append-only enforcement (D25). SQLite has no multi-event trigger, so UPDATE and
-- DELETE are separate objects per table; 002 documents the same limitation for its
-- duplicated insert/update bodies.
--
-- These are what make "never overwrite history" a property of the database rather than
-- of the code that happens to be writing today. Every one of them is unconditional:
-- there is no legitimate UPDATE or DELETE against a ledger row, so there is no
-- guarded WHERE to get wrong.

CREATE TRIGGER forecast_records_block_update
BEFORE UPDATE ON forecast_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'forecast_records is append-only: a stored forecast version is never updated (D25); write a new version instead');
END;

CREATE TRIGGER forecast_records_block_delete
BEFORE DELETE ON forecast_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'forecast_records is append-only: a stored forecast version is never deleted (D25)');
END;

CREATE TRIGGER lifecycle_events_block_update
BEFORE UPDATE ON lifecycle_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'lifecycle_events is append-only: a recorded lifecycle event is never updated (D25)');
END;

CREATE TRIGGER lifecycle_events_block_delete
BEFORE DELETE ON lifecycle_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'lifecycle_events is append-only: a recorded lifecycle event is never deleted (D25)');
END;

CREATE TRIGGER approval_events_block_update
BEFORE UPDATE ON approval_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'approval_events is append-only: an approval decision is never updated (D25)');
END;

CREATE TRIGGER approval_events_block_delete
BEFORE DELETE ON approval_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'approval_events is append-only: an approval decision is never deleted (D25)');
END;

CREATE TRIGGER submission_attempts_block_update
BEFORE UPDATE ON submission_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_attempts is append-only: an attempt is never overwritten; record a new attempt');
END;

CREATE TRIGGER submission_attempts_block_delete
BEFORE DELETE ON submission_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_attempts is append-only: an attempt is never deleted');
END;

CREATE TRIGGER resolution_events_block_update
BEFORE UPDATE ON resolution_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'resolution_events is append-only: an ingested resolution is never updated (D25)');
END;

CREATE TRIGGER resolution_events_block_delete
BEFORE DELETE ON resolution_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'resolution_events is append-only: an ingested resolution is never deleted (D25)');
END;

CREATE TRIGGER score_events_block_update
BEFORE UPDATE ON score_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'score_events is append-only: a computed score is never updated (D25)');
END;

CREATE TRIGGER score_events_block_delete
BEFORE DELETE ON score_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'score_events is append-only: a computed score is never deleted (D25)');
END;

-- Evidence: completable, never erasable. See the header for why UPDATE is left to
-- 002's completeness triggers on this pair.

CREATE TRIGGER research_runs_block_delete
BEFORE DELETE ON research_runs
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_runs is append-only: a retrieval run is never deleted');
END;

CREATE TRIGGER research_documents_block_delete
BEFORE DELETE ON research_documents
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_documents is append-only: retrieved evidence is never deleted');
END;

-- ... but "completable" is not "rewritable". The DELETE blocks above stop evidence being
-- erased and left nothing stopping it being re-identified in place: a stored run could
-- have its provider or start time rewritten, or a document its URL and content hash,
-- which detaches the evidence from what was actually retrieved just as effectively as
-- deleting it (GPT review round 1, non-blocking observation).
--
-- So identity and provenance are pinned and the completion columns stay open. What is
-- deliberately still writable: research_runs.completed_at_utc / error_summary / cost_usd
-- / raw_response_path / queries_json / provider_config_json / freshness_cutoff_utc /
-- agent_model / posts_dropped_no_url / question_id -- the fields M1-306 fills in when a
-- run finishes -- and every descriptive field of a document (title, publisher, summary,
-- reliability_tag and the rest), which M1-305 may refine. 002's completeness triggers
-- remain the enforcement on that pair and are unaffected.
--
-- `IS NOT` rather than `<>`: `<>` is NULL when either side is NULL, so a NULL comparison
-- is neither true nor false and the guard would silently not fire. Every column named
-- here is NOT NULL today, which is exactly why the operator must not depend on that.

CREATE TRIGGER research_runs_block_identity_update
BEFORE UPDATE ON research_runs
FOR EACH ROW
WHEN NEW.retrieval_run_id IS NOT OLD.retrieval_run_id
  OR NEW.provider         IS NOT OLD.provider
  OR NEW.started_at_utc   IS NOT OLD.started_at_utc
  OR NEW.created_at_utc   IS NOT OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'research_runs: a run may be completed but never re-identified; retrieval_run_id, provider and its timestamps are fixed at creation');
END;

CREATE TRIGGER research_documents_block_identity_update
BEFORE UPDATE ON research_documents
FOR EACH ROW
WHEN NEW.document_id      IS NOT OLD.document_id
  OR NEW.retrieval_run_id IS NOT OLD.retrieval_run_id
  OR NEW.canonical_url    IS NOT OLD.canonical_url
  OR NEW.content_sha256   IS NOT OLD.content_sha256
  OR NEW.retrieved_at_utc IS NOT OLD.retrieved_at_utc
BEGIN
    SELECT RAISE(ABORT, 'research_documents: a document may be annotated but never re-identified; document_id, retrieval_run_id, canonical_url, content_sha256 and retrieved_at_utc are fixed at creation');
END;
