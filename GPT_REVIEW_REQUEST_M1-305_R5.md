# GPT cross-model review — M1-305 round 5 (rebuttal on the P2 tiebreak-injectivity finding)

## ROLE

You are the same reviewer. In round 4 you confirmed the fold/replay-stability fix and raised one new
**P2**: the dedup tiebreak collides an astral scalar with its UTF-16 surrogate-pair spelling, so the
in-memory survivor is order-dependent; you proposed an injective key
(`ensure_ascii=False` + `.encode("utf-8", "surrogatepass")`). **That fix was declined, deliberately**,
because it appears to reintroduce the round-3 defect you yourself found. This round is a **focused
adjudication of that one disagreement.** Be adversarial, but engage the specific evidence: either
(a) agree the persisted-form key is correct, or (b) produce a **concrete** input where the current
code selects a survivor whose *persisted* form is input-order-dependent, or is replay-unstable across
a real `json.dumps → json.loads` boundary. A hypothetical without a failing persisted-form case is
not sufficient here.

## THE DISAGREEMENT

The tiebreak keys on the **canonical persisted form**:
`json.dumps(document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))`.

Your round-4 point: astral `chr(0x1F600)` (len 1) and surrogate-pair `chr(0xD83D)+chr(0xDE00)`
(len 2) yield the same key, so `deduplicate([a,b])` and `deduplicate([b,a])` return different
in-memory objects.

Our position (verified in-repo, evidence below): those two documents are the **same persisted row**,
so keying them equal is replay-*correct*, and your injective fix is replay-*unstable*.

## EVIDENCE (reproduced in this repo)

For `astral = title=chr(0x1F600)` and `pair = title=chr(0xD83D)+chr(0xDE00)`, same key otherwise:

- Distinct in memory: `astral.title != pair.title` → True; lengths 1 vs 2.
- Current key equal: True (they collide).
- **Real `json.dumps → json.loads` round-trip** (what the ledger store→replay does) maps **both** to
  the single scalar U+1F600 — `json.loads` recombines the pair. So they are the same persisted
  document.
- Current code: the returned survivor's **persisted** form is identical in both input orders
  (`_replayed(forward).title == _replayed(backward).title == chr(0x1F600)`).
- **Your injective key is replay-unstable:** `gpt_key(pair)` before-persist `!=` after-persist under
  a standard-JSON ledger — it distinguishes two documents the ledger stores as one, so the survivor's
  key in memory would not match its key after persistence (the round-3 failure mode).
- Reachability: a *separated* surrogate pair cannot come from real provider JSON (`json.loads` always
  recombines valid pairs); the input is schema-valid but unreachable through retrieval. (A *lone*
  surrogate — round 2 — is reachable and is handled: `ensure_ascii=True` escapes it, no raise.)

Conclusion we're asserting: the correct granularity for a replay ledger is **persisted identity**,
not in-memory string identity. Two documents with identical canonical JSON are one row; the tiebreak
orders those forms, and the survivor's persisted form is input-order-invariant.

## WHAT CHANGED THIS ROUND

- **No change to the key** (persisted-form, `ensure_ascii=True`).
- Docstring now states the contract: total over persisted forms, deliberately not over in-memory
  identity, with the astral/surrogate example and why the injective alternative is wrong.
- **Accepted your procedural point:** the round-3 replay test used a bare `model_dump(mode="json")`
  handoff that skipped JSON text encoding. `_replayed` now crosses a real
  `json.dumps → json.loads → validate_document` boundary, and a new test pins the astral/surrogate
  collapse-and-persist-identically property in both orders.

## WHAT TO DECIDE

1. Do you accept that keying on the persisted form is correct for a replay ledger, given both
   documents are one persisted row? If not:
2. Provide a **concrete** `ResearchDocument` pair (schema-valid, ideally retrieval-reachable) where
   the current code's survivor has an input-order-dependent **persisted** form, or is unstable across
   a real `json.dumps → json.loads` round-trip. That is the only thing that reopens this.
3. Separately, is the residual — that text fields admit JSON-problematic scalars at all — better
   addressed as input normalization at the writer (M1-602) / schema than in this dedup primitive? A
   yes/no with reasoning is useful; it is out of M1-305's scope either way.

## OUTPUT FORMAT

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **P2 adjudication:** WITHDRAWN / UPHELD — if upheld, the concrete replay-unstable persisted-form
  case (per item 2 above); a restatement of the in-memory-injectivity point alone will read as
  agreement that the persisted-form key is correct.
- **Any other new/remaining findings**, most severe first.
- **Checked and cleared:** 3–5 bullets.

The full delta (round-4 request → now) follows.

