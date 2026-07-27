# M1-303 — Implement Exa fallback — implementation notes

Running record of M1-303 decisions and deviations, in the spirit of `docs/M1-301-NOTES.md`.
**Merges back into `docs/M1-NOTES.md`** with the rest of the retrieval epic.

Acceptance: **use Exa only when AskNews fails or when official-source/web retrieval is required;
the configured fallback records why it ran and preserves citations; no silent provider switching.**

## Delivered

- **`src/whiskeyjack_bot/research/exa.py`** — the adapter and the fallback policy:
  `FallbackReason` (`Literal`), `FallbackDecision` (frozen), `ExaRetrieval` (frozen),
  `ExaFallbackError`, `decide_fallback(...)`, `build_exa_client(config)`, `retrieve_web(...)`.
- **`src/whiskeyjack_bot/research/transport.py`** — `apply_connection_retries`, moved out of
  `asknews.py` unchanged so both adapters share the one httpx/httpcore workaround rather than a
  copy of it. `asknews.py` imports it; no behaviour change, and its retry tests pass untouched.
- **`tests/unit/test_exa.py`** (97 cases) and **`tests/property/test_exa_properties.py`** (14
  properties). Full gate green: 750 passed + 1 xfail, `ruff check`, `ruff format --check`,
  `mypy --strict src` all clean.

**No new dependency, no migration, no schema change, no config change.** `httpx` was already a
declared direct dependency (M1-302), `RetrievalProviderConfig` already carries the `exa` provider
and `EXA_API_KEY`, and `RetrievalProvider`, migration 002's trigger vocabulary and
`AppConfig.secret_env_var_names()` already knew about both. **The dependency slot M1-303 claimed in
`docs/TRACKS.md` is therefore released unused** — see the registry.

## Deliberate choices

- **Direct `httpx` rather than `forecasting_tools.ExaSearcher` or `exa-py`.** D03 says use the
  package where it supports the workflow; here it does not, for the reasons M1-302 bypassed
  `AskNewsSearcher`, and more of them. `ExaSearcher` reads `EXA_API_KEY` through `os.getenv` +
  `assert` rather than the configured `api_key_env`; it is async/aiohttp in a synchronous pipeline;
  it discards the raw response body (which M1-306 must persist to replay) and the `costDollars`
  block; and it parses `publishedDate` as `fromisoformat(value.strip("Z"))`, yielding a **naive**
  datetime this project's schema rejects outright. `exa-py` 2.16.2 would add `openai`, `requests`
  and `python-dotenv` transitively for a single POST and likewise hides the raw body.

- **The API contract was verified, not assumed** — `https://api.exa.ai/openapi.json`, checked
  2026-07-27, recorded with that date in the module docstring. Two things the contract changed
  since the vendored wrapper was written: `livecrawl` is **deprecated** in favour of
  `maxAgeHours`, and the response carries `costDollars.total`, which the API documents as an
  *estimate*, "not an invoice record". We send `maxAgeHours: 24` and record the estimate on that
  understanding.

- **"No silent provider switching" is enforced by the signature, not by discipline.**
  `retrieve_web` **requires** a non-empty `fallback_reasons`; an empty sequence or a value outside
  the vocabulary raises `ExaFallbackError` before any billable call. `build_exa_client` refuses
  when `retrieval.fallback.provider` is not `exa` — running this adapter against a differently
  configured fallback *is* the silent switch — and that refusal is checked **before** the
  credential lookup. One INFO log line records the engagement, from constants and the integer
  question id only.

- **Every trigger is recorded, not a winner.** `decide_fallback` returns all of
  `primary_provider_failed`, `primary_returned_no_documents`, `official_source_required` that
  hold. "AskNews raised" and "AskNews returned nothing" are different facts about a run and the
  ledger reads them differently (`error_summary` distinguishes the two); collapsing them to a
  priority order would discard attribution for no benefit. The tuple is deduplicated and
  normalized to vocabulary order because it is persisted — the stored list must be a function of
  the triggers, not of the caller's bookkeeping.

- **The reason is persisted in `provider_config["fallback_reasons"]`** (owner decision), which maps
  to `research_runs.provider_config_json`, so the ledger alone answers "why was a second provider
  engaged" with no migration and no change to the M1-301 schema that three other branches depend
  on. Alternative considered and declined: a first-class `fallback_reason` column, which would have
  meant claiming migration 004 and touching the shared schema for a field only this adapter writes.

