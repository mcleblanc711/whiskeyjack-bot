# GPT cross-model review — M1-305 round 3 (re-review of the round-2 fix)

## ROLE

You are the same adversarial reviewer, doing a **narrow re-review of a single fix**. Across rounds 1
and 2 you cleared everything in M1-305 except one open item: in round 2 you found (P2) that
`dedup.py` `_sort_key`'s `model_dump_json()` tiebreak **raises** `PydanticSerializationError` for a
schema-valid document whose text field holds an unpaired surrogate — breaking totality and leaking
the offending character. That is the only thing this pass needs to judge: **is it fixed, correctly
and completely, without regression or new leak?** Don't re-litigate the already-cleared base. Prefer
one confirmed, reproducible finding over speculation.

## CONTEXT

Same project/constraints: immutable replayable ledger; pydantic v2 strict, `mypy --strict`, ruff
(100 cols); error messages never echo content; sanitizing raises use `from None`; no schema change,
migration, or new dependency. The full fix delta (round-2 request commit → now) is appended,
including `dedup.py`, the test, the updated notes, and `GPT_REVIEW_RESPONSE_M1-305_R2.md`. Gate after
the fix: `pytest` 425 passed; `ruff check`, `ruff format --check`, `mypy --strict src` all clean.

## THE FIX

`_sort_key`'s final tiebreak changed from `document.model_dump_json()` to
`repr(document.model_dump())`. Claim: `model_dump()` (Python mode) never UTF-8-encodes, so it cannot
raise on a lone surrogate; `repr` is deterministic (pydantic dumps fields in definition order) and
renders surrogates escaped; the string is used only as an internal sort key, never in a message, so
nothing input-derived can leak. The human-meaningful order (provenance, then earliest
`retrieved_at_utc`) is unchanged and still precedes the tiebreak.

## WHAT TO VERIFY (targeted)

1. **Does the surrogate case now behave?** `title=chr(0xD800)` on two same-key documents: does
   `deduplicate` complete without raising and pick an order-independent survivor? Any *other*
   schema-valid value that makes `repr(model_dump())` raise or become non-deterministic (recursion,
   NaN/Inf floats — note `cost_usd`/config are on `ResearchRun`, not `ResearchDocument`; unusual
   datetime/`None`/`Literal` renderings)?
2. **Is it still a total order?** Can two *distinct* `ResearchDocument`s produce equal
   `repr(model_dump())` (i.e. a collision that reintroduces order-dependence)? Is pydantic v2
   `model_dump()` field ordering actually stable enough to rely on here?
3. **Leak check.** Confirm the tiebreak value is never placed in an exception or log, and that after
   this change `dedup.py` raises nothing input-derived (`_PROVENANCE_RANK[...]` keyed by a validated
   `Literal`; datetime comparison total). Is the "used only as an internal key" claim actually true
   in the code path?
4. **No regression in the delta.** The round-1 provenance-wins and earliest-retrieval tests, and the
   round-1 order-independence test, must still hold under the new tiebreak.

Do NOT invent scope: rejecting surrogates at schema validation was deliberately declined (it would
touch the frozen `model.py` for a dedup-local problem; the schema intentionally accepts arbitrary
text). If you think that trade-off is wrong, say so as a finding with the concrete reason, but the
dedup-local fix is the intended scope.

## OUTPUT FORMAT

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Round-2 finding:** RESOLVED / NOT-RESOLVED, one line why.
- **New or remaining findings**, most severe first: severity, `file:line`, one-sentence claim, a
  concrete failing input, minimal fix.
- **Checked and cleared:** 3–5 bullets (esp. the totality/collision question and the leak check).

The full fix delta (round-2 request → now) follows.

