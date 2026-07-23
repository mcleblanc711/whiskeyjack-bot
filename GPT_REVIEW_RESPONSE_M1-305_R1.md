# M1-305 — response to review round 1

Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-1 verdict: **DO-NOT-APPROVE**, three
findings. **All three accepted and fixed.** Full gate green after the fixes: `pytest` 424 passed,
`ruff check`, `ruff format --check`, `mypy --strict src` all clean.

The primary risk I asked the reviewer to falsify — a host that survives `idna.encode(uts46=True)` in
`canonicalize_url` but is then rejected by the schema's strict `idna.encode` re-validation — was
**cleared**: 508k single-code-point label variants tested, no failing host. The canonicalization
design stands unchanged.

---

## F1 (P1) — dedup collapsed across runs, losing attribution — `research/dedup.py`

**Accepted.** `dedup_key` was `(canonical_url, content_sha256)`, dropping `retrieval_run_id`, so two
providers (two runs) that both surfaced one article collapsed to a single survivor with
`collapsed_count=1` — but the ledger's `UNIQUE(retrieval_run_id, canonical_url, content_sha256)`
deliberately keeps **both** rows, one per run. Collapsing them erased which run found the evidence
(an attribution loss — the exact thing the ledger exists to prevent) and, as the reviewer noted, the
surviving run depended on input order.

Root cause was a wrong reading in the original design: "duplicate reports collapse" was taken to mean
*cross-provider* collapse. It does not — it means preventing a **single run** from storing the same
artifact twice. Cross-run/cross-provider duplication is legitimate, distinct evidence the ledger
keeps by design, and the acceptance criterion itself frames dedup as *feeding* the run-scoped UNIQUE.

**Fix:** `dedup_key` is now `(retrieval_run_id, canonical_url, content_sha256)` — the ledger UNIQUE
exactly. `deduplicate` collapses only true intra-run duplicates and never crosses runs, so cross-run
provenance is preserved *by construction*. Module docstring rewritten to state this; the old
"across a question's runs it lets two providers collapse" narrative is deleted.

I took the "include the run id in the key" remedy over "separate presentation dedup from
persistence." The presentation-layer, cross-provider "one card per artifact" view the reviewer
alludes to is real, but it must *retain* every contributing run/provenance rather than drop them, and
belongs to forecast assembly (≈ M1-504), not this primitive. Building it now would be speculative
scope creep; it is documented as explicitly deferred in `docs/M1-305-NOTES.md`.

Guard: `test_same_artifact_from_different_runs_is_not_collapsed` — identical `canonical_url` +
`content_sha256`, different `retrieval_run_id`, asserts both survive and `collapsed_count == 0`.

## F2 (P2) — survivor selection was order-dependent — `research/dedup.py`

**Accepted.** `_prefer`'s final branch returned the first-seen document, which is input-order
dependent, while the docstring claimed "total and order-independent." Two duplicates equal in
provenance and `retrieved_at_utc` but differing in a non-key field (e.g. `title`) chose different
survivors when input order was reversed — not replay-stable.

**Fix:** selection is now a min over a **total** order,
`_sort_key = (_PROVENANCE_RANK[provenance], retrieved_at_utc, model_dump_json())`. The full canonical
serialization is a deterministic, arbitrary-but-total final tiebreak — exactly the reviewer's
suggested remedy — so the order is total (ties only when documents are byte-identical) and
independent of iteration order. `_prefer` is now `candidate if _sort_key(candidate) <
_sort_key(current) else current`.

The human-meaningful components stay first (stronger provenance, then earliest retrieval), so the
existing `test_collapse_keeps_the_stronger_provenance` and
`test_equal_provenance_ties_break_to_earliest_retrieval` remain meaningful and still pass.

Guard: `test_exact_tie_survivor_is_order_independent` — same key/provenance/timestamp, different
`title`, asserts `deduplicate([a, b])` and `deduplicate([b, a])` pick the same survivor.

## F3 (P2) — empty query segments silently deleted — `research/canonical.py`

**Accepted.** `_strip_tracking` did `if not pair: continue`, deleting empty `split("&")` entries
(`?x=1&&y=2` → `?x=1&y=2`) and leading/trailing separators — a second, undocumented lossy transform,
distinguishable by an endpoint that signs or dispatches on the raw query.

**Fix:** the empty-segment skip is removed. The loop now drops **only** segments whose percent-decoded
key is in `_TRACKING_PARAMS`; every other segment is kept verbatim, including empty strings and
leading/trailing separators. Tracking-key removal is now the sole query transform (percent-hex is
still uppercased over the rejoined query). Docstring updated to say so.

Guard: `test_empty_query_segments_are_preserved` — `?x=1&&y=2`, `?a=1&`, `?&a=1` unchanged;
`?utm_source=x&a=1` → `?a=1`.
