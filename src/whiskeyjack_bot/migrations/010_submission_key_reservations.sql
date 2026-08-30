-- M2-708: reserve an idempotency key atomically, before any network I/O.
--
-- `submission.require_key_unused` is a read, and has said so since M2-702. On the one
-- live path -- `submission_live.post_approved_forecast` -- the order was: derive the key,
-- read that nobody has spent it, GET the question, POST, then INSERT the attempt row.
-- `001`s `idempotency_key TEXT NOT NULL UNIQUE` refuses the *second row*, and the second
-- row is written after its post has already been made. So the constraint protects the
-- shape of the ledger and not the platform: two commands for one derived key could both
-- pass the read and both post, and the ledger would then be able to record only one of
-- the two live forecasts it caused. Raised by M2-702s round-1 review as a backlog
-- candidate, unreachable at the time because no gateway existed; M2-704 shipped one.
--
-- The claim has to be durable, because the failure it prevents is durable. A lock file
-- released by the kernel on crash reopens the window in exactly the case that matters,
-- and a write transaction held across the post would serialize the whole database behind
-- a multi-second HTTP call. So the claim is a row.
--
-- WHY TWO NEW TABLES RATHER THAN A COLUMN
--
-- Neither existing table can hold it:
--
--   * `submission_attempts` is written once, after the call has finished -- 003s
--     `submission_attempts_require_receipt_on_insert` requires `completed_at_utc`, and
--     003s block triggers forbid the UPDATE that filling it in later would need. A row
--     inserted before the post could never be completed.
--   * `lifecycle_events.event_type` is a column CHECK, and SQLite cannot widen a CHECK
--     without rebuilding the table -- the rebuild 009 refused for the same reason, and a
--     reservation is not a change in a records status anyway.
--
-- So: `submission_key_reservations` (the claim) and `submission_key_releases` (its
-- resolution). That pairing is not new here; it is the shape `submission_attempts` and
-- `submission_verifications` already have, and it is what lets both tables stay strictly
-- append-only (D25) while the *state* of a key still changes. A keys state is derived,
-- never stored:
--
--   spent     -- a `submission_attempts` row exists for it. Terminal.
--   reserved  -- a reservation exists with no release row and no attempt row.
--   released  -- every reservation for it carries a release row; the key is free again.
--   free      -- no reservation at all.
--
-- WHY A RELEASE EXISTS AT ALL
--
-- Because an atomic reservation creates a state that did not exist before: reserved, but
-- no attempt row. The key is a pure function of (tournament, question, forecast version,
-- payload hash), so a reservation with no exit does not block a retry -- it blocks that
-- forecast, permanently, on an append-only table. Two exits, and they are not the same
-- claim (owner decision, 2026-08-28):
--
--   * `not_posted`         -- the gateway proved no post was made. `MetaculusSubmission
--                             Gateway.post_attempted` is still false, so the refusal
--                             happened before the single `post` call. Written by the
--                             program, with no `released_by`: there is no person to name.
--   * `operator_abandoned` -- a human checked the platform and asserts nothing landed.
--                             This is the crash-mid-post case, where the program knows
--                             nothing. `released_by` is required by the writer for it,
--                             for `approve`s reason: an attribution claim about a person
--                             is never inferred from the machine.
--
-- `reason` is deliberately NOT a column CHECK. 009 established what a closed column
-- vocabulary costs on an append-only table -- widening one is a full rebuild -- and this
-- vocabulary is the kind that grows. The schema enforces the columns *shape* (non-blank
-- text, no NUL, bounded); `submission.ReservationReason` owns its *membership*, which is
-- the layer that can change without a migration.
--
-- WHAT THIS MIGRATION DOES NOT TOUCH
--
-- Every existing trigger body. 004, 006, 007 and 008 each rewrote
-- `forecast_records_require_draft_on_insert`, and 009 rewrote two more; this is the first
-- migration since 003 that rewrites none, because everything it constrains is new. There
-- is no upgrade precondition scan either (006s pattern), and it is not needed rather
-- than skipped: both tables are created by this migration, inside the one BEGIN/COMMIT
-- `ledger._apply_migration` wraps it in, so no row can exist before the triggers below
-- do. That is also why the release trigger compares against `reserved_at_utc` without
-- first probing that it is in the pinned form -- unlike 003s verification trigger, whose
-- rows could predate the pin, this tables cannot.

