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

Tests: `tests/unit/test_dedup_freshness.py` (56 cases, after review rounds 1–4). Full gate green —
`pytest` 427 passed, `ruff check`, `ruff format --check`, `mypy --strict src` all clean.

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
  matched case-insensitively against the percent-decoded key; **tracking-key removal is the only
  query transform** — every non-tracking parameter, and every empty segment / leading-trailing
  separator, is preserved byte-for-byte in its original position (the empty-segment preservation was
  a round-1 review fix; see below).

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

- **Dedup mirrors the ledger UNIQUE exactly.** `deduplicate` collapses documents sharing
  `(retrieval_run_id, canonical_url, content_sha256)` — **the ledger's UNIQUE, run id included**. It
  collapses only true intra-run duplicates (preventing a constraint violation) and **never collapses
  across runs**: two providers (two runs) that both surface one article are two legitimate ledger
  rows, and the run id is part of the attribution, so cross-run/cross-provider provenance is
  preserved *by construction*. (The initial cut keyed on `(canonical_url, content_sha256)` alone and
  collapsed across runs, losing exactly that attribution — corrected in round 1; see below.) On an
  intra-run collision the survivor is the minimum over a **total** order — stronger provenance
  (`direct_api` > `llm_reported`, a defensive tiebreak since intra-run provenance is uniform today
  but unenforced), then earliest `retrieved_at_utc`, then a canonical JSON dump of
  `model_dump(mode="json")` as a total, replay-stable final tiebreak. That makes the survivor
  independent of
  input order and replay-stable (round 1 fix). First-seen order of survivors is preserved.
  `DedupResult.collapsed_count` is exposed so a future writer can record an auditable dedup counter,
  in the spirit of `ResearchRun.posts_dropped_no_url`.

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
- **A presentation-layer "one card per artifact across providers" view is not built here.** Dedup
  mirrors the per-run ledger constraint; a cross-provider forecaster view would have to *retain*
  every contributing run/provenance rather than drop them, and belongs to forecast assembly, not
  this dedup. Building it now would be speculative scope creep of the kind the M1-301 retrospective
  warns against.
- **Host allow/deny policy (loopback, private ranges, homograph adjudication beyond IDNA) is not
  decided here.** `model.py` already states shape-validation holds no network code; canonicalization
  inherits that boundary — whether a *reachable* host is an *appropriate* one belongs to whatever
  fetches a URL.

## Cross-model review round 1 (2026-07-23)

GPT returned **DO-NOT-APPROVE** with three findings, all verified accurate and all fixed on this
branch. The primary risk I flagged for the reviewer — whether any host survives
`idna.encode(uts46=True)` in `canonicalize_url` but is then rejected by the schema's strict
`idna.encode` re-validation — was **cleared**: GPT tested 508k single-code-point label variants and
found no failing host, so the canonicalization design stands.

- **F1 (P1) — dedup collapsed across runs, losing attribution.** `dedup_key` had been
  `(canonical_url, content_sha256)`, dropping `retrieval_run_id`, so two providers' reports of one
  article collapsed to one survivor — but the ledger's `UNIQUE(retrieval_run_id, …)` deliberately
  keeps both, one per run. This erased which run found the evidence (an attribution loss the ledger
  exists to prevent) and was input-order-dependent. Root cause: the wrong reading that "duplicate
  reports collapse" meant *cross-provider* collapse; it means preventing a *single run* from storing
  the same artifact twice. **Fix:** `retrieval_run_id` is now in the key; dedup mirrors the ledger
  UNIQUE exactly and never crosses runs. Guarded by
  `test_same_artifact_from_different_runs_is_not_collapsed`.
- **F2 (P2) — survivor selection was order-dependent.** `_prefer`'s final branch kept the
  first-seen document, contradicting the docstring's "total and order-independent" claim: two
  duplicates equal in provenance and `retrieved_at_utc` but differing in a non-key field (e.g.
  `title`) chose different survivors on reversed input. **Fix:** selection is now a min over a total
  order `(_PROVENANCE_RANK, retrieved_at_utc, <tiebreak>)`; the tiebreak is a full serialization —
  see rounds 2 and 3 for how it became a canonical JSON dump. Guarded by
  `test_exact_tie_survivor_is_order_independent`.
- **F3 (P2) — empty query segments were silently deleted.** `_strip_tracking` dropped empty
  `split("&")` entries and leading/trailing separators (`?x=1&&y=2` → `?x=1&y=2`), a second,
  undocumented lossy transform an endpoint that signs/dispatches on the raw query can detect.
  **Fix:** empties and separators are preserved; the only query transform is tracking-key removal.
  Guarded by `test_empty_query_segments_are_preserved`.

## Cross-model review round 2 (2026-07-23)

Re-review of the round-1 fixes: **F1 and F3 confirmed resolved**, **F2 not resolved**, one new P2.

