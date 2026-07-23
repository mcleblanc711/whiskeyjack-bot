# GPT cross-model review — M1-305 round 2 (re-review of round-1 fixes)

## ROLE

You are the same adversarial reviewer, now doing a **focused re-review**. In round 1 you returned
DO-NOT-APPROVE with three findings and cleared the rest of the change (including the primary risk —
the `idna.encode(uts46=True)` canonicalize output vs strict `idna.encode` schema re-validation — via
508k single-code-point label variants, no failing host). All three findings were **accepted and
fixed**. Your job now is narrow: **verify each fix is correct, complete, and did not introduce a
regression**, and confirm nothing in the fix delta reopens a property you previously cleared. Do not
re-litigate the cleared base unless the fix delta touches it. Prefer one confirmed, reproducible
finding over speculation.

## CONTEXT

Same project and constraints as round 1: whiskeyjack-bot, immutable replayable attribution ledger;
pydantic v2 strict, `mypy --strict`, ruff (line length 100); error messages never echo content;
sanitizing raises use `from None`. M1-305 makes NO schema change, NO migration, NO new dependency.
The full fix delta (round-1 request commit → now) is appended, including the code, the updated
`docs/M1-305-NOTES.md`, and `GPT_REVIEW_RESPONSE_M1-305_R1.md` (my per-finding claims). Gate after
the fixes: `pytest` 424 passed; `ruff check`, `ruff format --check`, `mypy --strict src` all clean.

## WHAT CHANGED (the three fixes)

- **F1 (P1) — dedup no longer collapses across runs.** `dedup_key` is now
  `(retrieval_run_id, canonical_url, content_sha256)` — the ledger's `UNIQUE` exactly. `deduplicate`
  collapses only intra-run duplicates and never crosses runs, so cross-run/provider provenance is
  preserved by construction. A cross-provider "one card per artifact" presentation view is explicitly
  deferred (would have to retain every contributing run, belongs to forecast assembly).
- **F2 (P2) — survivor selection is now order-independent.** `_prefer` is a min over a total order
  `_sort_key = (_PROVENANCE_RANK[provenance], retrieved_at_utc, model_dump_json())`; the full
  serialization is the arbitrary-but-total, replay-stable final tiebreak.
- **F3 (P2) — empty query segments preserved.** `_strip_tracking` no longer deletes empty
  `split("&")` entries or leading/trailing separators; the only query transform is tracking-key
  removal.

## WHAT TO VERIFY (targeted)

1. **F1 correctness & completeness.** Does the 3-tuple key now match the ledger `UNIQUE` exactly, with
   no remaining path that collapses across `retrieval_run_id`? Is the "presentation view deferred"
   scope call acceptable, or does any *current* consumer need cross-run collapse (there is no consumer
   yet)? Any input where two documents that the ledger would store as distinct still collapse, or vice
   versa?
2. **F2 — is `_sort_key` a genuine total order and truly order-independent?** Specifically: can
   `model_dump_json()` ever be non-deterministic or non-total for two distinct documents (field
   ordering, float/`None`/datetime rendering, unicode normalization, equal serializations for
   non-identical docs)? Can it raise on any valid `ResearchDocument`? Does putting `provenance` and
   `retrieved_at_utc` ahead of it change any previously-correct outcome (the round-1 provenance and
   earliest-retrieval tests must still hold)?
3. **F3 — are empties and separators now preserved in every case**, including when a tracking key sits
   adjacent to an empty segment (e.g. `?utm_source=x&&b=2` → `?&b=2`)? Is that faithful/acceptable, or
   does removing a tracking pair still mangle surrounding structure in a surprising way? Confirm
   percent-hex is still uppercased over the rejoined query and that a lone tracking param
   (`?utm_source=x`) yields an empty query cleanly.
4. **Regression sweep of the delta only.** Did any fix disturb a property you cleared in round 1 —
   canonicalization round-trip/idempotence, freshness determinism/boundary, error-message hygiene
   (`CanonicalizationError` still constant + `from None`), or the docstring/behaviour agreement?