-- ---------------------------------------------------------------------------
-- The claim.
-- ---------------------------------------------------------------------------
--
-- `reservation_seq` numbers a keys reservations 1, 2, 3... and is what
-- `UNIQUE (idempotency_key, reservation_seq)` turns into a race that fails loudly rather
-- than one that silently reserves twice. It is the second line of defence and not the
-- first: `submission.reserve_submission_key` does its read and its insert inside
-- `lifecycle.transaction`, which is `BEGIN IMMEDIATE`, so the ordinary contended case is
-- a clean typed refusal. The same division `lifecycle_events.event_seq` documents.
--
-- `reserved_at_utc` is pinned to the fixed-width UTC form 003 pins its ordered columns
-- to, because `submission_key_releases.released_at_utc` is compared against it and a
-- lexicographic comparison against an unknown format is a coin toss that reads like a
-- check. `created_at_utc` is not pinned: nothing orders it.
CREATE TABLE submission_key_reservations (
    reservation_id     TEXT PRIMARY KEY NOT NULL,
    idempotency_key    TEXT NOT NULL,
    forecast_record_id TEXT NOT NULL REFERENCES forecast_records (record_id),
    reservation_seq    INTEGER NOT NULL,
    reserved_at_utc    TEXT NOT NULL,
    created_at_utc     TEXT NOT NULL,
    UNIQUE (idempotency_key, reservation_seq)
);

