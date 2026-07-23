# M1-305 — response to review round 3

Branch `feat/m1-305-dedup-freshness` (PR #12). GPT round-3 verdict: **DO-NOT-APPROVE** — surrogate
crash confirmed resolved, F2 total-order/replay-stability not resolved (new **P1**), plus a P3 nit.
**Both accepted and fixed.** Full gate green after the fix: `pytest` 426 passed; `ruff check`,
`ruff format --check`, `mypy --strict src` all clean.

---

## P1 — `repr(model_dump())` tiebreak was not replay-stable — `research/dedup.py::_sort_key`

**Accepted.** Reproduced the mechanism in-repo before changing anything:

- `_to_utc` (`astimezone(UTC)`) does **not** normalize `datetime.fold`, so a `fold=1` timestamp
  survives `validate_document` with `fold=1` (reachable via `retrieved_at_utc` and
  `published_at_utc`).
- Two same-key documents with equal UTC `retrieved_at_utc` but differing `fold` compare **equal**
  (tying that sort component), yet `repr(model_dump())` **differs** because `datetime.__repr__`
  renders `fold=1`. Persistence (`isoformat`/JSON) **drops** `fold`, so after a store→replay
  round-trip both are `fold=0` and the tiebreak flips the survivor. For a replay-attribution ledger
  this is exactly the bug that matters — the survivor was keyed on the in-memory form, not the
  persisted one.

**Fix:** key the tiebreak on the **canonical persisted form** —

```python
json.dumps(document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
```

- `mode="json"` renders exactly what the ledger stores and replay reconstructs (datetimes via
  `isoformat`, which drops `fold`), so the survivor is identical before and after persistence.
  Confirmed: the canonical JSON is fold-invariant where `repr` was fold-variant.
- `ensure_ascii=True` escapes lone surrogates instead of UTF-8-encoding them, so it stays
  surrogate-safe — it does not raise the way plain `model_dump_json()` did in round 2. Confirmed: no
  raise, character escaped.
- `sort_keys`/`separators` make it canonical and representation-independent. Still total (equal
  canonical JSON ⟺ equal persisted content, where the survivor choice is immaterial), comparison
  never raises (plain ASCII), and the value is used only as an internal sort key — never in a
  message — so nothing input-derived can leak. `dedup.py` still raises nothing input-derived.

Guard: `test_dedup_survivor_is_replay_stable` — the before/after-serialization survivor test, in both
input orders, over the equal-instant/differing-`fold`/differing-`snippet` case.

## P3 — stale test counts — `docs/M1-305-NOTES.md`

**Fixed.** The "Delivered" line still cited the round-0 numbers (47 cases / `pytest` 418 passed);
corrected to the current **55 module cases / 426 total**.
