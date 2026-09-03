-- M2-707: bind an approval to the submission payload it authorized.
--
-- 003 made an approval bind to `forecast_sha256`, and that is where the binding stopped.
-- One approved forecast therefore covered *every* payload built from it: M2-702 derives
-- an idempotency key from `(tournament, question, forecast_version, payload hash)`, so a
-- changed payload is a different key -- but it is not a different approval, and the key
-- seam had nothing to compare the payload against. `submission.submission_key_for_
-- approved_record` said so in its own docstring from the day it shipped, and decision
-- D33 recorded why it could not be closed then: the forecast->payload mapping did not
-- exist, so an operator asked to approve a payload hash would have been approving a value
-- nothing could compute. M1-502 and M1-503 have since landed, M2-707 builds the mapping,
-- and this migration is where the decision gets somewhere to live.
--
-- WHY A COLUMN AND NOT A TABLE
--
-- 010 added two tables because a reservation is a *new fact with its own lifetime* --
-- claimed, then released or spent -- that no existing row could carry. This is the
-- opposite case. The payload hash is a property of one approval decision, decided at the
-- instant that decision is made, immutable for as long as the decision is; it has exactly
-- the lifetime of the `approval_events` row and exactly its cardinality. A side table
-- would add a join to every approval read in exchange for a row count that is always
-- exactly one, and it would make "an approval with no payload binding" representable in
-- two different ways. `forecast_sha256` is already on this row for the same reasons.
--
-- WHY NULLABLE
--
-- 002/003/004/008's argument, unchanged: `ALTER TABLE ... ADD COLUMN` cannot add NOT NULL
-- without a default, and no default is honest for a row nobody computed a payload for. It
-- is also a real value on the write path and not merely a legacy artifact -- see the
-- decision split below. New rows are constrained by the trigger instead.
--
-- REQUIRED FOR `approved`, FORBIDDEN FOR `rejected`
--
-- A rejection authorizes nothing, so there is nothing for it to bind to. That is not the
-- whole reason. The payload is *derived* -- a numeric one runs the pinned SDK's CDF
-- conversion, which can refuse a percentile set -- so a record whose payload cannot be
-- built would become impossible to reject if a hash were required here, and rejecting is
-- the one decision that must always be available. Requiring it for `approved` and
-- forbidding it for `rejected` also means the column is never ambiguous: a NULL on an
-- approval is a pre-011 row and nothing else, which is what lets
-- `submission_key_for_approved_record` refuse those rather than guess.
--
-- WHAT THIS TRIGGER CANNOT CHECK, AND WHY THAT IS NOT A GAP IN IT
--
-- The schema owns the *shape* of the binding; it cannot own the *derivation*. Whether
-- 64 hex characters are the hash of the payload this record derives is a question about
-- canonical JSON, the pinned SDK's CDF conversion and the calibration configuration --
-- none of which exist inside SQLite. So the two layers here are not the usual pair of
-- identical rules: Python (`submission_payload.payload_sha256_for_record`, and the
-- derivation gate in `submission_live.post_approved_forecast`) decides that the hash is
-- the right one, and this trigger decides that a hash is present exactly when a decision
-- authorizes something. Both are needed and neither substitutes for the other -- an
-- approval row written by raw SQL with no payload hash is refused here, and one written
-- with the wrong payload hash fails closed at the key seam.
--
-- ONLY THE INSERT TRIGGER IS TOUCHED
--
-- A trigger's body cannot be ALTERed, so `approval_events_bind_forecast_hash_on_insert`
-- is dropped and recreated by the same name -- the pattern 004, 006, 007, 008 and 009
-- established. That touches the trigger definition and nothing else: not the table's
-- rows, not its append-only block triggers, and not its CHECK constraints, so this is not
-- the table-rebuild hazard 003's header describes. The recreated trigger is 003's
-- definition with M2-707's two clauses appended and NOTHING ELSE CHANGED.

ALTER TABLE approval_events ADD COLUMN payload_sha256 TEXT;

DROP TRIGGER approval_events_bind_forecast_hash_on_insert;

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

    -- Everything above this line is 003's definition, unchanged. Everything below is
    -- M2-707's.

    -- An approval authorizes one payload, so it carries that payload's hash. `length()`
    -- and the GLOB are the same shape test 003 applies to `forecast_sha256` above; the
    -- typeof() probe is part of the constraint for the affinity reason 002 documents for
    -- posts_dropped_no_url -- TEXT is affinity, not a type, and a blob of 64 bytes would
    -- otherwise satisfy length().
    SELECT RAISE(ABORT, 'approval_events: an approved decision must carry a payload_sha256 of 64 lowercase hex characters')
    WHERE NEW.decision = 'approved'
      AND (NEW.payload_sha256 IS NULL
           OR typeof(NEW.payload_sha256) <> 'text'
           OR length(NEW.payload_sha256) <> 64
           OR NEW.payload_sha256 GLOB '*[^0-9a-f]*');

    -- And a rejection carries none. Written as a refusal rather than left unconstrained
    -- so that a NULL on this column has exactly one meaning per decision: on `approved`
    -- it is a pre-011 row, and on `rejected` it is the only legal value.
    SELECT RAISE(ABORT, 'approval_events: a rejected decision authorizes no payload, so payload_sha256 must be NULL')
    WHERE NEW.decision = 'rejected'
      AND NEW.payload_sha256 IS NOT NULL;
END;