```diff
diff --git a/GPT_REVIEW_RESPONSE_M1-305_R2.md b/GPT_REVIEW_RESPONSE_M1-305_R2.md
new file mode 100644
index 0000000..7c2e277
--- /dev/null
+++ b/GPT_REVIEW_RESPONSE_M1-305_R2.md
@@ -0,0 +1,48 @@
+# M1-305 — response to review round 2
+
+Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-2 verdict: **DO-NOT-APPROVE** — F1 and F3
+confirmed resolved, F2 not resolved, one new **P2**. **Accepted and fixed.** Full gate green after
+the fix: `pytest` 425 passed; `ruff check`, `ruff format --check`, `mypy --strict src` all clean.
+
+---
+
+## F1 — RESOLVED (confirmed). F3 — RESOLVED (confirmed).
+
+No change. Thanks for re-verifying the 3-tuple key against the SQLite UNIQUE and the
+`?utm_source=x&&b=2` → `?&b=2` / idempotence behaviour.
+
+## F2 follow-on (P2) — serialization tiebreak raised on a lone surrogate — `research/dedup.py::_sort_key`
+
+**Accepted.** Reproduced exactly in-repo before changing anything:
+
+- `validate_document({… "title": "\ud800"})` is **accepted** — pydantic's `str` admits unpaired
+  surrogates.
+- `doc.model_dump_json()` **raises** `PydanticSerializationError: … 'utf-8' codec …`, so deduplicating
+  two such same-key documents raised, and the message carried the offending character — the total
+  order was not total, and it breached error hygiene. Provider JSON with an escaped lone surrogate is
+  a realistic source.
+
+**Fix:** the tiebreak is now `repr(document.model_dump())` instead of `document.model_dump_json()`:
+
+- `model_dump()` (Python mode) returns Python objects and **never UTF-8-encodes**, so it cannot raise
+  on a surrogate.
+- `repr(...)` is deterministic (pydantic v2 dumps fields in definition order; datetimes/`None`/
+  `Literal`s `repr` stably) and renders surrogates **escaped**, so the key is a plain comparable
+  `str`.
+- It is total on distinct-content documents (equal `repr` ⟺ equal `model_dump` ⟺ same content, where
+  the survivor choice is immaterial), and comparing two such strings never raises (Python orders by
+  code point; surrogate code points compare fine).
+- It is used **only** as an internal sort key — never placed in an exception message — so no
+  input-derived value can leak. After this change `dedup.py` raises nothing input-derived at all
+  (`_PROVENANCE_RANK[…]` is keyed by a validated `Literal`; datetime comparison cannot raise).
+
+The human-meaningful ordering (stronger provenance, then earliest `retrieved_at_utc`) stays ahead of
+the tiebreak, so the round-1 provenance and earliest-retrieval tests are unchanged and still pass.
+
+**Not taken:** rejecting unpaired surrogates at schema validation. That broadens the change into the
+frozen `model.py` and its text/URL fields for a problem that is entirely dedup-local; the schema
+intentionally accepts arbitrary text, so the dedup primitive is what must be robust to it.
+
+Guard: `test_dedup_tiebreak_is_surrogate_safe` — two same-key documents whose `title` holds a lone
+surrogate → `deduplicate` does not raise, collapses to one, and picks the same survivor on reversed
+input.
diff --git a/docs/M1-305-NOTES.md b/docs/M1-305-NOTES.md
index b68d724..2f0aa33 100644
--- a/docs/M1-305-NOTES.md
+++ b/docs/M1-305-NOTES.md
@@ -97,8 +97,8 @@ Tests: `tests/unit/test_dedup_freshness.py` (47 cases). Full gate green — `pyt
   collapsed across runs, losing exactly that attribution — corrected in round 1; see below.) On an
   intra-run collision the survivor is the minimum over a **total** order — stronger provenance
   (`direct_api` > `llm_reported`, a defensive tiebreak since intra-run provenance is uniform today
-  but unenforced), then earliest `retrieved_at_utc`, then the document's full `model_dump_json()`
-  serialization as an arbitrary-but-total final tiebreak. That makes the survivor independent of
+  but unenforced), then earliest `retrieved_at_utc`, then `repr(model_dump())` as an
+  arbitrary-but-total final tiebreak. That makes the survivor independent of
   input order and replay-stable (round 1 fix). First-seen order of survivors is preserved.
   `DedupResult.collapsed_count` is exposed so a future writer can record an auditable dedup counter,
   in the spirit of `ResearchRun.posts_dropped_no_url`.
@@ -150,11 +150,29 @@ found no failing host, so the canonicalization design stands.
   first-seen document, contradicting the docstring's "total and order-independent" claim: two
   duplicates equal in provenance and `retrieved_at_utc` but differing in a non-key field (e.g.
   `title`) chose different survivors on reversed input. **Fix:** selection is now a min over a total
-  order `(_PROVENANCE_RANK, retrieved_at_utc, model_dump_json())` — the full serialization is an
-  arbitrary-but-total, replay-stable final tiebreak. Guarded by
+  order `(_PROVENANCE_RANK, retrieved_at_utc, <tiebreak>)`; the tiebreak is a full serialization —
+  see round 2 for why it became `repr(model_dump())`. Guarded by
   `test_exact_tie_survivor_is_order_independent`.
 - **F3 (P2) — empty query segments were silently deleted.** `_strip_tracking` dropped empty
   `split("&")` entries and leading/trailing separators (`?x=1&&y=2` → `?x=1&y=2`), a second,
   undocumented lossy transform an endpoint that signs/dispatches on the raw query can detect.
   **Fix:** empties and separators are preserved; the only query transform is tracking-key removal.
   Guarded by `test_empty_query_segments_are_preserved`.
