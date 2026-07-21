-- M1-301: fields the research-document schema needs and 001 does not have.
--
-- 001_initial.sql built research_documents from the handoff's column list,
-- which predates the brief amendment introducing `provenance` (the amendment
-- assigns the backfill to M1-301). The other four columns are the same shape of
-- gap: the schema requires them, the initial migration has no slot for them.
--
-- 001 is not edited: ledger.py records each migration's sha256 when applied and
-- refuses to run against a database whose stored checksum no longer matches.
--
-- All columns are added NULLable. SQLite requires a non-null default on an
-- added NOT NULL column, and defaulting `provenance` to 'direct_api' would
-- stamp an unearned provenance claim onto any pre-existing row -- a false
-- attribution record, which the ledger exists to prevent. The Pydantic models
-- require these fields; database-level enforcement arrives with the write path
-- and its append-only triggers (M1-602/M1-603).
--
-- A CHECK is attached to the new `provenance` column, which ADD COLUMN permits.
-- No CHECK is added to the pre-existing `source_type` / `reliability_tag`
-- columns: constraining an existing column requires the full table-rebuild
-- procedure, which is not worth the risk on a merged migration for vocabularies
-- whose strict models already reject off-list values.

-- The URL exactly as the provider returned it. M1-305 rewrites canonical_url
-- for deduplication; without this column the as-retrieved URL is unrecoverable.
ALTER TABLE research_documents ADD COLUMN original_url TEXT;

-- 'direct_api': the pipeline retrieved the document itself.
-- 'llm_reported': a research agent reported it; content and timestamps are
-- claims, and the forecaster prompt caps how load-bearing such a document may be.
ALTER TABLE research_documents ADD COLUMN provenance TEXT
    CHECK (provenance IS NULL OR provenance IN ('direct_api', 'llm_reported'));

-- Identity of the second model participating in evidence gathering (M1-307).
ALTER TABLE research_runs ADD COLUMN agent_model TEXT;

-- Citation hygiene: agent-reported posts dropped for lacking a resolvable
-- status URL. Counted so a run's dropped-citation rate stays auditable.
ALTER TABLE research_runs ADD COLUMN posts_dropped_no_url INTEGER;

-- The question a run gathered evidence for. Runs are per question, but 001
-- carried the linkage only in the reverse direction, via
-- forecast_records.retrieval_run_id.
ALTER TABLE research_runs ADD COLUMN question_id INTEGER;
