# M1-305 — Deduplicate and freshness-tag evidence — implementation notes

Running record of M1-305 decisions and deviations, in the spirit of `docs/M1-301-NOTES.md`.
**Merges back into `docs/M1-NOTES.md`** with the rest of the retrieval epic (see the merge-back
trigger at the top of `docs/M1-301-NOTES.md`).

M1-305 is the direct dependent of M1-301, which deferred three things to it (URL canonicalization,
duplicate collapsing, freshness-tagging) and made it the owner of URL-validation *policy* beyond the
minimal syntactic gate. Acceptance: **duplicate reports collapse without losing provenance; stale
evidence is flagged deterministically.**

## Delivered

Three pure-primitive modules under `src/whiskeyjack_bot/research/`, mirroring how
`hashing.py::content_sha256` is a standalone primitive that adapters call:

- **`canonical.py`** — `canonicalize_url(url) -> str` and `CanonicalizationError`. Derives the
  `canonical_url` that `UNIQUE(retrieval_run_id, canonical_url, content_sha256)` keys dedup on.
- **`freshness.py`** — `FreshnessState`/`FreshnessReason` (`Literal`), `FreshnessVerdict` (frozen),
  `freshness_cutoff(reference, days)`, `assess_freshness(published, updated, cutoff)`,
  `assess_document(doc, cutoff)`.
- **`dedup.py`** — `dedup_key(doc)`, `deduplicate(docs) -> DedupResult`, `DedupResult` (frozen).

Tests: `tests/unit/test_dedup_freshness.py` (47 cases). Full gate green — `pytest` 418 passed,
`ruff check`, `ruff format --check`, `mypy --strict src` all clean.

## Deliberate choices

- **No schema change, no migration, no new dependency.** Freshness is derived at forecast time from
  timestamps the schema already carries (there is no freshness column on `research_documents`), so
  it is recomputed on replay rather than persisted. `canonical_url`/`content_sha256` are minted by
  adapters using these functions. This is the lowest-drift reading of "nothing here touches the DB",
  and it keeps the six-round-hardened `model.py` validator untouched. `idna` and `ipaddress` are
  already declared (M1-301); nothing new enters `pyproject.toml`/`uv.lock`.

- **The URL syntactic gate is reused, not re-implemented.** `canonicalize_url` runs
  `model._require_http_url` before normalizing, so there is exactly one definition of "is this a URL
  at all", and a second hand-rolled copy cannot drift from it. This is the direct application of the
  M1-301 retrospective's banked lesson (delegate to the authority; a hand-rolled second copy is
  where the next regression comes from). Host classification inside canonicalization branches
  **exactly as `_require_resolvable_hostname` branches** — `ipaddress` first, `idna` only for what
  is not an IP literal — because the round-6 finding was precisely that IP literals must never reach
  `idna`. Two agreement tests pin this: everything `validate_document` accepts, `canonicalize_url`
  accepts (IDN/IPv6/IPv4 families), and everything it rejects, `canonicalize_url` rejects as
  `CanonicalizationError` (Cf/format-char, ZWNJ-out-of-context, space, malformed-IP families). A
  third test asserts canonicalize output re-validates as a `canonical_url` and is idempotent.

- **Canonicalization is conservative where the round history says to be.** It lowercases scheme and
  host, folds IDN to its A-label (`idna.encode(uts46=True)`) and compresses/re-brackets IPv6, drops
  the default port, drops the fragment, drops userinfo, uppercases percent-octet hex, and normalizes
  an empty path to `/`. It does **not** decode percent-octets (decoding an unreserved octet is where
  subtle equivalence bugs live) and does **not** reorder or re-encode query parameters — order can be
  load-bearing, and re-encoding could alter a value that is part of the resource identity.

- **Tracking-param stripping is the one lossy step, and it is a closed, documented allowlist**
  (owner decision, 2026-07-22). Dedup keys on `canonical_url`, so two provider reports of one
  article differing only by a `utm_*`/`fbclid`/… tag would never collapse unless those tags are
  removed — which is the whole point of the item. `_TRACKING_PARAMS` is a module-level `frozenset`
  matched case-insensitively against the percent-decoded key; every non-tracking parameter is
  preserved byte-for-byte in its original position.