Do NOT invent scope: no schema/migration/dependency changes are wanted; freshness stays derived (not
persisted); fail-vs-flag gating is M1-504.

## OUTPUT FORMAT

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Resolution of round-1 findings:** F1 / F2 / F3 — each RESOLVED or NOT-RESOLVED with one line why.
- **New or remaining findings**, most severe first: severity (P1/P2/P3), `file:line`, one-sentence
  claim, a concrete failing input or scenario, minimal fix.
- **Checked and cleared:** 3–6 bullets on what you verified in the delta (esp. the `model_dump_json`
  total-order question and the adjacent-empty-segment case).

The full fix delta (round-1 request → now) follows.

```diff
diff --git a/GPT_REVIEW_RESPONSE_M1-305_R1.md b/GPT_REVIEW_RESPONSE_M1-305_R1.md
new file mode 100644
index 0000000..43651d0
--- /dev/null
+++ b/GPT_REVIEW_RESPONSE_M1-305_R1.md
@@ -0,0 +1,75 @@
+# M1-305 — response to review round 1
+
+Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-1 verdict: **DO-NOT-APPROVE**, three
+findings. **All three accepted and fixed.** Full gate green after the fixes: `pytest` 424 passed,
+`ruff check`, `ruff format --check`, `mypy --strict src` all clean.
+
+The primary risk I asked the reviewer to falsify — a host that survives `idna.encode(uts46=True)` in
+`canonicalize_url` but is then rejected by the schema's strict `idna.encode` re-validation — was
+**cleared**: 508k single-code-point label variants tested, no failing host. The canonicalization
+design stands unchanged.
+
+---
+
+## F1 (P1) — dedup collapsed across runs, losing attribution — `research/dedup.py`
+
+**Accepted.** `dedup_key` was `(canonical_url, content_sha256)`, dropping `retrieval_run_id`, so two
+providers (two runs) that both surfaced one article collapsed to a single survivor with
+`collapsed_count=1` — but the ledger's `UNIQUE(retrieval_run_id, canonical_url, content_sha256)`
+deliberately keeps **both** rows, one per run. Collapsing them erased which run found the evidence
+(an attribution loss — the exact thing the ledger exists to prevent) and, as the reviewer noted, the
+surviving run depended on input order.
+
+Root cause was a wrong reading in the original design: "duplicate reports collapse" was taken to mean
+*cross-provider* collapse. It does not — it means preventing a **single run** from storing the same
+artifact twice. Cross-run/cross-provider duplication is legitimate, distinct evidence the ledger
+keeps by design, and the acceptance criterion itself frames dedup as *feeding* the run-scoped UNIQUE.
+
+**Fix:** `dedup_key` is now `(retrieval_run_id, canonical_url, content_sha256)` — the ledger UNIQUE
+exactly. `deduplicate` collapses only true intra-run duplicates and never crosses runs, so cross-run
+provenance is preserved *by construction*. Module docstring rewritten to state this; the old
+"across a question's runs it lets two providers collapse" narrative is deleted.
+
+I took the "include the run id in the key" remedy over "separate presentation dedup from
+persistence." The presentation-layer, cross-provider "one card per artifact" view the reviewer
+alludes to is real, but it must *retain* every contributing run/provenance rather than drop them, and
+belongs to forecast assembly (≈ M1-504), not this primitive. Building it now would be speculative
+scope creep; it is documented as explicitly deferred in `docs/M1-305-NOTES.md`.
+
+Guard: `test_same_artifact_from_different_runs_is_not_collapsed` — identical `canonical_url` +
+`content_sha256`, different `retrieval_run_id`, asserts both survive and `collapsed_count == 0`.
+
+## F2 (P2) — survivor selection was order-dependent — `research/dedup.py`
+
+**Accepted.** `_prefer`'s final branch returned the first-seen document, which is input-order
+dependent, while the docstring claimed "total and order-independent." Two duplicates equal in
+provenance and `retrieved_at_utc` but differing in a non-key field (e.g. `title`) chose different
+survivors when input order was reversed — not replay-stable.
+
+**Fix:** selection is now a min over a **total** order,
+`_sort_key = (_PROVENANCE_RANK[provenance], retrieved_at_utc, model_dump_json())`. The full canonical
+serialization is a deterministic, arbitrary-but-total final tiebreak — exactly the reviewer's
+suggested remedy — so the order is total (ties only when documents are byte-identical) and
+independent of iteration order. `_prefer` is now `candidate if _sort_key(candidate) <
+_sort_key(current) else current`.
+
+The human-meaningful components stay first (stronger provenance, then earliest retrieval), so the
+existing `test_collapse_keeps_the_stronger_provenance` and
+`test_equal_provenance_ties_break_to_earliest_retrieval` remain meaningful and still pass.
+
+Guard: `test_exact_tie_survivor_is_order_independent` — same key/provenance/timestamp, different
+`title`, asserts `deduplicate([a, b])` and `deduplicate([b, a])` pick the same survivor.
+
+## F3 (P2) — empty query segments silently deleted — `research/canonical.py`
+
+**Accepted.** `_strip_tracking` did `if not pair: continue`, deleting empty `split("&")` entries
+(`?x=1&&y=2` → `?x=1&y=2`) and leading/trailing separators — a second, undocumented lossy transform,
+distinguishable by an endpoint that signs or dispatches on the raw query.
+
+**Fix:** the empty-segment skip is removed. The loop now drops **only** segments whose percent-decoded
+key is in `_TRACKING_PARAMS`; every other segment is kept verbatim, including empty strings and
+leading/trailing separators. Tracking-key removal is now the sole query transform (percent-hex is
+still uppercased over the rejoined query). Docstring updated to say so.
+
+Guard: `test_empty_query_segments_are_preserved` — `?x=1&&y=2`, `?a=1&`, `?&a=1` unchanged;
+`?utm_source=x&a=1` → `?a=1`.
diff --git a/docs/M1-305-NOTES.md b/docs/M1-305-NOTES.md
index 481d85e..b68d724 100644
--- a/docs/M1-305-NOTES.md
+++ b/docs/M1-305-NOTES.md
@@ -56,8 +56,10 @@ Tests: `tests/unit/test_dedup_freshness.py` (47 cases). Full gate green — `pyt
   (owner decision, 2026-07-22). Dedup keys on `canonical_url`, so two provider reports of one
   article differing only by a `utm_*`/`fbclid`/… tag would never collapse unless those tags are
   removed — which is the whole point of the item. `_TRACKING_PARAMS` is a module-level `frozenset`
