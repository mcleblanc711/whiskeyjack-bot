# M1-305 — response to review round 2

Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-2 verdict: **DO-NOT-APPROVE** — F1 and F3
confirmed resolved, F2 not resolved, one new **P2**. **Accepted and fixed.** Full gate green after
the fix: `pytest` 425 passed; `ruff check`, `ruff format --check`, `mypy --strict src` all clean.

---

## F1 — RESOLVED (confirmed). F3 — RESOLVED (confirmed).

No change. Thanks for re-verifying the 3-tuple key against the SQLite UNIQUE and the
`?utm_source=x&&b=2` → `?&b=2` / idempotence behaviour.

## F2 follow-on (P2) — serialization tiebreak raised on a lone surrogate — `research/dedup.py::_sort_key`

**Accepted.** Reproduced exactly in-repo before changing anything:

- `validate_document({… "title": "\ud800"})` is **accepted** — pydantic's `str` admits unpaired
  surrogates.
- `doc.model_dump_json()` **raises** `PydanticSerializationError: … 'utf-8' codec …`, so deduplicating
  two such same-key documents raised, and the message carried the offending character — the total
  order was not total, and it breached error hygiene. Provider JSON with an escaped lone surrogate is
  a realistic source.

**Fix:** the tiebreak is now `repr(document.model_dump())` instead of `document.model_dump_json()`:

- `model_dump()` (Python mode) returns Python objects and **never UTF-8-encodes**, so it cannot raise
  on a surrogate.
- `repr(...)` is deterministic (pydantic v2 dumps fields in definition order; datetimes/`None`/
  `Literal`s `repr` stably) and renders surrogates **escaped**, so the key is a plain comparable
  `str`.
- It is total on distinct-content documents (equal `repr` ⟺ equal `model_dump` ⟺ same content, where
  the survivor choice is immaterial), and comparing two such strings never raises (Python orders by
  code point; surrogate code points compare fine).
- It is used **only** as an internal sort key — never placed in an exception message — so no
  input-derived value can leak. After this change `dedup.py` raises nothing input-derived at all
  (`_PROVENANCE_RANK[…]` is keyed by a validated `Literal`; datetime comparison cannot raise).

The human-meaningful ordering (stronger provenance, then earliest `retrieved_at_utc`) stays ahead of
the tiebreak, so the round-1 provenance and earliest-retrieval tests are unchanged and still pass.

**Not taken:** rejecting unpaired surrogates at schema validation. That broadens the change into the
frozen `model.py` and its text/URL fields for a problem that is entirely dedup-local; the schema
intentionally accepts arbitrary text, so the dedup primitive is what must be robust to it.

Guard: `test_dedup_tiebreak_is_surrogate_safe` — two same-key documents whose `title` holds a lone
surrogate → `deduplicate` does not raise, collapses to one, and picks the same survivor on reversed
input.
