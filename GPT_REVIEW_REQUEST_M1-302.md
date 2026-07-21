# Cross-model review request — whiskeyjack-bot M1-302

You are a rigorous senior reviewer performing an independent cross-model review of
code authored by another AI model (Claude). Apply the **stricter reading**: when a
line could be read as either correct or subtly wrong, assume the wrong reading and
prove it can't happen from the diff. Do **not** rubber-stamp. If you approve, justify
why each risk area below is actually safe; if you don't, list blocking findings.

## Project context

`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary
product is an **attribution ledger**: an immutable, replayable SQLite record of every
forecast, its evidence, approvals, submission attempts, resolutions and scores. Python
3.11, `src/` layout, offline-first (tests run with sockets disabled), toolchain gates
are `pytest`, `ruff check`, `ruff format --check`, `mypy --strict src`.

This is **M1-302**, the first retrieval provider and the first producer of
`ResearchDocument`s. It builds directly on **M1-301** (the research-document schema),
which you reviewed across six rounds and approved. It is consumed by M1-305
(dedup/freshness), M1-306 (replayable retrieval runs) and eventually M1-402.

## Authoritative spec

From `docs/backlog/backlog.csv` (M1-302 row):

> **Implement AskNews adapter.** Retrieve current and historical news while retaining
> article-level provenance.
> **Acceptance:** "Mocked call returns normalized documents; missing credentials fail
> before a paid call."
> Depends on M1-301. Reference: https://docs.asknews.app/

Decision **D17**: AskNews is the primary retrieval provider.

Standing conventions this branch must honor:

- **Error hygiene**: messages never echo stored or retrieved values, and sanitizing
  raises use `from None` so a value cannot surface through exception text or a rendered
  traceback. This module handles arbitrary provider text and an API key in the same call
  frame, so it is the most likely place for that invariant to break.
- **Never print or persist secrets**; env-var *names* only in diagnostics.
- **Ambiguity rule 4**: where an acceptance criterion is ambiguous, implement the
  stricter reading and note it.
- **Append-only ledger**; no reachable submission path (unaffected here, but no code may
  weaken it).

## Deliberate choices / out of scope (challenge the rationale, but these are not omissions)

- **`forecasting_tools.AskNewsSearcher` was rejected.** The pinned SDK ships a wrapper,
  but `get_formatted_news()` returns a single pre-formatted markdown string via
  `_format_articles()`, discarding the article-level provenance this item exists to
  preserve. It also reads credentials from the environment itself (bypassing the config
  contract), hardcodes a 12-second `asyncio.sleep`, and keeps an on-disk cache. We call
  `asknews_sdk` directly. **Challenge this if you think the wrapper could have been
  adapted.**
- **API-key auth only.** The committed config declares a single `api_key_env`. AskNews
  also supports an OAuth `client_id`/`client_secret` pair; supporting both was explicitly
  scoped out by the owner, who confirmed the account uses an API key.
- **No disk persistence.** `raw_responses` is returned in memory; `run.raw_response_path`
  and every `raw_artifact_path` stay `None`. File layout and the replay contract are
  M1-306's, and pre-empting them here would fix decisions that item owns.
- **URL validation untouched.** `canonical_url == original_url`. M1-305 owns URL policy
  and canonicalization. Rounds 4-6 of the M1-301 review were *entirely* regressions from
  extending that validator, so this adapter adds none of its own.
- **`reliability_tag` left `None`.** The vocabulary (`official_primary`, `verified_org`,
  `journalist`, `unverified_social`) is social-source oriented; tagging news publishers is
  M1-305/M1-308's call.
- **Queries are supplied by the caller.** No backlog item covers query generation, so
  deriving them from a question here would be scope creep.
- **`now` is injected** rather than read from the clock, so `started_at_utc` and every
  `retrieved_at_utc` are deterministic under test and under replay.

## Risk areas to pressure-test

1. **The credential boundary is the item's safety property.** The claim is that
   `AskNewsSDK(api_key=...)` performs no network I/O — verified against asknews==0.13.54
   on 2026-07-21 by reading `BaseAPIClient.__init__` and `security.APIKey`: it builds an
   `httpx.Client` and an auth object, and API-key mode skips the OAuth token round-trip
   entirely. **Verify this claim independently.** If construction *can* touch the network
   on any path (proxy env vars, `verify_ssl` cert loading, a lazy auth flow), the
   "fails before a paid call" criterion is not actually met.

   The falsifiers I could think of were tested rather than argued: the construction tests
   were re-run with `HTTPS_PROXY`/`HTTP_PROXY` pointing at an unroutable address and
   `SSL_CERT_FILE` set, and still pass under the three network guards. **Find a falsifier
   I did not think of** — that is more valuable here than re-confirming the ones I did.

2. **Content-hash source rule: `full_text` > `summary` > title.** This defines document
   identity. Known caveat, stated in the notes: AskNews's `summary` is LLM-generated and
   may not be byte-stable across calls, so a `full_text`-less article can hash differently
   on re-retrieval. Is that acceptable, or does it need a different identity rule? Does
   the empty-string fallback (`_hash_source` returning `""`) create a collision class
   where several contentless articles share one hash and get wrongly collapsed?

3. **Secret and PII hygiene under adversarial provider text.** Every raise in
   `research/asknews.py` is meant to be a constant. The SDK failure path re-raises
   `from None`. `provider_config` is meant to carry only call parameters. Only
   `Author.name` is read, never `Author.email`. **Try to find a path where an article
   field, a query string, or the API key reaches an exception message, `error_summary`,
   `provider_config`, or a log record.**

4. **The broad `except Exception` around `search_news`.** It converts every SDK failure
   into `AskNewsRetrievalError` with a constant message. Does that swallow something it
   shouldn't (e.g. `KeyboardInterrupt` is excluded, but what about programming errors in
   our own argument construction, which would be masked as a provider failure)?

5. **The per-article `except (ResearchSchemaError, AttributeError, TypeError, ValueError)`.**
   Dropping a bad article rather than failing the run is intended. Is the tuple too broad
   — could it hide a genuine bug in `_to_document` as a silent drop? Is it too narrow —
   what shape drift would escape it?

6. **Intra-run duplicate collapsing.** The claim is this is UNIQUE-constraint safety
   (`UNIQUE (retrieval_run_id, canonical_url, content_sha256)`), not deduplication, and
   that cross-run dedup remains M1-305's. Is the key correct? Does keeping the *first*
   occurrence lose anything the later one had (the current pass runs before the
   historical one)?

7. **`error_summary` as the home for drop/collapse counts.** `ResearchRun` has no typed
   counter; the alternative was a migration `003`. Is overloading `error_summary` — whose
   schema-level purpose is "set when the run failed or returned nothing" — a misuse that
   will confuse M1-306 or the validation gate M1-504?

8. **Config change blast radius.** `timeout_seconds` and `retries` were added to the
   *shared* `RetrievalProviderConfig`, which the Exa fallback also uses. Both have
   defaults, so existing configs still validate — confirm that, and confirm
   `config.example.yaml` and the config tests stayed in step.

9. **`cost_usd` left `None`.** AskNews reports `Usage.credits` (an integer credit count,
   not currency). The argument is that converting without a configured rate would put an
   unearned number in an attribution ledger. Is silently dropping the credit count from
   the *run* (it survives only inside `raw_responses`) the right call?

10. **Dependency declaration.** `asknews>=0.13,<0.14` was added as a direct dependency; it
    was previously only transitive via `forecasting-tools`, which bounds it
    `>=0.9.1,<0.14.0`. Confirm the bound cannot conflict and that `uv.lock` is in step.

## What changed

Branch `feat/m1-302-asknews-adapter`, one commit off `master` (`a0cbb67`).

| File | Change |
|---|---|
| `src/whiskeyjack_bot/research/asknews.py` | **new** — the adapter |
| `tests/unit/test_asknews.py` | **new** — 27 tests |
| `src/whiskeyjack_bot/config.py` | `RetrievalProviderConfig` gains `timeout_seconds`, `retries` |
| `config.example.yaml` | the two new knobs on `primary` and `fallback` |
| `src/whiskeyjack_bot/research/__init__.py` | exports |
| `pyproject.toml`, `uv.lock` | declare `asknews` |
| `tests/unit/test_dependency_pins.py` | `test_asknews_is_a_declared_dependency` |
| `docs/M1-301-NOTES.md` | M1-302 section (epic-wide notes file) |

Gates at the tip: `pytest` 329 passed, `ruff check` clean, `ruff format --check` clean,
`mypy --strict src` clean, `uv sync --locked` clean.

## How to get the diff

```bash
git fetch origin feat/m1-302-asknews-adapter
git diff master...origin/feat/m1-302-asknews-adapter
```

Review the full diff, not just the summary above.
