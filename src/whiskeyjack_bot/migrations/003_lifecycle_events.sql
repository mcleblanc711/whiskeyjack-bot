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
-- WHY A REFETCH IS NOT AN ATTEMPT:
--
-- Round 2 made an unconfirmed post non-terminal (`submission_uncertain`, approved ->
-- approved) so that a later refetch could resolve it. It gave that refetch nothing to
-- write. `submission_attempts` is append-only, so the original attempt can never be
-- updated to say "confirmed after all", and 001 declares `idempotency_key` NOT NULL
-- UNIQUE -- so the only way to reach `submitted` was a second attempt row carrying a
-- *new* key, which is a second live post. That is the blind retry the handoff forbids
-- ("uncertain timeout where posting may have succeeded: block retry until refetch
-- resolves state"), and it was the ledger's only documented way out of uncertainty
-- (GPT review round 3, finding 1; reproduced).
--
-- So `submission_verifications` below records the *observation* -- the refetch -- as its
-- own append-only row, and `submission_confirmed` / `submission_disconfirmed` carry the
-- record out of the uncertain state without inventing a request that never happened.
-- The two new event types are defined here rather than left to M2-704 for the reason the
-- resolution and score transitions are: `event_type` is a CHECK constraint, SQLite cannot
-- alter one, and adding a member later means rebuilding an append-only table.
--
-- The retry itself is NOT blocked here, and round 3's attempt to block it was withdrawn.
-- A trigger refusing a second submission event looked like "block retry until refetch
-- resolves state" and was not: everything this schema sees arrives *after* the fact.
-- `record_submission_attempt` is handed a finished receipt, so refusing it cannot stop a
-- post -- it can only make a post that already happened unrecordable, and the SQL half
-- was worse still, since the attempt row commits before the event is refused (GPT review
-- round 4, finding 1; reproduced two stored attempts against one event).
--
-- A ledger that refuses a fact does not prevent the act, it deletes the evidence. So the
-- rule stays where it can be kept: M2-704 asks *before* it posts, and
-- `whiskeyjack_bot.lifecycle.unresolved_uncertainties()` is what it asks. What this
-- schema contributes is the uncertain state itself, the two events that resolve it, and
-- an honest record of every attempt that was made -- including one that should not have
-- been. Serializing concurrent submitters needs a durable reservation, which is M2-704's
-- to build and cannot be a constraint on a table of past events either.
--
-- There is no `phase` column. Every event type names its own pipeline phase already
-- (`submission_failed` happens at submission), so a phase column would be a second
-- spelling of `event_type` that has to be kept in agreement with it. The *reason* a
-- phase failed is `detail_code`, which is not derivable and so is stored.
--
-- Timestamps are TEXT ISO-8601 UTC, matching 001. Trigger messages name fields and
-- never interpolate row values, per the project-wide error-hygiene rule.

-- PRECONDITION: every forecast record already stored must be a draft.
--
-- Everything below constrains rows written from here on. A ledger that already holds a
-- non-draft record is a different problem: its status sits where this migration says no
-- status may be written, and once the append-only triggers exist no statement can
-- reconcile it. current_status() would answer 'approved' for a record whose read_history()
-- is empty -- an approved state with no approval event, which is the exact failure this
-- item is accepted against (GPT review round 2, finding 1; reproduced).
--
-- Two answers were available: synthesize the missing history, or refuse the upgrade.
-- Synthesizing it would invent transitions, actors and timestamps nobody recorded --
-- fabricated attribution data, in the one table that exists to be trusted -- so this
-- refuses. RAISE() is legal only inside a trigger body, so the refusal is a CHECK that
-- the offending row violates and the table's name carries the reason; ledger.py wraps
-- the failure without echoing it, and applies each migration inside BEGIN/COMMIT with a
-- ROLLBACK on error, so a refused upgrade leaves the database exactly at version 2.
--
-- A pre-003 *draft* is unaffected. It keeps its honest NULL hash, stays readable, and is
-- unapprovable -- the case the hash column below is written for.
CREATE TEMP TABLE migration_003_requires_every_forecast_record_to_be_a_draft (
    violation TEXT NOT NULL CHECK (violation = 'none')
);

INSERT INTO migration_003_requires_every_forecast_record_to_be_a_draft (violation)
SELECT 'non_draft_record' FROM forecast_records WHERE status <> 'draft' LIMIT 1;

DROP TABLE migration_003_requires_every_forecast_record_to_be_a_draft;

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

-- What a refetch saw, recorded as evidence in its own right. This is the row that
-- resolves an uncertain submission; see "WHY A REFETCH IS NOT AN ATTEMPT" in the header.
--
-- It carries no `forecast_record_id`. The attempt it verifies already names one, and the
-- link probe below joins through it -- a second copy would be a second claim about the
-- same fact, with nothing keeping the two in agreement.
--
-- `outcome` is two-valued on purpose. A refetch that could not be *performed* -- the API
-- was down, the request timed out -- observed nothing, changes no state, and produces no
-- lifecycle event; recording it here would be a detail row no event can cite. The
-- uncertainty and its `detail_code` already say the post is unconfirmed, and an operator
-- record of failed check attempts is telemetry rather than attribution. This is a
-- judgment call in an immutable CHECK: if M2-704 finds it needs those observations, they
-- need their own table, not a third outcome whose meaning is "no outcome".
--
-- `refetched_forecast_snapshot` mirrors the column of the same name on
-- `submission_attempts` (001): what the platform actually returned, so the confirmation
-- can be audited rather than taken on faith. Nullable in the DDL, and **required for a
-- `confirmed` outcome** by the insert trigger below -- an `absent` outcome has nothing to
-- store, which is why the column cannot simply be NOT NULL. Round 3 wrote the rationale
-- above and left the column unconditionally nullable, so a confirmation could carry the
-- record to `submitted` on no evidence at all while this comment claimed otherwise (GPT
-- review round 4, finding 2; reproduced). A stated guarantee with no constraint behind it
-- is the failure mode this file exists to avoid.
--
-- Round 4's fix said "empty-or-whitespace counts as absent, in both layers", and round 5
-- found that only one layer meant it. The writer blanks with Python's `str.strip()`, which
-- removes all 29 Unicode whitespace codepoints; the trigger used SQLite's one-argument
-- `trim()`, which removes U+0020 and nothing else. A snapshot of "\n\t" was refused by the
-- writer, accepted here, and carried a record to `submitted` on two bytes of nothing --
-- the same guarantee-without-a-constraint defect as round 4, one layer down. The trigger
-- now spells its whitespace set out (see below) instead of inheriting a default that had
-- never been compared against the writer's.
--
-- There is deliberately no uniqueness over `submission_attempt_id`. A refetch can be
-- repeated, and each run is a true observation at its own time -- a post that was absent
-- at 12:00 and present at 12:05 is two facts, not one fact rewritten. What is bounded is
-- how many of them become *state*: the partial unique index on the link column allows one
-- lifecycle event per verification, and the first one to land carries the record out of
-- `approved`, from where neither resolution is legal again. So the log can hold several
-- observations of an attempt and the history still shows exactly one resolution.
CREATE TABLE submission_verifications (
    verification_id             INTEGER PRIMARY KEY,
    submission_attempt_id       TEXT NOT NULL REFERENCES submission_attempts (attempt_id),
    outcome                     TEXT NOT NULL CHECK (outcome IN ('confirmed', 'absent')),
    observed_at_utc             TEXT NOT NULL,
    refetched_forecast_snapshot TEXT,
    created_at_utc              TEXT NOT NULL
);

-- 001's reason for its explicit foreign-key indexes: SQLite does not auto-index a plain
-- REFERENCES column, and every read of this table starts from the attempt.
CREATE INDEX idx_submission_verifications_attempt
    ON submission_verifications (submission_attempt_id);

-- The lifecycle spine: one row per state transition, ordered per record.
--
-- `event_seq` is per record and contiguous from 1, which is a stronger claim than the
-- rowid's global order: it makes a *missing* event detectable. It is also the
-- concurrency guard. Two writers that both read "current status = validated" and
-- append seq 2 cannot both win -- UNIQUE(forecast_record_id, event_seq) turns the loser
-- into a loud IntegrityError instead of a silently reordered history. (The writers
-- take the write lock up front with BEGIN IMMEDIATE, so this is defence in depth.)
--
-- The detail row is referenced through five typed, nullable foreign keys rather than a
-- polymorphic (related_table, related_id) pair. A polymorphic pair cannot be a real
-- foreign key, and comparing it would mean CASTing across SQLite's affinity rules --
-- the same trap 002 documents for posts_dropped_no_url. Exactly one is set, and which
-- one is fixed by event_type; the trigger enforces both.
--
-- ... and each of them is cited by at most one event, which is the partial unique index
-- below rather than another probe. The trigger checked that a detail row belonged to this
-- record and recorded this outcome, and never that it had not already been used: since
-- `rejected` and `submission_uncertain` are self-transitions, two immutable events could
-- cite one approval decision or one attempt, so the history could show a post attempted
-- twice on the evidence of a single receipt (round 3, finding 2; reproduced).
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
            'submitted', 'submission_uncertain', 'submission_failed',
            'submission_confirmed', 'submission_disconfirmed', 'resolved', 'scored'
        )
    ),
    from_status           TEXT NOT NULL CHECK (
        from_status IN ('draft', 'validated', 'approved', 'submitted', 'failed', 'resolved', 'scored')
    ),
    to_status             TEXT NOT NULL CHECK (
        to_status IN ('draft', 'validated', 'approved', 'submitted', 'failed', 'resolved', 'scored')
    ),
    -- 'rejected_by_reviewer' was here in the first draft and is deliberately gone. A
    -- rejection is a decision, not a failure: it lands validated -> validated, its account
    -- is the actor and note on the approval_events row it cites, and the probe below
    -- forbids a detail_code on it -- so the code had no reachable writer. Removed rather
    -- than shipped as dead vocabulary in an immutable migration, which is the same call
    -- round 1 made on research_failed/generation_failed. (Round 2, finding 8.)
    detail_code           TEXT CHECK (
        detail_code IS NULL OR detail_code IN (
            'provider_error', 'provider_unavailable', 'no_evidence', 'stale_evidence',
            'malformed_response', 'schema_invalid', 'calibration_invalid',
            'http_error', 'timeout', 'refetch_mismatch', 'refetch_missing',
            'internal_error'
        )
    ),
    approval_event_id     INTEGER REFERENCES approval_events (event_id),
    submission_attempt_id TEXT REFERENCES submission_attempts (attempt_id),
    submission_verification_id INTEGER REFERENCES submission_verifications (verification_id),
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

