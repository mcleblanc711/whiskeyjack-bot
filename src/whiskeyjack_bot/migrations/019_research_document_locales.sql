-- M1-357 (D53): the document-locale sidecar.
--
-- WHY A NEW TABLE AND NOT A COLUMN
--
-- What the local-source lens has to remember about a document -- the language the provider
-- reported, the native title next to the provider's English one, when the provider first saw
-- it, and whether its publisher is based in the place the question is about -- has no field on
-- `ResearchDocument`, and cannot be given one. `research/packet.py` hashes the full
-- `model_dump` of every document, and `submission_policy.require_research_artifacts`
-- recomputes that hash from stored rows before an approval. A new document field would appear
-- in every dump (as `null` on every existing row) and move every stored packet hash, so every
-- pending approval would be refused. A table keyed to `document_id` changes no existing row,
-- no existing dump and no existing hash. The packet carries these rows under its own 1.1.0
-- rule, which is used only when a packet has some (`research/packet.py`).
--
-- WHAT A ROW CLAIMS
--
-- Provider facts, stored exactly as the provider reported them, and one classification of ours:
--
--   language, provider_story_country, native_title, english_title, provider_first_seen_at_utc,
--   provider_labels_json -- verbatim from the provider, NULL where it said nothing. Never
--   invented, never normalized (AskNews writes `en`, `en-US` and `en-us`; all three are kept).
--   `provider_story_country` is named for what the 2026-10-16 probe showed it is: AskNews's
--   tag for the story, not the publisher.
--
--   translation_source -- who produced `english_title`: `provider:asknews` or `none`. The
--   translation model is not a column because AskNews does not disclose it; M1-361 adds one
--   when the pipeline translates for itself.
--
--   locale_code, publisher_origin -- which locale the lens run targeted, and where this
--   document's publisher sits relative to it (`research/locales.py:publisher_origin`: local,
--   external or unclassified). Stored rather than re-derived, because the derivation reads the
--   registry file and a later edit to that file must not change what an old row says.
--
-- APPEND-ONLY
--
-- Like every evidence table (003): no UPDATE, no DELETE. A row is written in the same
-- transaction as its document (`research/store.py`), so a document never exists without the
-- locale facts that were known when it was written, and a locale row never exists without its
-- document.

CREATE TABLE research_document_locales (
    document_id TEXT PRIMARY KEY REFERENCES research_documents (document_id),
    locale_code TEXT NOT NULL,
    publisher_origin TEXT NOT NULL,
    language TEXT,
    provider_story_country TEXT,
    native_title TEXT,
    english_title TEXT,
    translation_source TEXT NOT NULL,
    provider_first_seen_at_utc TEXT,
    provider_labels_json TEXT,
    created_at_utc TEXT NOT NULL
);

-- Vocabularies are trigger clauses rather than CHECKs, so a later item can widen one with a
-- DROP/CREATE (the 004-018 escape hatch) instead of rebuilding an append-only table.
CREATE TRIGGER research_document_locales_validate_on_insert
BEFORE INSERT ON research_document_locales
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_document_locales: document_id must name an existing document')
    WHERE NOT EXISTS (
        SELECT 1 FROM research_documents WHERE document_id = NEW.document_id
    );
    SELECT RAISE(ABORT, 'research_document_locales: publisher_origin is not in the schema vocabulary')
    WHERE NEW.publisher_origin NOT IN ('local', 'external', 'unclassified');
    SELECT RAISE(ABORT, 'research_document_locales: translation_source is not in the schema vocabulary')
    WHERE NEW.translation_source NOT IN ('provider:asknews', 'none');
    SELECT RAISE(ABORT, 'research_document_locales: locale_code must be non-blank')
    WHERE typeof(NEW.locale_code) <> 'text' OR length(trim(NEW.locale_code)) = 0;
    SELECT RAISE(ABORT, 'research_document_locales: an untranslated row cannot carry an English title')
    WHERE NEW.translation_source = 'none' AND NEW.english_title IS NOT NULL
      AND NEW.english_title IS NOT NEW.native_title;
END;

CREATE TRIGGER research_document_locales_no_update
BEFORE UPDATE ON research_document_locales
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_document_locales is append-only: a locale row is never updated');
END;

CREATE TRIGGER research_document_locales_no_delete
BEFORE DELETE ON research_document_locales
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'research_document_locales is append-only: a locale row is never deleted');
END;
