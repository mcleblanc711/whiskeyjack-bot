# Cross-model review request — whiskeyjack-bot M1-201, round 2

You are a rigorous senior reviewer performing an independent cross-model review of code
authored by another AI model (Claude). Apply the **stricter reading**: when a line could be
read as either correct or subtly wrong, assume the wrong reading and prove it can't happen
from the diff. Do **not** rubber-stamp.

This is a **re-review of fixes**, not a fresh review. You issued DO-NOT-APPROVE on this branch
with 4 must-fix and 2 optional findings. Five were accepted and fixed; one High is **partially
rejected** with reasoning you should engage with directly rather than restate. The diff below
is round 1 only (`git diff <round-1-base>..HEAD`) — the original branch diff you already
reviewed is unchanged beneath it.

A re-review has a specific failure mode: confirming that each fix "looks right" without
checking whether it actually closes the failure scenario you originally described, or whether
it opened a new one. Weight your effort accordingly.

## Project context

`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary product is
an **attribution ledger**: an immutable, replayable SQLite record of every forecast, its
evidence, approvals, submission attempts, resolutions and scores. Python 3.11, `src/` layout,
offline-first (tests run with sockets disabled), toolchain gates are `pytest`, `ruff check`,
`ruff format --check`, `mypy --strict src`.

**M1-201** is the canonical internal question schema plus the mapping onto it from the pinned
`forecasting-tools==0.2.92` models. Acceptance: "Golden binary, multiple-choice and numeric
fixtures validate and retain resolution fine print."

## What changed, and what you should verify about each

Every mechanical claim in your review was reproduced against the pinned SDK before any change
was made. All four reproduced exactly as you described.

### 1. Unsupported type tags (your High) — `normalize.py`

You identified two distinct defects in one expression. Both confirmed:

- `question_type = []` raised `TypeError: unhashable type: 'list'` from the
  `not in _SUPPORTED_TYPES` test itself — *before* the `try` block, so it escaped the error
  boundary entirely.
- `question_type = "privateFAKE123456"` was reproduced verbatim in the message.

Fix: `isinstance(question_type, str)` is now tested first, and the tag is named only when it is
a member of `_KNOWN_SDK_TYPES` (derived via `get_args` from the SDK's own `QuestionBasicType`);
anything else renders as `'unknown'`.

**Verify:** is there any remaining path where a non-`str` reaches a hash-requiring operation?
Note the `tag = ...` expression re-tests `isinstance` — confirm that is actually necessary
(it is entered with a non-`str` still live) and not redundant defensive noise. Is deriving
`_KNOWN_SDK_TYPES` from the SDK's `QuestionBasicType` sound, or does it couple our error
hygiene to an SDK enum that could gain a member and silently widen what we echo?

### 2. Non-finite floats (your Medium) — `model.py`

Confirmed including your stated mechanism: NaN passes `_bounds_ordered` because both ordering
comparisons are false, `model_dump_json()` writes `null`, and the adapter then rejects its own
output. `+inf` on `upper_bound` behaves identically.

Fix: `_Finite = Annotated[float, Field(allow_inf_nan=False)]` applied to `lower_bound`,
`upper_bound`, `zero_point`, `nominal_lower_bound`, `nominal_upper_bound`, `question_weight`.

**Verify:** is that the complete set of float fields — including any reachable via the union
adapter that I missed? Does `allow_inf_nan=False` behave as expected on the `_Finite | None`
optionals specifically? And does the round-trip claim now hold for *all* float values that pass
validation, or only the ones tested (e.g. very large finite floats, denormals, `-0.0`)?

### 3. Malformed option sets (your Medium) — `model.py`

Confirmed: `["A","A",""]` and `["A"]` both validated under `min_length=1`.

Fix: `min_length=2` plus an after-validator rejecting blank/whitespace-only labels and
duplicates, with messages that don't reprint the labels.

**Verify:** duplicate detection uses exact string equality via `set()`. Is that the right
equivalence for M1-404's "every exact option once", or should it be
whitespace-normalized/case-folded — and if so, does normalizing there create a *different*
ambiguity when the normalized forms collide but the exact labels differ? Also: `min_length=2`
is a judgment call flagged below; assess it as a correctness question, not a style one.

### 4. Catch boundary (your Low) — `normalize.py`

Agreed. SDK field reads are now fenced in their own `try/except (AttributeError, TypeError)`;
model construction happens outside that fence, under the `ValidationError` boundary only. The
type-specific reads (`q.options`, `q.lower_bound`, …) moved inside the field-read fence too.

Verified by monkeypatching `CanonicalBinaryQuestion` to raise `TypeError` — it now surfaces as
`TypeError` where previously it was reported as a malformed input record.

**Verify:** the control flow was restructured from `if/return` inside one `try` to a build-dict
-then-construct shape. That is the change most likely to have introduced a new defect. Check
specifically: can `fields` now reach a constructor carrying keys the target model doesn't
accept (the models are `extra="forbid"`)? Is the `elif` chain in the field-read block exhaustive
against the dispatch chain in the construction block — could the two disagree if a fourth
supported type is added to `config.SupportedQuestionType`? Is the trailing `AssertionError`
still genuinely unreachable?

### 5. Vacuous group-linkage test (your Low) — `test_questions.py`

Correct. Every repo fixture has a null group parent, so the old check passed against a mapped
constant `None`. Rebuilt on synthetic non-null linkage.

**Verify:** the same vacuity class elsewhere in the suite — are there other tests that would
still pass if the field under test were replaced with a constant?

## The disputed finding — read this before re-raising it

Your second High said the canonical schema "cannot carry a source-backed question domain" and
that SDK categories are discarded. **The data-loss half is accepted and fixed. The
"source-backed domain" half is rejected on the following grounds — engage with these
specifically rather than restating the finding.**

- No `forecasting_tools` question class has a domain field. All six checked at the pinned
  0.2.92. The only domain-shaped field anywhere is `MetaculusQuestion.categories:
  list[Category]`, populated from `post_api_json["projects"]["category"]`.
- This project's domain taxonomy is a **different vocabulary**, defined in
  `config/x_accounts.yaml` (`econ_data`, `monetary_policy`, `space_launch`, `health`, …),
  whose own header calls it "the canonical set for question-domain matching."
- **No mapping exists between the two**, and nothing in the repo defines one. So any `domain`
  value M1-201 emitted would be a *derived classification* — which is precisely the "unplanned
  parallel contract" your finding warns against.
- No backlog item assigns a domain to a *question*. The only spec text is one bullet — "domain
  and question-type tags" — in the **forecast record** field list in `CODEX_HANDOFF.md`, and
  that record is M1-602's deliverable, not this slice's.

**What was accepted:** `normalize.py` is the single place SDK field names are read, so anything
dropped there is unrecoverable downstream without a re-fetch. The canonical base now carries
`source_categories: list[str]` (`c.slug or c.name` per entry — `Category.slug` is optional,
`name` is required), uninterpreted, named `source_*` so it is not mistaken for the project's
domain tag.

If you still believe M1-201 must emit a derived `domain`, **name the mapping source**. Asserting
the requirement again without one is not actionable — it commits the project to a
Metaculus-category → x_accounts-taxonomy mapping that nothing currently defines, and that
decision should be written down before it is built.

## Open questions I want your judgment on

These are genuine judgment calls, not rhetorical. Disagreement here is useful.

1. **`source_categories` shape.** Slug-or-name strings, or the full SDK `Category` objects
   (`id`, `name`, `slug`, `emoji`, `description`)? Strings are a stable internal contract and
   keep the schema decoupled from the SDK — the whole point of the slice — but the `id` is the
   only stable identifier if slugs ever change, and `slug or name` silently mixes two
   namespaces in one list. Which is the lesser error?
2. **`min_length=2` on multiple-choice options.** The strict reading: a one-option
   multiple-choice question cannot be meaningfully forecast, and the project's posture is to
   fail loudly on malformed records. The risk: it hard-fails a real MiniBench question we
   didn't anticipate, and a normalization refusal is more disruptive than a downstream
   validation failure. Is the strictness correct?
3. **Fixture coverage.** All three repo fixtures have empty category lists and null group
   parents, so both the new passthrough and the rebuilt linkage test are pinned by synthetic
   objects. Is synthetic coverage adequate here, or does this need real fixtures before merge
   (noting the comprehensive golden set is explicitly Codex's T-901, not this slice)?

## Scope boundary (do not re-raise as findings)

Group *unpacking* is M1-202. The logged diagnostic event for refusals is M1-203. The
comprehensive valid/invalid golden fixture set is Codex's T-901. `cdf_size` point-count
enforcement is M1-503. Domain classification is M1-307/M1-602. Community prediction,
previous forecasts and raw `api_json` are deliberately excluded from the canonical schema.

## A verification caveat that matters

`pyproject.toml` sets `follow_imports = "skip"` for `forecasting_tools.*` (the SDK ships no
`py.typed`), so **`mypy --strict` cannot type-check any `q.<field>` read in `normalize.py`** —
including the new `q.categories`. The clean mypy run is not evidence about SDK field names.

Your round-1 verification against the installed SDK was the useful check there. `q.categories`,
`Category.slug` and `Category.name` are the new reads; please verify them the same way rather
than trusting the type checker's silence.

## Gates

```
173 passed  (was 138; 69 in tests/unit/test_questions.py, was 34)
ruff check .          All checks passed
ruff format --check   25 files already formatted
mypy --strict src     Success: no issues found in 14 source files
git diff --check      clean
```

## Output format

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Per-fix status** for items 1–5 above: does the fix actually close the failure scenario from
  your round-1 finding? CLOSED / PARTIALLY CLOSED / NOT CLOSED, one line each.
- **New findings** introduced by the fixes, ranked (Blocker / High / Medium / Low / Nit), each
  with `file:line`, a one-line defect statement, and a concrete failure scenario (inputs →
  wrong outcome). Separate must-fix from optional. A regression introduced by a fix outranks a
  pre-existing nit — say so if you find one.
- **Your position on the disputed domain finding**, addressing the four grounds above. If you
  maintain it, name the mapping source.
- **Answers to the three open questions**, with reasoning.
- Explicitly note anything you **cannot** verify from the diff alone, in particular claims
  about the pinned SDK's behaviour.

The round-1 diff (`git diff 0571284..HEAD`) follows. `GPT_REVIEW_RESPONSE_M1-201_R1.md` is
included in it and restates much of the above — skip it if you've read this far.

---

```diff
diff --git a/GPT_REVIEW_RESPONSE_M1-201_R1.md b/GPT_REVIEW_RESPONSE_M1-201_R1.md
new file mode 100644
index 0000000..957d586
--- /dev/null
+++ b/GPT_REVIEW_RESPONSE_M1-201_R1.md
@@ -0,0 +1,162 @@
+# M1-201 — response to review round 1
+
+Commit: `5f21a5a` on `feat/m1-201-canonical-question-model`.
+
+Five of six findings accepted and fixed. One High is **partially rejected** — the concern
+behind it is valid and the recoverable half is fixed, but the remedy as specified cannot be
+built. Details below.
+
+I reproduced all four mechanical claims against the pinned SDK before changing anything.
+Every one behaved exactly as described.
+
+---
+
+## Accepted
+
+### High — unsupported type tags (`normalize.py`)
+
+Both halves confirmed, and they were separate defects:
+
+- `question_type = []` raised `TypeError: unhashable type: 'list'` out of the
+  `question_type not in _SUPPORTED_TYPES` test itself — before the `try`, so it escaped the
+  error boundary entirely. Fixed by testing `isinstance(question_type, str)` first.
+- `question_type = "privateFAKE123456"` was reproduced verbatim in the message. The tag is
+  now named only when it is a member of `_KNOWN_SDK_TYPES`, derived via `get_args` from the
+  SDK's own `QuestionBasicType`; anything else renders as `'unknown'`.
+
+The second fix is the one that matters for the contract: the docstring asserted the tag "is a
+fixed SDK enum value, not stored content," which was an assumption about the caller rather
+than something the code enforced. It is now enforced.
+
+Note the `!r` on a known tag is retained deliberately — `'conditional'` is a fixed enum value
+and naming it is what makes the error actionable.
+
+Tests: the parametrize table now includes `[]`, `{}`, `["binary"]`, `{"binary": 1}`, plus a
+dedicated test asserting a known tag *is* named and a foreign tag is not (checked against both
+`str(exc)` and the rendered traceback, matching the existing planted-secret pattern).
+
+### Medium — non-finite floats
+
+Confirmed exactly, including the mechanism: NaN passes `_bounds_ordered` because both ordering
+comparisons are false, `model_dump_json()` writes `null`, and the adapter then rejects its own
+output. `+inf` on `upper_bound` behaves the same way.
+
+Fixed with a single alias applied to every canonical float — `lower_bound`, `upper_bound`,
+`zero_point`, `nominal_lower_bound`, `nominal_upper_bound`, `question_weight`:
+
+```python
+_Finite = Annotated[float, Field(allow_inf_nan=False)]
+```
+
+Tests: NaN/±inf rejected across the full cross-product of non-finite value × float field, plus
+a positive test that a fully-populated numeric question survives `model_dump_json()` →
+`validate_json()`.
+
+### Medium — malformed option sets
+
+Confirmed: `["A","A",""]` and `["A"]` both validated. Now `min_length=2` plus an after-validator
+rejecting blank/whitespace-only labels and duplicates. Messages state *that* there is a blank or
+duplicate, not which — option labels are record content and fall under the no-echo rule (there
+is now a test for that specifically).
+
+I took the strict reading on single-option sets: a one-option multiple-choice question cannot be
+meaningfully forecast, and the project's posture elsewhere is to fail loudly on malformed
+records. Flagging it as a judgment call in case you disagree — the argument against is that it
+hard-fails a real MiniBench question we didn't anticipate.
+
+### Low — catch boundary
+
+Agreed, and this was the finding I'd most have wanted caught. Field reads are now fenced in
+their own `try/except (AttributeError, TypeError)`; construction happens outside it under the
+`ValidationError` boundary only. The type-specific reads (`q.options`, `q.lower_bound`, …) moved
+inside the field-read fence too, not just `_common_fields`.
+
+Verified by monkeypatching `CanonicalBinaryQuestion` to raise `TypeError` — it now surfaces as
+`TypeError`, where previously it was reported as a malformed input record.
+
+### Low — vacuous group-linkage test
+
+Correct, and the same vacuity would have hit the new category work: every repo fixture has a
+null group parent *and* an empty category list, so any fixture-driven check passes against a
+hardcoded constant. The test now uses synthetic non-null linkage
+(`group_question_option="Above 3%"`, `question_ids_of_group=[91001, 91002, 91003]`), and the new
+category tests are synthetic for the same reason.
+
+---
+
+## Partially rejected — "canonical schema cannot carry a source-backed question domain"
+
+**The finding is right that data is being lost. It is wrong about what data exists.**
+
+No SDK question class has a domain field. I checked all six in the pinned
+`forecasting_tools==0.2.92`. The only domain-shaped field anywhere is
+`MetaculusQuestion.categories: list[Category]`, populated from
+`post_api_json["projects"]["category"]`.
+
+Meanwhile this project's domain taxonomy is defined in `config/x_accounts.yaml` —
+`econ_data`, `monetary_policy`, `space_launch`, `health`, … — and that file's own header calls
+it "the canonical set for question-domain matching." There is no mapping from Metaculus
+categories to those tags anywhere in the repo, and inventing one is M1-307's job, not this
+slice's. So "a source-backed domain field" is not available to be mapped: any `domain` value
+M1-201 emitted would be a *derived classification*, which is exactly the "unplanned parallel
+contract" the finding warns against.
+
+On the spec grounds: no backlog item assigns a domain to a question. The only spec text is one
+bullet — "domain and question-type tags" — in the **forecast record** field list in
+`CODEX_HANDOFF.md`, and that record is M1-602's deliverable. M1-201's acceptance criterion is
+"golden binary, multiple-choice and numeric fixtures validate and retain resolution fine print."
+
+**What I did accept:** `normalize.py` is the single place SDK field names are read, so anything
+dropped there is unrecoverable downstream without a re-fetch. Dropping `categories` there means
+the domain information has no path into the pipeline at all — that part of the finding stands.
+
+So the canonical base now carries:
+
+```python
+source_categories: list[str] = Field(default_factory=list)
+```
+
+Uninterpreted passthrough, `c.slug or c.name` per entry (`Category.slug` is optional; `name` is
+required). Named `source_*` deliberately, with a field comment stating it is **not** the
+project's domain tag, so a later reader doesn't wire it into allowlist matching by mistake.
+Domain *classification* stays with M1-307 / M1-602, which now have source data to classify from.
+
+If you think M1-201 should additionally emit a derived `domain`, say so explicitly and name the
+mapping source — I'd want that written down as a decision before building it, because it commits
+the project to a Metaculus-category → x_accounts-taxonomy mapping that nothing currently defines.
+
+---
+
+## Verification
+
+```
+173 passed  (was 138; 69 in tests/unit/test_questions.py, was 34)
+ruff check .          All checks passed
+ruff format --check   25 files already formatted
+mypy --strict src     Success: no issues found in 14 source files
+git diff --check      clean
+```
+
+All four original reproductions re-run and confirmed closed; the internal-`TypeError`
+mislabeling checked by monkeypatch as described above.
+
+**One caveat worth your attention on re-review:** `pyproject.toml` sets
+`follow_imports = "skip"` for `forecasting_tools.*` (the SDK ships no `py.typed`), so
+`mypy --strict` cannot check *any* `q.<field>` read in `normalize.py` — including the new
+`q.categories`. The clean mypy run is not evidence about SDK field names; the tests are. Your
+round-1 note confirming the mapped fields against the installed SDK was the useful check there,
+and `categories` is the one new read to verify the same way.
+
+---
+
+## Requested for round 2
+
+Scope boundary for this slice, so we don't re-litigate: M1-201 is the canonical schema plus the
+SDK mapping. Group *unpacking* is M1-202, the logged diagnostic event for refusals is M1-203,
+the comprehensive golden fixture set is Codex's T-901, cdf point-count enforcement is M1-503,
+and domain classification is M1-307/M1-602.
+
+Please focus on: whether `source_categories` is the right shape for the passthrough (slug-or-name
+vs. carrying the full `Category` objects), whether the `min_length=2` strictness on options is
+correct or over-tight, and anything in the reordered `normalize_question` control flow that the
+split boundaries got wrong.
diff --git a/docs/M1-NOTES.md b/docs/M1-NOTES.md
index cf9c90b..8ecb8cb 100644
--- a/docs/M1-NOTES.md
+++ b/docs/M1-NOTES.md
@@ -61,7 +61,8 @@ Delivered:
 - `tests/unit/test_questions.py` — 34 tests: per-type mapping, fine-print retention against the raw
   fixtures, MC options, numeric bounds/cdf, group-identity carry-through, union round-trip,
   malformed-record table, and no-leak planted-secret paths. Suite: 138 passed; ruff check +
