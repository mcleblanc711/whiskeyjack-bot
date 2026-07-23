# GPT cross-model review — M1-305 round 4 (re-review of the round-3 fix)

## ROLE

You are the same adversarial reviewer, doing a **narrow re-review of a single fix**. Across rounds
1–3 you cleared everything in M1-305 except the dedup tiebreak. In round 3 you found (P1) that
`repr(model_dump())` keyed on the in-memory form and was **not replay-stable**: `datetime.fold`
differs in memory but is dropped by the persisted JSON, so the survivor could flip across a
store→replay round-trip. That is the only open item. **Is it fixed — replay-stable, still total,
still surrogate-safe, no leak, no regression?** Don't re-litigate the cleared base. Prefer one
confirmed, reproducible finding over speculation.

## CONTEXT

Same project/constraints: immutable **replayable** attribution ledger — a dedup survivor must be the
one a replay from the ledger's stored JSON would pick. pydantic v2 strict, `mypy --strict`, ruff (100
cols); error messages never echo content; no schema change/migration/dependency. The full fix delta
(round-3 request → now) is appended, including `dedup.py`, the test, the notes, and
`GPT_REVIEW_RESPONSE_M1-305_R3.md`. Gate after the fix: `pytest` 426 passed; `ruff`, `format`,
`mypy --strict src` all clean.

## THE FIX

`_sort_key`'s tiebreak changed from `repr(document.model_dump())` to:

```python
json.dumps(document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
```

Claim: `mode="json"` renders exactly the persisted form (datetimes via `isoformat`, dropping
`fold`), so the survivor is identical before and after a store→replay round-trip; `ensure_ascii=True`
escapes lone surrogates instead of UTF-8-encoding (so it doesn't raise like round 2's
`model_dump_json()`); `sort_keys`/`separators` make it canonical. Provenance-then-earliest ordering
still precedes the tiebreak.

## WHAT TO VERIFY (targeted)

1. **Replay stability — the round-3 finding.** Does the equal-instant/differing-`fold` case now pick
   the **same** survivor before and after `validate_document(doc.model_dump(mode="json"))`? Any other
   in-memory/persisted discrepancy on a `ResearchDocument` field that `mode="json"` does **not**
   normalize and that could still flip the survivor (timezone offset rendering, microsecond
   precision, `None` vs absent, string normalization)?
2. **Still total & deterministic.** Can two **distinct** persisted documents produce the same
   canonical JSON (a collision that reintroduces order-dependence)? Is `model_dump(mode="json")`
   itself deterministic here (it feeds `sort_keys=True`)?
3. **Still surrogate-safe & leak-free.** Confirm `json.dumps(..., ensure_ascii=True)` does not raise
   on a lone-surrogate text field and the escaped value is never placed in an exception/log; confirm
   `dedup.py` still raises nothing input-derived.
4. **No regression in the delta.** Round-1 provenance-wins / earliest-retrieval / order-independence
   and round-2 surrogate-safety tests must still hold.

Do NOT invent scope: no schema/migration/dependency changes; rejecting surrogates at schema
validation remains out of scope by design (dedup-local problem; schema intentionally accepts
arbitrary text).

## OUTPUT FORMAT

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Round-3 P1:** RESOLVED / NOT-RESOLVED, one line why.
- **New or remaining findings**, most severe first: severity, `file:line`, one-sentence claim, a
  concrete failing input, minimal fix.
- **Checked and cleared:** 3–5 bullets (esp. the replay-stability and totality/collision questions).

The full fix delta (round-3 request → now) follows.