-- ---------------------------------------------------------------------------
-- Its resolution.
-- ---------------------------------------------------------------------------
--
-- `reservation_id ... UNIQUE` is the whole releasing rule at the schema level: a
-- reservation is released once, and a second release for it is refused by the index
-- rather than by a reader remembering to look.
CREATE TABLE submission_key_releases (
    release_id      TEXT PRIMARY KEY NOT NULL,
    reservation_id  TEXT NOT NULL UNIQUE REFERENCES submission_key_reservations (reservation_id),
    reason          TEXT NOT NULL,
    released_by     TEXT,
    note            TEXT,
    released_at_utc TEXT NOT NULL,
    created_at_utc  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- What a reservation must be.
-- ---------------------------------------------------------------------------
--
-- The identifier clauses are 006s predicate, character for character, including the 29
-- codepoints where Pythons `str.isspace()` is true. One-argument `trim()` strips U+0020
-- and nothing else, which is the hole M1-603 spent five rounds on; and `typeof()` is
-- checked because a blob identifier is a row this schemas own readers can never hand
-- back. A fourth spelling of blank is exactly the defect this family of guards descends
-- from, so the set is copied rather than paraphrased.
--
-- `forecast_record_id` is probed ahead of the foreign key so the message is this schemas
-- own rather than SQLites, the order `lifecycle_events_validate_on_insert` uses.
CREATE TRIGGER submission_key_reservations_validate_on_insert
BEFORE INSERT ON submission_key_reservations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_key_reservations: reservation_id must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.reservation_id IS NULL
       OR typeof(NEW.reservation_id) <> 'text'
       OR length(NEW.reservation_id) > 200
       OR instr(NEW.reservation_id, char(0)) > 0
       OR trim(NEW.reservation_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    SELECT RAISE(ABORT, 'submission_key_reservations: idempotency_key must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.idempotency_key IS NULL
       OR typeof(NEW.idempotency_key) <> 'text'
       OR length(NEW.idempotency_key) > 200
       OR instr(NEW.idempotency_key, char(0)) > 0
       OR trim(NEW.idempotency_key,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    SELECT RAISE(ABORT, 'submission_key_reservations: forecast_record_id does not name a stored forecast record')
    WHERE NOT EXISTS (
        SELECT 1 FROM forecast_records WHERE record_id = NEW.forecast_record_id
    );

    SELECT RAISE(ABORT, 'submission_key_reservations: reserved_at_utc must be a UTC timestamp of the form YYYY-MM-DDTHH:MM:SS.ffffff+00:00')
    WHERE typeof(NEW.reserved_at_utc) <> 'text'
       OR length(NEW.reserved_at_utc) <> 32
       OR NEW.reserved_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00';

    -- INTEGER is affinity, not a type (002s note on posts_dropped_no_url), so typeof()
    -- is part of the constraint and not decoration.
    SELECT RAISE(ABORT, 'submission_key_reservations: reservation_seq must be the next sequence number for this idempotency key')
    WHERE typeof(NEW.reservation_seq) <> 'integer'
       OR NEW.reservation_seq <> COALESCE(
              (SELECT max(reservation_seq) FROM submission_key_reservations
                WHERE idempotency_key = NEW.idempotency_key), 0) + 1;

    -- The invariant the whole item rests on: at most one live reservation per key. Every
    -- earlier reservation of this key must already carry its release row.
    SELECT RAISE(ABORT, 'submission_key_reservations: this idempotency key is already reserved and that reservation has not been released')
    WHERE EXISTS (
        SELECT 1 FROM submission_key_reservations r
         WHERE r.idempotency_key = NEW.idempotency_key
           AND NOT EXISTS (
               SELECT 1 FROM submission_key_releases x WHERE x.reservation_id = r.reservation_id
           )
    );

    -- A spent key is never reserved again. This is the same refusal `require_key_unused`
    -- makes one layer up, in the layer that cannot be raced.
    SELECT RAISE(ABORT, 'submission_key_reservations: this idempotency key has already been used by a recorded submission attempt')
    WHERE EXISTS (
        SELECT 1 FROM submission_attempts WHERE idempotency_key = NEW.idempotency_key
    );

    -- A key is derived from (tournament, question, forecast version, payload hash), and
    -- 001 declares UNIQUE (question_id, tournament_id, forecast_version), so key ->
    -- record is a function. Two records claiming one key means one of the two derivations
    -- is wrong, and the reservation is the last place that is cheap to notice. Both ends
    -- of the join, which is 006s argument for guarding forecast_records.tournament_id.
    SELECT RAISE(ABORT, 'submission_key_reservations: this idempotency key was already reserved against a different forecast record')
    WHERE EXISTS (
        SELECT 1 FROM submission_key_reservations r
         WHERE r.idempotency_key = NEW.idempotency_key
           AND r.forecast_record_id <> NEW.forecast_record_id
    );
END;

-- ---------------------------------------------------------------------------
-- What a release must be.
-- ---------------------------------------------------------------------------
--
-- A release says a reservation was abandoned. It is not a way to un-spend a key: if an
-- attempt row exists, the reservation was *consumed*, and recording it as abandoned would
-- assert something false about an irreversible call. That refusal is the last clause
-- below, and it is the one that keeps the derived-state table in this files header from
-- having two answers for one key.
CREATE TRIGGER submission_key_releases_validate_on_insert
BEFORE INSERT ON submission_key_releases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_key_releases: release_id must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.release_id IS NULL
       OR typeof(NEW.release_id) <> 'text'
       OR length(NEW.release_id) > 200
       OR instr(NEW.release_id, char(0)) > 0
       OR trim(NEW.release_id,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    SELECT RAISE(ABORT, 'submission_key_releases: reservation_id does not name a stored key reservation')
    WHERE NOT EXISTS (
        SELECT 1 FROM submission_key_reservations WHERE reservation_id = NEW.reservation_id
    );

    SELECT RAISE(ABORT, 'submission_key_releases: reason must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.reason IS NULL
       OR typeof(NEW.reason) <> 'text'
       OR length(NEW.reason) > 200
       OR instr(NEW.reason, char(0)) > 0
       OR trim(NEW.reason,
               char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                    8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                    8232, 8233, 8239, 8287, 12288)) = '';

    -- Nullable, because the program releases its own reservation and has no person to
    -- name. Present means a claim about a human, and a blank one is worse than none.
    SELECT RAISE(ABORT, 'submission_key_releases: released_by, when present, must be non-blank text of at most 200 characters and no NUL')
    WHERE NEW.released_by IS NOT NULL
      AND (typeof(NEW.released_by) <> 'text'
           OR length(NEW.released_by) > 200
           OR instr(NEW.released_by, char(0)) > 0
           OR trim(NEW.released_by,
                   char(9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192,
                        8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
                        8232, 8233, 8239, 8287, 12288)) = '');

    SELECT RAISE(ABORT, 'submission_key_releases: note, when present, must be text')
    WHERE NEW.note IS NOT NULL AND typeof(NEW.note) <> 'text';

    SELECT RAISE(ABORT, 'submission_key_releases: released_at_utc must be a UTC timestamp of the form YYYY-MM-DDTHH:MM:SS.ffffff+00:00')
    WHERE typeof(NEW.released_at_utc) <> 'text'
       OR length(NEW.released_at_utc) <> 32
       OR NEW.released_at_utc NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00';

    -- Exact, because both sides are pinned to the same fixed-width UTC form.
    SELECT RAISE(ABORT, 'submission_key_releases: released_at_utc is earlier than the reservation it releases')
    WHERE NEW.released_at_utc < (
        SELECT reserved_at_utc FROM submission_key_reservations
         WHERE reservation_id = NEW.reservation_id
    );

    SELECT RAISE(ABORT, 'submission_key_releases: this reservation was consumed by a recorded submission attempt and was not abandoned')
    WHERE EXISTS (
        SELECT 1 FROM submission_attempts a
         WHERE a.idempotency_key = (
             SELECT r.idempotency_key FROM submission_key_reservations r
              WHERE r.reservation_id = NEW.reservation_id
         )
    );
END;

-- ---------------------------------------------------------------------------
-- Append-only enforcement (D25).
-- ---------------------------------------------------------------------------
--
-- Same shape and same reason as 003s: SQLite has no multi-event trigger, so UPDATE and
-- DELETE are separate objects per table, and every one of them is unconditional -- there
-- is no legitimate UPDATE or DELETE against a ledger row, so there is no guarded WHERE to
-- get wrong. These are also what make the release table a *record* rather than a way to
-- rewind a claim: a reservation cannot be edited into a different key, and a release
-- cannot be deleted to make an abandoned key look spent.
--
-- They depend on `PRAGMA recursive_triggers = ON`, which `ledger.connect` sets and reads
-- back: without it `INSERT OR REPLACE` resolves a UNIQUE conflict by a delete that fires
-- no BEFORE DELETE trigger, which would let a single REPLACE overwrite a live reservation
-- with another one.

CREATE TRIGGER submission_key_reservations_block_update
BEFORE UPDATE ON submission_key_reservations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_key_reservations is append-only: a key reservation is never updated (D25); append a submission_key_releases row instead');
END;

CREATE TRIGGER submission_key_reservations_block_delete
BEFORE DELETE ON submission_key_reservations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_key_reservations is append-only: a key reservation is never deleted (D25); append a submission_key_releases row instead');
END;

CREATE TRIGGER submission_key_releases_block_update
BEFORE UPDATE ON submission_key_releases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_key_releases is append-only: a recorded release is never updated (D25)');
END;

CREATE TRIGGER submission_key_releases_block_delete
BEFORE DELETE ON submission_key_releases
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'submission_key_releases is append-only: a recorded release is never deleted (D25)');
END;
