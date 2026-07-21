# Cross-model review request — whiskeyjack-bot M1-201

You are a rigorous senior reviewer performing an independent cross-model review of
code authored by another AI model (Claude). Apply the **stricter reading**: when a
line could be read as either correct or subtly wrong, assume the wrong reading and
prove it can't happen from the diff. Do **not** rubber-stamp. If you approve, justify
why each risk area below is actually safe; if you don't, list blocking findings.

## Project context

`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary
product is an **attribution ledger**: an immutable, replayable SQLite record of every
forecast, its evidence, approvals, submission attempts, resolutions and scores. Python
3.11, `src/` layout, offline-first (tests run with sockets disabled), toolchain gates
are `pytest`, `ruff check`, `ruff format --check`, `mypy --strict src`.

This is **M1-201**, the first slice of the Question Normalization epic: the canonical
internal question schema and the mapping onto it from the pinned SDK's models. Questions
previously flowed through the pipeline as `forecasting_tools` Pydantic objects; this
introduces our own stable schema so an SDK bump cannot ripple downstream. Already merged
and in the tree: M0 (config/fetch/snapshots) and M1-601 (ledger migration + DB layer).

## Authoritative spec (backlog + decisions)

- **M1-201 task:** "Define canonical question model — map package question objects into a
  stable internal schema." Dependency M0-103. Owner: Claude Code.
- **M1-201 acceptance:** "Golden binary, multiple-choice and numeric fixtures validate and
  retain resolution fine print."
- **D20:** support binary, multiple-choice and numeric in v1. (Rejected alternatives:
  binary-only; every package type.)
- **D21:** defer date and conditional questions — "not required by current Summer template
  and adds validation complexity."
- Downstream dependents this schema gates: **M1-202** (group-question unpacking — "preserve
  group parent identity while processing each subquestion as a forecastable record"),
  **M1-203** (reject unsupported types safely — "flag date/conditional types as deferred
  instead of coercing or submitting"; "create a diagnostic event and make zero
  model/submission calls"), and **T-901** (Codex's golden schema fixtures).
- The canonical forecast record downstream (CODEX_HANDOFF.md) must carry: "question text,
  background, resolution criteria, fine print, bounds/options and timestamps; domain and
  question-type tags."
- Error-hygiene convention (established by `ConfigError`/`SnapshotError`/`LedgerError`):
  error messages never echo stored/file values, and sanitizing raises use `from None` so a
  mistakenly stored secret cannot surface through the exception text or a rendered traceback.
- Community prediction must never become a forecaster input (D-series); snapshots may
  contain it, but the input packet must exclude it.

## Deliberate choices / out of scope (challenge the rationale, but these are not omissions)

- **Placement in a `questions/` subpackage** mirroring the existing `metaculus/` package,
  rather than the flat `schemas.py` + `normalize.py` in the handoff's *proposed* tree —
  because M1-202/M1-203 add type-specific logic to the same area. (Same class of deviation
  as M1-601 moving migrations inside the package.)
- **Dispatch on the SDK's `question_type` literal rather than `isinstance`.** The SDK's
  `DiscreteQuestion` *subclasses* `NumericQuestion`, so an `isinstance(q, NumericQuestion)`
  test would silently normalize the unsupported `discrete` type as numeric.
- Unsupported types raise `UnsupportedQuestionTypeError`; converting that refusal into a
  **logged diagnostic event** is explicitly **M1-203**, not this slice.
- Group **unpacking** is **M1-202**; M1-201 only carries the parent linkage
  (`group_question_option`, `question_ids_of_group`) through unchanged.
- The comprehensive valid/invalid **golden fixture set is Codex's T-901**, authored blind;
  this branch ships only the tests proving its own model and mapping.
- `cdf_size` is stored as a plain `int`; enforcing the 201-point count
  (`config.expected_cdf_points`) is calibration-time validation (**M1-503**).
- No new dependency: `pydantic` is already a runtime dep; `uv.lock` is untouched.

## What to scrutinize (pressure-test these specifically)

1. **Type dispatch soundness.** `_SUPPORTED_TYPES` is derived from
   `config.SupportedQuestionType` via `get_args`, and dispatch compares `question_type`
   strings. Is there *any* SDK question class whose `question_type` is one of the three
   supported tags but whose field set differs from what the mapping reads? Conversely, can
   a supported question ever arrive with a missing/None `question_type` and be wrongly
   refused? Is the `raise AssertionError("unreachable")` genuinely unreachable, and is
   `AssertionError` (not `NormalizationError`) the right escape hatch if D20 later grows a
   fourth type?
2. **Field-mapping fidelity.** Does the canonical schema lose anything M1-202/M1-203/M1-40x
   will need? Specifically: no `state`/`status`, no `published_time`,
   `actual_resolution_time`, `cp_reveal_time`, `num_forecasters`, `default_project_id`,
   `categories`, or the raw `api_json` are carried through. Is dropping any of these a
   defect given the downstream record spec above, or correctly deferred? Are the
   post-vs-question ID semantics (`id_of_post` → `post_id`, `id_of_question` →
   `question_id`) right, and is requiring both as non-null `int` safe for real MiniBench data?
3. **Error hygiene — does it actually hold?** `_sanitize` rebuilds the `ValidationError`
   with `include_input=False, include_url=False` and keeps `err["msg"]` and `err["loc"]`.
   Can a pydantic `msg` ever interpolate the offending **value** (e.g. literal/enum
   mismatches, custom validators, union discriminator errors) and thus leak? Does the
   `except (AttributeError, TypeError)` handler's message leak anything via
   `question_type!r`? Are the planted-secret tests exercising real leak paths or trivially
   green?
4. **The catch-all `except (AttributeError, TypeError)`.** Is this too broad — could it mask
   a genuine bug in our own construction (e.g. a typo'd kwarg raising TypeError) as a
   "malformed question" and thereby hide a defect? Where should the boundary be?
5. **Discriminated union correctness.** `qtype` is declared on the base as
   `SupportedQuestionType` and overridden in each subclass as a `Literal` with a default.
   Is that override sound in pydantic v2? Does `CanonicalQuestionAdapter` round-trip
   losslessly (including datetimes) for the ledger's future `record_json` serialization —
   or does `model_dump()` vs `model_dump(mode="json")` hide a problem?
6. **Validation strength.** Is `title: str = Field(min_length=1)` plus
   `options: list[str] = Field(min_length=1)` plus the numeric `lower < upper` validator the
   right set? What malformed-but-plausible real payload would pass validation and produce a
   bad forecast downstream — e.g. duplicate MC options, `zero_point` inside the bounds
   (log-scaled questions), `nominal_*` bounds contradicting `range_*`, negative
   `question_weight`, `cdf_size != 201`?
7. **Community-prediction exclusion.** `BinaryQuestion.community_prediction_at_access_time`
   is deliberately *not* mapped. Confirm nothing in the canonical schema can carry a
   community aggregate into a forecaster input packet.
8. **Scope creep / hallucinations.** Anything implemented beyond M1-201, any invented SDK
   API or field name that doesn't exist on the pinned `forecasting-tools==0.2.92` models, or
   any claim in comments/docstrings/`M1-NOTES.md` not supported by the code?

## Output format

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Findings**, ranked by severity (Blocker / High / Medium / Low / Nit), each with
  `file:line`, a one-line defect statement, and a concrete failure scenario (inputs → wrong
  outcome). Separate must-fix from optional.
- Explicitly note anything you **cannot** verify from the diff alone (in particular, claims
  about the pinned SDK's behaviour).
- If APPROVE, one line per risk area (1–8) stating why it's safe.

The complete branch diff (`git diff master...feat/m1-201-canonical-question-model`) follows.

---

```diff
diff --git a/docs/M1-NOTES.md b/docs/M1-NOTES.md
index 31c5de1..cf9c90b 100644
--- a/docs/M1-NOTES.md
+++ b/docs/M1-NOTES.md
@@ -38,3 +38,51 @@ Deferred (do not read the absence as an omission):
   where the write paths are built.
 - `record_id` generation (UUIDv7/ULID) belongs with the first writer (**M1-602**); no ID minting
   in this DB-layer-only slice.