```diff
diff --git a/GPT_REVIEW_RESPONSE_M1-305_R3.md b/GPT_REVIEW_RESPONSE_M1-305_R3.md
new file mode 100644
index 0000000..b2e92e5
--- /dev/null
+++ b/GPT_REVIEW_RESPONSE_M1-305_R3.md
@@ -0,0 +1,47 @@
+# M1-305 — response to review round 3
+
+Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-3 verdict: **DO-NOT-APPROVE** — surrogate
+crash confirmed resolved, F2 total-order/replay-stability not resolved (new **P1**), plus a P3 nit.
+**Both accepted and fixed.** Full gate green after the fix: `pytest` 426 passed; `ruff check`,
+`ruff format --check`, `mypy --strict src` all clean.
+
+---
+
+## P1 — `repr(model_dump())` tiebreak was not replay-stable — `research/dedup.py::_sort_key`
+
+**Accepted.** Reproduced the mechanism in-repo before changing anything:
+
+- `_to_utc` (`astimezone(UTC)`) does **not** normalize `datetime.fold`, so a `fold=1` timestamp
+  survives `validate_document` with `fold=1` (reachable via `retrieved_at_utc` and
+  `published_at_utc`).
+- Two same-key documents with equal UTC `retrieved_at_utc` but differing `fold` compare **equal**
+  (tying that sort component), yet `repr(model_dump())` **differs** because `datetime.__repr__`
+  renders `fold=1`. Persistence (`isoformat`/JSON) **drops** `fold`, so after a store→replay
+  round-trip both are `fold=0` and the tiebreak flips the survivor. For a replay-attribution ledger
+  this is exactly the bug that matters — the survivor was keyed on the in-memory form, not the
+  persisted one.
+
+**Fix:** key the tiebreak on the **canonical persisted form** —
+
+```python
+json.dumps(document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
+```
+
+- `mode="json"` renders exactly what the ledger stores and replay reconstructs (datetimes via
+  `isoformat`, which drops `fold`), so the survivor is identical before and after persistence.
+  Confirmed: the canonical JSON is fold-invariant where `repr` was fold-variant.
+- `ensure_ascii=True` escapes lone surrogates instead of UTF-8-encoding them, so it stays
+  surrogate-safe — it does not raise the way plain `model_dump_json()` did in round 2. Confirmed: no
+  raise, character escaped.
+- `sort_keys`/`separators` make it canonical and representation-independent. Still total (equal
+  canonical JSON ⟺ equal persisted content, where the survivor choice is immaterial), comparison
+  never raises (plain ASCII), and the value is used only as an internal sort key — never in a
+  message — so nothing input-derived can leak. `dedup.py` still raises nothing input-derived.
+
+Guard: `test_dedup_survivor_is_replay_stable` — the before/after-serialization survivor test, in both
+input orders, over the equal-instant/differing-`fold`/differing-`snippet` case.
+
+## P3 — stale test counts — `docs/M1-305-NOTES.md`
+
+**Fixed.** The "Delivered" line still cited the round-0 numbers (47 cases / `pytest` 418 passed);
+corrected to the current **55 module cases / 426 total**.
diff --git a/docs/M1-305-NOTES.md b/docs/M1-305-NOTES.md
index 2f0aa33..d4f7dab 100644
--- a/docs/M1-305-NOTES.md
+++ b/docs/M1-305-NOTES.md
@@ -21,8 +21,8 @@ Three pure-primitive modules under `src/whiskeyjack_bot/research/`, mirroring ho
   `assess_document(doc, cutoff)`.
 - **`dedup.py`** — `dedup_key(doc)`, `deduplicate(docs) -> DedupResult`, `DedupResult` (frozen).
 
-Tests: `tests/unit/test_dedup_freshness.py` (47 cases). Full gate green — `pytest` 418 passed,
-`ruff check`, `ruff format --check`, `mypy --strict src` all clean.
+Tests: `tests/unit/test_dedup_freshness.py` (55 cases, after review rounds 1–3). Full gate green —
+`pytest` 426 passed, `ruff check`, `ruff format --check`, `mypy --strict src` all clean.
 
 ## Deliberate choices
 
@@ -97,8 +97,9 @@ Tests: `tests/unit/test_dedup_freshness.py` (47 cases). Full gate green — `pyt
   collapsed across runs, losing exactly that attribution — corrected in round 1; see below.) On an
   intra-run collision the survivor is the minimum over a **total** order — stronger provenance
   (`direct_api` > `llm_reported`, a defensive tiebreak since intra-run provenance is uniform today
-  but unenforced), then earliest `retrieved_at_utc`, then `repr(model_dump())` as an
-  arbitrary-but-total final tiebreak. That makes the survivor independent of
+  but unenforced), then earliest `retrieved_at_utc`, then a canonical JSON dump of
+  `model_dump(mode="json")` as a total, replay-stable final tiebreak. That makes the survivor
+  independent of
   input order and replay-stable (round 1 fix). First-seen order of survivors is preserved.
   `DedupResult.collapsed_count` is exposed so a future writer can record an auditable dedup counter,
   in the spirit of `ResearchRun.posts_dropped_no_url`.