-- One event per detail row. Every link column gets the same rule, not just the two whose
-- self-transitions made the hole reachable: a rule that only covers the cases someone
-- could reproduce today is a rule that has to be revisited when M4-802 and M5-803 write
-- the other two.
--
-- Partial rather than plain, though SQLite already treats NULLs in a UNIQUE index as
-- distinct: writing the predicate out says that "unlinked" is the normal case rather than
-- leaving it to a reader to know that rule. They double as the foreign-key indexes these
-- columns otherwise lack.
--
-- An index rather than a trigger probe, which the schema would otherwise prefer for the
-- sake of its own message: a UNIQUE violation here reports the index's column, never a
-- value, so the error-hygiene rule holds either way, and this way the constraint is also
-- the access path M1-604's join uses. Note the consequence for INSERT OR REPLACE: each of
-- these is another conflict target whose replacement DELETE must be caught by
-- lifecycle_events_block_delete, which is what `PRAGMA recursive_triggers` (ledger.py) is
-- load-bearing for.
CREATE UNIQUE INDEX lifecycle_events_one_event_per_approval
    ON lifecycle_events (approval_event_id) WHERE approval_event_id IS NOT NULL;

CREATE UNIQUE INDEX lifecycle_events_one_event_per_attempt
    ON lifecycle_events (submission_attempt_id) WHERE submission_attempt_id IS NOT NULL;

