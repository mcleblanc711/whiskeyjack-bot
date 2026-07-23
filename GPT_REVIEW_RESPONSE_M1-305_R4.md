# M1-305 — response to review round 4

Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-4 verdict: **DO-NOT-APPROVE** — round-3
replay-stability (fold) confirmed resolved; one new **P2** (tiebreak not injective over in-memory
strings). **The finding is accurate, but its suggested fix is declined with evidence** (owner
decision); the valid procedural half — the replay test was too weak — is accepted and fixed. Gate
green: `pytest` 427 passed; `ruff check`, `ruff format --check`, `mypy --strict src` all clean.

---

## P2 — injectivity over in-memory strings — finding accepted, suggested fix **declined**

**The observation is correct.** An astral scalar `chr(0x1F600)` (len 1) and its UTF-16 surrogate-pair
spelling `chr(0xD83D)+chr(0xDE00)` (len 2) are distinct Python strings, both validate, and both
serialize to the same `ensure_ascii=True` canonical JSON — so they collide and the *in-memory*
returned survivor is input-order-dependent.

**The suggested fix — `ensure_ascii=False` + `.encode("utf-8", "surrogatepass")` — is declined,
because it is replay-unstable**, which is precisely the property round 3 required. Verified in-repo:

- After a **real** `json.dumps → json.loads` round-trip (what the ledger does when it stores and a
  replay reconstructs), **both documents collapse to the single scalar U+1F600** — `json.loads`
  recombines the surrogate pair. They are the *same persisted row*.
- `gpt_key(pair)` **differs before vs after** that round-trip (`before == after` → `False`). So the
  injective key distinguishes two documents the ledger stores identically, and the survivor's key in
  memory would not match its key after persistence — reopening the round-3 bug (key ≠ persisted
  form) in a new guise.

The tiebreak deliberately orders **persisted forms, not in-memory string identity**. For a replay
ledger that is the correct granularity: two documents the canonical JSON cannot tell apart *are* one
row, so keying them equal is replay-correct, and the surviving document's **persisted** form is
input-order-invariant — confirmed in both input orders. Injectivity over in-memory identity is not a
goal; matching persisted identity is.

Two further points on scope/reachability:

- A *separated* surrogate pair cannot arise from real provider JSON — `json.loads` always recombines
  `😀` into U+1F600 — so the input is schema-valid but unreachable through the retrieval
  path. (A *lone* surrogate, the round-2 case, is reachable and is handled: `ensure_ascii=True`
  escapes it without raising.)
- The genuine root-cause question — whether text fields should admit JSON-problematic scalars at all
  — belongs to input normalization at the writer (M1-602) or the schema (`model.py`), not to this
  dedup primitive. M1-305's tiebreak owns replay-stable determinism, and it has that.

**Docstring updated** to state the contract explicitly: total over persisted forms, deliberately not
over in-memory identity, with the astral/surrogate example and why the injective alternative is
wrong.

## Accepted — the replay test was too weak

Correct: the round-3 test used a bare `validate_document(doc.model_dump(mode="json"))` handoff, which
skips JSON *text* encoding. The `_replayed` helper now crosses a real
`json.dumps → json.loads → validate_document` boundary, so the fold test exercises actual persistence
normalization, and a new test — `test_json_equivalent_titles_collapse_and_persist_identically` —
pins that the astral/surrogate pair collapse and persist to the identical document in either input
order.