```diff
diff --git a/GPT_REVIEW_RESPONSE_M1-305_R4.md b/GPT_REVIEW_RESPONSE_M1-305_R4.md
new file mode 100644
index 0000000..a44974d
--- /dev/null
+++ b/GPT_REVIEW_RESPONSE_M1-305_R4.md
@@ -0,0 +1,56 @@
+# M1-305 — response to review round 4
+
+Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-4 verdict: **DO-NOT-APPROVE** — round-3
+replay-stability (fold) confirmed resolved; one new **P2** (tiebreak not injective over in-memory
+strings). **The finding is accurate, but its suggested fix is declined with evidence** (owner
+decision); the valid procedural half — the replay test was too weak — is accepted and fixed. Gate
+green: `pytest` 427 passed; `ruff check`, `ruff format --check`, `mypy --strict src` all clean.
+
+---
+
+## P2 — injectivity over in-memory strings — finding accepted, suggested fix **declined**
+
+**The observation is correct.** An astral scalar `chr(0x1F600)` (len 1) and its UTF-16 surrogate-pair
+spelling `chr(0xD83D)+chr(0xDE00)` (len 2) are distinct Python strings, both validate, and both
+serialize to the same `ensure_ascii=True` canonical JSON — so they collide and the *in-memory*
+returned survivor is input-order-dependent.
+
+**The suggested fix — `ensure_ascii=False` + `.encode("utf-8", "surrogatepass")` — is declined,
+because it is replay-unstable**, which is precisely the property round 3 required. Verified in-repo:
+
+- After a **real** `json.dumps → json.loads` round-trip (what the ledger does when it stores and a
+  replay reconstructs), **both documents collapse to the single scalar U+1F600** — `json.loads`
+  recombines the surrogate pair. They are the *same persisted row*.
+- `gpt_key(pair)` **differs before vs after** that round-trip (`before == after` → `False`). So the
+  injective key distinguishes two documents the ledger stores identically, and the survivor's key in
+  memory would not match its key after persistence — reopening the round-3 bug (key ≠ persisted
+  form) in a new guise.
+
+The tiebreak deliberately orders **persisted forms, not in-memory string identity**. For a replay
+ledger that is the correct granularity: two documents the canonical JSON cannot tell apart *are* one
+row, so keying them equal is replay-correct, and the surviving document's **persisted** form is
+input-order-invariant — confirmed in both input orders. Injectivity over in-memory identity is not a
+goal; matching persisted identity is.
+
+Two further points on scope/reachability:
+
+- A *separated* surrogate pair cannot arise from real provider JSON — `json.loads` always recombines
+  `😀` into U+1F600 — so the input is schema-valid but unreachable through the retrieval
+  path. (A *lone* surrogate, the round-2 case, is reachable and is handled: `ensure_ascii=True`
+  escapes it without raising.)
+- The genuine root-cause question — whether text fields should admit JSON-problematic scalars at all
+  — belongs to input normalization at the writer (M1-602) or the schema (`model.py`), not to this
+  dedup primitive. M1-305's tiebreak owns replay-stable determinism, and it has that.
+
+**Docstring updated** to state the contract explicitly: total over persisted forms, deliberately not
+over in-memory identity, with the astral/surrogate example and why the injective alternative is
+wrong.
+
+## Accepted — the replay test was too weak
+
+Correct: the round-3 test used a bare `validate_document(doc.model_dump(mode="json"))` handoff, which
+skips JSON *text* encoding. The `_replayed` helper now crosses a real
+`json.dumps → json.loads → validate_document` boundary, so the fold test exercises actual persistence
+normalization, and a new test — `test_json_equivalent_titles_collapse_and_persist_identically` —
+pins that the astral/surrogate pair collapse and persist to the identical document in either input
+order.
diff --git a/docs/M1-305-NOTES.md b/docs/M1-305-NOTES.md
index d4f7dab..cfef66f 100644
--- a/docs/M1-305-NOTES.md
+++ b/docs/M1-305-NOTES.md
@@ -21,8 +21,8 @@ Three pure-primitive modules under `src/whiskeyjack_bot/research/`, mirroring ho
   `assess_document(doc, cutoff)`.
 - **`dedup.py`** — `dedup_key(doc)`, `deduplicate(docs) -> DedupResult`, `DedupResult` (frozen).
 
-Tests: `tests/unit/test_dedup_freshness.py` (55 cases, after review rounds 1–3). Full gate green —
-`pytest` 426 passed, `ruff check`, `ruff format --check`, `mypy --strict src` all clean.
+Tests: `tests/unit/test_dedup_freshness.py` (56 cases, after review rounds 1–4). Full gate green —
+`pytest` 427 passed, `ruff check`, `ruff format --check`, `mypy --strict src` all clean.
 
 ## Deliberate choices
 
@@ -198,4 +198,31 @@ replay-stability was **still not resolved**, with a new **P1**.
   escapes rather than UTF-8-encodes, so it does not raise); `sort_keys`/`separators` make it
   canonical. Guarded by `test_dedup_survivor_is_replay_stable`.
 - **P3 nit — stale test counts.** The "Delivered" line still cited the round-0 numbers (47 cases /
-  418 total); corrected to the current 55 module cases / 426 total.
+  418 total); corrected to the current 56 module cases / 427 total.
+
+## Cross-model review round 4 (2026-07-23)
+
+Re-review of the round-3 fix: **replay-stability (fold) confirmed resolved.** One new **P2**, which
+was **evaluated and its suggested fix declined with evidence** (owner decision) — the only accepted
+part is a test-strengthening.
+
+- **P2 (finding accepted, fix declined) — the tiebreak is not injective over in-memory strings.** An
+  astral scalar (`chr(0x1F600)`, len 1) and its UTF-16 surrogate-pair spelling
+  (`chr(0xD83D)+chr(0xDE00)`, len 2) are distinct Python strings, both schema-valid, but produce the
+  **same** `ensure_ascii=True` canonical JSON, so they collide and the *in-memory* returned survivor
+  is input-order-dependent. GPT proposed an injective key (`ensure_ascii=False` +
+  `.encode("utf-8", "surrogatepass")`). **Declined**, because it is *replay-unstable*: verified
+  in-repo that (a) after a real `json.dumps`→`json.loads` round-trip — what the ledger does —
+  **both documents collapse to the one scalar U+1F600**, i.e. they are the same persisted row; and
+  (b) `gpt_key(pair)` differs before vs after that round-trip, reopening exactly the round-3 bug
+  (key ≠ persisted form). The tiebreak deliberately orders **persisted forms**, not in-memory string
+  identity: two documents the ledger cannot tell apart are keyed equal, and the surviving document's
+  *persisted* form is input-order-invariant (confirmed both ways). A separated surrogate pair also
+  cannot arise from real provider JSON — `json.loads` always recombines `😀` into U+1F600
+  — so the input is schema-valid but not reachable through the retrieval path. Docstring updated to
+  state the "total over persisted forms, not in-memory identity" contract explicitly.
+- **Accepted (test hardening):** GPT correctly noted the round-3 replay test used a bare
+  `model_dump(mode="json")` handoff, which skips JSON text encoding. The `_replayed` helper now
+  crosses a real `json.dumps`→`json.loads`→`validate_document` boundary, and a new test
+  (`test_json_equivalent_titles_collapse_and_persist_identically`) pins that the astral/surrogate
+  pair collapse and persist identically in either order.
diff --git a/src/whiskeyjack_bot/research/dedup.py b/src/whiskeyjack_bot/research/dedup.py
index dfa3fa8..5258dec 100644
--- a/src/whiskeyjack_bot/research/dedup.py
+++ b/src/whiskeyjack_bot/research/dedup.py
@@ -62,29 +62,40 @@ def dedup_key(document: ResearchDocument) -> tuple[str, str, str]:
 
 
 def _sort_key(document: ResearchDocument) -> tuple[int, datetime, str]:
-    """A total order over same-key documents, used to pick the survivor.
+    """A total order over same-key documents' **persisted forms**, to pick a survivor.
 
     Stronger provenance first, then the earliest ``retrieved_at_utc`` (the first
     observation of the artifact), then the document's **canonical persisted form**
-    as a total, replay-stable final tiebreak -- so the survivor is independent of
-    input order even when two duplicates differ only in a non-key field.
+    as the final tiebreak -- so the survivor a replay picks is independent of input
+    order even when two duplicates differ only in a non-key field.
 
     The tiebreak keys on ``model_dump(mode="json")``, not the in-memory
-    ``model_dump()``/``repr``: the survivor must be the one a replay would pick,
-    and replay reconstructs documents from the ledger's JSON. The Python form
-    carries distinctions the persisted form drops -- notably ``datetime.fold``,
-    which is absent from ``isoformat`` -- so two timestamps that are equal but
-    differ in ``fold`` would order differently in memory yet identically after a
-    store->replay round-trip, flipping the survivor (cross-model review round 3).
-    ``mode="json"`` renders exactly the stored form, so before == after.
+    ``model_dump()``/``repr``: the survivor must be the one a replay would pick, and
+    replay reconstructs documents from the ledger's JSON. The Python form carries
+    distinctions the persisted form drops -- notably ``datetime.fold``, absent from
+    ``isoformat`` -- so two timestamps that are equal but differ in ``fold`` would
+    order differently in memory yet identically after a store->replay round-trip,
+    flipping the survivor (cross-model review round 3). ``mode="json"`` renders
+    exactly the stored form, so before == after.
 
     ``ensure_ascii=True`` escapes lone surrogates (a schema-valid text field may
     hold ``"\\ud800"`` from provider JSON) instead of UTF-8-encoding them, so it
     does not raise the way plain ``model_dump_json()`` did (round 2).
-    ``sort_keys``/``separators`` make the string canonical. It is total (equal
-    canonical JSON <=> equal persisted content, where the choice is immaterial),
-    comparison never raises (plain ASCII), and it is used only as an internal sort
-    key -- never in a message -- so nothing input-derived can leak.
+    ``sort_keys``/``separators`` make the string canonical. Comparison never raises
+    (plain ASCII), and the key is used only as an internal sort key -- never in a
+    message -- so nothing input-derived can leak.
+
+    The order is total over **persisted forms**, deliberately not over in-memory
+    string identity: two documents whose canonical JSON is identical compare equal
+    here, because the ledger stores them as one row and a replay cannot tell them
+    apart. The clearest case is an astral scalar and its UTF-16 surrogate-pair
+    spelling (``"\\U0001f600"`` vs ``"\\ud83d\\ude00"``): distinct Python strings,
+    but ``json.loads`` recombines the pair, so both persist and replay as the one
+    scalar. Keying them equal is therefore replay-*correct* -- the survivor's
+    persisted form is input-order-invariant even where the in-memory object is not.
+    Making the key injective over in-memory identity (e.g. surrogatepass bytes)
+    would instead diverge it from the stored form and reopen the round-3 bug
+    (cross-model review round 4, resolved by keeping the persisted-form key).
     """
     return (
         _PROVENANCE_RANK[document.provenance],
diff --git a/tests/unit/test_dedup_freshness.py b/tests/unit/test_dedup_freshness.py
index 51505b1..46f7e11 100644
--- a/tests/unit/test_dedup_freshness.py
+++ b/tests/unit/test_dedup_freshness.py
@@ -5,6 +5,7 @@ the stronger provenance."""
 
 from __future__ import annotations
 
+import json
 from datetime import datetime, timezone
 
 import pytest
@@ -235,6 +236,14 @@ def _hash(text: str) -> str:
     return content_sha256(text)
 
 
+def _replayed(doc: ResearchDocument) -> ResearchDocument:
+    # Cross a REAL json.dumps -> json.loads text boundary, exactly as the ledger
+    # stores and a replay reconstructs -- not a bare model_dump(mode="json") handoff,
+    # which skips JSON encoding and so misses the normalization the boundary imposes.
+    text = json.dumps(doc.model_dump(mode="json"), ensure_ascii=True)
+    return validate_document(json.loads(text))
+
+
 def test_identical_artifacts_collapse() -> None:
     body = _hash("payrolls rose")
     a = _document(content_sha256=body)
@@ -321,16 +330,12 @@ def test_dedup_survivor_is_replay_stable() -> None:
     def survivor(docs: list[ResearchDocument]) -> str | None:
         return deduplicate(docs).documents[0].snippet
 
-    def replayed(doc: ResearchDocument) -> ResearchDocument:
-        # Round-trip through the persisted JSON form, as the ledger + replay would.
-        return validate_document(doc.model_dump(mode="json"))
-
     before = survivor([a, b])
-    after = survivor([replayed(a), replayed(b)])
+    after = survivor([_replayed(a), _replayed(b)])
     assert before == after
     # And it stays order-independent both before and after persistence.
     assert survivor([b, a]) == before
-    assert survivor([replayed(b), replayed(a)]) == after
+    assert survivor([_replayed(b), _replayed(a)]) == after
 
 
 def test_dedup_tiebreak_is_surrogate_safe() -> None:
@@ -344,3 +349,21 @@ def test_dedup_tiebreak_is_surrogate_safe() -> None:
     backward = deduplicate([b, a])
     assert len(forward.documents) == 1
     assert forward.documents[0].title == backward.documents[0].title
+
+
+def test_json_equivalent_titles_collapse_and_persist_identically() -> None:
+    # An astral scalar and its UTF-16 surrogate-pair spelling are distinct Python
+    # strings but the same persisted document: json.loads recombines the pair, so
+    # both round-trip to the one scalar. The tiebreak keys on the persisted form, so
+    # they collide -- correctly -- and whichever the collapse returns, its persisted
+    # form is the same in either input order (replay-stable). Injectivity over
+    # in-memory identity is deliberately not a goal; matching persisted identity is.
+    body = _hash("astral scalar vs its surrogate-pair spelling")
+    astral = _document(title=chr(0x1F600), content_sha256=body)
+    pair = _document(title=chr(0xD83D) + chr(0xDE00), content_sha256=body)
+    assert astral.title != pair.title  # distinct in memory
+    assert len(deduplicate([astral, pair]).documents) == 1  # collide by design
+
+    forward = deduplicate([astral, pair]).documents[0]
+    backward = deduplicate([pair, astral]).documents[0]
+    assert _replayed(forward).title == _replayed(backward).title == chr(0x1F600)
```
