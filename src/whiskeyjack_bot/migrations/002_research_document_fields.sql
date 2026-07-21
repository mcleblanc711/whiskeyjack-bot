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
-- attribution record, which the ledger exists to prevent.
--
-- NULLable columns alone, though, would leave the database accepting *new*
-- provenance-less rows forever, which is the hole the models exist to close;
-- relying on "the write path validates first" makes the guarantee a convention
-- rather than a constraint. So enforcement is done with BEFORE INSERT / BEFORE
-- UPDATE triggers instead of column constraints. That is what separates the two
-- populations a column default cannot: rows written from here on must be
-- complete, while rows that predate this migration keep their honest NULLs.
-- (Consequence, and intended: a legacy row cannot be UPDATEd until it is also
-- backfilled. The ledger is append-only, so nothing should be updating it
-- anyway -- M1-602/M1-603 add the triggers that forbid UPDATE outright.)
--
-- The triggers also carry the vocabulary checks that a CHECK constraint cannot
-- be retrofitted onto the pre-existing `source_type` / `reliability_tag`
-- columns without the full 12-step table rebuild, and enforce the social-
-- document trust contract that ResearchDocument enforces model-side. Numeric
-- range checks (cost_usd, posts_dropped_no_url) stay model-side: an off-range
-- number is a bad measurement, not a row that cannot be interpreted.

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

-- Enforcement. Each trigger body is a sequence of guarded RAISE(ABORT) probes:
-- `SELECT RAISE(...) WHERE <violation>` fires only when the row is bad. Messages
-- name fields and never interpolate row values -- a trigger message reaches the
-- same logs as a sanitized ResearchSchemaError, and document text is untrusted.
--
-- Insert and update bodies are identical per table and are spelled out twice:
-- SQLite has no multi-event trigger, and a shared helper would have to be a
-- table-valued function it also does not have.

CREATE TRIGGER research_documents_require_provenance_on_insert
BEFORE INSERT ON research_documents
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_documents: original_url, provenance and source_type are required')
    WHERE NEW.original_url IS NULL OR NEW.provenance IS NULL OR NEW.source_type IS NULL;

    SELECT RAISE(ABORT, 'research_documents: source_type is not in the schema vocabulary')
    WHERE NEW.source_type NOT IN ('news', 'web', 'official', 'structured', 'social');

    SELECT RAISE(ABORT, 'research_documents: reliability_tag is not in the schema vocabulary')
    WHERE NEW.reliability_tag IS NOT NULL
      AND NEW.reliability_tag NOT IN
          ('official_primary', 'verified_org', 'journalist', 'unverified_social');

    SELECT RAISE(ABORT, 'research_documents: social documents must be llm_reported and carry a reliability_tag')
    WHERE NEW.source_type = 'social'
      AND (NEW.provenance <> 'llm_reported' OR NEW.reliability_tag IS NULL);
END;

CREATE TRIGGER research_documents_require_provenance_on_update
BEFORE UPDATE ON research_documents
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_documents: original_url, provenance and source_type are required')
    WHERE NEW.original_url IS NULL OR NEW.provenance IS NULL OR NEW.source_type IS NULL;

    SELECT RAISE(ABORT, 'research_documents: source_type is not in the schema vocabulary')
    WHERE NEW.source_type NOT IN ('news', 'web', 'official', 'structured', 'social');

    SELECT RAISE(ABORT, 'research_documents: reliability_tag is not in the schema vocabulary')
    WHERE NEW.reliability_tag IS NOT NULL
      AND NEW.reliability_tag NOT IN
          ('official_primary', 'verified_org', 'journalist', 'unverified_social');

    SELECT RAISE(ABORT, 'research_documents: social documents must be llm_reported and carry a reliability_tag')
    WHERE NEW.source_type = 'social'
      AND (NEW.provenance <> 'llm_reported' OR NEW.reliability_tag IS NULL);
END;

CREATE TRIGGER research_runs_require_question_on_insert
BEFORE INSERT ON research_runs
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_runs: question_id is required')
    WHERE NEW.question_id IS NULL;

    SELECT RAISE(ABORT, 'research_runs: provider is not in the schema vocabulary')
    WHERE NEW.provider NOT IN ('asknews', 'exa', 'structured', 'xai_x_search');

    SELECT RAISE(ABORT, 'research_runs: provider xai_x_search requires agent_model and posts_dropped_no_url')
    WHERE NEW.provider = 'xai_x_search'
      AND (NEW.agent_model IS NULL
           OR trim(NEW.agent_model) = ''
           OR NEW.posts_dropped_no_url IS NULL);
END;

CREATE TRIGGER research_runs_require_question_on_update
BEFORE UPDATE ON research_runs
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_runs: question_id is required')
    WHERE NEW.question_id IS NULL;

    SELECT RAISE(ABORT, 'research_runs: provider is not in the schema vocabulary')
    WHERE NEW.provider NOT IN ('asknews', 'exa', 'structured', 'xai_x_search');

    SELECT RAISE(ABORT, 'research_runs: provider xai_x_search requires agent_model and posts_dropped_no_url')
    WHERE NEW.provider = 'xai_x_search'
      AND (NEW.agent_model IS NULL
           OR trim(NEW.agent_model) = ''
           OR NEW.posts_dropped_no_url IS NULL);
END;
