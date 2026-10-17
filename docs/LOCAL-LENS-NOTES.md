# Local-source lens — assessment, probe, design and evaluation plan

**Opened:** 2026-10-16 by owner request. **Decision:** D53. **Items:** M1-356, M1-357, M1-358
(Phase 1), M1-359 and M5-805 (Phase 2), M1-360 and M1-361 (Phase 3), M6-901 (deferred
framework).

The goal is to give Whiskeyjack evidence from **local sources**, meaning publishers based in
the countries a question is about, while keeping the ability to measure whether that
evidence improves forecasts. For a Strait of Hormuz question, that means Iranian outlets and
institutions, and Omani and Emirati ones. It does not mean Persian-language services run from
London.

This file is the running record for the lens, as `M1-NOTES.md` is for M1. Every figure
below was measured on the date given; the command or source is named so it can be measured
again.

---

## 1. What the pipeline does today (verified against code, 2026-10-16, master `ec08cc6`)

- **One English query per question.** `research/orchestrate.py:derive_queries` builds it
  from the title, group parent title, resolution criteria and fine print. One AskNews
  `"latest news"` call costs 1 credit (M1-352) and is settled exactly from `usage.credits`
  (M1-336). Exa is called only as the reason-gated fallback. **Neither provider is sent a
  language, country, domain or location parameter**, apart from Exa `includeDomains` when the
  question names a resolution source.
- **Non-English evidence already arrives, then loses its language.** The 137 stored AskNews
  raw artifacts hold 1,552 articles. Of those, **52% are not English**: es 160, pt 105, ru 98,
  ar 71, zh 66, de 57, tr 46, fr 43, ja 39, uk 32. **None are Persian.** Each article carries
  `language`, `country`, the native `title`, `eng_title`, an English `summary`, `crawl_date`
  and `reporting_voice`. `research/asknews.py:_to_document` keeps `eng_title` and the summary
  and drops the rest, and `ResearchDocument` has no field to hold them. Separately, AskNews's
  `crawl_date` is stored as `updated_at_utc`, but it is really the time AskNews first saw the
  article. That is recorded here, not rewritten.
- **The packet hash fixes what can change.** `research/packet.py` hashes the full
  `model_dump` of every run and document, and `submission_policy.require_research_artifacts`
  recomputes that hash from stored rows before an approval. **A new field on
  `ResearchDocument` or `ResearchRun` would change the hash of every stored packet and refuse
  every pending approval.** Locale data therefore goes in a sidecar table (M1-357).
- **The relevance filter only reads English.** `research/quality.py:relevant` requires
  English title terms in the document's title or snippet. AskNews documents pass because
  their stored text is AskNews's English translation. An untranslated Persian document would
  be dropped before the model ever saw it.
- **A reservation needs a budget context.** `research/durable.py` reserves and settles only
  inside `tournament_state.budget_context`, which exists inside the worker. Outside it,
  `begin_call` and `complete_call` do nothing. Lens calls therefore run in the worker, where
  the series cap and the AskNews sub-cap (D51, M1-355) apply to them.
- **Configuration is bound to the activation.** A new `AppConfig` field moves
  `config_sha256` and retires the live activation (`tournament_state.bindings`), even when it
  defaults to off.
- **There is no notion of an experiment.** Nothing in the codebase represents an arm or a
  variant. `UNIQUE(question_id, tournament_id, forecast_version)` and the report's
  latest-posted-version rule rule out a second forecast per question in `forecast_records`.

## 2. Provider capability probe (2026-10-16, owner-authorized, USD 0.064 spent)

The probe was a one-off script outside the repository and outside any budget context, so
its spend is not in the ledger. Query: *Iran Strait of Hormuz closure shipping threat*, and
the curated Persian terms *تنگه هرمز ایران بستن کشتیرانی*. Settings were 8 results and the
`latest news` strategy with `hours_back=720`.

| Call | Result | Billed |
| --- | --- | --- |
| AskNews, English query, `languages=["fa"]` | **HTTP 422 from the server** (`asknews_sdk.errors.ValidationError`) | 0 |
| AskNews, Persian query, `languages=["fa"]` | **HTTP 422** | 0 |
| AskNews, English query, `countries=["IR"]` | 8: one AFP tanker story in en/ar/es/pt/fr, plus Tehran Times | 1 credit |
| AskNews, English query, `languages=["ar"]` | 8 Arabic outlets (Lebanon, Saudi Arabia, Egypt), with `eng_title` | 1 credit |
| Exa, Persian query | 8: BBC Persian ×4, Mehr, Iran International, Defa Press, SNN | USD 0.007 |
| Exa, Persian query, `userLocation: "IR"` | substantially the same set, with Tabnak | USD 0.007 |