CREATE UNIQUE INDEX lifecycle_events_one_event_per_verification
    ON lifecycle_events (submission_verification_id) WHERE submission_verification_id IS NOT NULL;

CREATE UNIQUE INDEX lifecycle_events_one_event_per_resolution
    ON lifecycle_events (resolution_event_id) WHERE resolution_event_id IS NOT NULL;

CREATE UNIQUE INDEX lifecycle_events_one_event_per_score
    ON lifecycle_events (score_event_id) WHERE score_event_id IS NOT NULL;

-- The state machine. `to_status = 'failed'` is terminal by omission -- there is no
-- transition out of it, because a retry is a new forecast *version* (M1-602), not a
-- resurrected record. `rejected` is deliberately validated -> validated: the seven
-- states in 001's CHECK have no 'rejected' member, and the handoff requires a rejected
-- approval to "leave the last valid record intact". Rejection records a decision; it
-- does not move the record.
--
-- `submission_uncertain` is approved -> approved for a related reason, and it is the one
-- transition that exists because terminality would be a lie. An attempt that posted but
-- whose refetch did not confirm it (M2-704's uncertain timeout) is neither a verified
-- success nor an outright failure; the first draft recorded it as `submission_failed` and
-- so moved the record to terminal `failed`, at which point a later confirming refetch had
-- no legal event to record and the ledger would disagree with the platform for good --
-- while the handoff requires exactly the opposite, that an uncertain attempt "block retry
-- until a refetch resolves the state" (GPT review round 2, finding 3; reproduced). Owner
-- decision: the record stays `approved`, the uncertainty is recorded with its
-- detail_code, and a refetch can still carry it to `submitted` or `failed`. Deciding it
-- here rather than in M2-704 is the same reasoning the resolution and score transitions
-- are defined here for -- migrations are immutable, and a missing event type later costs
-- a whole migration.
--
-- What round 2 left missing is `submission_confirmed` / `submission_disconfirmed`, the
-- two ways out of that state. They are the *refetch's* transitions, not another attempt's:
-- they cite a `submission_verifications` row, so reaching `submitted` no longer requires
-- minting a second idempotency key for a post that was never made (round 3, finding 1).
-- `submission_disconfirmed` lands in terminal `failed` for the same reason a (0, 0)
-- attempt does -- the post is not there, and the retry is a new forecast version.
--
-- (0, 1) -- the request errored but a refetch found the forecast -- stays `uncertain`
-- rather than becoming `submitted` (owner decision, round 3). `success = 0` means no
-- receipt, and the handoff's prohibited claims forbid saying a live call succeeded
-- without one. A confirming refetch is exactly what `submission_confirmed` is for, and
-- routing it through a verification row keeps the confirmation itself in the ledger
-- instead of inferring it from an attempt that failed.
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
           (NEW.event_type = 'validated'            AND NEW.from_status = 'draft'     AND NEW.to_status = 'validated')
        OR (NEW.event_type = 'validation_failed'    AND NEW.from_status = 'draft'     AND NEW.to_status = 'failed')
        OR (NEW.event_type = 'validation_failed'    AND NEW.from_status = 'validated' AND NEW.to_status = 'failed')
        OR (NEW.event_type = 'rejected'             AND NEW.from_status = 'validated' AND NEW.to_status = 'validated')
        OR (NEW.event_type = 'approved'             AND NEW.from_status = 'validated' AND NEW.to_status = 'approved')
        OR (NEW.event_type = 'submitted'            AND NEW.from_status = 'approved'  AND NEW.to_status = 'submitted')
        OR (NEW.event_type = 'submission_uncertain' AND NEW.from_status = 'approved'  AND NEW.to_status = 'approved')
        OR (NEW.event_type = 'submission_failed'    AND NEW.from_status = 'approved'  AND NEW.to_status = 'failed')
        OR (NEW.event_type = 'submission_confirmed' AND NEW.from_status = 'approved'  AND NEW.to_status = 'submitted')
        OR (NEW.event_type = 'submission_disconfirmed' AND NEW.from_status = 'approved' AND NEW.to_status = 'failed')
        OR (NEW.event_type = 'resolved'             AND NEW.from_status = 'submitted' AND NEW.to_status = 'resolved')
        OR (NEW.event_type = 'scored'               AND NEW.from_status = 'resolved'  AND NEW.to_status = 'scored')
    );

    -- A failure that does not say why is an unfalsifiable claim about the pipeline,
    -- which is the same objection 002 raised against an unconstrained accountability
    -- counter. Keyed on the destination, so a later event type that ends in `failed`
    -- inherits the requirement without anyone remembering to add it.
    SELECT RAISE(ABORT, 'lifecycle_events: an event ending in failed requires detail_code')
    WHERE NEW.to_status = 'failed' AND NEW.detail_code IS NULL;

    -- ... and the one event that carries a reason without ending in `failed`. An
    -- uncertain submission is only interesting for *why* it is uncertain --
    -- refetch_missing, refetch_mismatch, timeout -- and without the code it is a record
    -- that something unspecified went unconfirmed, which no later attempt can act on.
    SELECT RAISE(ABORT, 'lifecycle_events: an uncertain submission requires detail_code')
    WHERE NEW.event_type = 'submission_uncertain' AND NEW.detail_code IS NULL;

    -- The converse, which the first draft left open: nothing stopped a `validated` or
    -- `submitted` event carrying detail_code = 'internal_error', so the immutable history
    -- could hold a success annotated with a failure (round 2, finding 8; reproduced).
    -- The list is spelled out rather than written as a NOT IN of the failure types, so a
    -- later event type is unconstrained until someone classifies it deliberately --
    -- an omission that shows up as an unenforced rule rather than as a wrong one.
    SELECT RAISE(ABORT, 'lifecycle_events: this event type carries no detail_code')
    WHERE NEW.event_type IN (
             'validated', 'rejected', 'approved', 'submitted', 'submission_confirmed',
             'resolved', 'scored'
         )
      AND NEW.detail_code IS NOT NULL;

    -- Exactly one detail foreign key, and the right one for the event type.
    SELECT RAISE(ABORT, 'lifecycle_events: an approval event must link exactly one approval_events row')
    WHERE NEW.event_type IN ('approved', 'rejected')
      AND (NEW.approval_event_id IS NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.submission_verification_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: a submission event must link exactly one submission_attempts row')
    WHERE NEW.event_type IN ('submitted', 'submission_uncertain', 'submission_failed')
      AND (NEW.submission_attempt_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_verification_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    -- The refetch's own two events cite the observation, not the attempt. Linking the
    -- attempt instead would be the second-post problem again: the attempt row is the
    -- record of a request, and no request was made.
    SELECT RAISE(ABORT, 'lifecycle_events: a submission verification event must link exactly one submission_verifications row')
    WHERE NEW.event_type IN ('submission_confirmed', 'submission_disconfirmed')
      AND (NEW.submission_verification_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: a resolution event must link exactly one resolution_events row')
    WHERE NEW.event_type = 'resolved'
      AND (NEW.resolution_event_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.submission_verification_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: a score event must link exactly one score_events row')
    WHERE NEW.event_type = 'scored'
      AND (NEW.score_event_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.submission_verification_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL);

    SELECT RAISE(ABORT, 'lifecycle_events: this event type carries no detail row')
    WHERE NEW.event_type IN ('validated', 'validation_failed')
      AND (NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.submission_verification_id IS NOT NULL
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

    -- ... and it must bind the hash the record actually stores. The approval_events
    -- insert trigger below makes that true of every approval written from here on, but it
    -- never sees a row that predates this migration -- and every such row's record has a
    -- NULL hash after the ALTER above, so an approval carrying an arbitrary digest could
    -- be linked and carry the record to `approved` unbound to any content (round 2,
    -- finding 2; reproduced by raw insert against an upgraded v2 ledger). Checked at the
    -- link, which is the moment the decision becomes the record's state.
    --
    -- `f.forecast_sha256 IS NOT NULL` is not redundant with the equality: both sides NULL
    -- would make `=` NULL rather than true, so the probe would fire -- but only by
    -- accident of three-valued logic, and a reader has to be able to see that a record
    -- with no hash is unapprovable by construction, not by side effect.
    SELECT RAISE(ABORT, 'lifecycle_events: the linked approval_events row does not bind the forecast hash this record stores')
    WHERE NEW.approval_event_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM approval_events a
            JOIN forecast_records f ON f.record_id = NEW.forecast_record_id
           WHERE a.event_id = NEW.approval_event_id
             AND f.forecast_sha256 IS NOT NULL
             AND a.forecast_sha256 = f.forecast_sha256
      );

    SELECT RAISE(ABORT, 'lifecycle_events: the linked submission_attempts row is for another forecast record')
    WHERE NEW.submission_attempt_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND forecast_record_id = NEW.forecast_record_id
      );

    -- The three submission events partition the (success, verified_by_refetch) pair, and
    -- the partition is total: every attempt has exactly one legal event, so no outcome
    -- can be recorded as something it was not and none is left with no event at all.
    --
    --   (1, 1)  submitted             the post went through and a refetch confirmed it
    --   (1, 0)  submission_uncertain  it went through; the refetch did not confirm it
    --   (0, 1)  submission_uncertain  it errored; the refetch says something is there
    --   (0, 0)  submission_failed     it did not go through and nothing is there
    --
    -- The first line is M2-704's "success requires refetch confirmation" as a constraint
    -- rather than a convention: a `submitted` state no refetch confirmed is precisely the
    -- unverified claim the ledger exists to prevent. The middle two are why the pair is
    -- read rather than `success` alone -- the two signals disagreeing is a third outcome,
    -- not a failure, and collapsing it into one was round 2's finding 3.
    SELECT RAISE(ABORT, 'lifecycle_events: a submitted event requires a successful, refetch-verified attempt')
    WHERE NEW.event_type = 'submitted'
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND success = 1
             AND verified_by_refetch = 1
      );

    SELECT RAISE(ABORT, 'lifecycle_events: an uncertain submission requires an attempt whose success and refetch disagree')
    WHERE NEW.event_type = 'submission_uncertain'
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND success <> verified_by_refetch
      );

    SELECT RAISE(ABORT, 'lifecycle_events: a submission_failed event requires an attempt that neither succeeded nor was confirmed')
    WHERE NEW.event_type = 'submission_failed'
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND success = 0
             AND verified_by_refetch = 0
      );

    -- A second attempt while an uncertainty stands is deliberately NOT refused here; see
    -- "WHY A REFETCH IS NOT AN ATTEMPT" in the header for what replaced round 3's probe.
    -- The consequence to notice is that a record may now hold several
    -- `submission_uncertain` events -- one per attempt -- and each still cites its own
    -- attempt row, because the partial unique index below allows a detail row to back
    -- exactly one event.

    -- The verification's attempt is what ties it to this record; the row itself stores no
    -- forecast_record_id, so the join is the ownership check.
    SELECT RAISE(ABORT, 'lifecycle_events: the linked submission_verifications row verifies an attempt on another forecast record')
    WHERE NEW.submission_verification_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM submission_verifications v
            JOIN submission_attempts s ON s.attempt_id = v.submission_attempt_id
           WHERE v.verification_id = NEW.submission_verification_id
             AND s.forecast_record_id = NEW.forecast_record_id
      );

    -- ... and what it saw decides which event it can back, the same way an approval's
    -- `decision` does. Without this, a refetch that found nothing could carry the record
    -- to `submitted`.
    SELECT RAISE(ABORT, 'lifecycle_events: the linked submission_verifications row records a different observation than this event')
    WHERE NEW.submission_verification_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM submission_verifications
           WHERE verification_id = NEW.submission_verification_id
             AND outcome = CASE NEW.event_type
                               WHEN 'submission_confirmed' THEN 'confirmed'
                               WHEN 'submission_disconfirmed' THEN 'absent'
                           END
      );

    -- A verification resolves an *uncertainty*. An attempt this ledger recorded as
    -- `submitted` or `submission_failed` has already been accounted for, and re-deciding
    -- it from a later refetch would overwrite that account with a second, contradicting
    -- one -- which is what append-only exists to prevent.
    --
    -- `e.forecast_record_id = NEW.forecast_record_id` is implied by the ownership probe
    -- above -- an attempt belongs to one record, and an event citing it had to pass that
    -- same probe -- and is written out anyway. A constraint that holds only because
    -- another constraint holds is one refactor away from holding for no reason, and this
    -- one is cheap.
    SELECT RAISE(ABORT, 'lifecycle_events: the verified submission attempt was not recorded as uncertain, so there is nothing for a refetch to resolve')
    WHERE NEW.submission_verification_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM submission_verifications v
            JOIN lifecycle_events e ON e.submission_attempt_id = v.submission_attempt_id
           WHERE v.verification_id = NEW.submission_verification_id
             AND e.event_type = 'submission_uncertain'
             AND e.forecast_record_id = NEW.forecast_record_id
      );

    SELECT RAISE(ABORT, 'lifecycle_events: the linked resolution_events row is for another forecast record')
    WHERE NEW.resolution_event_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM resolution_events
           WHERE event_id = NEW.resolution_event_id
             AND forecast_record_id = NEW.forecast_record_id
      );

    -- A resolution_events row carries its own question_id (001), and it is nullable in
    -- the reverse direction -- forecast_record_id is a nullable REFERENCES -- so pointing
    -- at the right record is not the same claim as resolving the right question. Without
    -- this, another question's outcome could resolve this forecast and M5-803 would then
    -- score it against that outcome (round 2, finding 6; reproduced). A separate probe
    -- from the one above so the two failures are told apart in the log.
    SELECT RAISE(ABORT, 'lifecycle_events: the linked resolution_events row resolves a different question than this forecast')
    WHERE NEW.resolution_event_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM resolution_events r
            JOIN forecast_records f ON f.record_id = NEW.forecast_record_id
           WHERE r.event_id = NEW.resolution_event_id
             AND r.question_id = f.question_id
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