-  format + `mypy --strict src` clean.
+  format + `mypy --strict src` clean. (GPT review round 1 raised this to 69 module tests /
+  173 suite — see the round-1 section below.)

 Hardening — a question object missing the fields its declared type requires is reported as a
 `NormalizationError`, not a raw `AttributeError`/`TypeError`. This is the same defect class as the
@@ -86,3 +87,45 @@ Deferred (do not read the absence as an omission):
   slice ships only the tests that prove its own model and mapping.
 - `cdf_size` is stored as a plain int. Enforcing the 201-point count (`config.expected_cdf_points`)
   is calibration-time validation, i.e. **M1-503**.
+
+### M1-201 — GPT review round 1
+
+All four mechanical findings reproduced against the pinned SDK and were fixed:
+
+- **Tag handling** (`normalize.py`). Two defects in one expression. An unhashable
+  `question_type` (a list) raised a raw `TypeError` out of the `in _SUPPORTED_TYPES` test,
+  escaping the module's error boundary entirely — `isinstance` is now tested first. And an
+  arbitrary string tag was echoed verbatim, so the tag is now named only when it is a member
+  of `_KNOWN_SDK_TYPES` (derived from the SDK's own `QuestionBasicType`); anything else
+  renders as `'unknown'`. The docstring claim that the tag "is a fixed SDK enum value" is now
+  enforced rather than assumed.
+- **Finite floats** (`model.py`). Pydantic accepts NaN/±inf for a bare `float`; NaN also slips
+  past `_bounds_ordered` because both ordering comparisons are false. `model_dump_json` then
+  writes `null` and the union adapter cannot read the record back — so the round-trip the
+  module advertises was conditionally false. All canonical floats now use
+  `_Finite = Annotated[float, Field(allow_inf_nan=False)]`.
+- **Option-set integrity** (`model.py`). `["A","A",""]` and `["A"]` both validated under the
+  old `min_length=1`. M1-404 must emit "every exact option once with probabilities summing to
+  one", which is unrepresentable when duplicate labels collapse as mapping keys — so the
+  constraint belongs at the input contract, not downstream. Now `min_length=2` plus a
+  validator rejecting blank and duplicate labels (without echoing them).
+- **Catch boundary** (`normalize.py`). One `try` spanned both SDK field reads and canonical
+  model construction, so a future internal `TypeError` in construction would have been
+  reported as a malformed input record. Field reads are now fenced separately; construction
+  errors stay visible.
+
+Decision — **`source_categories` carries the SDK's `categories` slugs through uninterpreted.**
+The review asked for a source-backed *domain* field. No SDK question class has one: the only
+domain-shaped field is `categories: list[Category]`, and this project's domain taxonomy lives
+in `config/x_accounts.yaml` (`econ_data`, `space_launch`, …) with no mechanical mapping from
+Metaculus categories. No backlog item assigns a domain to a *question*; the only spec text is
+one bullet in the downstream **forecast record** list (`CODEX_HANDOFF.md`), owned by M1-602.
+The recoverable half of the concern is real, though — `normalize.py` is the single place SDK
+fields are read, so a field dropped there cannot be recovered downstream without a re-fetch.
+Hence the passthrough, named `source_*` so it is not mistaken for the project's domain tag.
+Deriving an actual domain tag remains **M1-307 / M1-602**.
+
+All three repo fixtures carry an empty category list, so the passthrough is pinned by
+synthetic-object tests rather than fixture assertions — a fixture-driven check would have
+passed against a hardcoded `[]`. The same vacuity affected the group-linkage test the review
+flagged (every fixture has a null group parent); it now uses non-null synthetic linkage.
diff --git a/src/whiskeyjack_bot/questions/model.py b/src/whiskeyjack_bot/questions/model.py
index 3eb92ee..87774aa 100644
--- a/src/whiskeyjack_bot/questions/model.py
+++ b/src/whiskeyjack_bot/questions/model.py
@@ -29,6 +29,11 @@ from pydantic import Field, TypeAdapter, model_validator

 from whiskeyjack_bot.config import SupportedQuestionType, _StrictModel

+# Pydantic accepts NaN and +/-infinity for a bare ``float``, but ``model_dump_json``
+# serializes them as JSON ``null`` -- which then fails to validate back, breaking the
+# round-trip the discriminated union promises. Every canonical float is finite.
+_Finite = Annotated[float, Field(allow_inf_nan=False)]
+

 class _CanonicalQuestionBase(_StrictModel):
     """Fields shared by every supported question type.
@@ -51,7 +56,14 @@ class _CanonicalQuestionBase(_StrictModel):
     close_time: datetime | None = None
     scheduled_resolution_time: datetime | None = None
     tournament_slugs: list[str] = Field(default_factory=list)
-    question_weight: float | None = None
+    question_weight: _Finite | None = None
+    # Uninterpreted passthrough of the SDK's ``categories`` slugs. NOT the project's
+    # domain tag: that taxonomy lives in config/x_accounts.yaml (econ_data,
+    # space_launch, ...) and has no mechanical mapping from Metaculus categories.
+    # Carried here only because normalize.py is the single place SDK fields are read,
+    # so anything dropped here is unrecoverable downstream without a re-fetch.
+    # Deriving a domain tag from this belongs to M1-307 / the forecast record (M1-602).
+    source_categories: list[str] = Field(default_factory=list)
     # Group-parent identity is carried through unchanged so M1-202 can unpack
     # subquestions without losing the parent linkage.
     group_question_option: str | None = None
@@ -64,26 +76,42 @@ class CanonicalBinaryQuestion(_CanonicalQuestionBase):

 class CanonicalMultipleChoiceQuestion(_CanonicalQuestionBase):
     qtype: Literal["multiple_choice"] = "multiple_choice"
-    options: list[str] = Field(min_length=1)
+    # At least two: a one-option multiple-choice question cannot be forecast, and
+    # M1-404 must emit "every exact option once with probabilities summing to one" --
+    # which is unrepresentable if labels collapse as mapping keys. The option set is
+    # therefore constrained here, at the input contract, rather than downstream.
+    options: list[str] = Field(min_length=2)
     option_is_instance_of: str | None = None

+    @model_validator(mode="after")
+    def _options_are_labelled_and_distinct(self) -> CanonicalMultipleChoiceQuestion:
+        # Do not echo the labels: mirror the project-wide rule that a validation
+        # message never reprints record content.
+        if any(not option.strip() for option in self.options):
+            raise ValueError("multiple-choice options must not be blank")
+        if len(set(self.options)) != len(self.options):
+            raise ValueError("multiple-choice options must be distinct")
+        return self
+

 class CanonicalNumericQuestion(_CanonicalQuestionBase):
     qtype: Literal["numeric"] = "numeric"
-    lower_bound: float
-    upper_bound: float
+    lower_bound: _Finite
+    upper_bound: _Finite
     open_lower_bound: bool
     open_upper_bound: bool
-    zero_point: float | None = None
+    zero_point: _Finite | None = None
     # The Metaculus/SDK cdf resolution; agrees with config.expected_cdf_points
     # (Literal[201]). Kept as a plain int here -- calibration-time enforcement
     # of the point count belongs to the validation epic (M1-503), not the model.
     cdf_size: int
-    nominal_lower_bound: float | None = None
-    nominal_upper_bound: float | None = None
+    nominal_lower_bound: _Finite | None = None
+    nominal_upper_bound: _Finite | None = None

     @model_validator(mode="after")
     def _bounds_ordered(self) -> CanonicalNumericQuestion:
+        # NaN is refused by the field's allow_inf_nan=False before this runs, so the
+        # comparison cannot be silently false for a non-finite bound.
         if self.lower_bound >= self.upper_bound:
             # Do not echo the bound values: mirror the project-wide rule that a
             # validation message never reprints record content.
diff --git a/src/whiskeyjack_bot/questions/normalize.py b/src/whiskeyjack_bot/questions/normalize.py
index ece7039..d223099 100644
--- a/src/whiskeyjack_bot/questions/normalize.py
+++ b/src/whiskeyjack_bot/questions/normalize.py
@@ -24,7 +24,7 @@ from __future__ import annotations

 from typing import Any, get_args

-from forecasting_tools.data_models.questions import MetaculusQuestion
+from forecasting_tools.data_models.questions import MetaculusQuestion, QuestionBasicType
 from pydantic import ValidationError

 from whiskeyjack_bot.config import SupportedQuestionType
@@ -38,6 +38,10 @@ from whiskeyjack_bot.questions.model import (
 # Derived from the single source of truth in config (D20), so adding a type
 # there cannot leave this dispatch silently out of step.
 _SUPPORTED_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))
+# The SDK's own six-value tag enum. Only a member of this set is safe to name in an
+# error message; any other value reached the tag slot from outside the SDK's own
+# models and is therefore unvetted content under the no-echo rule.
+_KNOWN_SDK_TYPES: frozenset[str] = frozenset(get_args(QuestionBasicType))


 class NormalizationError(Exception):
@@ -53,9 +57,10 @@ class NormalizationError(Exception):
 class UnsupportedQuestionTypeError(NormalizationError):
     """The question is a type deferred in v1 (date/conditional/discrete, D21).

-    Raised before any model or submission call is made. The ``question_type``
-    tag it carries is a fixed SDK enum value, not stored content, so naming it
-    is safe under the no-echo rule.
+    Raised before any model or submission call is made. The message names the
+    ``question_type`` tag only when it is one of the SDK's own enum values
+    (``_KNOWN_SDK_TYPES``); anything else renders as ``'unknown'``, since an
+    arbitrary value in that slot is unvetted content under the no-echo rule.
     """


@@ -86,6 +91,9 @@ def _common_fields(q: MetaculusQuestion) -> dict[str, Any]:
         "scheduled_resolution_time": q.scheduled_resolution_time,
         "tournament_slugs": q.tournament_slugs,
         "question_weight": q.question_weight,
+        # Slug where the SDK supplies one (it is optional on Category), else the
+        # required name. Carried uninterpreted -- see the field comment in model.py.
+        "source_categories": [category.slug or category.name for category in q.categories],
         "group_question_option": q.group_question_option,
         "question_ids_of_group": q.question_ids_of_group,
     }
@@ -98,27 +106,32 @@ def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
     :class:`NormalizationError` if a supported type fails canonical validation.
     """
     question_type = getattr(q, "question_type", None)
-    if question_type not in _SUPPORTED_TYPES:
+    # isinstance before membership: an unhashable tag (a list, say) would raise a raw
+    # TypeError out of the frozenset test itself, escaping the boundary below.
+    if not isinstance(question_type, str) or question_type not in _SUPPORTED_TYPES:
         # Refused before any field is read, so an unsupported type can never
-        # reach a model or submission call (D21). Only the fixed type tag is
-        # named -- it is an SDK enum value, not stored content.
-        tag = question_type if isinstance(question_type, str) and question_type else "unknown"
+        # reach a model or submission call (D21).
+        tag = (
+            question_type
+            if isinstance(question_type, str) and question_type in _KNOWN_SDK_TYPES
+            else "unknown"
+        )
         raise UnsupportedQuestionTypeError(
             f"question type {tag!r} is not supported in v1 (binary, multiple_choice, numeric only)"
         )
+
+    # Field reads are fenced separately from model construction: a TypeError raised
+    # while building a canonical model is our bug, and must stay visible rather than
+    # being reported as a malformed input record.
     try:
-        common = _common_fields(q)
-        if question_type == "binary":
-            return CanonicalBinaryQuestion(**common)
+        fields = _common_fields(q)
         if question_type == "multiple_choice":
-            return CanonicalMultipleChoiceQuestion(
-                **common,
+            fields.update(
                 options=q.options,
                 option_is_instance_of=q.option_is_instance_of,
             )
-        if question_type == "numeric":
-            return CanonicalNumericQuestion(
-                **common,
+        elif question_type == "numeric":
+            fields.update(
                 lower_bound=q.lower_bound,
                 upper_bound=q.upper_bound,
                 open_lower_bound=q.open_lower_bound,
@@ -128,9 +141,6 @@ def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
                 nominal_lower_bound=q.nominal_lower_bound,
                 nominal_upper_bound=q.nominal_upper_bound,
             )
-    except ValidationError as exc:
-        # from None: the ValidationError text echoes the offending input values.
-        raise _sanitize(exc) from None
     except (AttributeError, TypeError):
         # A question object missing the fields its own type declares. Without
         # this the raw AttributeError escapes to callers that only handle
@@ -141,6 +151,17 @@ def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
             f"question object does not expose the fields required for type {question_type!r} "
             "(detail withheld: it can echo question contents)"
         ) from None
+
+    try:
+        if question_type == "binary":
+            return CanonicalBinaryQuestion(**fields)
+        if question_type == "multiple_choice":
+            return CanonicalMultipleChoiceQuestion(**fields)
+        if question_type == "numeric":
+            return CanonicalNumericQuestion(**fields)
+    except ValidationError as exc:
+        # from None: the ValidationError text echoes the offending input values.
+        raise _sanitize(exc) from None
     # Unreachable: question_type was checked against _SUPPORTED_TYPES above.
     raise AssertionError("unreachable: unhandled supported question type")

diff --git a/tests/unit/test_questions.py b/tests/unit/test_questions.py
index 52f917b..7d62cb9 100644
--- a/tests/unit/test_questions.py
+++ b/tests/unit/test_questions.py
@@ -16,6 +16,7 @@ from typing import Any
 import pytest
 from forecasting_tools.data_models.data_organizer import DataOrganizer
 from forecasting_tools.data_models.questions import (
+    Category,
     DateQuestion,
     DiscreteQuestion,
     MetaculusQuestion,
@@ -110,15 +111,6 @@ def test_numeric_bounds_preserved() -> None:
     assert canonical.cdf_size == 201


-def test_group_identity_is_carried_through() -> None:
-    """M1-202 unpacks subquestions; the parent linkage must survive M1-201."""
-    for sdk, canonical in zip(
-        load_fixture_questions(), normalize_questions(load_fixture_questions())
-    ):
-        assert canonical.group_question_option == sdk.group_question_option
-        assert canonical.question_ids_of_group == sdk.question_ids_of_group
-
-
 def test_canonical_questions_round_trip_through_the_union_adapter() -> None:
     for canonical in normalize_questions(load_fixture_questions()):
         restored = CanonicalQuestionAdapter.validate_python(canonical.model_dump())
@@ -170,9 +162,17 @@ def test_discrete_question_is_rejected_despite_subclassing_numeric() -> None:
         normalize_question(question)


-@pytest.mark.parametrize("tag", ["conditional", "date", "discrete", "", None, 7])
+@pytest.mark.parametrize(
+    "tag",
+    ["conditional", "date", "discrete", "", None, 7, [], {}, ["binary"], {"binary": 1}],
+)
 def test_unsupported_tags_are_refused_without_reading_any_field(tag: object) -> None:
-    """A bare tag is enough to refuse: no field access, so no model call."""
+    """A bare tag is enough to refuse: no field access, so no model call.
+
+    The unhashable cases are the regression guard: testing ``tag in frozenset``
+    before ``isinstance(tag, str)`` raises a raw ``TypeError`` that escapes the
+    module's error boundary entirely.
+    """

     class _OnlyTag:
         question_type = tag
@@ -181,6 +181,25 @@ def test_unsupported_tags_are_refused_without_reading_any_field(tag: object) ->
         normalize_question(_OnlyTag())  # type: ignore[arg-type]


+def test_unsupported_tag_error_names_only_known_sdk_types() -> None:
+    """A tag outside the SDK's own enum is unvetted content, so it is not echoed."""
+
+    class _KnownTag:
+        question_type = "conditional"
+
+    class _ForeignTag:
+        question_type = PLANTED_SECRET
+
+    with pytest.raises(UnsupportedQuestionTypeError, match="conditional"):
+        normalize_question(_KnownTag())  # type: ignore[arg-type]
+
+    with pytest.raises(UnsupportedQuestionTypeError) as excinfo:
+        normalize_question(_ForeignTag())  # type: ignore[arg-type]
+    assert PLANTED_SECRET not in str(excinfo.value)
+    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
+    assert "unknown" in str(excinfo.value)
+
+
 # --- malformed records ------------------------------------------------------

 PLANTED_SECRET = "privateFAKE123456"
@@ -203,6 +222,7 @@ def fake_sdk_question(**overrides: object) -> SimpleNamespace:
         "scheduled_resolution_time": None,
         "tournament_slugs": ["minibench"],
         "question_weight": 1.0,
+        "categories": [],
         "group_question_option": None,
         "question_ids_of_group": None,
     }
@@ -210,6 +230,52 @@ def fake_sdk_question(**overrides: object) -> SimpleNamespace:
     return SimpleNamespace(**base)


+def test_group_identity_is_carried_through() -> None:
+    """M1-202 unpacks subquestions; the parent linkage must survive M1-201.
+
+    Uses non-null linkage deliberately: every repo fixture has a null group
+    parent, so a fixture-driven check passes even if both fields are mapped to a
+    constant ``None``.
+    """
+    canonical = normalize_question(
+        fake_sdk_question(  # type: ignore[arg-type]
+            group_question_option="Above 3%",
+            question_ids_of_group=[91001, 91002, 91003],
+        )
+    )
+    assert canonical.group_question_option == "Above 3%"
+    assert canonical.question_ids_of_group == [91001, 91002, 91003]
+
+
+# --- source categories (uninterpreted SDK passthrough) ----------------------
+
+
+def test_source_categories_are_carried_through() -> None:
+    """normalize is the only place SDK fields are read, so a dropped category is
+    unrecoverable downstream. Slug wins where present; name is the fallback."""
+    canonical = normalize_question(
+        fake_sdk_question(  # type: ignore[arg-type]
+            categories=[
+                Category(id=1, name="Economics", slug="economy"),
+                Category(id=2, name="Geopolitics", slug=None),
+            ]
+        )
+    )
+    assert canonical.source_categories == ["economy", "Geopolitics"]
+
+
+def test_source_categories_default_to_empty() -> None:
+    assert normalize_question(fake_sdk_question()).source_categories == []  # type: ignore[arg-type]
+
+
+def test_source_categories_survive_the_round_trip() -> None:
+    canonical = normalize_question(
+        fake_sdk_question(categories=[Category(id=1, name="Health", slug="health")])  # type: ignore[arg-type]
+    )
+    restored = CanonicalQuestionAdapter.validate_json(canonical.model_dump_json())
+    assert restored == canonical
+
+
 @pytest.mark.parametrize(
     ("description", "overrides", "match"),
     [
@@ -309,3 +375,103 @@ def test_unsupported_error_is_a_normalization_error() -> None:
 def test_normalize_questions_propagates_the_first_failure() -> None:
     with pytest.raises(UnsupportedQuestionTypeError):
         normalize_questions([*load_fixture_questions(), _synthetic_date_question()])
+
+
+# --- schema integrity: finite floats ----------------------------------------
+
+
+def numeric_kwargs(**overrides: object) -> dict[str, Any]:
+    base: dict[str, Any] = {
+        "question_id": 91001,
+        "post_id": 90001,
+        "title": "[SYNTHETIC] How many?",
+        "lower_bound": 0.0,
+        "upper_bound": 100.0,
+        "open_lower_bound": False,
+        "open_upper_bound": True,
+        "cdf_size": 201,
+    }
+    base.update(overrides)
+    return base
+
+
+NON_FINITE = [float("nan"), float("inf"), float("-inf")]
+
+
+@pytest.mark.parametrize("value", NON_FINITE)
+@pytest.mark.parametrize(
+    "field",
+    ["lower_bound", "upper_bound", "zero_point", "nominal_lower_bound", "nominal_upper_bound"],
+)
+def test_non_finite_numeric_fields_are_rejected(field: str, value: float) -> None:
+    """NaN slips past ``lower >= upper`` (both comparisons are false) and then
+    serializes to JSON null, so the union adapter cannot read the record back."""
+    with pytest.raises(ValidationError):
+        CanonicalNumericQuestion(**numeric_kwargs(**{field: value}))
+
+
+@pytest.mark.parametrize("value", NON_FINITE)
+def test_non_finite_question_weight_is_rejected(value: float) -> None:
+    with pytest.raises(ValidationError):
+        CanonicalBinaryQuestion(
+            question_id=91001, post_id=90001, title="[SYNTHETIC] Will it?", question_weight=value
+        )
+
+
+def test_numeric_question_survives_a_json_round_trip() -> None:
+    """The positive half of the finite-float contract."""
+    canonical = CanonicalNumericQuestion(
+        **numeric_kwargs(zero_point=1.0, nominal_lower_bound=0.0, nominal_upper_bound=100.0)
+    )
+    restored = CanonicalQuestionAdapter.validate_json(canonical.model_dump_json())
+    assert restored == canonical
+    assert type(restored) is CanonicalNumericQuestion
+
+
+# --- schema integrity: multiple-choice option sets --------------------------
+
+
+def choice_kwargs(options: list[str]) -> dict[str, Any]:
+    return {
+        "question_id": 91001,
+        "post_id": 90001,
+        "title": "[SYNTHETIC] Which?",
+        "options": options,
+    }
+
+
+@pytest.mark.parametrize(
+    ("description", "options"),
+    [
+        ("duplicate labels", ["A", "B", "A"]),
+        ("blank label", ["A", "B", ""]),
+        ("whitespace-only label", ["A", "B", "   "]),
+        ("single option", ["A"]),
+        ("no options", []),
+        ("duplicated blank", ["", ""]),
+    ],
+)
+def test_malformed_option_sets_are_rejected(description: str, options: list[str]) -> None:
+    """M1-404 must emit every exact option once; duplicates collapse as mapping
+    keys and blanks cannot be matched back to a source option."""
+    with pytest.raises(ValidationError):
+        CanonicalMultipleChoiceQuestion(**choice_kwargs(options))
+
+
+def test_well_formed_option_set_is_accepted() -> None:
+    canonical = CanonicalMultipleChoiceQuestion(**choice_kwargs(["Yes", "No", "Too close"]))
+    assert canonical.options == ["Yes", "No", "Too close"]
+
+
+def test_option_validation_errors_never_echo_the_labels() -> None:
+    """Option labels are record content, so the no-echo rule covers them too."""
+    with pytest.raises(NormalizationError) as excinfo:
+        normalize_question(
+            fake_sdk_question(  # type: ignore[arg-type]
+                question_type="multiple_choice",
+                options=[PLANTED_SECRET, PLANTED_SECRET],
+                option_is_instance_of=None,
+            )
+        )
+    assert PLANTED_SECRET not in str(excinfo.value)
+    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
```
