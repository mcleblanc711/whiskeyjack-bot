# M1-201 — response to review round 1

Commit: `5f21a5a` on `feat/m1-201-canonical-question-model`.

Five of six findings accepted and fixed. One High is **partially rejected** — the concern
behind it is valid and the recoverable half is fixed, but the remedy as specified cannot be
built. Details below.

I reproduced all four mechanical claims against the pinned SDK before changing anything.
Every one behaved exactly as described.

---

## Accepted

### High — unsupported type tags (`normalize.py`)

Both halves confirmed, and they were separate defects:

- `question_type = []` raised `TypeError: unhashable type: 'list'` out of the
  `question_type not in _SUPPORTED_TYPES` test itself — before the `try`, so it escaped the
  error boundary entirely. Fixed by testing `isinstance(question_type, str)` first.
- `question_type = "privateFAKE123456"` was reproduced verbatim in the message. The tag is
  now named only when it is a member of `_KNOWN_SDK_TYPES`, derived via `get_args` from the
  SDK's own `QuestionBasicType`; anything else renders as `'unknown'`.

The second fix is the one that matters for the contract: the docstring asserted the tag "is a
fixed SDK enum value, not stored content," which was an assumption about the caller rather
than something the code enforced. It is now enforced.

Note the `!r` on a known tag is retained deliberately — `'conditional'` is a fixed enum value
and naming it is what makes the error actionable.

Tests: the parametrize table now includes `[]`, `{}`, `["binary"]`, `{"binary": 1}`, plus a
dedicated test asserting a known tag *is* named and a foreign tag is not (checked against both
`str(exc)` and the rendered traceback, matching the existing planted-secret pattern).

### Medium — non-finite floats

Confirmed exactly, including the mechanism: NaN passes `_bounds_ordered` because both ordering
comparisons are false, `model_dump_json()` writes `null`, and the adapter then rejects its own
output. `+inf` on `upper_bound` behaves the same way.

Fixed with a single alias applied to every canonical float — `lower_bound`, `upper_bound`,
`zero_point`, `nominal_lower_bound`, `nominal_upper_bound`, `question_weight`:

```python
_Finite = Annotated[float, Field(allow_inf_nan=False)]
```

Tests: NaN/±inf rejected across the full cross-product of non-finite value × float field, plus
a positive test that a fully-populated numeric question survives `model_dump_json()` →
`validate_json()`.

### Medium — malformed option sets

Confirmed: `["A","A",""]` and `["A"]` both validated. Now `min_length=2` plus an after-validator
rejecting blank/whitespace-only labels and duplicates. Messages state *that* there is a blank or
duplicate, not which — option labels are record content and fall under the no-echo rule (there
is now a test for that specifically).

I took the strict reading on single-option sets: a one-option multiple-choice question cannot be
meaningfully forecast, and the project's posture elsewhere is to fail loudly on malformed
records. Flagging it as a judgment call in case you disagree — the argument against is that it
hard-fails a real MiniBench question we didn't anticipate.

### Low — catch boundary

Agreed, and this was the finding I'd most have wanted caught. Field reads are now fenced in
their own `try/except (AttributeError, TypeError)`; construction happens outside it under the
`ValidationError` boundary only. The type-specific reads (`q.options`, `q.lower_bound`, …) moved
inside the field-read fence too, not just `_common_fields`.

Verified by monkeypatching `CanonicalBinaryQuestion` to raise `TypeError` — it now surfaces as
`TypeError`, where previously it was reported as a malformed input record.

### Low — vacuous group-linkage test

Correct, and the same vacuity would have hit the new category work: every repo fixture has a
null group parent *and* an empty category list, so any fixture-driven check passes against a
hardcoded constant. The test now uses synthetic non-null linkage
(`group_question_option="Above 3%"`, `question_ids_of_group=[91001, 91002, 91003]`), and the new
category tests are synthetic for the same reason.

---

## Partially rejected — "canonical schema cannot carry a source-backed question domain"

**The finding is right that data is being lost. It is wrong about what data exists.**

No SDK question class has a domain field. I checked all six in the pinned
`forecasting_tools==0.2.92`. The only domain-shaped field anywhere is
`MetaculusQuestion.categories: list[Category]`, populated from
`post_api_json["projects"]["category"]`.

Meanwhile this project's domain taxonomy is defined in `config/x_accounts.yaml` —
`econ_data`, `monetary_policy`, `space_launch`, `health`, … — and that file's own header calls
it "the canonical set for question-domain matching." There is no mapping from Metaculus
categories to those tags anywhere in the repo, and inventing one is M1-307's job, not this
slice's. So "a source-backed domain field" is not available to be mapped: any `domain` value
M1-201 emitted would be a *derived classification*, which is exactly the "unplanned parallel
contract" the finding warns against.

On the spec grounds: no backlog item assigns a domain to a question. The only spec text is one
bullet — "domain and question-type tags" — in the **forecast record** field list in
`CODEX_HANDOFF.md`, and that record is M1-602's deliverable. M1-201's acceptance criterion is
"golden binary, multiple-choice and numeric fixtures validate and retain resolution fine print."

**What I did accept:** `normalize.py` is the single place SDK field names are read, so anything
dropped there is unrecoverable downstream without a re-fetch. Dropping `categories` there means
the domain information has no path into the pipeline at all — that part of the finding stands.

So the canonical base now carries:

```python
source_categories: list[str] = Field(default_factory=list)
```

Uninterpreted passthrough, `c.slug or c.name` per entry (`Category.slug` is optional; `name` is
required). Named `source_*` deliberately, with a field comment stating it is **not** the
project's domain tag, so a later reader doesn't wire it into allowlist matching by mistake.
Domain *classification* stays with M1-307 / M1-602, which now have source data to classify from.

If you think M1-201 should additionally emit a derived `domain`, say so explicitly and name the
mapping source — I'd want that written down as a decision before building it, because it commits
the project to a Metaculus-category → x_accounts-taxonomy mapping that nothing currently defines.

---

## Verification

```
173 passed  (was 138; 69 in tests/unit/test_questions.py, was 34)
ruff check .          All checks passed
ruff format --check   25 files already formatted
mypy --strict src     Success: no issues found in 14 source files
git diff --check      clean
```

All four original reproductions re-run and confirmed closed; the internal-`TypeError`
mislabeling checked by monkeypatch as described above.

**One caveat worth your attention on re-review:** `pyproject.toml` sets
`follow_imports = "skip"` for `forecasting_tools.*` (the SDK ships no `py.typed`), so
`mypy --strict` cannot check *any* `q.<field>` read in `normalize.py` — including the new
`q.categories`. The clean mypy run is not evidence about SDK field names; the tests are. Your
round-1 note confirming the mapped fields against the installed SDK was the useful check there,
and `categories` is the one new read to verify the same way.

---

## Requested for round 2

Scope boundary for this slice, so we don't re-litigate: M1-201 is the canonical schema plus the
SDK mapping. Group *unpacking* is M1-202, the logged diagnostic event for refusals is M1-203,
the comprehensive golden fixture set is Codex's T-901, cdf point-count enforcement is M1-503,
and domain classification is M1-307/M1-602.

Please focus on: whether `source_categories` is the right shape for the passthrough (slug-or-name
vs. carrying the full `Category` objects), whether the `min_length=2` strictness on options is
correct or over-tight, and anything in the reordered `normalize_question` control flow that the
split boundaries got wrong.
