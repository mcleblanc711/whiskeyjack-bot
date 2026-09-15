-- M2-713: record a live post the ledger never wrote down.
--
-- `submission_live.post_approved_forecast` posts, refetches, writes the artifact and only
-- then writes the `submission_attempts` row. Anything that stops it between the post and
-- that row -- a ledger that refuses the write (a busy lock, an I/O error), or a process that
-- dies during the refetch (Ctrl-C, systemd's start timeout, the OOM killer) -- leaves a
-- forecast live on Metaculus, no attempt row, an unreleased key reservation and a record
-- still `approved`. `release-key` is the wrong way out and nothing else existed.
--
-- WHY NOT AN ATTEMPT ROW
--
-- Two shapes reach that state and neither can be an honest `submission_attempts` row.
--
--   * The process died before the artifact was written. Nothing observed the POST return or
--     raise, so `success` -- `INTEGER NOT NULL CHECK (success IN (0, 1))` since 001 -- has
--     no true value to hold, and neither do `requested_at_utc` or `completed_at_utc`. Any
--     value written there is a guess stored on an append-only table, and a guessed
--     `success = 1` is the claim CODEX_HANDOFF.md prohibits: a live call that succeeded,
--     with no recorded receipt.
--   * The ledger refused the write after the artifact landed. The artifact holds the
--     receipt, but not the `detail_code` its event needs: for a POST that returned and a
--     refetch that could not be read, that code came from the refetch's transport error,
--     which the artifact does not store. Copying the receipt would mean inventing it.
--
-- So a reconciliation is its own detail row, the claim/resolution shape 003 (attempts and
-- verifications) and 010 (reservations and releases) already use, and it moves the record
-- the way a later refetch always has: `submission_confirmed`, approved -> submitted. That
-- event already means "a refetch confirmed an outcome that was not settled at the time",
-- which is exactly this state. `submitted` would say a successful, refetch-verified
-- *attempt* exists, and none does.
--
-- WHAT A RECONCILIATION MUST CARRY
--
-- A person's assertion (`observed_by`, `note`) AND the program's own confirming refetch
-- (`refetched_forecast_snapshot`, whose outcome must be `confirmed`) AND the program-written
-- evidence that the post was reached: the reservation it was made under, and the
-- `forecast_intent` journal row `submission_policy` commits immediately before every POST.
-- The payload digest must be the one the record's approval authorized. The captured
-- artifact, when it exists, is pinned by path and sha256 and never copied: it is evidence
-- the row points at, not a receipt the row claims.
--
-- `attempt_id` names the attempt: it is the identity `live_attempt_id(key)` gives the post,
-- the one its attempt row would have carried. SQLite has no sha256 function, so the
-- derivation is checked by the writer; this schema checks its shape and that no attempt
-- row holds it.
--
-- HOW THE KEY ENDS
--
-- Spent, never released: the post landed, so the key is not free. 010's derivation reads a
-- key as spent when an attempt row exists; a reconciliation is the second way. The
-- reservation keeps no release row, so 010's own "already reserved and not released"
-- clause already refuses a new claim on the key, and 012's whole-question guard keeps
-- counting it. Two NEW triggers close what those do not: a release of a reconciled
-- reservation, and an attempt row for a key a reconciliation already spent -- which would
-- be two records of one post.
--
-- WHAT THIS REWRITES, AND WHAT IT DOES NOT
--
-- Every event type requires exactly one detail link, so `lifecycle_events` gains a link
-- column by ADD COLUMN -- not the CHECK rebuild 003's header forbids -- and
-- `lifecycle_events_validate_on_insert` is dropped and recreated for the second time
-- (after 009). The recreated trigger is 009's definition with THREE marked hunks and
-- NOTHING ELSE CHANGED: the verification-link clause stands aside for a reconciled
-- confirmation, a reconciliation link is refused on anything but `submission_confirmed`
-- and alongside any other link, and a linked reconciliation must be this record's.
-- `tests/unit/test_submission_reconciliations.py` strips the marked hunks and asserts the
-- remainder is byte-identical to 009's body. No existing table is rebuilt, and no existing
-- row changes meaning: every stored event holds NULL in the new column.
--
-- No upgrade precondition scan is needed: the table is created here, inside the one
-- BEGIN/COMMIT `ledger._apply_migration` wraps this file in, so no row predates its
-- triggers.

CREATE TABLE submission_reconciliations (
    reconciliation_id           TEXT PRIMARY KEY NOT NULL,
    reservation_id              TEXT NOT NULL UNIQUE REFERENCES submission_key_reservations (reservation_id),
    forecast_record_id          TEXT NOT NULL REFERENCES forecast_records (record_id),
    attempt_id                  TEXT NOT NULL UNIQUE,
    request_payload_sha256      TEXT NOT NULL,
    intent_event_id             TEXT NOT NULL REFERENCES tournament_events (event_id),
    artifact_path               TEXT,
    artifact_sha256             TEXT,
    observed_by                 TEXT NOT NULL,
    note                        TEXT NOT NULL,
    refetched_at_utc            TEXT NOT NULL,
    refetched_forecast_snapshot TEXT NOT NULL,
    created_at_utc              TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- What a reconciliation must be.
-- ---------------------------------------------------------------------------
--
-- The identifier clauses are 006's predicate, character for character, for 010's reason: a
-- fourth spelling of blank is the defect this family of guards descends from.
-- `forecast_record_id` is probed first so the message is this schema's own rather than the
-- foreign key's.
CREATE TRIGGER submission_reconciliations_validate_on_insert
BEFORE INSERT ON submission_reconciliations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_reconciliations: reconciliation_id must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.reconciliation_id IS NULL
       OR typeof(NEW.reconciliation_id) <> 'text'
       OR length(NEW.reconciliation_id) > 200
       OR instr(NEW.reconciliation_id, char(0)) > 0
       OR trim(NEW.reconciliation_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    SELECT RAISE(ABORT, 'submission_reconciliations: forecast_record_id does not name a stored forecast record')
    WHERE NOT EXISTS (
        SELECT 1 FROM forecast_records WHERE record_id = NEW.forecast_record_id
    );

    SELECT RAISE(ABORT, 'submission_reconciliations: reservation_id does not name a key reservation held against this forecast record')
    WHERE NOT EXISTS (
        SELECT 1 FROM submission_key_reservations
         WHERE reservation_id = NEW.reservation_id
           AND forecast_record_id = NEW.forecast_record_id
    );

    -- A release is a person's assertion that nothing landed. Reconciling the same claim
    -- would record both answers to one question.
    SELECT RAISE(ABORT, 'submission_reconciliations: this reservation was released, which records that nothing was posted under it')
    WHERE EXISTS (
        SELECT 1 FROM submission_key_releases WHERE reservation_id = NEW.reservation_id
    );

    -- The whole premise: the post is unrecorded. An attempt row under the key, or under the
    -- attempt id, is the post already written down.
    SELECT RAISE(ABORT, 'submission_reconciliations: a submission attempt already records the post made under this reservation')
    WHERE EXISTS (
        SELECT 1 FROM submission_attempts a
          JOIN submission_key_reservations r ON r.idempotency_key = a.idempotency_key
         WHERE r.reservation_id = NEW.reservation_id
    )
       OR EXISTS (
        SELECT 1 FROM submission_attempts WHERE attempt_id = NEW.attempt_id
    );

    -- `wjlive-1-` and 64 lowercase hex: the shape `submission_live.live_attempt_id` mints.
    SELECT RAISE(ABORT, 'submission_reconciliations: attempt_id must be a live attempt identifier')
    WHERE typeof(NEW.attempt_id) <> 'text'
       OR length(NEW.attempt_id) <> 73
       OR substr(NEW.attempt_id, 1, 9) <> 'wjlive-1-'
       OR substr(NEW.attempt_id, 10) GLOB '*[^0-9a-f]*';

    SELECT RAISE(ABORT, 'submission_reconciliations: request_payload_sha256 must be 64 lowercase hex characters')
    WHERE typeof(NEW.request_payload_sha256) <> 'text'
       OR length(NEW.request_payload_sha256) <> 64
       OR NEW.request_payload_sha256 GLOB '*[^0-9a-f]*';

    -- 011's binding, at the reconciliation: what is recorded as posted is what a decision
    -- authorized. A record holds at most one approval (M2-701), so EXISTS is exact.
    SELECT RAISE(ABORT, 'submission_reconciliations: request_payload_sha256 is not the payload this record''s approval authorized')
    WHERE NOT EXISTS (
        SELECT 1 FROM approval_events
         WHERE forecast_record_id = NEW.forecast_record_id
           AND decision = 'approved'
           AND payload_sha256 = NEW.request_payload_sha256
    );

    -- The record's derived status, 003's COALESCE. Anything but `approved` has either not
    -- been authorized or has already been accounted for.
    SELECT RAISE(ABORT, 'submission_reconciliations: the forecast record is not awaiting submission')
    WHERE COALESCE(
        (SELECT to_status FROM lifecycle_events
          WHERE forecast_record_id = NEW.forecast_record_id
          ORDER BY event_seq DESC LIMIT 1),
        (SELECT status FROM forecast_records WHERE record_id = NEW.forecast_record_id)
    ) IS NOT 'approved';

    -- While the record is `approved`, every uncertain event it holds is unresolved
    -- (`lifecycle.unresolved_uncertainties`). The post gate refuses to post past one, so a
    -- reconciliation standing beside one describes a post that gate never allowed.
    SELECT RAISE(ABORT, 'submission_reconciliations: this record holds a submission attempt whose outcome is unresolved')
    WHERE EXISTS (
        SELECT 1 FROM lifecycle_events
         WHERE forecast_record_id = NEW.forecast_record_id
           AND event_type = 'submission_uncertain'
    );

    -- The program-written evidence that the POST was reached, committed by
    -- `submission_policy`'s `before_post` immediately ahead of it.
    SELECT RAISE(ABORT, 'submission_reconciliations: intent_event_id does not name this record''s durable submission intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM tournament_events
         WHERE event_id = NEW.intent_event_id
           AND kind = 'forecast_intent'
           AND scope = NEW.forecast_record_id
    );

    -- Both or neither: a path with no digest pins nothing, and a digest with no path names
    -- nothing an auditor can find.
    SELECT RAISE(ABORT, 'submission_reconciliations: artifact_path and artifact_sha256 are recorded together or not at all')
    WHERE (NEW.artifact_path IS NULL) <> (NEW.artifact_sha256 IS NULL);

    SELECT RAISE(ABORT, 'submission_reconciliations: artifact_path, when present, must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.artifact_path IS NOT NULL
      AND (typeof(NEW.artifact_path) <> 'text'
           OR length(NEW.artifact_path) > 200
           OR instr(NEW.artifact_path, char(0)) > 0
           OR trim(NEW.artifact_path,
                   char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                        8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                        8232, 8233, 8239, 8287, 12288)) = '');

    SELECT RAISE(ABORT, 'submission_reconciliations: artifact_sha256, when present, must be 64 lowercase hex characters')
    WHERE NEW.artifact_sha256 IS NOT NULL
      AND (typeof(NEW.artifact_sha256) <> 'text'
           OR length(NEW.artifact_sha256) <> 64
           OR NEW.artifact_sha256 GLOB '*[^0-9a-f]*');

    -- A claim about a person, so a blank one is worse than none. `approve`'s rule.
    SELECT RAISE(ABORT, 'submission_reconciliations: observed_by must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.observed_by IS NULL
       OR typeof(NEW.observed_by) <> 'text'
       OR length(NEW.observed_by) > 200
       OR instr(NEW.observed_by, char(0)) > 0
       OR trim(NEW.observed_by,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    -- Required, unlike a release's: the row records what a person saw, and an assertion
    -- with no content is not one.
    SELECT RAISE(ABORT, 'submission_reconciliations: note must be non-blank text of at most 4000 characters and no NUL')
    WHERE NEW.note IS NULL
       OR typeof(NEW.note) <> 'text'
       OR length(NEW.note) > 4000
       OR instr(NEW.note, char(0)) > 0
       OR trim(NEW.note,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    SELECT RAISE(ABORT, 'submission_reconciliations: refetched_at_utc must be a UTC timestamp of the form YYYY-MM-DDTHH:MM:SS.ffffff+00:00')
    WHERE typeof(NEW.refetched_at_utc) <> 'text'
       OR length(NEW.refetched_at_utc) <> 32
       OR NEW.refetched_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00';

    -- Exact, because both sides are pinned to the same fixed-width UTC form (010's release
    -- clause, for the same pair of columns).
    SELECT RAISE(ABORT, 'submission_reconciliations: refetched_at_utc is earlier than the reservation it reconciles')
    WHERE NEW.refetched_at_utc < (
        SELECT reserved_at_utc FROM submission_key_reservations
         WHERE reservation_id = NEW.reservation_id
    );

    -- The confirming observation is the program's half of the evidence, and it is what
    -- carries the record to `submitted`. A CASE rather than OR, because `json_extract` on
    -- malformed text raises rather than answering and SQLite does not promise to evaluate
    -- the terms of an OR in the order they are written.
    SELECT RAISE(ABORT, 'submission_reconciliations: refetched_forecast_snapshot must record a confirming refetch')
    WHERE CASE
              WHEN typeof(NEW.refetched_forecast_snapshot) = 'text'
               AND json_valid(NEW.refetched_forecast_snapshot)
              THEN json_extract(NEW.refetched_forecast_snapshot, '$.outcome')
          END IS NOT 'confirmed';
END;

-- ---------------------------------------------------------------------------
-- Append-only enforcement (D25), 003/010's shape.
-- ---------------------------------------------------------------------------
--
-- Unconditional: there is no legitimate UPDATE or DELETE of a reconciliation, so there is no
-- guarded WHERE to get wrong. They rest on `PRAGMA recursive_triggers = ON` for 010's reason:
-- without it an INSERT OR REPLACE on `reservation_id` or `attempt_id` would delete the row it
-- replaces without firing BEFORE DELETE.

CREATE TRIGGER submission_reconciliations_block_update
BEFORE UPDATE ON submission_reconciliations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_reconciliations is append-only: a recorded reconciliation is never updated (D25)');
END;

CREATE TRIGGER submission_reconciliations_block_delete
BEFORE DELETE ON submission_reconciliations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_reconciliations is append-only: a recorded reconciliation is never deleted (D25)');
END;

-- ---------------------------------------------------------------------------
-- A reconciled key is spent.
-- ---------------------------------------------------------------------------
--
-- Two NEW triggers; 010's are not touched. 010 refuses a release of a reservation an attempt
-- consumed, keyed on the attempt. A reconciliation consumes one too, and a release written
-- afterwards would say nothing landed about a post a person and a refetch both saw.
CREATE TRIGGER submission_key_releases_refuse_reconciled
BEFORE INSERT ON submission_key_releases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_key_releases: this reservation was reconciled as a post that reached the platform and was not abandoned')
    WHERE EXISTS (
        SELECT 1 FROM submission_reconciliations WHERE reservation_id = NEW.reservation_id
    );
END;

-- And the other direction: once a reconciliation records the post made under a key, an
-- attempt row for that key -- or carrying the attempt id the reconciliation named -- would be
-- a second record of one post. On the live path this cannot arise (the key is held by an
-- unreleased reservation, so nothing can claim it to post), which is why it belongs here: the
-- layer that holds against a writer that did not go through that path.
CREATE TRIGGER submission_attempts_refuse_reconciled_key
BEFORE INSERT ON submission_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_attempts: a reconciliation already records the post made under this idempotency key')
    WHERE EXISTS (
        SELECT 1 FROM submission_reconciliations c
          JOIN submission_key_reservations r ON r.reservation_id = c.reservation_id
         WHERE r.idempotency_key = NEW.idempotency_key
    )
       OR EXISTS (
        SELECT 1 FROM submission_reconciliations WHERE attempt_id = NEW.attempt_id
    );
END;

-- ---------------------------------------------------------------------------
-- The lifecycle link.
-- ---------------------------------------------------------------------------
--
-- NULL on every stored event, which is what each one already meant. The partial UNIQUE index
-- is 003's "one event per detail row" rule for the new column, and like 003's it is another
-- conflict target whose REPLACE delete `lifecycle_events_block_delete` must catch.

ALTER TABLE lifecycle_events ADD COLUMN submission_reconciliation_id TEXT REFERENCES submission_reconciliations (reconciliation_id);

CREATE UNIQUE INDEX lifecycle_events_one_event_per_reconciliation
    ON lifecycle_events (submission_reconciliation_id) WHERE submission_reconciliation_id IS NOT NULL;

-- 009's definition with the three hunks marked `M2-713` and NOTHING ELSE CHANGED; see the
-- header. The comments inside it are 009's and still describe 009's clauses.
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
      AND NEW.submission_reconciliation_id IS NULL  -- M2-713: a reconciled confirmation is checked below
      AND (NEW.submission_verification_id IS NULL
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);

    -- >>> M2-713
    -- The second way to confirm an unsettled submission: a reconciliation of a post the
    -- ledger never recorded. It backs `submission_confirmed` and nothing else, and it is the
    -- event's only link. Written as one clause over the link rather than as a new arm in
    -- every clause above, so every other event type keeps its 009 definition unchanged and
    -- still cannot carry this link: this clause refuses it on all of them.
    SELECT RAISE(ABORT, 'lifecycle_events: a submission reconciliation links only a submission_confirmed event, and nothing else beside it')
    WHERE NEW.submission_reconciliation_id IS NOT NULL
      AND (NEW.event_type <> 'submission_confirmed'
           OR NEW.approval_event_id IS NOT NULL
           OR NEW.submission_attempt_id IS NOT NULL
           OR NEW.submission_verification_id IS NOT NULL
           OR NEW.resolution_event_id IS NOT NULL
           OR NEW.score_event_id IS NOT NULL);
    -- <<< M2-713

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

    -- >>> M2-713
    -- The reconciliation carries its own forecast_record_id, so the ownership check is
    -- direct rather than the join the verification probe above needs. The foreign key would
    -- also refuse an unknown id, after this trigger and with SQLite's message.
    SELECT RAISE(ABORT, 'lifecycle_events: the linked submission_reconciliations row is for another forecast record')
    WHERE NEW.submission_reconciliation_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM submission_reconciliations
           WHERE reconciliation_id = NEW.submission_reconciliation_id
             AND forecast_record_id = NEW.forecast_record_id
      );
    -- <<< M2-713

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
