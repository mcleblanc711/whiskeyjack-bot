-- M2-711: what the refetch established, and the submission whose outcome is unknown.
--
-- `lifecycle.record_submission_attempt` derived its event from (success,
-- verified_by_refetch), and that pair has no member meaning "the post raised AND the
-- refetch could not be performed, so the platform state is unknown". It fell into
-- (0, 0) alongside the genuinely failed attempt, which 003 maps to `submission_failed`
-- and therefore to terminal `failed` -- a permanent claim that the post did not go
-- through, made on no observation at all. M2-704 named this in its own module rather
-- than papering over it (the alternative was writing verified_by_refetch = 1 for a
-- refetch that never happened) and filed it here.
--
-- The information already existed one layer up. `submission_live.classify_refetch`
-- returns a four-valued outcome -- confirmed / absent / mismatched / unreadable -- and
-- the seam into the ledger collapsed it to one bit. This migration carries the
-- vocabulary across the seam and splits the (0, 0) cell on it.
--
-- WHY NOT A NEW `lifecycle_events.event_type` MEMBER
--
-- Because `event_type` is a column CHECK, and SQLite cannot widen a CHECK without
-- rebuilding the table. Rebuilding `lifecycle_events` means dropping its append-only
-- block triggers and copying every row out and back, which 003's header calls
-- "precisely the operation the ledger exists to make impossible". So the vocabulary
-- member goes on `submission_attempts`, where ADD COLUMN reaches it, and
-- `submission_uncertain` widens to cover the new cell.
--
-- That is not a workaround dressed as a design. `submission_uncertain` already means
-- exactly what this state is: approved -> approved, named by
-- `lifecycle.unresolved_uncertainties` so `post_approved_forecast`'s gate stays shut
-- against a blind retry, and resolvable by `record_submission_verification` when a later
-- refetch does establish something. A distinct event type would have been a second name
-- for one lifecycle state, bought with a rebuild of the table the ledger's guarantees
-- rest on. Why an attempt is uncertain is carried by `detail_code` and by this column,
-- which is what they are for. (Owner decision at plan time.)
--
-- `detail_code`'s CHECK is untouched for the same reason and needs nothing: the post's
-- own error code -- timeout, provider_unavailable, http_error -- is the honest account of
-- why the outcome is unknown, and every one is already a member.
--
-- NULLABILITY, AND NO BACKFILL
--
-- `refetch_outcome` is NULLable in the DDL for 002/003/004/008's reason -- ADD COLUMN
-- cannot add NOT NULL without a default, and no default is honest for a row nobody
-- observed. The constraint in force on new rows is exactly NOT NULL plus the vocabulary,
-- via the BEFORE INSERT trigger below. No probe is needed against pre-existing rows: the
-- column does not exist before this file runs, so every one of them holds NULL, and both
-- triggers below read NULL as 003 read the absence of this information.
--
-- ONLY TWO INSERT TRIGGERS ARE TOUCHED
--
-- A trigger's body cannot be ALTERed, so each is dropped and recreated by the same name
-- -- the pattern 004, 006, 007 and 008 established. That touches the trigger definitions
-- and nothing else: not the tables' rows, not their append-only block triggers, and not
-- their CHECK constraints, so this is not the table-rebuild hazard 003's header
-- describes. `submission_attempts_require_receipt_on_insert` is 006's definition with
-- M2-711's clauses appended and NOTHING ELSE CHANGED.
-- `lifecycle_events_validate_on_insert` is 003's definition with exactly one block
-- replaced -- the three probes that read the submission partition, and the comment above
-- them -- and NOTHING ELSE CHANGED. The replaced block is marked where it appears.

ALTER TABLE submission_attempts ADD COLUMN refetch_outcome TEXT;

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

    -- Everything above this line is 006's definition -- which is 003's body with M1-607's
    -- two identifier clauses in front of it -- unchanged. Everything below is M2-711's.

    -- What the refetch that accompanied this post actually established. Required on every
    -- new row, because an attempt that does not say is exactly the ambiguity this
    -- migration closes: `verified_by_refetch = 0` conflates "the refetch looked and the
    -- forecast is not there" with "the refetch could not be performed at all", and the
    -- second of those is not evidence of anything. NULL remains storable only for rows
    -- written before this column existed; nothing can write one from here on.
    --
    -- The vocabulary is `submission_live.classify_refetch`'s own, member for member, and
    -- is pinned here rather than spelled differently: `mismatched` (something newer than
    -- the baseline is there and it is not what was sent) and `unreadable` (no observation
    -- was made) are the two the ledger previously had nowhere to put. A column CHECK would
    -- say the same thing, but `ADD COLUMN` cannot add one to an existing table, and a
    -- rebuild of an append-only table is the operation 003's header exists to forbid --
    -- so the trigger is where the vocabulary lives, which also means widening it later
    -- costs one more DROP/CREATE rather than a rebuild.
    SELECT RAISE(ABORT, 'submission_attempts: refetch_outcome is required and must be one of confirmed, absent, mismatched, unreadable')
    WHERE NEW.refetch_outcome IS NULL
       OR typeof(NEW.refetch_outcome) <> 'text'
       OR NEW.refetch_outcome NOT IN ('confirmed', 'absent', 'mismatched', 'unreadable');

    -- The two columns are one fact recorded twice, so they are made to agree here rather
    -- than left to agree by convention. `lifecycle.SubmissionAttempt` has a single field
    -- and derives `verified_by_refetch` from it, so no writer in this package can
    -- disagree with itself; this is what closes the raw-SQL path, and it is what lets the
    -- `submitted` probe in lifecycle_events_validate_on_insert stay as 003 wrote it.
    --
    -- Written as an inequality of two truth values rather than as two implications: SQLite
    -- evaluates each side to 0 or 1, and one clause that cannot be half-applied is worth
    -- more here than two that read marginally more like prose.
    SELECT RAISE(ABORT, 'submission_attempts: refetch_outcome and verified_by_refetch must agree; confirmed is exactly a refetch-verified attempt')
    WHERE (NEW.refetch_outcome = 'confirmed') <> (NEW.verified_by_refetch = 1);