-- A submission receipt is written once, after the attempt has finished, and the
-- append-only blocks below mean it can never be completed later. So the two fields that
-- make it a receipt rather than an assertion are required at insert:
--
-- * `completed_at_utc`. 001 left it nullable because it predates that rule, and the
--   writer's own dataclass defaulted it to None -- so a verified submission could be
--   recorded with no completion time at all, permanently (round 2, finding 5;
--   reproduced). There is no in-flight row to leave open: the ledger only hears about an
--   attempt once it is over.
-- * `http_status`, when there is one. A status is either absent (no response arrived) or
--   an HTTP status code; -1, 0, 600 and 2**63-1 all persisted as audit data before this
--   (round 2, finding 7). typeof() is part of the constraint for the affinity reason 002
--   documents for posts_dropped_no_url: INTEGER is affinity, not a type.
-- * ... and it must be a receipt for an interval that ran forwards. The writer compared
--   the two instants and the schema did not, so a direct insert could persist an attempt
--   that completed a day before it was requested -- permanently, on an append-only table
--   whose `requested_at_utc` is what an idempotency key is reasoned about against (round
--   3, finding 3; reproduced). Every other rule in this item holds in both layers; this
--   one held in one.
--
--   Round 3 made that comparison with julianday(), and julianday() is a *float day
--   number*: a day is 86400 seconds, a double has ~16 significant digits, so microseconds
--   fall off the end. Two timestamps one microsecond apart compare exactly equal -- the
--   schema accepted a reversed receipt the Python writer rejects, which is the same
--   two-layer disagreement round 3 set out to close (GPT review round 4, finding 3;
--   reproduced at .000001 vs .000002, milliseconds survive and microseconds do not).
--
--   Exactness needs a form that orders lexicographically, so these columns are pinned to
--   one: `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, fixed width 32, always UTC, microseconds
--   always present -- what `lifecycle._require_utc` now renders for every timestamp it
--   writes. Then `<` on TEXT is the comparison, and it is exact.
--
--   This reverses round 3's "no format pin, do not become the lone outlier" reasoning,
--   deliberately and narrowly. That argument holds for a column nothing is compared
--   against; it does not survive a column an *ordering claim* rests on, where the choice
--   is not "pin or stay uniform" but "pin or compare wrongly". So the pin covers exactly
--   the three columns that are compared -- requested_at_utc, completed_at_utc and
--   submission_verifications.observed_at_utc -- and `occurred_at_utc` / `created_at_utc`
--   stay unpinned, because event order is `event_seq` and nothing orders those.
--
-- INSERT only. UPDATE and DELETE on this table are refused outright below, so there is
-- no second path a row can arrive by.
CREATE TRIGGER submission_attempts_require_receipt_on_insert
BEFORE INSERT ON submission_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_attempts: completed_at_utc is required; an attempt is recorded once, after it has finished')
    WHERE NEW.completed_at_utc IS NULL;

    SELECT RAISE(ABORT, 'submission_attempts: requested_at_utc and completed_at_utc must be UTC timestamps of the form YYYY-MM-DDTHH:MM:SS.ffffff+00:00')
    WHERE typeof(NEW.requested_at_utc) <> 'text'
       OR typeof(NEW.completed_at_utc) <> 'text'
       OR length(NEW.requested_at_utc) <> 32
       OR length(NEW.completed_at_utc) <> 32
       OR NEW.requested_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
       OR NEW.completed_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00';

    -- Exact, because both sides are pinned to the same fixed-width UTC form above.
    SELECT RAISE(ABORT, 'submission_attempts: completed_at_utc is earlier than requested_at_utc')
    WHERE NEW.completed_at_utc < NEW.requested_at_utc;

    SELECT RAISE(ABORT, 'submission_attempts: http_status must be an integer HTTP status code between 100 and 599')
    WHERE NEW.http_status IS NOT NULL
      AND (typeof(NEW.http_status) <> 'integer'
           OR NEW.http_status < 100
           OR NEW.http_status > 599);
END;

-- A verification is a receipt too, and gets the same treatment: it names an attempt this
-- ledger holds, it carries what it saw when it says it saw something, and it cannot have
-- been observed before that attempt finished. The attempt-exists probe runs ahead of the
-- foreign key so the message is this schema's own, as with lifecycle_events above.
--
-- The comparison against the attempt's own `completed_at_utc` **refuses rather than
-- guesses** when that stored value is not in the pinned form. A row written before this
-- migration could hold any shape, and a lexicographic comparison against an unknown format
-- is not a comparison -- it is a coin toss that reads like a check. The population is
-- expected to be empty (this module is the only writer of that table and is new here), so
-- the honest failure costs nothing and the silent wrong answer would cost everything.
CREATE TRIGGER submission_verifications_require_receipt_on_insert
BEFORE INSERT ON submission_verifications
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_verifications: submission_attempt_id does not name a stored submission attempt')
    WHERE NOT EXISTS (
        SELECT 1 FROM submission_attempts WHERE attempt_id = NEW.submission_attempt_id
    );

    -- The character set is the writer's, written out rather than assumed. SQLite's
    -- one-argument `trim()` strips U+0020 alone, so tabs, newlines and NBSP -- all
    -- whitespace to the `str.strip()` this mirrors -- passed straight through it
    -- (round 5). These are the 29 codepoints where Python's `str.isspace()` is true.
    -- Frozen here the moment this migration lands: a test asserts the two definitions
    -- still agree over the whole set, so a later Unicode change surfaces as a failed
    -- test and a migration 004, not as a silently reopened hole.
    SELECT RAISE(ABORT, 'submission_verifications: a confirmed refetch must carry the forecast snapshot it saw')
    WHERE NEW.outcome = 'confirmed'
      AND (NEW.refetched_forecast_snapshot IS NULL
           OR typeof(NEW.refetched_forecast_snapshot) <> 'text'
           OR trim(NEW.refetched_forecast_snapshot,
                   char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                        8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                        8232, 8233, 8239, 8287, 12288)) = '');

    SELECT RAISE(ABORT, 'submission_verifications: observed_at_utc must be a UTC timestamp of the form YYYY-MM-DDTHH:MM:SS.ffffff+00:00')
    WHERE typeof(NEW.observed_at_utc) <> 'text'
       OR length(NEW.observed_at_utc) <> 32
       OR NEW.observed_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00';

    SELECT RAISE(ABORT, 'submission_verifications: the attempt this verifies stores a completion time in a form that cannot be ordered against an observation')
    WHERE NOT EXISTS (
        SELECT 1 FROM submission_attempts
         WHERE attempt_id = NEW.submission_attempt_id
           AND typeof(completed_at_utc) = 'text'
           AND length(completed_at_utc) = 32
           AND completed_at_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
    );

    SELECT RAISE(ABORT, 'submission_verifications: observed_at_utc is earlier than the completion of the attempt it verifies')
    WHERE NEW.observed_at_utc < (
        SELECT completed_at_utc FROM submission_attempts
         WHERE attempt_id = NEW.submission_attempt_id
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

CREATE TRIGGER submission_verifications_block_update
BEFORE UPDATE ON submission_verifications
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_verifications is append-only: what a refetch saw is never rewritten; record a new observation');
END;

CREATE TRIGGER submission_verifications_block_delete
BEFORE DELETE ON submission_verifications
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_verifications is append-only: a recorded observation is never deleted');
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
-- agent_model / posts_dropped_no_url -- the fields M1-306 fills in when a run finishes --
-- and every descriptive field of a document (title, publisher, summary, reliability_tag
-- and the rest), which M1-305 may refine. 002's completeness triggers remain the
-- enforcement on that pair and are unaffected.
--
-- The first cut of these pins named only the columns 001 declared NOT NULL, and that was
-- the wrong test for what identity is. 002 requires research_runs.question_id and
-- research_documents.original_url / provenance / source_type of every row it inserts, so
-- they are established at creation like the rest -- nullable only because ADD COLUMN
-- cannot retrofit NOT NULL, which is a fact about SQLite and not about the data. Leaving
-- them open let a stored run be reassigned to another question and a document retrieved
-- from a provider API be rewritten as an agent's claim, which is a provenance forgery
-- (round 2, finding 4; reproduced on all four columns).
--
-- Those four are guarded as `OLD.x IS NOT NULL AND NEW.x IS NOT OLD.x` rather than
-- unconditionally, because a row that predates the migration that required them holds an
-- honest NULL, and 002's update triggers refuse *any* update to such a row until it is
-- backfilled. An unconditional pin would make the backfill 002 anticipates impossible and
-- freeze those rows for good. NULL -> value once; value -> anything else never.
--
-- `IS NOT` rather than `<>`: `<>` is NULL when either side is NULL, so a NULL comparison
-- is neither true nor false and the guard would silently not fire. The columns above the
-- carve-out are NOT NULL today, which is exactly why the operator must not depend on that.

CREATE TRIGGER research_runs_block_identity_update
BEFORE UPDATE ON research_runs
FOR EACH ROW
WHEN NEW.retrieval_run_id IS NOT OLD.retrieval_run_id
  OR NEW.provider         IS NOT OLD.provider
  OR NEW.started_at_utc   IS NOT OLD.started_at_utc
  OR NEW.created_at_utc   IS NOT OLD.created_at_utc
  OR (OLD.question_id IS NOT NULL AND NEW.question_id IS NOT OLD.question_id)
BEGIN
    SELECT RAISE(ABORT, 'research_runs: a run may be completed but never re-identified; retrieval_run_id, provider, question_id and its timestamps are fixed at creation');
END;

CREATE TRIGGER research_documents_block_identity_update
BEFORE UPDATE ON research_documents
FOR EACH ROW
WHEN NEW.document_id      IS NOT OLD.document_id
  OR NEW.retrieval_run_id IS NOT OLD.retrieval_run_id
  OR NEW.canonical_url    IS NOT OLD.canonical_url
  OR NEW.content_sha256   IS NOT OLD.content_sha256
  OR NEW.retrieved_at_utc IS NOT OLD.retrieved_at_utc
  OR (OLD.original_url IS NOT NULL AND NEW.original_url IS NOT OLD.original_url)
  OR (OLD.provenance   IS NOT NULL AND NEW.provenance   IS NOT OLD.provenance)
  OR (OLD.source_type  IS NOT NULL AND NEW.source_type  IS NOT OLD.source_type)
BEGIN
    SELECT RAISE(ABORT, 'research_documents: a document may be annotated but never re-identified; document_id, retrieval_run_id, canonical_url, original_url, content_sha256, retrieved_at_utc, provenance and source_type are fixed at creation');
END;