-  matched case-insensitively against the percent-decoded key; every non-tracking parameter is
-  preserved byte-for-byte in its original position.
+  matched case-insensitively against the percent-decoded key; **tracking-key removal is the only
+  query transform** — every non-tracking parameter, and every empty segment / leading-trailing
+  separator, is preserved byte-for-byte in its original position (the empty-segment preservation was
+  a round-1 review fix; see below).
 
 - **Userinfo is dropped from the canonical URL** — it is not part of the resource identity for
   dedup, and keeping it would write credentials into the stored dedup key. `original_url` still
@@ -86,17 +88,20 @@ Tests: `tests/unit/test_dedup_freshness.py` (47 cases). Full gate green — `pyt
   Splitting them keeps the epic boundary: tagging is evidence about the document, gating is policy
   about the forecast.
 
-- **Dedup preserves the stronger provenance.** `deduplicate` collapses documents sharing
-  `(canonical_url, content_sha256)` — the ledger's UNIQUE minus the run id. The key is the artifact
-  identity, so the scope of a collapse is the input the caller passes: one run's documents for
-  strict per-run semantics, or a question's whole set to dedup across providers (which is where
-  "without losing provenance" bites — within one run, provenance is uniform). On collision the
-  survivor carries the **stronger claim** (`direct_api` > `llm_reported`): a verified retrieval is
-  never silently downgraded to a reported one, nor a reported one upgraded to verified. Ties on equal
-  provenance break to the earliest `retrieved_at_utc`, then first-seen — a total, order-independent
-  rule, so the result is stable. First-seen order is preserved. `DedupResult.collapsed_count` is
-  exposed so a future writer can record an auditable dedup counter, in the spirit of
-  `ResearchRun.posts_dropped_no_url`.
+- **Dedup mirrors the ledger UNIQUE exactly.** `deduplicate` collapses documents sharing
+  `(retrieval_run_id, canonical_url, content_sha256)` — **the ledger's UNIQUE, run id included**. It
+  collapses only true intra-run duplicates (preventing a constraint violation) and **never collapses
+  across runs**: two providers (two runs) that both surface one article are two legitimate ledger
+  rows, and the run id is part of the attribution, so cross-run/cross-provider provenance is
+  preserved *by construction*. (The initial cut keyed on `(canonical_url, content_sha256)` alone and
+  collapsed across runs, losing exactly that attribution — corrected in round 1; see below.) On an
+  intra-run collision the survivor is the minimum over a **total** order — stronger provenance
+  (`direct_api` > `llm_reported`, a defensive tiebreak since intra-run provenance is uniform today
+  but unenforced), then earliest `retrieved_at_utc`, then the document's full `model_dump_json()`
+  serialization as an arbitrary-but-total final tiebreak. That makes the survivor independent of
+  input order and replay-stable (round 1 fix). First-seen order of survivors is preserved.
+  `DedupResult.collapsed_count` is exposed so a future writer can record an auditable dedup counter,
+  in the spirit of `ResearchRun.posts_dropped_no_url`.
 
 ## Error hygiene
 
@@ -114,7 +119,42 @@ already-validated `ResearchDocument`s and pure timestamps.
   did. The consumer contract is documented in each module docstring.
 - **The stale/insufficient-research gate is M1-504** (`fail_on_stale_research` /
   `flag_on_stale_research`), which depends on this item.
+- **A presentation-layer "one card per artifact across providers" view is not built here.** Dedup
+  mirrors the per-run ledger constraint; a cross-provider forecaster view would have to *retain*
+  every contributing run/provenance rather than drop them, and belongs to forecast assembly, not
+  this dedup. Building it now would be speculative scope creep of the kind the M1-301 retrospective
+  warns against.
 - **Host allow/deny policy (loopback, private ranges, homograph adjudication beyond IDNA) is not
   decided here.** `model.py` already states shape-validation holds no network code; canonicalization
   inherits that boundary — whether a *reachable* host is an *appropriate* one belongs to whatever
   fetches a URL.
+
+## Cross-model review round 1 (2026-07-23)
+
+GPT returned **DO-NOT-APPROVE** with three findings, all verified accurate and all fixed on this
+branch. The primary risk I flagged for the reviewer — whether any host survives
+`idna.encode(uts46=True)` in `canonicalize_url` but is then rejected by the schema's strict
+`idna.encode` re-validation — was **cleared**: GPT tested 508k single-code-point label variants and
+found no failing host, so the canonicalization design stands.
+
+- **F1 (P1) — dedup collapsed across runs, losing attribution.** `dedup_key` had been
+  `(canonical_url, content_sha256)`, dropping `retrieval_run_id`, so two providers' reports of one
+  article collapsed to one survivor — but the ledger's `UNIQUE(retrieval_run_id, …)` deliberately
+  keeps both, one per run. This erased which run found the evidence (an attribution loss the ledger
+  exists to prevent) and was input-order-dependent. Root cause: the wrong reading that "duplicate
+  reports collapse" meant *cross-provider* collapse; it means preventing a *single run* from storing
+  the same artifact twice. **Fix:** `retrieval_run_id` is now in the key; dedup mirrors the ledger
+  UNIQUE exactly and never crosses runs. Guarded by
+  `test_same_artifact_from_different_runs_is_not_collapsed`.
+- **F2 (P2) — survivor selection was order-dependent.** `_prefer`'s final branch kept the
+  first-seen document, contradicting the docstring's "total and order-independent" claim: two
+  duplicates equal in provenance and `retrieved_at_utc` but differing in a non-key field (e.g.
+  `title`) chose different survivors on reversed input. **Fix:** selection is now a min over a total
+  order `(_PROVENANCE_RANK, retrieved_at_utc, model_dump_json())` — the full serialization is an
+  arbitrary-but-total, replay-stable final tiebreak. Guarded by
+  `test_exact_tie_survivor_is_order_independent`.
+- **F3 (P2) — empty query segments were silently deleted.** `_strip_tracking` dropped empty
+  `split("&")` entries and leading/trailing separators (`?x=1&&y=2` → `?x=1&y=2`), a second,
+  undocumented lossy transform an endpoint that signs/dispatches on the raw query can detect.
+  **Fix:** empties and separators are preserved; the only query transform is tracking-key removal.
+  Guarded by `test_empty_query_segments_are_preserved`.
diff --git a/src/whiskeyjack_bot/research/canonical.py b/src/whiskeyjack_bot/research/canonical.py
index f25b242..0f28a0b 100644
--- a/src/whiskeyjack_bot/research/canonical.py
+++ b/src/whiskeyjack_bot/research/canonical.py
@@ -135,13 +135,17 @@ def _strip_tracking(query: str) -> str:
     Splits on the raw ``&`` rather than round-tripping through ``parse_qsl`` /
     ``urlencode`` so a preserved parameter is stored exactly as the provider sent
     it -- re-encoding could alter a value that is part of the resource identity.
+
+    Tracking-key removal is the **only** query transform: empty segments
+    (``a&&b``) and leading/trailing separators are preserved, because an endpoint
+    that dispatches on or signs the raw query can distinguish them, and dropping
+    them was a second, undocumented lossy step (cross-model review round 1,
+    finding 3). A segment goes only if its percent-decoded key is a tracking tag.
     """
     if not query:
         return query
     kept: list[str] = []
     for pair in query.split("&"):
-        if not pair:
-            continue  # An empty segment ("a&&b") carries nothing; not preserved.
         key = pair.split("=", 1)[0]
         if unquote(key).lower() in _TRACKING_PARAMS:
             continue
diff --git a/src/whiskeyjack_bot/research/dedup.py b/src/whiskeyjack_bot/research/dedup.py
index 08d001e..b45c13a 100644
--- a/src/whiskeyjack_bot/research/dedup.py
+++ b/src/whiskeyjack_bot/research/dedup.py
@@ -1,32 +1,38 @@
-"""Provenance-preserving deduplication of research evidence (M1-305).
-
-Collapses documents that are the **same underlying artifact** -- identical
-``canonical_url`` *and* identical ``content_sha256`` -- into one, so a forecaster
-is not shown, and the ledger is not asked to store, the same article twice. The
-key mirrors the ledger's ``UNIQUE(retrieval_run_id, canonical_url,
-content_sha256)`` (M1-601) minus the run id: within one run this prevents a
-constraint violation, and across a question's runs it is what lets two providers
-that both surfaced one article collapse to a single piece of evidence. The scope
-of a collapse is the input the caller passes -- one run's documents for strict
-per-run semantics, or a question's whole set to dedup across providers.
-
-**Without losing provenance** is the acceptance criterion and the delicate part.
+"""Deduplication of research evidence, mirroring the ledger constraint (M1-305).
+
+Collapses documents the ledger would refuse as duplicates: the key is
+``(retrieval_run_id, canonical_url, content_sha256)``, **exactly** the ledger's
+``UNIQUE(retrieval_run_id, canonical_url, content_sha256)`` (M1-601). Within one
+run this prevents a constraint violation from two reports of one article; it
+**never collapses across runs**, because two providers (two runs) that both
+surface one article are two legitimate ledger rows -- the run id is part of the
+attribution, and merging them would erase which run found the evidence. So
+cross-run/cross-provider provenance is preserved *by construction*, not by a
+merge rule (an earlier cut keyed on ``(canonical_url, content_sha256)`` alone and
+lost exactly that -- cross-model review round 1, finding 1).
+
 ``provenance`` distinguishes a document the pipeline fetched (``direct_api``) from
 one a research agent merely reported (``llm_reported``), and the forecaster
-prompt's evidence caps read it. When the same artifact arrives both ways, the
-survivor must carry the *stronger* claim (``direct_api``): a verified retrieval is
-never silently downgraded to a reported one, nor a reported one upgraded to
-verified. ``original_url`` is a schema field on whichever document survives, so
-the as-retrieved URL is never lost either.
-
-Pure and deterministic: no I/O, first-seen order preserved, ties broken by a
-total, timestamp-based rule so the same input always yields the same output.
+prompt's evidence caps read it. Within a single run it is uniform today, but the
+schema does not enforce that, so on an intra-run collision the survivor still
+carries the *stronger* claim (``direct_api``) -- a verified retrieval is never
+silently downgraded, nor a reported one upgraded. ``original_url`` is a schema
+field on whichever document survives, so the as-retrieved URL is never lost.
+
+Pure and deterministic: no I/O, first-seen order preserved, and the survivor of a
+collision is a min over a **total** order (so the choice is independent of input
+order and replay-stable -- round 1, finding 2).
+
+A presentation-layer "one card per artifact across providers" view is deliberately
+*not* built here: it would have to retain every contributing run/provenance rather
+than drop them, and belongs to forecast assembly, not this dedup.
 """
 
 from __future__ import annotations
 
 from collections.abc import Iterable
 from dataclasses import dataclass
+from datetime import datetime
 
 from whiskeyjack_bot.research.model import Provenance, ResearchDocument
 
@@ -49,36 +55,39 @@ class DedupResult:
     collapsed_count: int
 
 
-def dedup_key(document: ResearchDocument) -> tuple[str, str]:
-    """The artifact identity a collapse is keyed on: ``(canonical_url, hash)``."""
-    return (document.canonical_url, document.content_sha256)
+def dedup_key(document: ResearchDocument) -> tuple[str, str, str]:
+    """The ledger's dedup identity: ``(retrieval_run_id, canonical_url, hash)``."""
+    return (document.retrieval_run_id, document.canonical_url, document.content_sha256)
 
 
-def _prefer(current: ResearchDocument, candidate: ResearchDocument) -> ResearchDocument:
-    """Choose the survivor of two same-artifact documents.
+def _sort_key(document: ResearchDocument) -> tuple[int, datetime, str]:
+    """A total order over same-key documents, used to pick the survivor.
 
-    Stronger provenance wins; on equal provenance the earliest ``retrieved_at_utc``
-    wins (the first observation of the artifact); a remaining tie keeps the
-    first-seen document. Total and order-independent, so the result is stable.
+    Stronger provenance first, then the earliest ``retrieved_at_utc`` (the first
+    observation of the artifact), then the document's full canonical serialization
+    -- an arbitrary but total and replay-stable final tiebreak that makes the
+    order independent of input order even when two duplicates differ only in a
+    non-key field such as ``title``.
     """
-    current_rank = _PROVENANCE_RANK[current.provenance]
-    candidate_rank = _PROVENANCE_RANK[candidate.provenance]
-    if candidate_rank < current_rank:
-        return candidate
-    if candidate_rank > current_rank:
-        return current
-    if candidate.retrieved_at_utc < current.retrieved_at_utc:
-        return candidate
-    return current
+    return (
+        _PROVENANCE_RANK[document.provenance],
+        document.retrieved_at_utc,
+        document.model_dump_json(),
+    )
+
+
+def _prefer(current: ResearchDocument, candidate: ResearchDocument) -> ResearchDocument:
+    """Return whichever of two same-key documents is smaller in ``_sort_key``."""
+    return candidate if _sort_key(candidate) < _sort_key(current) else current
 
 
 def deduplicate(documents: Iterable[ResearchDocument]) -> DedupResult:
-    """Collapse same-artifact documents, preserving the strongest provenance.
+    """Collapse duplicates by the ledger's key, keeping one survivor per key.
 
     Returns the survivors in first-seen order and the count of duplicates removed.
     """
-    survivors: dict[tuple[str, str], ResearchDocument] = {}
-    order: list[tuple[str, str]] = []
+    survivors: dict[tuple[str, str, str], ResearchDocument] = {}
+    order: list[tuple[str, str, str]] = []
     collapsed = 0
     for document in documents:
         key = dedup_key(document)
diff --git a/tests/unit/test_dedup_freshness.py b/tests/unit/test_dedup_freshness.py
index 109bd18..e40a68b 100644
--- a/tests/unit/test_dedup_freshness.py
+++ b/tests/unit/test_dedup_freshness.py
@@ -142,6 +142,23 @@ def test_canonicalize_rejects_what_the_schema_rejects(url: str) -> None:
         canonicalize_url(url)
 
 
+@pytest.mark.parametrize(
+    "url, expected",
+    [
+        # Tracking-key removal is the only query transform: empty segments and
+        # leading/trailing separators survive, because a query-signing or
+        # -dispatching endpoint can distinguish them.
+        ("https://example.org/a?x=1&&y=2", "https://example.org/a?x=1&&y=2"),
+        ("https://example.org/a?a=1&", "https://example.org/a?a=1&"),
+        ("https://example.org/a?&a=1", "https://example.org/a?&a=1"),
+        # Tracking is still stripped; the surrounding structure is left intact.
+        ("https://example.org/a?utm_source=x&a=1", "https://example.org/a?a=1"),
+    ],
+)
+def test_empty_query_segments_are_preserved(url: str, expected: str) -> None:
+    assert canonicalize_url(url) == expected
+
+
 def test_canonicalization_error_never_echoes_the_url() -> None:
     secret = "hunter2-do-not-print"
     try:
@@ -263,3 +280,28 @@ def test_equal_provenance_ties_break_to_earliest_retrieval() -> None:
     result = deduplicate([later, earlier])
     assert len(result.documents) == 1
     assert result.documents[0].retrieved_at_utc == datetime(2026, 7, 17, 6, tzinfo=timezone.utc)
+
+
+def test_same_artifact_from_different_runs_is_not_collapsed() -> None:
+    # The key is (retrieval_run_id, canonical_url, content_sha256), exactly the
+    # ledger's UNIQUE: two providers (two runs) that both surface one article are
+    # two legitimate rows, and collapsing them would erase which run found it.
+    body = _hash("one article, two providers")
+    from_asknews = _document(retrieval_run_id="run-asknews", content_sha256=body)
+    from_exa = _document(retrieval_run_id="run-exa", content_sha256=body)
+    result = deduplicate([from_asknews, from_exa])
+    assert result.collapsed_count == 0
+    assert {d.retrieval_run_id for d in result.documents} == {"run-asknews", "run-exa"}
+
+
+def test_exact_tie_survivor_is_order_independent() -> None:
+    # Same key, same provenance, same retrieved_at, differing only in a non-key
+    # field: the survivor must not depend on input order (the full-serialization
+    # tiebreak makes the selection a min over a total order).
+    body = _hash("one artifact, two records")
+    a = _document(title="Headline A", content_sha256=body)
+    b = _document(title="Headline B", content_sha256=body)
+    forward = deduplicate([a, b])
+    backward = deduplicate([b, a])
+    assert len(forward.documents) == 1
+    assert forward.documents[0].title == backward.documents[0].title
```