END;

DROP TRIGGER lifecycle_events_validate_on_insert;

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

    -- The three submission events partition the attempt, and the partition is total:
    -- every attempt has exactly one legal event, so no outcome can be recorded as
    -- something it was not and none is left with no event at all.
    --
    -- **M2-711 widened what the partition reads.** 003 read the (success,
    -- verified_by_refetch) pair alone, and that pair has no member meaning "the post
    -- raised AND the refetch could not be performed". It put that case in (0, 0) with the
    -- genuinely failed one, so a lost connection became terminal `failed` -- a permanent
    -- claim that the post did not go through, which is more than was ever observed. The
    -- new `refetch_outcome` column carries what the refetch actually established, and the
    -- (0, 0) cell splits on it:
    --
    --   success  refetch_outcome            event
    --   -------  -------------------------  --------------------
    --   1        confirmed                  submitted
    --   1        absent/mismatched/unread.  submission_uncertain
    --   0        confirmed                  submission_uncertain
    --   0        absent                     submission_failed
    --   0        mismatched                 submission_uncertain   <- M2-711
    --   0        unreadable                 submission_uncertain   <- M2-711
    --
    -- `mismatched` moves for the same reason `unreadable` does, and it is the same cell:
    -- `classify_refetch` returns it only when an entry NEWER than the baseline is on the
    -- platform and does not match what was sent, so "it did not go through and nothing is
    -- there" is false of it too. Which of the two it is is a human judgement, and an
    -- uncertain record is where a human can still make it.
    --
    -- The `submitted` probe is UNCHANGED, and deliberately so: the equivalence clause on
    -- `submission_attempts_require_receipt_on_insert` makes `verified_by_refetch = 1` and
    -- `refetch_outcome = 'confirmed'` the same condition, so restating it here would be a
    -- second spelling of one rule rather than a second rule.
    SELECT RAISE(ABORT, 'lifecycle_events: a submitted event requires a successful, refetch-verified attempt')
    WHERE NEW.event_type = 'submitted'
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND success = 1
             AND verified_by_refetch = 1
      );

    -- 003's disagreement test, plus M2-711's second arm. The message changes with it: the
    -- rule is no longer "the two signals disagree" but "the platform did not settle it",
    -- and a probe whose message describes a narrower rule than it enforces is one a reader
    -- has to reproduce by execution to trust.
    SELECT RAISE(ABORT, 'lifecycle_events: an uncertain submission requires an attempt the platform did not settle')
    WHERE NEW.event_type = 'submission_uncertain'
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND (success <> verified_by_refetch
                  OR (success = 0
                      AND verified_by_refetch = 0
                      AND refetch_outcome IN ('mismatched', 'unreadable')))
      );

    -- `COALESCE(refetch_outcome, 'absent')` is what keeps every row written before this
    -- migration meaning exactly what it meant. Such a row holds NULL in the new column --
    -- `ADD COLUMN` cannot invent a value for it -- and reading NULL as `absent` reproduces
    -- 003's rule for it verbatim. It is not a default for new rows: the receipt trigger
    -- refuses those outright unless they name an outcome, so the COALESCE is unreachable
    -- for anything this ledger accepts from here on.
    SELECT RAISE(ABORT, 'lifecycle_events: a submission_failed event requires an attempt that neither succeeded nor was confirmed')
    WHERE NEW.event_type = 'submission_failed'
      AND NOT EXISTS (
          SELECT 1 FROM submission_attempts
           WHERE attempt_id = NEW.submission_attempt_id
             AND success = 0
             AND verified_by_refetch = 0
             AND COALESCE(refetch_outcome, 'absent') = 'absent'
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