- **Userinfo is dropped from the canonical URL** — it is not part of the resource identity for
  dedup, and keeping it would write credentials into the stored dedup key. `original_url` still
  preserves the as-retrieved URL byte-for-byte, so this is not an attribution loss; it is also a
  secret-hygiene win.

- **Freshness is deterministic by construction.** `assess_freshness` is a pure function of the
  timestamps the caller supplies; **no module here reads `datetime.now()`**. The caller derives the
  window with `freshness_cutoff(reference, days)` from a reference time (e.g. the run's
  `started_at_utc` or the question snapshot time) and `retrieval.freshness_days_default` (or a
  per-question override). Effective date is `updated_at` when present, else `published_at`;
  `retrieved_at` is deliberately not used — it records when *we* fetched the document, not how old
  its content is, and a fresh fetch of stale content is still stale. The boundary is **inclusive at
  the cutoff**: a document dated exactly at the cutoff is `fresh` (the window is "on or after"). This
  is documented because "outside the window" is ambiguous at the boundary; the inclusive reading is
  the natural one for a "last N days" window and is pinned by a test.

- **An undated document is `stale` / `undatable`** (owner decision, 2026-07-22; stricter reading per
  CLAUDE.md ambiguity rule 4). It cannot be shown to fall within the window, so it is flagged rather
  than allowed to pass unchecked where M1-504 could never catch it. The `undatable` reason is kept
  distinct from `before_cutoff` so a consumer can tell "we checked and it is old" from "we could not
  check".

- **M1-305 only tags; it does not gate.** It never reads `forecast.fail_on_stale_research` /
  `flag_on_stale_research` — that fail-vs-flag policy is **M1-504** (which depends on this item).
  Splitting them keeps the epic boundary: tagging is evidence about the document, gating is policy
  about the forecast.

- **Dedup preserves the stronger provenance.** `deduplicate` collapses documents sharing
  `(canonical_url, content_sha256)` — the ledger's UNIQUE minus the run id. The key is the artifact
  identity, so the scope of a collapse is the input the caller passes: one run's documents for
  strict per-run semantics, or a question's whole set to dedup across providers (which is where
  "without losing provenance" bites — within one run, provenance is uniform). On collision the
  survivor carries the **stronger claim** (`direct_api` > `llm_reported`): a verified retrieval is
  never silently downgraded to a reported one, nor a reported one upgraded to verified. Ties on equal
  provenance break to the earliest `retrieved_at_utc`, then first-seen — a total, order-independent
  rule, so the result is stable. First-seen order is preserved. `DedupResult.collapsed_count` is
  exposed so a future writer can record an auditable dedup counter, in the spirit of
  `ResearchRun.posts_dropped_no_url`.

## Error hygiene

`CanonicalizationError` is module-owned and sanitized: a URL is row content, so its message is the
constant `_BAD_URL` and never echoes the input. Every raise uses `from None` (the `idna`/`urlsplit`
exceptions embed the offending value, and a chained `__cause__` would reprint it through a
traceback). A test plants a secret in a rejected URL and asserts it is absent from the message and
that `__cause__ is None`. `freshness.py`/`dedup.py` raise nothing input-derived — they operate on
already-validated `ResearchDocument`s and pure timestamps.

## Deferred / boundaries (do not read the absence as an omission)

- **Wiring into adapters is the adapters' job.** No adapter exists yet to call these primitives
  (M1-302/M1-303 are the consumers); M1-305 ships the primitives + tests, exactly as `hashing.py`
  did. The consumer contract is documented in each module docstring.
- **The stale/insufficient-research gate is M1-504** (`fail_on_stale_research` /
  `flag_on_stale_research`), which depends on this item.
- **Host allow/deny policy (loopback, private ranges, homograph adjudication beyond IDNA) is not
  decided here.** `model.py` already states shape-validation holds no network code; canonicalization
  inherits that boundary — whether a *reachable* host is an *appropriate* one belongs to whatever
  fetches a URL.