+
+## M1-201 — Canonical question model
+
+Questions have so far flowed through the pipeline as the pinned SDK's own Pydantic models
+(`forecasting_tools.data_models.questions`), which track the package and can shift under us.
+M1-201 introduces the **stable internal schema** the rest of M1 depends on instead, so an SDK bump
+cannot ripple through retrieval, forecast generation, validation and the ledger writers. It gates
+M1-202/M1-203 and Codex's T-901.
+
+Delivered:
+- `src/whiskeyjack_bot/questions/model.py` — strict Pydantic models (reusing `config._StrictModel`,
+  `extra="forbid"`) as a `qtype`-discriminated union: `CanonicalBinaryQuestion`,
+  `CanonicalMultipleChoiceQuestion`, `CanonicalNumericQuestion`, plus the `CanonicalQuestion` union
+  alias and a `CanonicalQuestionAdapter` for validating raw dicts. Common fields carry
+  `resolution_criteria` + `fine_print` (the M1-201 retention target) and the group-parent identity
+  (`group_question_option`, `question_ids_of_group`) that M1-202 needs.
+- `src/whiskeyjack_bot/questions/normalize.py` — `normalize_question()` / `normalize_questions()`,
+  the single place SDK field names are read. `NormalizationError` follows the
+  `ConfigError`/`SnapshotError`/`LedgerError` hygiene rule (inputs stripped via
+  `errors(include_input=False)`; `from None`).
+- `tests/unit/test_questions.py` — 34 tests: per-type mapping, fine-print retention against the raw
+  fixtures, MC options, numeric bounds/cdf, group-identity carry-through, union round-trip,
+  malformed-record table, and no-leak planted-secret paths. Suite: 138 passed; ruff check +
+  format + `mypy --strict src` clean.
+
+Hardening — a question object missing the fields its declared type requires is reported as a
+`NormalizationError`, not a raw `AttributeError`/`TypeError`. This is the same defect class as the
+M0-103 review finding against `SnapshotError` (callers only handle the module's own error type),
+so it is pinned by a test here rather than left for review to rediscover.
+
+Decision — **dispatch keys on the SDK's `question_type` literal, not `isinstance`.** The SDK's
+`DiscreteQuestion` *subclasses* `NumericQuestion`, so an `isinstance(q, NumericQuestion)` test would
+silently normalize the unsupported `discrete` type as numeric. `_SUPPORTED_TYPES` is derived from
+`config.SupportedQuestionType` via `get_args`, so D20's type list stays single-sourced. A regression
+test pins this (`test_discrete_question_is_rejected_despite_subclassing_numeric`).
+
+Deviation — placed in a **`questions/` subpackage** mirroring `metaculus/`, rather than the flat
+`schemas.py` + `normalize.py` in the handoff's proposed tree, since M1-202/M1-203 add type-specific
+logic to the same area.
+
+Deferred (do not read the absence as an omission):
+- Unsupported types raise `UnsupportedQuestionTypeError` (before any field is read, so zero
+  model/submission calls). Turning that refusal into a **logged diagnostic event** is **M1-203**.
+- Group **unpacking** is **M1-202**; M1-201 only carries the parent linkage through unchanged.
+- The comprehensive valid/invalid **golden fixture set is Codex's T-901**, authored blind; this
+  slice ships only the tests that prove its own model and mapping.
+- `cdf_size` is stored as a plain int. Enforcing the 201-point count (`config.expected_cdf_points`)
+  is calibration-time validation, i.e. **M1-503**.
diff --git a/src/whiskeyjack_bot/questions/__init__.py b/src/whiskeyjack_bot/questions/__init__.py
new file mode 100644
index 0000000..784c53f
--- /dev/null
+++ b/src/whiskeyjack_bot/questions/__init__.py
@@ -0,0 +1,27 @@
+"""Canonical question schema and normalization from the pinned SDK models."""
+
+from whiskeyjack_bot.questions.model import (
+    CanonicalBinaryQuestion,
+    CanonicalMultipleChoiceQuestion,
+    CanonicalNumericQuestion,
+    CanonicalQuestion,
+    CanonicalQuestionAdapter,
+)
+from whiskeyjack_bot.questions.normalize import (
+    NormalizationError,
+    UnsupportedQuestionTypeError,
+    normalize_question,
+    normalize_questions,
+)
+
+__all__ = [
+    "CanonicalBinaryQuestion",
+    "CanonicalMultipleChoiceQuestion",
+    "CanonicalNumericQuestion",
+    "CanonicalQuestion",
+    "CanonicalQuestionAdapter",
+    "NormalizationError",
+    "UnsupportedQuestionTypeError",
+    "normalize_question",
+    "normalize_questions",
+]
diff --git a/src/whiskeyjack_bot/questions/model.py b/src/whiskeyjack_bot/questions/model.py
new file mode 100644
index 0000000..3eb92ee
--- /dev/null
+++ b/src/whiskeyjack_bot/questions/model.py
@@ -0,0 +1,105 @@
+"""Canonical internal question schema (M1-201).
+
+The bot fetches questions as the pinned ``forecasting-tools`` SDK's Pydantic
+models (:mod:`forecasting_tools.data_models.questions`). Those models track the
+SDK and can shift under us. This module defines a **stable internal schema**
+that the rest of Milestone 1 -- retrieval, forecast generation, validation and
+the ledger writers -- depends on instead, so a SDK bump cannot ripple through
+the whole pipeline. :mod:`whiskeyjack_bot.questions.normalize` maps the SDK
+objects onto these models.
+
+Scope is fixed by decisions D20 (support binary, multiple-choice and numeric in
+v1) and D21 (defer date and conditional). Only the three supported types have a
+canonical model here; rejecting the deferred types is
+:mod:`whiskeyjack_bot.questions.normalize`'s job (and, as a diagnostic event,
+M1-203's).
+
+The models are strict (``extra="forbid"``, reusing ``config._StrictModel``) so a
+malformed record fails validation loudly -- that is the M1-201 acceptance
+contract: golden binary, multiple-choice and numeric fixtures validate and
+retain their resolution fine print.
+"""
+
+from __future__ import annotations
+
+from datetime import datetime
+from typing import Annotated, Literal
+
+from pydantic import Field, TypeAdapter, model_validator
+
+from whiskeyjack_bot.config import SupportedQuestionType, _StrictModel
+
+
+class _CanonicalQuestionBase(_StrictModel):
+    """Fields shared by every supported question type.
+
+    Field names are canonical (ours), not the SDK's; the mapping from SDK
+    attribute names lives in :mod:`whiskeyjack_bot.questions.normalize`.
+    """
+
+    qtype: SupportedQuestionType
+    question_id: int
+    post_id: int
+    url: str | None = None
+    title: str = Field(min_length=1)
+    background_info: str | None = None
+    # The resolution fine print is the headline retention target of M1-201.
+    resolution_criteria: str | None = None
+    fine_print: str | None = None
+    unit_of_measure: str | None = None
+    open_time: datetime | None = None
+    close_time: datetime | None = None
+    scheduled_resolution_time: datetime | None = None
+    tournament_slugs: list[str] = Field(default_factory=list)
+    question_weight: float | None = None
+    # Group-parent identity is carried through unchanged so M1-202 can unpack
+    # subquestions without losing the parent linkage.
+    group_question_option: str | None = None
+    question_ids_of_group: list[int] | None = None
+
+
+class CanonicalBinaryQuestion(_CanonicalQuestionBase):
+    qtype: Literal["binary"] = "binary"
+
+
+class CanonicalMultipleChoiceQuestion(_CanonicalQuestionBase):
+    qtype: Literal["multiple_choice"] = "multiple_choice"
+    options: list[str] = Field(min_length=1)
+    option_is_instance_of: str | None = None
+
+
+class CanonicalNumericQuestion(_CanonicalQuestionBase):
+    qtype: Literal["numeric"] = "numeric"
+    lower_bound: float
+    upper_bound: float
+    open_lower_bound: bool
+    open_upper_bound: bool
+    zero_point: float | None = None
+    # The Metaculus/SDK cdf resolution; agrees with config.expected_cdf_points
+    # (Literal[201]). Kept as a plain int here -- calibration-time enforcement
+    # of the point count belongs to the validation epic (M1-503), not the model.
+    cdf_size: int
+    nominal_lower_bound: float | None = None
+    nominal_upper_bound: float | None = None
+
+    @model_validator(mode="after")
+    def _bounds_ordered(self) -> CanonicalNumericQuestion:
+        if self.lower_bound >= self.upper_bound:
+            # Do not echo the bound values: mirror the project-wide rule that a
+            # validation message never reprints record content.
+            raise ValueError("numeric lower_bound must be strictly less than upper_bound")
+        return self
+
+
+# Discriminated union: pydantic selects the subclass by the ``qtype`` tag, so a
+# serialized canonical question round-trips back to the right type.
+CanonicalQuestion = Annotated[
+    CanonicalBinaryQuestion | CanonicalMultipleChoiceQuestion | CanonicalNumericQuestion,
+    Field(discriminator="qtype"),
+]
+
+# Adapter for validating/round-tripping a raw dict against the union (the models
+# themselves are validated directly when constructed by ``normalize``).
+CanonicalQuestionAdapter: TypeAdapter[
+    CanonicalBinaryQuestion | CanonicalMultipleChoiceQuestion | CanonicalNumericQuestion
+] = TypeAdapter(CanonicalQuestion)
diff --git a/src/whiskeyjack_bot/questions/normalize.py b/src/whiskeyjack_bot/questions/normalize.py
new file mode 100644
index 0000000..ece7039
--- /dev/null
+++ b/src/whiskeyjack_bot/questions/normalize.py
@@ -0,0 +1,150 @@
+"""Map pinned-SDK question objects onto the canonical schema (M1-201).
+
+``forecasting_tools`` returns questions as its own Pydantic models
+(:class:`~forecasting_tools.data_models.questions.MetaculusQuestion` and
+subclasses). :func:`normalize_question` maps a single such object onto the
+matching :mod:`whiskeyjack_bot.questions.model` canonical model, and is the one
+place the SDK's field names are read -- everything downstream sees only the
+canonical schema.
+
+Type dispatch keys on the SDK's ``question_type`` literal rather than
+``isinstance``: ``DiscreteQuestion`` subclasses ``NumericQuestion`` in the SDK,
+so an ``isinstance(q, NumericQuestion)`` test would silently swallow the
+unsupported ``discrete`` type. Only the three v1 types (D20) map; ``date``,
+``conditional``, ``discrete`` and anything else are refused with
+:class:`UnsupportedQuestionTypeError` (D21). Turning that refusal into a logged
+diagnostic event -- rather than an exception the caller must catch -- is M1-203.
+
+Error hygiene matches ``ConfigError``/``SnapshotError``/``LedgerError``: a
+:class:`NormalizationError` never echoes field values (a mistakenly stored
+secret must not surface), and sanitizing raises use ``from None``.
+"""
+
+from __future__ import annotations
+
+from typing import Any, get_args
+
+from forecasting_tools.data_models.questions import MetaculusQuestion
+from pydantic import ValidationError
+
+from whiskeyjack_bot.config import SupportedQuestionType
+from whiskeyjack_bot.questions.model import (
+    CanonicalBinaryQuestion,
+    CanonicalMultipleChoiceQuestion,
+    CanonicalNumericQuestion,
+    CanonicalQuestion,
+)
+
+# Derived from the single source of truth in config (D20), so adding a type
+# there cannot leave this dispatch silently out of step.
+_SUPPORTED_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))
+
+
+class NormalizationError(Exception):
+    """A question cannot be mapped onto the canonical schema.
+
+    Same hygiene rule as ``ConfigError``: the message never echoes question
+    field values (which can carry a mistakenly pasted secret), and sanitizing
+    raises use ``from None`` so a wrapped ``ValidationError`` -- whose own text
+    interpolates the offending input -- cannot resurface through the cause chain.
+    """
+
+
+class UnsupportedQuestionTypeError(NormalizationError):
+    """The question is a type deferred in v1 (date/conditional/discrete, D21).
+
+    Raised before any model or submission call is made. The ``question_type``
+    tag it carries is a fixed SDK enum value, not stored content, so naming it
+    is safe under the no-echo rule.
+    """
+
+
+def _sanitize(exc: ValidationError) -> NormalizationError:
+    """Rebuild a ValidationError as a NormalizationError with inputs stripped."""
+    problems = [
+        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
+        for err in exc.errors(include_input=False, include_url=False)
+    ]
+    return NormalizationError(
+        "cannot normalize question:\n" + "\n".join(f"  - {p}" for p in problems)
+    )
+
+
+def _common_fields(q: MetaculusQuestion) -> dict[str, Any]:
+    """Read the fields shared by every supported type off the SDK object."""
+    return {
+        "question_id": q.id_of_question,
+        "post_id": q.id_of_post,
+        "url": q.page_url,
+        "title": q.question_text,
+        "background_info": q.background_info,
+        "resolution_criteria": q.resolution_criteria,
+        "fine_print": q.fine_print,
+        "unit_of_measure": q.unit_of_measure,
+        "open_time": q.open_time,
+        "close_time": q.close_time,
+        "scheduled_resolution_time": q.scheduled_resolution_time,
+        "tournament_slugs": q.tournament_slugs,
+        "question_weight": q.question_weight,
+        "group_question_option": q.group_question_option,
+        "question_ids_of_group": q.question_ids_of_group,
+    }
+
+
+def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
+    """Map one SDK question onto its canonical model.
+
+    Raises :class:`UnsupportedQuestionTypeError` for deferred types (D21) and
+    :class:`NormalizationError` if a supported type fails canonical validation.
+    """
+    question_type = getattr(q, "question_type", None)
+    if question_type not in _SUPPORTED_TYPES:
+        # Refused before any field is read, so an unsupported type can never
+        # reach a model or submission call (D21). Only the fixed type tag is
+        # named -- it is an SDK enum value, not stored content.
+        tag = question_type if isinstance(question_type, str) and question_type else "unknown"
+        raise UnsupportedQuestionTypeError(
+            f"question type {tag!r} is not supported in v1 (binary, multiple_choice, numeric only)"
+        )
+    try:
+        common = _common_fields(q)
+        if question_type == "binary":
+            return CanonicalBinaryQuestion(**common)
+        if question_type == "multiple_choice":
+            return CanonicalMultipleChoiceQuestion(
+                **common,
+                options=q.options,
+                option_is_instance_of=q.option_is_instance_of,
+            )
+        if question_type == "numeric":
+            return CanonicalNumericQuestion(
+                **common,
+                lower_bound=q.lower_bound,
+                upper_bound=q.upper_bound,
+                open_lower_bound=q.open_lower_bound,
+                open_upper_bound=q.open_upper_bound,
+                zero_point=q.zero_point,
+                cdf_size=q.cdf_size,
+                nominal_lower_bound=q.nominal_lower_bound,
+                nominal_upper_bound=q.nominal_upper_bound,
+            )
+    except ValidationError as exc:
+        # from None: the ValidationError text echoes the offending input values.
+        raise _sanitize(exc) from None
+    except (AttributeError, TypeError):
+        # A question object missing the fields its own type declares. Without
+        # this the raw AttributeError escapes to callers that only handle
+        # NormalizationError -- the same defect found against SnapshotError in
+        # M0-103 review. Constant message + from None: the underlying error can
+        # carry the object's repr, and with it stored field values.
+        raise NormalizationError(
+            f"question object does not expose the fields required for type {question_type!r} "
+            "(detail withheld: it can echo question contents)"
+        ) from None
+    # Unreachable: question_type was checked against _SUPPORTED_TYPES above.
+    raise AssertionError("unreachable: unhandled supported question type")
+
+
+def normalize_questions(questions: list[MetaculusQuestion]) -> list[CanonicalQuestion]:
+    """Normalize a list of SDK questions; propagates the first failure."""
+    return [normalize_question(q) for q in questions]
diff --git a/tests/unit/test_questions.py b/tests/unit/test_questions.py
new file mode 100644
index 0000000..52f917b
--- /dev/null
+++ b/tests/unit/test_questions.py
@@ -0,0 +1,311 @@
+"""M1-201 acceptance: golden binary, multiple-choice and numeric fixtures
+normalize into the canonical schema and retain their resolution fine print.
+
+Deferred types (D21) are refused before any field is read, so an unsupported
+question can never reach a model or submission call. Comprehensive valid/invalid
+golden records are Codex's T-901; this suite covers the model + mapping only.
+"""
+
+import json
+import traceback
+from datetime import datetime, timezone
+from pathlib import Path
+from types import SimpleNamespace
+from typing import Any
+
+import pytest
+from forecasting_tools.data_models.data_organizer import DataOrganizer
+from forecasting_tools.data_models.questions import (
+    DateQuestion,
+    DiscreteQuestion,
+    MetaculusQuestion,
+    NumericQuestion,
+)
+from pydantic import ValidationError
+
+from whiskeyjack_bot.questions import (
+    CanonicalBinaryQuestion,
+    CanonicalMultipleChoiceQuestion,
+    CanonicalNumericQuestion,
+    CanonicalQuestionAdapter,
+    NormalizationError,
+    UnsupportedQuestionTypeError,
+    normalize_question,
+    normalize_questions,
+)
+
+FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
+API_POSTS = FIXTURES / "api_posts"
+
+
+def load_fixture_questions() -> list[MetaculusQuestion]:
+    posts = sorted(API_POSTS.glob("*_post.json"))
+    return [
+        DataOrganizer.get_question_from_post_json(json.loads(p.read_text(encoding="utf-8")))
+        for p in posts
+    ]
+
+
+def raw_post(name: str) -> dict[str, Any]:
+    return json.loads((API_POSTS / f"{name}_post.json").read_text(encoding="utf-8"))
+
+
+def normalized_by_type() -> dict[str, Any]:
+    return {q.qtype: q for q in normalize_questions(load_fixture_questions())}
+
+
+def test_fixtures_normalize_to_expected_canonical_types() -> None:
+    canonical = normalize_questions(load_fixture_questions())
+    assert {type(q) for q in canonical} == {
+        CanonicalBinaryQuestion,
+        CanonicalMultipleChoiceQuestion,
+        CanonicalNumericQuestion,
+    }
+    assert {q.qtype for q in canonical} == {"binary", "multiple_choice", "numeric"}
+
+
+@pytest.mark.parametrize("name", ["binary", "multiple_choice", "numeric"])
+def test_normalization_retains_resolution_fine_print(name: str) -> None:
+    """The headline M1-201 acceptance criterion."""
+    source = raw_post(name)["question"]
+    canonical = normalized_by_type()[name]
+    assert canonical.resolution_criteria == source["resolution_criteria"]
+    assert canonical.fine_print == source["fine_print"]
+    # Guard against the assertion passing on a pair of Nones.
+    assert canonical.resolution_criteria
+    assert canonical.fine_print
+
+
+@pytest.mark.parametrize("name", ["binary", "multiple_choice", "numeric"])
+def test_identity_and_common_fields_preserved(name: str) -> None:
+    post = raw_post(name)
+    canonical = normalized_by_type()[name]
+    assert canonical.question_id == post["question"]["id"]
+    assert canonical.post_id == post["id"]
+    assert canonical.title == post["question"]["title"]
+    assert canonical.unit_of_measure == post["question"]["unit"]
+    assert "minibench" in canonical.tournament_slugs
+    assert canonical.question_weight == post["question"]["question_weight"]
+    assert canonical.open_time == datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
+
+
+def test_multiple_choice_options_preserved() -> None:
+    source = raw_post("multiple_choice")["question"]
+    canonical = normalized_by_type()["multiple_choice"]
+    assert canonical.options == source["options"]
+    assert canonical.option_is_instance_of == source["group_variable"]
+
+
+def test_numeric_bounds_preserved() -> None:
+    scaling = raw_post("numeric")["question"]["scaling"]
+    canonical = normalized_by_type()["numeric"]
+    assert canonical.lower_bound == scaling["range_min"]
+    assert canonical.upper_bound == scaling["range_max"]
+    assert canonical.zero_point == scaling["zero_point"]
+    assert canonical.open_lower_bound is False
+    assert canonical.open_upper_bound is True
+    assert canonical.nominal_lower_bound == scaling["nominal_min"]
+    assert canonical.nominal_upper_bound == scaling["nominal_max"]
+    # Agrees with config.expected_cdf_points (Literal[201]).
+    assert canonical.cdf_size == 201
+
+
+def test_group_identity_is_carried_through() -> None:
+    """M1-202 unpacks subquestions; the parent linkage must survive M1-201."""
+    for sdk, canonical in zip(
+        load_fixture_questions(), normalize_questions(load_fixture_questions())
+    ):
+        assert canonical.group_question_option == sdk.group_question_option
+        assert canonical.question_ids_of_group == sdk.question_ids_of_group
+
+
+def test_canonical_questions_round_trip_through_the_union_adapter() -> None:
+    for canonical in normalize_questions(load_fixture_questions()):
+        restored = CanonicalQuestionAdapter.validate_python(canonical.model_dump())
+        assert restored == canonical
+        assert type(restored) is type(canonical)
+
+
+def test_canonical_models_reject_unknown_fields() -> None:
+    payload = normalized_by_type()["binary"].model_dump()
+    payload["surprise"] = "extra"
+    with pytest.raises(ValidationError):
+        CanonicalQuestionAdapter.validate_python(payload)
+
+
+# --- deferred / unsupported types (D21) ------------------------------------
+
+
+def _synthetic_date_question() -> DateQuestion:
+    return DateQuestion(
+        question_text="[SYNTHETIC] When?",
+        lower_bound=datetime(2026, 1, 1, tzinfo=timezone.utc),
+        upper_bound=datetime(2027, 1, 1, tzinfo=timezone.utc),
+        open_lower_bound=False,
+        open_upper_bound=False,
+    )
+
+
+def test_date_question_is_rejected() -> None:
+    with pytest.raises(UnsupportedQuestionTypeError, match="date"):
+        normalize_question(_synthetic_date_question())
+
+
+def test_discrete_question_is_rejected_despite_subclassing_numeric() -> None:
+    """Regression guard for the SDK's inheritance trap.
+
+    ``DiscreteQuestion`` subclasses ``NumericQuestion``, so dispatching on
+    ``isinstance(q, NumericQuestion)`` would silently normalize an unsupported
+    type as numeric. Dispatch keys on the ``question_type`` tag instead.
+    """
+    question = DiscreteQuestion(
+        question_text="[SYNTHETIC] How many?",
+        lower_bound=0.0,
+        upper_bound=10.0,
+        open_lower_bound=False,
+        open_upper_bound=False,
+    )
+    assert isinstance(question, NumericQuestion)  # the trap is real
+    with pytest.raises(UnsupportedQuestionTypeError, match="discrete"):
+        normalize_question(question)
+
+
+@pytest.mark.parametrize("tag", ["conditional", "date", "discrete", "", None, 7])
+def test_unsupported_tags_are_refused_without_reading_any_field(tag: object) -> None:
+    """A bare tag is enough to refuse: no field access, so no model call."""
+
+    class _OnlyTag:
+        question_type = tag
+
+    with pytest.raises(UnsupportedQuestionTypeError):
+        normalize_question(_OnlyTag())  # type: ignore[arg-type]
+
+
+# --- malformed records ------------------------------------------------------
+
+PLANTED_SECRET = "privateFAKE123456"
+
+
+def fake_sdk_question(**overrides: object) -> SimpleNamespace:
+    """A minimal stand-in exposing the SDK attribute names normalize reads."""
+    base: dict[str, object] = {
+        "question_type": "binary",
+        "id_of_question": 91001,
+        "id_of_post": 90001,
+        "page_url": "https://example.invalid/q/1",
+        "question_text": "[SYNTHETIC] Will it?",
+        "background_info": None,
+        "resolution_criteria": "Resolves YES if it does.",
+        "fine_print": "Per the source's own timestamp.",
+        "unit_of_measure": None,
+        "open_time": None,
+        "close_time": None,
+        "scheduled_resolution_time": None,
+        "tournament_slugs": ["minibench"],
+        "question_weight": 1.0,
+        "group_question_option": None,
+        "question_ids_of_group": None,
+    }
+    base.update(overrides)
+    return SimpleNamespace(**base)
+
+
+@pytest.mark.parametrize(
+    ("description", "overrides", "match"),
+    [
+        ("missing question id", {"id_of_question": None}, "question_id"),
+        ("missing post id", {"id_of_post": None}, "post_id"),
+        ("non-integer question id", {"id_of_question": "ninety"}, "question_id"),
+        ("empty title", {"question_text": ""}, "title"),
+        (
+            "multiple choice with no options",
+            {"question_type": "multiple_choice", "options": [], "option_is_instance_of": None},
+            "options",
+        ),
+        (
+            "numeric bounds inverted",
+            {
+                "question_type": "numeric",
+                "lower_bound": 500.0,
+                "upper_bound": 0.0,
+                "open_lower_bound": False,
+                "open_upper_bound": True,
+                "zero_point": None,
+                "cdf_size": 201,
+                "nominal_lower_bound": None,
+                "nominal_upper_bound": None,
+            },
+            "lower_bound",
+        ),
+    ],
+)
+def test_malformed_records_raise_normalization_error(
+    description: str, overrides: dict[str, object], match: str
+) -> None:
+    with pytest.raises(NormalizationError, match=match):
+        normalize_question(fake_sdk_question(**overrides))  # type: ignore[arg-type]
+
+
+@pytest.mark.parametrize(
+    ("description", "overrides"),
+    [
+        ("secret as an invalid question id", {"id_of_question": PLANTED_SECRET}),
+        ("secret as an invalid post id", {"id_of_post": PLANTED_SECRET}),
+        (
+            "secret as invalid multiple-choice options",
+            {
+                "question_type": "multiple_choice",
+                "options": PLANTED_SECRET,
+                "option_is_instance_of": None,
+            },
+        ),
+        (
+            "secret as an invalid weight",
+            {"question_weight": PLANTED_SECRET, "id_of_question": None},
+        ),
+    ],
+)
+def test_normalization_errors_never_echo_field_values(
+    description: str, overrides: dict[str, object]
+) -> None:
+    """Same rule as ConfigError/SnapshotError: pydantic's own rendering prints
+    the offending input, so a mistakenly stored credential must be stripped from
+    both the message and the cause chain."""
+    with pytest.raises(NormalizationError) as excinfo:
+        normalize_question(fake_sdk_question(**overrides))  # type: ignore[arg-type]
+    assert PLANTED_SECRET not in str(excinfo.value), description
+    rendered = "".join(traceback.format_exception(excinfo.value))
+    assert PLANTED_SECRET not in rendered, description
+
+
+def test_question_missing_its_type_fields_raises_normalization_error() -> None:
+    """A raw AttributeError must not escape to callers.
+
+    Same defect class as the M0-103 review finding against SnapshotError: the
+    CLI only handles the module's own error type, so every malformed shape has
+    to arrive as one.
+    """
+    incomplete = fake_sdk_question(question_type="multiple_choice")  # no options attr
+    with pytest.raises(NormalizationError, match="does not expose the fields"):
+        normalize_question(incomplete)  # type: ignore[arg-type]
+
+
+def test_missing_field_error_does_not_echo_question_contents() -> None:
+    incomplete = fake_sdk_question(
+        question_type="numeric",  # numeric bound attrs absent
+        resolution_criteria=PLANTED_SECRET,
+    )
+    with pytest.raises(NormalizationError) as excinfo:
+        normalize_question(incomplete)  # type: ignore[arg-type]
+    assert PLANTED_SECRET not in str(excinfo.value)
+    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
+
+
+def test_unsupported_error_is_a_normalization_error() -> None:
+    """Callers may catch the base type to handle every normalization refusal."""
+    assert issubclass(UnsupportedQuestionTypeError, NormalizationError)
+
+
+def test_normalize_questions_propagates_the_first_failure() -> None:
+    with pytest.raises(UnsupportedQuestionTypeError):
+        normalize_questions([*load_fixture_questions(), _synthetic_date_question()])
```