- **F2 follow-on (P2) — the serialization tiebreak raised on lone surrogates.** The round-1 fix used
  `model_dump_json()` as the total-order tiebreak, but a schema-valid text field may hold an unpaired
  surrogate (`"\ud800"`, e.g. from provider JSON), and JSON serialization UTF-8-encodes, so
  `model_dump_json()` **raises** `PydanticSerializationError` on it — the "total order" was not total,
  and the uncaught exception **echoed the offending character** (an error-hygiene breach). Verified in
  repo: `validate_document` accepts such a title; `model_dump_json()` raises; `repr(model_dump())`
  does not. **Fix:** the tiebreak is now `repr(document.model_dump())` — Python-mode dump returns
  objects and never encodes (so it cannot raise), `repr` renders surrogates escaped and is
  deterministic, and it is used only as an internal sort key (never in a message), so nothing
  input-derived can leak. After this, `dedup.py` raises nothing input-derived at all. Guarded by
  `test_dedup_tiebreak_is_surrogate_safe`. Rejecting surrogates at schema validation was rejected as
  the fix: it would touch the frozen `model.py` for a dedup-local problem, and the schema
  intentionally accepts arbitrary text — the primitive is what must be robust to it.

## Cross-model review round 3 (2026-07-23)

Re-review of the round-2 fix: **surrogate crash confirmed resolved**, but F2's total-order/
replay-stability was **still not resolved**, with a new **P1**.

- **F2 follow-on (P1) — the `repr` tiebreak was not replay-stable.** `repr(model_dump())` keys on the
  **in-memory** Python form, which carries `datetime.fold`; the **persisted** form (JSON/`isoformat`)
  drops it. Two same-key documents with equal UTC `retrieved_at_utc` but differing `fold` compare
  equal (tying that component) yet produce different `repr` — so the survivor chosen in memory could
  differ from the one a store→replay round-trip would pick, flipping the result. For a
  replay-attribution ledger that is the class of bug that matters. Verified in repo: `_to_utc`
  (`astimezone(UTC)`) does not normalize `fold`, so `fold=1` survives validation (reachable via
  `retrieved_at_utc` and `published_at_utc`), and `repr(model_dump())` differs on it while
  `model_dump(mode="json")` does not. **Fix:** the tiebreak now keys on the **canonical persisted
  form** — `json.dumps(document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True,
  separators=(",", ":"))`. `mode="json"` renders exactly the stored form (fold-invariant), so the
  survivor is the same before and after persistence; `ensure_ascii=True` keeps it surrogate-safe (it
  escapes rather than UTF-8-encodes, so it does not raise); `sort_keys`/`separators` make it
  canonical. Guarded by `test_dedup_survivor_is_replay_stable`.
- **P3 nit — stale test counts.** The "Delivered" line still cited the round-0 numbers (47 cases /
  418 total); corrected to the current 56 module cases / 427 total.

## Cross-model review round 4 (2026-07-23)

Re-review of the round-3 fix: **replay-stability (fold) confirmed resolved.** One new **P2**, which
was **evaluated and its suggested fix declined with evidence** (owner decision) — the only accepted
part is a test-strengthening.

- **P2 (finding accepted, fix declined) — the tiebreak is not injective over in-memory strings.** An
  astral scalar (`chr(0x1F600)`, len 1) and its UTF-16 surrogate-pair spelling
  (`chr(0xD83D)+chr(0xDE00)`, len 2) are distinct Python strings, both schema-valid, but produce the
  **same** `ensure_ascii=True` canonical JSON, so they collide and the *in-memory* returned survivor
  is input-order-dependent. GPT proposed an injective key (`ensure_ascii=False` +
  `.encode("utf-8", "surrogatepass")`). **Declined**, because it is *replay-unstable*: verified
  in-repo that (a) after a real `json.dumps`→`json.loads` round-trip — what the ledger does —
  **both documents collapse to the one scalar U+1F600**, i.e. they are the same persisted row; and
  (b) `gpt_key(pair)` differs before vs after that round-trip, reopening exactly the round-3 bug
  (key ≠ persisted form). The tiebreak deliberately orders **persisted forms**, not in-memory string
  identity: two documents the ledger cannot tell apart are keyed equal, and the surviving document's
  *persisted* form is input-order-invariant (confirmed both ways). A separated surrogate pair also
  cannot arise from real provider JSON — `json.loads` always recombines `😀` into U+1F600
  — so the input is schema-valid but not reachable through the retrieval path. Docstring updated to
  state the "total over persisted forms, not in-memory identity" contract explicitly.
- **Accepted (test hardening):** GPT correctly noted the round-3 replay test used a bare
  `model_dump(mode="json")` handoff, which skips JSON text encoding. The `_replayed` helper now
  crosses a real `json.dumps`→`json.loads`→`validate_document` boundary, and a new test
  (`test_json_equivalent_titles_collapse_and_persist_identically`) pins that the astral/surrogate
  pair collapse and persist identically in either order.