What it establishes:

1. **AskNews has no Persian.** The server rejects `fa` as a language value. The installed
   SDK (asknews 0.13.54) accepts it, so the refusal is AskNews's API, not our client.
2. **AskNews's `country` field describes the story, not the publisher.** The `countries=["IR"]`
   results are AFP (a French agency) copy datelined in Iran. That tag must never be read as
   locality, so the sidecar stores it as `provider_story_country`.
3. **That one call is also a perfect syndication fixture.** It returned one wire story in
   five languages. Counting those as five confirmations is exactly the error the overlap
   analysis (M1-356) exists to prevent.
4. **Language is not locality.** Exa's Persian results mix Iranian publishers (Mehr, Defa
   Press, SNN, Tabnak) with London-based services (BBC Persian, Iran International). The owner's
   correction: **the target is local publishers.** Publisher origin is therefore a separate,
   registry-backed dimension: `local`, `external` (based outside the locale but aimed at it:
   exile, diaspora or a foreign state's language service) or `unclassified`. It is never
   inferred from language and never inferred from the provider's country tag.
5. **Exa returns native text with no translation**, and several results were months old: the
   probe sent no date filter.
6. **Unverified, because the probe's authorized call count was used up:** whether Exa
   `includeDomains` restricted to Iranian domains returns results inside the 30-day freshness
   window. This is the first thing to read when the lens is first enabled live.

## 3. Design (Phase 1)

**Locale registry** (`config/locales.yaml`, M1-356). This file is versioned and is not part
of `AppConfig`. Its sha256 is recorded on every lens run. Each entry is a country, or a
region with a `parent` (Quebec, for example, adds `fr`), and carries:

- **English triggers:** names, demonyms, places and institutions;
- **`languages`:** ISO 639-1 codes;
- **`local_domains` and `external_domains`:** curated publisher and institution domains with
  **no credibility weighting**. Iranian state, semi-official, independent and official outlets
  are all `local`;
- **`native_terms`** (optional): the local names of places and institutions.

A cross-border term lists every locale it touches; Hormuz lists IR, OM and AE.

**Detection** is deterministic: whole-word, case-insensitive matching of the triggers against
the title, group parent title and resolution criteria. A question that matches nothing
triggers no lens. A lens is spent only where it can reach something baseline research does
not: a locale with `local_domains`, or a language other than English. So a question about the
United States alone costs nothing extra.

**Lens dispatch** (M1-358) is at most `local_lens.max_calls_per_question` calls (default 2),
in a stable order:

- **AskNews**, for the languages it supports, with `domain_url` set to `local_domains` where
  AskNews indexes them;
- **Exa** otherwise, with `native_terms` as the query and `includeDomains` set to
  `local_domains`.

This is not a word-for-word translation of the English question: the native terms are the
local names of places and institutions. Every call goes through `begin_call` and
`complete_call`. Lens runs are ordinary `research_runs` rows whose `provider_config` records
`lens: "local"`, the locales, languages, domains and the registry sha256.

**Collect-only (owner decision).** Lens runs are never added to `research_checkpoint`, so
the packet the submitted forecast is built from is exactly today's. That forecast is the
control. A `local_lens_checkpoint` journal event names the baseline and lens runs together.
None of the following ever changes baseline research or the forecast: a lens failure, a
sub-cap refusal or a timeout. Each is recorded.

**Sidecar** (`019_research_document_locales.sql`, M1-357). It holds:

- provider-reported `language`, `provider_story_country`, `native_title` and `english_title`;
- `translation_source` (`provider:asknews` or `none`; the translation model is NULL because
  AskNews does not disclose it);
- `provider_first_seen_at_utc` (AskNews `crawl_date`);
- `publisher_origin`;
- `provider_labels_json`: AskNews's `reporting_voice` and `provocative`, attributed to AskNews.

Rows are append-only, written in the same transaction as their documents, and never invent a
missing value. **Packet hash 1.1.0** is added beside 1.0.0 and is not an edit to it. A packet
with no locale rows still hashes byte-identically under 1.0.0. A packet with locale rows
hashes the 1.0.0 payload plus its sorted locales. `load_packet`, `usable_packet` and
`without_future` carry locales through, so every reconstruction path in `submission_policy`
still reproduces the hash.

**Overlap** (`research/overlap.py`, M1-356). Each lens document gets exactly one class:

- `same_url`;
- `same_content`;
- `republication`: a near-duplicate English title of a baseline document, or a same-publisher wire copy;
- `novel`;
- `unassessed`: no English title to compare, which is the Exa Persian case until translation
  exists.

Near-duplicates among lens documents form syndication clusters with one representative.
**Repetition across languages is one report, not corroboration.**

## 4. Evaluation design (Phase 2: M1-359, M5-805)

*Status (2026-10-17):* M1-359 shipped the shadow arm, disabled (D54): `retrieval.local_lens.shadow`, the `shadow_forecasts` table (020) and `prompts/forecaster-tournament-lens.md`. Persian lens documents are filtered out before the shadow's model sees them, because the relevance filter is English-only (M1-361). See `docs/M1-NOTES.md` § M1-359.

- **Arms.** *Control:* the forecast the worker submits today, from baseline research.
  *Experiment:* a shadow forecast generated in the same worker pass, from baseline plus lens
  runs, at the same `now` cutoff, with the same model, model settings and config. It uses a
  lens-aware prompt that differs from the control prompt only in a section on local evidence.
  The shadow forecast is stored in its own append-only table, **never in `forecast_records`**,
  so approval, idempotency and submission cannot reach it.
- **Primary outcome.** The paired per-question difference in the M4-802 local score (binary
  Brier; multiclass Brier and log score for multiple choice). Report the mean difference with
  a bootstrap 95% interval and n.
- **Secondary outcomes.**
  - calibration bins per arm;
  - local-publisher documents gained per question;
  - overlap classes;
  - language and publisher diversity;
  - incremental cost (lens calls plus the shadow model call);
  - latency;
  - model tokens.
- **Pre-registered honesty rule.** The report states n and the interval and **makes no
  improvement claim** until the interval excludes zero on the lens-triggered population. At
  the paired-score spread typical of these questions, a 0.01 Brier difference needs on the
  order of 200 resolved, lens-triggered questions, which is months at MiniBench volume.
- **Prospective only.** Provider search results are not snapshots of what was available at
  a past cutoff, so no historical backfill arm is built. Synthetic fixtures live only in
  tests and temporary ledgers. They are flagged as synthetic and cannot reach a live statistic.

## 5. Cost and operational impact

- **Disabled (the shipped default):** no calls, and no change to any packet, hash or forecast.
  Merging M1-358 still moves `config_sha256`, so it merges after project 33129 ends
  (2026-10-23) or between batches, followed by `tournament enable`.
- **Enabled:** applies only to triggered questions, at ≤ 2 calls each. AskNews is USD 0.025
  per call; Exa was USD 0.007 per call in the probe and reserves USD 0.05. That puts the
  ceiling at about USD 0.05–0.10 per triggered question, against roughly USD 0.20 per question
  today. Lens calls run after baseline research, so the AskNews sub-cap stops the lens and
  never the baseline.
- **Phase 2:** one shadow model call per triggered question. This will be measured, not
  assumed.

## 6. Known limitations

- AskNews's translation is provider-side and its model is undisclosed. AskNews returns no
  original full text, only the native title.
- AskNews cannot search Persian. Persian local sources come only through Exa, untranslated,
  until M1-361.
- Publisher origin is only as good as the curated domain lists. Anything outside them is
  `unclassified`, not guessed.
- Overlap detection is title-level, not claim-level. A new claim and an old claim restated in
  new words cannot yet be told apart (M1-360).
- The relevance filter reads English terms only (M1-361).
- `provider_first_seen_at_utc` is when AskNews first saw an article, not when we did.
  `retrieved_at_utc` is ours.
- The paired sample will be too small to support any claim for months.

## 7. Toward political information ecosystems (M6-901, deferred)

Nothing from section 9 of the request is built: no actor registry, graph store, faction
classifier or regime dashboard. Three Phase 1 choices keep that work possible later:

- descriptive metadata lives in **append-only sidecars keyed to `document_id`**, so an
  actor, faction or propagation layer can attach to evidence without touching hashed records;
- **publication time, provider first-seen time and our retrieval time are kept apart**,
  which propagation analysis needs;
- **provider labels are attributed to the provider**, and publisher origin comes from a
  versioned registry, so later representativeness, influence and diagnostic-value measures
  can tell what a source *is* from what someone *said about* it.