- **Content-hash source rule (pinned): `text` → title → `""`.** Deliberately **not** highlights,
  and the request asks for `contents.text` rather than `contents.highlights` for the same reason:
  Exa generates highlights per query, so one page retrieved by two queries would hash differently
  and defeat both the intra-run collapse and M1-305's dedup. `maxCharacters` is pinned at 4000
  (ceiling is 10000) so document identity does not depend on how much of a page Exa extracted that
  day, and `maxAgeHours: 24` prefers cached content over a live crawl for the same reason — and
  because a live crawl costs more.

- **`canonical_url` is derived, not copied.** `canonical.py`'s contract says adapters call
  `canonicalize_url` once per document and that copying `original_url` was only the pre-M1-305
  placeholder. M1-305 has since merged, so this adapter canonicalizes; `original_url` keeps the
  as-retrieved URL, so nothing is lost. (`asknews.py` still copies — it predates the merge. Worth a
  follow-up, deliberately not done here: changing it changes existing AskNews `canonical_url`s.)

- **`publishedDate` gets one uniform rule.** Offset-aware values convert to UTC; a date-only value
  (the documented `YYYY-MM-DD`) or an offset-less timestamp is pinned to **midnight UTC of the
  stated date**; anything unparseable yields `None`. The pinning carries up to roughly a day of
  error in either direction, which is recorded in the docstring rather than hidden; the
  alternative — discarding the only date the provider gave — makes every document undated, and
  M1-305's owner decision treats undated as stale. A bad date never costs the citation.

- **`source_type` is `official` only when the search was constrained to a caller-supplied
  `include_domains` allowlist**, otherwise `web`. Tagging on the strength of the *reason* would let
  `official_source_required` label whatever the open web returned — an unearned attribution claim
  (ambiguity rule 4: stricter reading).

- **`publisher` stays `None`.** Exa returns no publisher field, and deriving one from the hostname
  would put a value in the ledger that no provider asserted. `summary` likewise stays `None` (it is
  reserved for the pipeline's own summarization; Exa's text is provider material and goes in
  `snippet`, bounded at 500 characters — the full text stays in the raw response for M1-306).

- **Failure is data, not an exception** (the M1-302 rule). A run makes up to
  `max_queries_per_question` billable calls, so a mid-run failure stops early, sets
  `provider_failed`, fills `error_summary` with the same constants-only wording AskNews uses, and
  returns everything already retrieved. A 200 whose body is not a mapping, or carries no `results`
  list, is treated the same way — it is a contract breach, not an empty answer, and continuing
  would pay for more calls against a provider that is not answering in the documented shape. The
  body is recorded first either way, because the call was billed. Provider exceptions are
  **discarded, never inspected**: an httpx error can quote the request, and the request carries the
  API key in a header.

- **Cost is recorded but qualified.** `costDollars.total` is summed across calls, skipping values
  that are `bool` (an `int` subclass — `true` would otherwise be recorded as one dollar),
  non-numeric, negative or non-finite. `NaN`/`Infinity` are reachable in practice: they are not
  valid JSON but `json.loads` accepts them, and a stored non-finite cost would validate here and
  fail at ledger-write time, after the money was spent.

## Notes for downstream items

- **M1-306** owns persistence and replay: `raw_responses` are in memory only, and
  `run.raw_response_path` / every `raw_artifact_path` stay `None`. It also owns the wiring —
  `decide_fallback(primary_failed=..., primary_documents=..., official_source_required=...)` then
  `retrieve_web(..., fallback_reasons=decision.reasons)`.
- **M1-504** reads `error_summary`, and this adapter keeps its meaning identical to the AskNews
  adapter's: set when the run failed or returned nothing, never for routine drops or intra-run
  duplicate collapsing (those ride on `ExaRetrieval`).
- The known **`content_sha256` lone-surrogate defect** (CLAUDE.md gotcha, open owner decision)
  reaches this adapter through Exa's `text`. It degrades to a counted drop rather than a crash,
  because `UnicodeEncodeError` is a `ValueError` and the per-result catch includes it. The property
  suite exercises the path (~45 of 400 generated results hit it).