+
+## Cross-model review round 2 (2026-07-23)
+
+Re-review of the round-1 fixes: **F1 and F3 confirmed resolved**, **F2 not resolved**, one new P2.
+
+- **F2 follow-on (P2) — the serialization tiebreak raised on lone surrogates.** The round-1 fix used
+  `model_dump_json()` as the total-order tiebreak, but a schema-valid text field may hold an unpaired
+  surrogate (`"\ud800"`, e.g. from provider JSON), and JSON serialization UTF-8-encodes, so
+  `model_dump_json()` **raises** `PydanticSerializationError` on it — the "total order" was not total,
+  and the uncaught exception **echoed the offending character** (an error-hygiene breach). Verified in
+  repo: `validate_document` accepts such a title; `model_dump_json()` raises; `repr(model_dump())`
+  does not. **Fix:** the tiebreak is now `repr(document.model_dump())` — Python-mode dump returns
+  objects and never encodes (so it cannot raise), `repr` renders surrogates escaped and is
+  deterministic, and it is used only as an internal sort key (never in a message), so nothing
+  input-derived can leak. After this, `dedup.py` raises nothing input-derived at all. Guarded by
+  `test_dedup_tiebreak_is_surrogate_safe`. Rejecting surrogates at schema validation was rejected as
+  the fix: it would touch the frozen `model.py` for a dedup-local problem, and the schema
+  intentionally accepts arbitrary text — the primitive is what must be robust to it.
diff --git a/src/whiskeyjack_bot/research/dedup.py b/src/whiskeyjack_bot/research/dedup.py
index b45c13a..29fd407 100644
--- a/src/whiskeyjack_bot/research/dedup.py
+++ b/src/whiskeyjack_bot/research/dedup.py
@@ -64,15 +64,28 @@ def _sort_key(document: ResearchDocument) -> tuple[int, datetime, str]:
     """A total order over same-key documents, used to pick the survivor.
 
     Stronger provenance first, then the earliest ``retrieved_at_utc`` (the first
-    observation of the artifact), then the document's full canonical serialization
-    -- an arbitrary but total and replay-stable final tiebreak that makes the
-    order independent of input order even when two duplicates differ only in a
-    non-key field such as ``title``.
+    observation of the artifact), then the document's full Python-mode dump as an
+    arbitrary but total and replay-stable final tiebreak -- so the order is
+    independent of input order even when two duplicates differ only in a non-key
+    field such as ``title``.
+
+    The tiebreak is ``repr(model_dump())``, **not** ``model_dump_json()``: a
+    schema-valid text field may hold an unpaired surrogate (``"\\ud800"``, e.g.
+    from provider JSON), and JSON serialization UTF-8-encodes, so
+    ``model_dump_json()`` *raises* ``PydanticSerializationError`` on it -- which
+    both breaks totality and, uncaught, echoes the offending character (cross-model
+    review round 2). ``model_dump()`` returns Python objects and never encodes, so
+    it cannot raise; ``repr`` renders such characters escaped, deterministically
+    (pydantic dumps fields in definition order), and the string is used only as an
+    internal sort key -- never placed in a message -- so nothing input-derived can
+    leak. Two ``repr`` strings always compare (Python orders by code point, and
+    surrogate code points compare fine); equal ``repr`` means equal content, where
+    the choice of survivor is immaterial.
     """
     return (
         _PROVENANCE_RANK[document.provenance],
         document.retrieved_at_utc,
-        document.model_dump_json(),
+        repr(document.model_dump()),
     )
 
 
diff --git a/tests/unit/test_dedup_freshness.py b/tests/unit/test_dedup_freshness.py
index e40a68b..7e31901 100644
--- a/tests/unit/test_dedup_freshness.py
+++ b/tests/unit/test_dedup_freshness.py
@@ -305,3 +305,16 @@ def test_exact_tie_survivor_is_order_independent() -> None:
     backward = deduplicate([b, a])
     assert len(forward.documents) == 1
     assert forward.documents[0].title == backward.documents[0].title
+
+
+def test_dedup_tiebreak_is_surrogate_safe() -> None:
+    # A text field may hold an unpaired surrogate (schema-valid; e.g. from provider
+    # JSON). The tiebreak must not raise on it -- model_dump_json() would, and would
+    # leak the character -- and must still be order-independent.
+    body = _hash("surrogate in the title")
+    a = _document(title="\ud800", content_sha256=body)
+    b = _document(title="\ud801", content_sha256=body)
+    forward = deduplicate([a, b])
+    backward = deduplicate([b, a])
+    assert len(forward.documents) == 1
+    assert forward.documents[0].title == backward.documents[0].title
```