@@ -151,7 +152,7 @@ found no failing host, so the canonicalization design stands.
   duplicates equal in provenance and `retrieved_at_utc` but differing in a non-key field (e.g.
   `title`) chose different survivors on reversed input. **Fix:** selection is now a min over a total
   order `(_PROVENANCE_RANK, retrieved_at_utc, <tiebreak>)`; the tiebreak is a full serialization —
-  see round 2 for why it became `repr(model_dump())`. Guarded by
+  see rounds 2 and 3 for how it became a canonical JSON dump. Guarded by
   `test_exact_tie_survivor_is_order_independent`.
 - **F3 (P2) — empty query segments were silently deleted.** `_strip_tracking` dropped empty
   `split("&")` entries and leading/trailing separators (`?x=1&&y=2` → `?x=1&y=2`), a second,
@@ -176,3 +177,25 @@ Re-review of the round-1 fixes: **F1 and F3 confirmed resolved**, **F2 not resol
   `test_dedup_tiebreak_is_surrogate_safe`. Rejecting surrogates at schema validation was rejected as
   the fix: it would touch the frozen `model.py` for a dedup-local problem, and the schema
   intentionally accepts arbitrary text — the primitive is what must be robust to it.
+
+## Cross-model review round 3 (2026-07-23)
+
+Re-review of the round-2 fix: **surrogate crash confirmed resolved**, but F2's total-order/
+replay-stability was **still not resolved**, with a new **P1**.
+
+- **F2 follow-on (P1) — the `repr` tiebreak was not replay-stable.** `repr(model_dump())` keys on the
+  **in-memory** Python form, which carries `datetime.fold`; the **persisted** form (JSON/`isoformat`)
+  drops it. Two same-key documents with equal UTC `retrieved_at_utc` but differing `fold` compare
+  equal (tying that component) yet produce different `repr` — so the survivor chosen in memory could
+  differ from the one a store→replay round-trip would pick, flipping the result. For a
+  replay-attribution ledger that is the class of bug that matters. Verified in repo: `_to_utc`
+  (`astimezone(UTC)`) does not normalize `fold`, so `fold=1` survives validation (reachable via
+  `retrieved_at_utc` and `published_at_utc`), and `repr(model_dump())` differs on it while
+  `model_dump(mode="json")` does not. **Fix:** the tiebreak now keys on the **canonical persisted
+  form** — `json.dumps(document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True,
+  separators=(",", ":"))`. `mode="json"` renders exactly the stored form (fold-invariant), so the
+  survivor is the same before and after persistence; `ensure_ascii=True` keeps it surrogate-safe (it
+  escapes rather than UTF-8-encodes, so it does not raise); `sort_keys`/`separators` make it
+  canonical. Guarded by `test_dedup_survivor_is_replay_stable`.
+- **P3 nit — stale test counts.** The "Delivered" line still cited the round-0 numbers (47 cases /
+  418 total); corrected to the current 55 module cases / 426 total.
diff --git a/src/whiskeyjack_bot/research/dedup.py b/src/whiskeyjack_bot/research/dedup.py
index 29fd407..dfa3fa8 100644
--- a/src/whiskeyjack_bot/research/dedup.py
+++ b/src/whiskeyjack_bot/research/dedup.py
@@ -30,6 +30,7 @@ than drop them, and belongs to forecast assembly, not this dedup.
 
 from __future__ import annotations
 
+import json
 from collections.abc import Iterable
 from dataclasses import dataclass
 from datetime import datetime
