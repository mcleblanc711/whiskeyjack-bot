-- M1-306: the discarded-evidence counters a research run has nowhere to record.
--
-- Numbered 005, not 004. This was written as 004 while `docs/TRACKS.md` said 004 was
-- the next free number; M1-606 held that claim on its own unmerged branch and landed
-- `004_pipeline_failure_events.sql` on master first. The migration claim in TRACKS.md
-- is advisory -- nothing reads that column -- so the enforcement is
-- `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement,
-- and this is that check working: the collision surfaced at the daily master merge,
-- before either migration could be applied twice under one number. Renumbering is
-- safe precisely because 004 here was never on master and so was never immutable.
--
-- Both retrieval adapters already count two things and can persist neither. AskNews
-- (M1-302) and Exa (M1-303) return `documents_dropped` (results that could not be
-- normalized into a usable document) and `duplicates_collapsed` (repeats of one article
-- inside a single run) on their own in-memory result objects, because `research_runs`
-- has no column for either. `docs/M1-301-NOTES.md` left the decision to this item:
-- "M1-306 decides whether they become columns". They do.
--
-- 002 already settled the principle, and settled it against its own first instinct. That
-- migration's first cut argued such numbers could stay model-side because an off-range
-- count is "a bad measurement, not an uninterpretable row", and then reversed: a stored
-- -1 for `posts_dropped_no_url` "is not a bad measurement but an unfalsifiable claim
-- about how much evidence was discarded". `documents_dropped` is the same claim about the
-- same subject -- how much retrieved evidence never reached the ledger -- so it is stored
-- the same way, with the same guard.
--
-- Both columns are NULLable, and that is load-bearing rather than a concession to
-- `ADD COLUMN`:
--
--   * rows written before this migration never had the counts and keep an honest NULL;
--   * M1-306 writes a run in two phases -- `open_run` inserts identity and the start time
--     *before* the billable calls, so a crash still leaves the spend attributable -- and
--     at insert time nothing has been dropped or collapsed yet, because nothing has been
--     retrieved yet.
--
-- So NULL means *unmeasured* and 0 is the auditable claim that nothing was discarded. A
-- NOT NULL DEFAULT 0 would collapse those two into the second, manufacturing a
-- measurement for every row that never took one -- exactly the failure 002 named when it
-- refused to default `provenance` to 'direct_api'.
--
-- typeof() is part of each CHECK for the reason 002 spells out: `INTEGER` in SQLite is
-- *affinity*, not a type. A REAL that cannot be losslessly converted stays REAL and a
-- non-numeric string stays TEXT, so without it both 1.5 and 'garbage' satisfy `>= 0` and
-- are stored as-is -- 'garbage' passing only because SQLite orders TEXT above every
-- number. Both columns are new, so every pre-existing row is NULL and passes.
--
-- No trigger changes. 003 pins a run's identity and provenance (retrieval_run_id,
-- provider, question_id, its timestamps) and deliberately leaves the completion columns
-- writable; these are completion columns, so `complete_run` may fill them in and nothing
-- may re-identify a run through them. 001/002/003 are not edited: ledger.py records each
-- migration's sha256 when it is applied and refuses to run against a database whose
-- stored checksum no longer matches.

-- Retrieved results that could not be normalized into a usable document. Counted so a
-- run's drop rate stays auditable rather than being inferred from a document shortfall.
ALTER TABLE research_runs ADD COLUMN documents_dropped INTEGER
    CHECK (documents_dropped IS NULL
           OR (typeof(documents_dropped) = 'integer' AND documents_dropped >= 0));

-- Repeats of one article collapsed within this run. Distinct from cross-run dedup, which
-- never happens: two providers surfacing one article are two legitimate rows, because the
-- run id is part of the attribution (M1-305).
ALTER TABLE research_runs ADD COLUMN duplicates_collapsed INTEGER
    CHECK (duplicates_collapsed IS NULL
           OR (typeof(duplicates_collapsed) = 'integer' AND duplicates_collapsed >= 0));