@@ -64,28 +65,36 @@ def _sort_key(document: ResearchDocument) -> tuple[int, datetime, str]:
     """A total order over same-key documents, used to pick the survivor.
 
     Stronger provenance first, then the earliest ``retrieved_at_utc`` (the first
-    observation of the artifact), then the document's full Python-mode dump as an
-    arbitrary but total and replay-stable final tiebreak -- so the order is
-    independent of input order even when two duplicates differ only in a non-key
-    field such as ``title``.
-
-    The tiebreak is ``repr(model_dump())``, **not** ``model_dump_json()``: a
-    schema-valid text field may hold an unpaired surrogate (``"\\ud800"``, e.g.
-    from provider JSON), and JSON serialization UTF-8-encodes, so
-    ``model_dump_json()`` *raises* ``PydanticSerializationError`` on it -- which
-    both breaks totality and, uncaught, echoes the offending character (cross-model
-    review round 2). ``model_dump()`` returns Python objects and never encodes, so
-    it cannot raise; ``repr`` renders such characters escaped, deterministically
-    (pydantic dumps fields in definition order), and the string is used only as an
-    internal sort key -- never placed in a message -- so nothing input-derived can
-    leak. Two ``repr`` strings always compare (Python orders by code point, and
-    surrogate code points compare fine); equal ``repr`` means equal content, where
-    the choice of survivor is immaterial.
+    observation of the artifact), then the document's **canonical persisted form**
+    as a total, replay-stable final tiebreak -- so the survivor is independent of
+    input order even when two duplicates differ only in a non-key field.
+
+    The tiebreak keys on ``model_dump(mode="json")``, not the in-memory
+    ``model_dump()``/``repr``: the survivor must be the one a replay would pick,
+    and replay reconstructs documents from the ledger's JSON. The Python form
+    carries distinctions the persisted form drops -- notably ``datetime.fold``,
+    which is absent from ``isoformat`` -- so two timestamps that are equal but
+    differ in ``fold`` would order differently in memory yet identically after a
+    store->replay round-trip, flipping the survivor (cross-model review round 3).
+    ``mode="json"`` renders exactly the stored form, so before == after.
+
+    ``ensure_ascii=True`` escapes lone surrogates (a schema-valid text field may
+    hold ``"\\ud800"`` from provider JSON) instead of UTF-8-encoding them, so it
+    does not raise the way plain ``model_dump_json()`` did (round 2).
+    ``sort_keys``/``separators`` make the string canonical. It is total (equal
+    canonical JSON <=> equal persisted content, where the choice is immaterial),
+    comparison never raises (plain ASCII), and it is used only as an internal sort
+    key -- never in a message -- so nothing input-derived can leak.
     """
     return (
         _PROVENANCE_RANK[document.provenance],
         document.retrieved_at_utc,
-        repr(document.model_dump()),
+        json.dumps(
+            document.model_dump(mode="json"),
+            ensure_ascii=True,
+            sort_keys=True,
+            separators=(",", ":"),
+        ),
     )
 
 
diff --git a/tests/unit/test_dedup_freshness.py b/tests/unit/test_dedup_freshness.py
index 7e31901..51505b1 100644
--- a/tests/unit/test_dedup_freshness.py
+++ b/tests/unit/test_dedup_freshness.py
@@ -307,6 +307,32 @@ def test_exact_tie_survivor_is_order_independent() -> None:
     assert forward.documents[0].title == backward.documents[0].title
 
 
+def test_dedup_survivor_is_replay_stable() -> None:
+    # Equal UTC instants that differ only in datetime.fold compare equal, so the
+    # tiebreak decides. fold is carried in memory but dropped by JSON/isoformat,
+    # so a tiebreak keyed on the in-memory form would pick a different survivor
+    # before vs after a store->replay round-trip. The canonical-JSON key must not.
+    body = _hash("one artifact, two records with different fold")
+    fold0 = datetime(2026, 7, 17, tzinfo=timezone.utc, fold=0)
+    fold1 = datetime(2026, 7, 17, tzinfo=timezone.utc, fold=1)
+    a = _document(retrieved_at_utc=fold0, snippet="A", content_sha256=body)
+    b = _document(retrieved_at_utc=fold1, snippet="B", content_sha256=body)
+
+    def survivor(docs: list[ResearchDocument]) -> str | None:
+        return deduplicate(docs).documents[0].snippet
+
+    def replayed(doc: ResearchDocument) -> ResearchDocument:
+        # Round-trip through the persisted JSON form, as the ledger + replay would.
+        return validate_document(doc.model_dump(mode="json"))
+
+    before = survivor([a, b])
+    after = survivor([replayed(a), replayed(b)])
+    assert before == after
+    # And it stays order-independent both before and after persistence.
+    assert survivor([b, a]) == before
+    assert survivor([replayed(b), replayed(a)]) == after
+
+
 def test_dedup_tiebreak_is_surrogate_safe() -> None:
     # A text field may hold an unpaired surrogate (schema-valid; e.g. from provider
     # JSON). The tiebreak must not raise on it -- model_dump_json() would, and would
```
