# Cross-model review request — whiskeyjack-bot M1-202

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
are `pytest`, `ruff check`, `ruff format --check`, `mypy --strict src`. All four pass on
this branch; suite went 301 → 323.

This is **M1-202**, the second item in the Question Normalization epic. M1-201 (merged)
defined the canonical question model and the SDK → canonical mapping. M1-203 (not
started) will turn type refusals into logged diagnostic events.

The SDK is `forecasting-tools==0.2.92`, pinned and never floated. It is untyped
(mypy override). **Every claim below about SDK behaviour was verified by reading the
installed source in `.venv`, not from documentation or memory** — but verify the
readings, since the whole design rests on them.

## Authoritative spec

Backlog row `M1-202`, verbatim:

```
M1-202,Question Normalization,Handle group-question unpacking,
Preserve group parent identity while processing each subquestion as a forecastable record.,
M1-201,High,Claude Code,
Unpacked fixtures produce one unique internal question per subquestion and no duplicate IDs.,
M,Not Started,https://github.com/Metaculus/forecasting-tools
```

Binding project constraints that bear on this diff (from `CLAUDE.md`):

- **Error hygiene, non-negotiable:** every module owns a sanitized exception; an error
  message **never echoes stored/file/field values**; sanitizing raises use `from None`
  so an underlying exception cannot reprint a value through its text or a rendered
  traceback. Callers handle only the module's own error type — **a raw
  `AttributeError`/`KeyError`/`ValueError` escaping is a review finding (it has been,
  twice).**
- **Community prediction is never a forecaster input in v1.**
- Never persist hidden chain-of-thought; concise auditable rationale only.
- If an acceptance criterion is ambiguous, implement the **stricter reading** and note it.
- Closed enums are module-level `Literal` aliases validated with `get_args`, not `enum.Enum`.
- Pin `forecasting-tools==0.2.92`; if spec and observed package behaviour conflict, stop
  and ask rather than silently adapting.

## The SDK facts the whole design rests on — please verify these first

If any of these is wrong, most of the diff is wrong. All refer to
`forecasting-tools==0.2.92`.

1. **There is no `GroupQuestion` class.** `group_of_questions` is a post-level API tag,
   never a member of `QuestionBasicType` (`data_models/questions.py:55-60`), so it can
   never appear in `q.question_type`. Group handling is fetch-time expansion only.
2. **Expansion deep-copies the parent post once per subquestion**
   (`helpers/metaculus_client.py:682-700`). Per sibling that means `post_id` and
   `page_url` are **identical** (the URL is built from the post id,
   `data_models/questions.py:177`), `fine_print`/`description`/`resolution_criteria` are
   **identical** (the parent's, overwriting each subquestion's own), and only
   `id_of_question` is unique.
3. **The parent post's title is dropped.** `question_text` is taken from
   `question_json["title"]` — the subquestion block (`data_models/questions.py:171`).
   The post-level title is not carried into any SDK field.
4. **The raw post payload is retained on every question object** as `api_json`
   (`data_models/questions.py:111-118`, assigned at `:205`), and it contains the
   community-prediction `aggregations` (read at `:335` for binary questions).
5. **The SDK already expands groups on the live path.** `metaculus/fetch.py:85` calls
   `get_all_open_questions_from_tournament`, whose `group_question_mode` default is
   `"unpack_subquestions"` (`helpers/metaculus_api.py:207`), and our config default is
   the same (`config.MetaculusConfig.group_question_mode`). So `normalize_question`
   already receives subquestions pre-separated in production.

## What this diff does

- `questions/groups.py` (new) — `unpack_group_post()` and `is_group_post()`. Our own
  expansion of a raw group post into one SDK question per subquestion.
- `questions/model.py` — one new canonical field, `group_parent_title: str | None`.
- `questions/normalize.py` — `_group_parent_title()` recovery from `api_json`, and
  batch-level duplicate-`question_id` enforcement in `normalize_questions()`.
- `tests/fixtures/api_posts/group/minibench_group.json` (new) — the repo's first group
  fixture, in a subdirectory (see risk area 7).
- `tests/unit/test_groups.py` (new) — 22 tests.
- `tests/unit/test_questions.py` — one line: `api_json` added to the `fake_sdk_question`
  base, because that helper asserts every override key is actually read by normalize.

**No migration.** Canonical questions are not persisted yet — there is no questions
table. `question_id` already anchors the ledger's
`UNIQUE (question_id, tournament_id, forecast_version)` (`migrations/001_initial.sql:40`).

## Deliberate choices (challenge the rationale, but these are not omissions)

- **`question_id` is the internal identity anchor, not `post_id` or `url`.** Forced by
  SDK fact 2: siblings share the post. Agrees with the ledger's existing unique constraint.
- **We own the expansion rather than calling the SDK's.** The SDK's is
  `MetaculusClient._unpack_group_question` — a private static method on a network-bound
  client class. Owning a public seam is what makes a raw group post exercisable offline.
  Cost: ~20 duplicated lines that can drift. Guarded by
  `test_our_unpacking_matches_the_pinned_sdk`.
- **`group_parent_title` carries only the parent's title, never `api_json` itself.**
  Forced by SDK facts 3 and 4: the title is needed (a subquestion can be titled just
  `"September 2026"`, stating no question), but the payload carries community-prediction
  aggregations and the canonical model is the forecaster's input boundary.
- **Tolerant parent overrides.** The SDK indexes `group_json["fine_print"]` etc.
  directly and raises `KeyError` when absent; we override only keys actually present.
  Strictly more tolerant; never erases a subquestion value by replacing it with `None`.
- **Type policy stays in `normalize`, not `groups`.** A well-formed `date` subquestion
  expands fine and is refused downstream by `normalize_question` (D21), so the reason
  reported is the real one rather than "malformed group".
- **Nothing in the pipeline calls `unpack_group_post` yet** (SDK fact 5). The seam exists
  for offline fixtures and any future raw-post path. If you think an unused public seam
  is itself the defect, say so — that is a legitimate finding.
- **Out of scope, deliberately:** per-question skip + logged diagnostic events for
  deferred types is **M1-203**; the comprehensive valid/invalid golden fixture set is
  **Codex's T-901**; `cdf_size` enforcement is **M1-503**.

## What to scrutinize (pressure-test these specifically)

1. **Does the duplicate guard actually satisfy the acceptance criterion?**
   `normalize_questions` counts duplicates *after* normalizing everything. Is checking
   post-hoc rather than during the loop a defect (wasted work, or a failure ordering
   that hides a different error)? Is `question_id` genuinely sufficient as an identity
   key, or does "one unique internal question per subquestion" demand a composite
   (e.g. `(question_id, tournament)`) that this misses? Note the ledger constraint is
   `(question_id, tournament_id, forecast_version)` — is keying on `question_id` alone
   at this layer too narrow, or correctly narrower?

2. **The `api_json` read in `_group_parent_title`.** It is guarded by
   `if not q.question_ids_of_group`. Enumerate the inputs it now governs: non-group
   questions, group members with `api_json={}` (snapshot replay), group members whose
   payload is a non-dict, a blank/whitespace title, and a *conditional* question (which
   also populates `question_ids_of_group`? — verify this, it is the case I am least sure
   of). Does any of them raise, or silently attach the wrong parent's title? **M1-301's
   review found three successive defects of exactly this shape** — a guard that was right
   about the reported case and wrong about the range around it.

3. **Is `group_parent_title` on the right model?** It sits on `_CanonicalQuestionBase`,
   so every binary/MC/numeric question carries it as `None`. Alternative was a separate
   group-linkage sub-model. Also: is a nullable field the right shape, or should a group
   member without a recoverable parent title be a hard error? Argue the stricter reading.

4. **Error hygiene on the new paths.** `unpack_group_post` catches bare `Exception`
   around `DataOrganizer.get_question_from_post_json`. Is that too broad — does it
   swallow a programming error of ours (a `TypeError` from our own dict-building) and
   misreport it as malformed input? The M1-201 precedent deliberately fenced field
   *reads* separately from model *construction* for exactly this reason; this diff does
   not. Is that a regression? Separately: verify the duplicate-count message and the
   malformed-post messages cannot echo content through any path, including
   `traceback.format_exception`.

5. **Fidelity of our expansion to the SDK's.** Read
   `MetaculusClient._unpack_group_question` and our `unpack_group_post` side by side.
   Beyond the documented tolerant-override deviation, is there **any** input on which
   they diverge? Consider: a group block whose `questions` contains duplicate ids;
   subquestions of mixed types; a group post that also carries a top-level `"question"`
   key; `question_ids_of_group` ordering. The drift test compares only one well-formed
   fixture — it proves agreement on that fixture, **not in general**, and I want the gap
   characterized rather than assumed small.

6. **Is the drift test load-bearing or vacuous?** It excludes `date_accessed` and
   `api_json` from comparison. Does excluding `api_json` hide a real divergence (we
   deep-copy the post; does the SDK's copy differ from ours in a way that matters
   downstream)? Would the test still fail if our expansion were subtly wrong — e.g. if
   we forgot the `question_ids_of_group` assignment entirely?

7. **The fixture.** It lives at `tests/fixtures/api_posts/group/minibench_group.json`,
   deliberately outside the `api_posts/*_post.json` glob used by
   `load_fixture_questions()` (which would feed it to `DataOrganizer` and break every
   existing question test). Is a subdirectory the right guard, or is it fragile — would a
   future `glob("**/*.json")` reintroduce the break? Also: is the fixture *realistic*?
   I hand-authored it against my reading of the API shape and **have no live group post
   to check it against**. If it is unrepresentative, every test in this diff is
   proving something about a shape Metaculus never emits.

8. **Vacuity sweep.** M1-201's review found three vacuous tests (assertions that pass
   against a constant `None`, shared fixture constants making distinctness untestable).
   Which tests here would still pass if the feature were reverted to a stub? I believe
   `test_group_parent_title_is_load_bearing_not_decorative` and
   `test_siblings_share_the_post_but_not_the_question_id` are the two that genuinely
   cannot — confirm or refute.

9. **Scope creep / hallucinations.** `is_group_post` is exported but unused by the
   pipeline. Any invented SDK API, any claim in a comment or in `docs/M1-202-NOTES.md`
   not supported by the code, anything implemented beyond the acceptance criterion?
   Note the project rule: **speculative hardening beyond a finding is where the next
   finding comes from.**

## Known risk I am declaring up front

**Unverifiable offline:** whether real MiniBench group subquestion titles are already
self-describing. If they always are, `group_parent_title` is redundant (though not
wrong). I chose it as the stricter reading because the failure mode — a forecaster
receiving the bare string `"September 2026"` — is silent and expensive, and cannot be
detected after the fact from the ledger. Tell me if you think that trade is wrong.

## Output format

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Findings**, ranked by severity (Blocker / High / Medium / Low / Nit), each with
  `file:line`, a one-line defect statement, and a concrete failure scenario (inputs →
  wrong outcome). Separate must-fix from optional.
- Explicitly note anything you **cannot** verify from the diff alone — in particular any
  of the five SDK facts, if you cannot check the installed source.
- If APPROVE, one line per risk area (1–9) stating why it's safe.

---

# Full branch diff (`git diff master...feat/m1-202-group-question-unpacking`)

```diff
diff --git a/docs/M1-202-NOTES.md b/docs/M1-202-NOTES.md
new file mode 100644
index 0000000..e931694
--- /dev/null
+++ b/docs/M1-202-NOTES.md
@@ -0,0 +1,148 @@
+# Milestone 1 — Question Normalization epic (M1-20x) implementation notes
+
+Running record of M1-20x decisions and deviations, in the spirit of `docs/M0-REVIEW.md`
+and `docs/M1-NOTES.md`.
+
+**This file is temporary and merges back into `docs/M1-NOTES.md`.** It is split out because
+M1 is being built across parallel worktrees (one branch per backlog item), and
+`docs/M1-NOTES.md` is the one file every branch would otherwise append to — guaranteeing a
+textual merge conflict on every merge.
+
+**Merge-back trigger:** when the Question Normalization epic (M1-201 through M1-203) is
+complete and merged to master, append these sections to `docs/M1-NOTES.md` in issue order
+and delete this file. Do it as a single docs-only commit, after the last M1-20x merge.
+Note that **M1-201 predates this convention** and already appended directly to
+`docs/M1-NOTES.md`; only M1-202 onward live here.
+
+## M1-202 — Group-question unpacking
+
+Acceptance: *unpacked fixtures produce one unique internal question per subquestion and no
+duplicate IDs.*
+
+### What the criterion is actually guarding against
+
+Group questions arrive as a single *post* carrying a `group_of_questions` block.
+`group_of_questions` is a post-level tag, never a `question_type`, so there is no
+`GroupQuestion` model in the pinned SDK and expansion is purely a fetch-time concern.
+
+Expansion — ours and the SDK's alike — deep-copies the **parent post** once per subquestion
+and swaps in that subquestion's block. Verified against `forecasting-tools==0.2.92`
+(`helpers/metaculus_client.py:682-700`), every sibling therefore shares:
+
+| field | across siblings |
+|---|---|
+| `question_id` | **unique** |
+| `post_id`, `url` | identical (the URL is built from the post id) |
+| `fine_print`, `background_info`, `resolution_criteria` | identical — the parent's, overwriting each subquestion's own |
+| `question_ids_of_group` | identical (full sibling list) |
+| `group_question_option` | unique (the subquestion's label) |
+
+So **any identity keyed on `post_id` or `url` collapses an entire group to one record.**
+That is the failure the acceptance criterion is written against, and
+`test_siblings_share_the_post_but_not_the_question_id` pins it explicitly so a future
+refactor cannot reintroduce it quietly.
+
+Decision: **`question_id` is the internal identity anchor.** It agrees with the ledger's
+existing `UNIQUE (question_id, tournament_id, forecast_version)`
+(`migrations/001_initial.sql:40`), so no migration and no schema change were needed —
+canonical questions are not persisted yet, and there is no questions table.
+
+### Delivered
+
+- `src/whiskeyjack_bot/questions/groups.py` — `unpack_group_post()` and `is_group_post()`.
+  Our own expansion, mirroring the SDK's semantics.
+- `src/whiskeyjack_bot/questions/model.py` — one new canonical field, `group_parent_title`.
+- `src/whiskeyjack_bot/questions/normalize.py` — `_group_parent_title()` recovery, and
+  batch-level duplicate-`question_id` enforcement in `normalize_questions()`.
+- `tests/fixtures/api_posts/group/minibench_group.json` — the repo's first group fixture.
+- `tests/unit/test_groups.py` — 22 tests. Suite 301 → 323.
+
+### Decision — we own the expansion rather than calling the SDK's
+
+The SDK's expansion is reachable only as `MetaculusClient._unpack_group_question`, a private
+static method on a network-bound client class. Binding to a `_`-prefixed API is not a
+contract, and it is what makes a raw group post exercisable as an offline fixture.
+
+The cost is ~20 duplicated lines that can drift on an SDK bump. Mitigated by
+`test_our_unpacking_matches_the_pinned_sdk`, which compares our output to the SDK's
+field-by-field on the same fixture. It is a **drift alarm, not a guarantee** — it proves the
+two agree on this fixture, not on all inputs.
+
+Excluded from that comparison: `date_accessed` (set at construction, so two expansions
+legitimately differ) and `api_json` (the raw post echoed back verbatim by both — comparing it
+adds nothing and would dominate the diff on failure).
+
+### Decision — `group_parent_title`, and why only the title
+
+Expansion sets each subquestion's `question_text` from the **subquestion** block
+(`questions.py:171`) and discards the parent post's title entirely. Metaculus titles some
+subquestions with only their option label, so a subquestion can reach the forecaster as the
+bare string `"September 2026"` — which states no question at all.
+
+The parent title survives on `api_json`, the raw post payload the SDK retains, and is lifted
+back out into a canonical `group_parent_title`.
+
+**Only the title is lifted; `api_json` itself is never carried onto the canonical model.**
+The payload contains the community-prediction `aggregations`, and the canonical question is
+the forecaster's input boundary — *community prediction is never a forecaster input in v1*
+is a hard constraint, and carrying the payload would breach it by accident.
+
+`group_parent_title` degrades to `None` rather than raising when the payload is absent (a
+question rebuilt from a snapshot has no obligation to carry one) or blank. The fixture
+deliberately contains a label-only subquestion title so
+`test_group_parent_title_is_load_bearing_not_decorative` fails if the field ever stops doing
+real work — the vacuity failure mode M1-201 hit three times.
+
+### Decision — duplicate IDs are refused at the boundary
+
+`normalize_questions()` now enforces `question_id` uniqueness across the batch. A duplicate
+would otherwise collide on the ledger's unique constraint — but only *after* a forecast had
+been generated and paid for. The error reports the **count only**, not the colliding ids:
+an id is low-risk content, but the no-echo rule is unconditional and the softer reading of it
+has been a review finding before.
+
+### Deviation from the SDK — tolerant parent overrides
+
+The SDK indexes `group_json["fine_print"]`, `["description"]` and `["resolution_criteria"]`
+directly, raising `KeyError` when a group block omits one. `unpack_group_post` overrides only
+the keys actually **present**. This is strictly more tolerant and never erases a
+subquestion's own value by replacing it with `None`. On a well-formed post the two are
+identical, which is what the drift test compares.
+
+### Deferred (do not read the absence as an omission)
+
+- **Type policy stays in `normalize`, not `groups`.** A well-formed `date` subquestion
+  expands without complaint and is refused downstream by `normalize_question` (D21), so the
+  reason reported is the real one rather than "malformed group". Pinned by
+  `test_deferred_subquestion_types_are_refused_by_normalize_not_unpack`.
+- **A deferred subquestion still aborts the whole batch**, because `normalize_questions`
+  propagates the first failure. Turning that into a per-question skip with a logged
+  diagnostic event is **M1-203**, which owns the diagnostic path.
+- **Nothing calls `unpack_group_post` in the pipeline yet.** On the live path the SDK
+  already expands groups inside `get_all_open_questions_from_tournament`
+  (`metaculus/fetch.py:85`, `group_question_mode` default `"unpack_subquestions"`), so
+  `normalize_question` receives subquestions already separated. Our seam exists for offline
+  fixtures and for any future path that reads raw post JSON.
+- The comprehensive valid/invalid **golden fixture set is Codex's T-901**; this slice ships
+  only the group fixture its own acceptance needs.
+
+### Standing risk — not verifiable offline
+
+Whether real MiniBench group subquestion titles are already self-describing cannot be checked
+without live data. `group_parent_title` is the **stricter reading** (brief rule 4): it costs
+one nullable field and removes a class of unforecastable prompt input. If live data shows
+subquestion titles are always full questions, the field becomes redundant but not wrong.
+
+Same class as M1-201's standing risk about one-option multiple-choice questions.
+
+### Fixture note
+
+`tests/fixtures/api_posts/group/minibench_group.json` sits in a **subdirectory**. The existing
+loader `load_fixture_questions()` (`tests/unit/test_questions.py`) globs `api_posts/*_post.json`
+non-recursively and feeds each straight to `DataOrganizer.get_question_from_post_json`, which
+asserts `"question" in post_json` and knows nothing about groups. A group post placed under
+that glob would break **every** existing question test.
+
+Per M1-201's review history, the fixture varies its subquestion ids, timestamps and question
+weights rather than reusing shared constants — shared-constant vacuity was a repeat finding
+there, and this fixture's entire purpose is that siblings differ.
diff --git a/src/whiskeyjack_bot/questions/__init__.py b/src/whiskeyjack_bot/questions/__init__.py
index 4b1cd74..d846c3a 100644
--- a/src/whiskeyjack_bot/questions/__init__.py
+++ b/src/whiskeyjack_bot/questions/__init__.py
@@ -1,5 +1,6 @@
 """Canonical question schema and normalization from the pinned SDK models."""
 
+from whiskeyjack_bot.questions.groups import is_group_post, unpack_group_post
 from whiskeyjack_bot.questions.model import (
     CanonicalBinaryQuestion,
     CanonicalMultipleChoiceQuestion,
@@ -24,6 +25,8 @@ __all__ = [
     "NormalizationError",
     "SourceCategory",
     "UnsupportedQuestionTypeError",
+    "is_group_post",
     "normalize_question",
     "normalize_questions",
+    "unpack_group_post",
 ]
diff --git a/src/whiskeyjack_bot/questions/groups.py b/src/whiskeyjack_bot/questions/groups.py
new file mode 100644
index 0000000..dfe2be1
--- /dev/null
+++ b/src/whiskeyjack_bot/questions/groups.py
@@ -0,0 +1,118 @@
+"""Expand a Metaculus group post into one question per subquestion (M1-202).
+
+A group question arrives from the API as a single *post* carrying a
+``group_of_questions`` block, not as a question type: ``group_of_questions`` is a
+post-level tag and never appears in ``question_type``, so there is no
+``GroupQuestion`` model in the pinned SDK and nothing downstream of
+:mod:`whiskeyjack_bot.questions.normalize` ever sees a group as such. Expansion is
+purely a fetch-time concern.
+
+On the live path the SDK already expands groups: ``MetaculusClient`` does it inside
+``get_all_open_questions_from_tournament`` when ``group_question_mode`` is
+``"unpack_subquestions"`` (the committed default, see ``config.MetaculusConfig``),
+so :func:`whiskeyjack_bot.questions.normalize.normalize_question` receives
+subquestions already separated. :func:`unpack_group_post` exists because the SDK's
+own expansion is reachable only through a private static method on a
+network-bound client class; owning a public, offline seam is what lets a raw group
+post be exercised as a fixture. ``test_questions.py`` pins our output against the
+SDK's on the same fixture, so a semantic change on an SDK bump fails loudly rather
+than silently diverging.
+
+**The identity trap this module exists to make testable.** Expansion deep-copies the
+*parent post* once per subquestion and swaps in that subquestion's block. Every
+sibling therefore shares ``post_id`` and ``page_url`` (the URL is built from the post
+id), and shares the parent's ``fine_print``/``description``/``resolution_criteria``,
+which overwrite the subquestion's own. Only ``id_of_question`` distinguishes them.
+Any identity keyed on the post would collapse a whole group to one record -- which is
+why ``question_id`` is the canonical anchor, matching the ledger's
+``UNIQUE (question_id, tournament_id, forecast_version)``. Batch-level enforcement of
+that uniqueness lives in ``normalize.normalize_questions``.
+
+Error hygiene matches the rest of the package: a malformed post raises
+:class:`~whiskeyjack_bot.questions.normalize.NormalizationError` with a constant,
+value-free message and ``from None``, so neither a raw ``KeyError``/``ValueError``
+nor an SDK message interpolating post content can reach a caller.
+"""
+
+from __future__ import annotations
+
+import copy
+from typing import Any
+
+from forecasting_tools.data_models.data_organizer import DataOrganizer
+from forecasting_tools.data_models.questions import MetaculusQuestion
+
+from whiskeyjack_bot.questions.normalize import NormalizationError
+
+# Fields the parent group block overrides on every subquestion. The subquestion
+# blocks carry only their own titles and options; the shared framing lives once on
+# the parent, so an un-overridden subquestion would reach the forecaster without the
+# resolution rules that actually govern it.
+_PARENT_OVERRIDES = ("fine_print", "description", "resolution_criteria")
+
+
+def is_group_post(post_json: dict[str, Any]) -> bool:
+    """Whether a raw API post carries a group block and needs expanding."""
+    return isinstance(post_json, dict) and isinstance(post_json.get("group_of_questions"), dict)
+
+
+def unpack_group_post(post_json: dict[str, Any]) -> list[MetaculusQuestion]:
+    """Expand one group post into one SDK question per subquestion.
+
+    Mirrors the pinned SDK's expansion semantics: the parent's framing fields
+    overwrite each subquestion's, and every resulting question carries the full
+    sibling id list in ``question_ids_of_group``.
+
+    Raises :class:`NormalizationError` if the post is not a well-formed group post.
+    Deferred subquestion *types* are not rejected here -- a ``date`` subquestion
+    expands fine and is refused later by ``normalize_question`` (D21), keeping type
+    policy in one place.
+    """
+    if not is_group_post(post_json):
+        raise NormalizationError("post is not a group question post (no group_of_questions block)")
+
+    group_json: dict[str, Any] = post_json["group_of_questions"]
+    question_jsons = group_json.get("questions")
+    if not isinstance(question_jsons, list) or not question_jsons:
+        raise NormalizationError("group question post has no subquestions")
+    if not all(isinstance(q, dict) for q in question_jsons):
+        raise NormalizationError("group question post has a malformed subquestion block")
+
+    try:
+        question_ids: list[int] = [q["id"] for q in question_jsons]
+    except KeyError:
+        # Constant message + from None: the KeyError text is safe, but the rule is
+        # unconditional and the subquestion dict must not reach a traceback.
+        raise NormalizationError("group subquestion is missing its question id") from None
+
+    questions: list[MetaculusQuestion] = []
+    for question_json in question_jsons:
+        subquestion = copy.deepcopy(question_json)
+        # Deviation from the SDK, which indexes these keys directly and raises
+        # KeyError when a group block omits one. Overriding only the keys actually
+        # present is strictly more tolerant and never erases a subquestion's own
+        # value by replacing it with None.
+        for key in _PARENT_OVERRIDES:
+            if key in group_json:
+                subquestion[key] = group_json[key]
+
+        subquestion_post = copy.deepcopy(post_json)
+        subquestion_post["question"] = subquestion
+
+        try:
+            question = DataOrganizer.get_question_from_post_json(subquestion_post)
+        except Exception:
+            # Deliberately broad: the SDK signals a bad post with AssertionError,
+            # KeyError, a ValueError whose text interpolates the offending type
+            # string, or a pydantic ValidationError echoing input values. All of
+            # them are unvetted content, and a caller handling only
+            # NormalizationError would otherwise see a raw exception escape.
+            raise NormalizationError(
+                "group subquestion could not be parsed as a question "
+                "(detail withheld: it can echo post contents)"
+            ) from None
+
+        question.question_ids_of_group = question_ids.copy()
+        questions.append(question)
+
+    return questions
diff --git a/src/whiskeyjack_bot/questions/model.py b/src/whiskeyjack_bot/questions/model.py
index ac71057..67bc536 100644
--- a/src/whiskeyjack_bot/questions/model.py
+++ b/src/whiskeyjack_bot/questions/model.py
@@ -89,6 +89,14 @@ class _CanonicalQuestionBase(_StrictModel):
     # subquestions without losing the parent linkage.
     group_question_option: str | None = None
     question_ids_of_group: list[int] | None = None
+    # M1-202. Unpacking builds each subquestion's ``title`` from the subquestion
+    # block and discards the parent post's title, so a subquestion whose own title
+    # is just an option label ("September 2024") is not self-describing on its own.
+    # The parent title is recovered here so the forecaster always receives what is
+    # actually being asked. Only the title is lifted from the raw post payload --
+    # never the payload itself, which carries community-prediction aggregations
+    # that must not reach a forecaster input (v1 hard constraint).
+    group_parent_title: str | None = None
 
 
 class CanonicalBinaryQuestion(_CanonicalQuestionBase):
diff --git a/src/whiskeyjack_bot/questions/normalize.py b/src/whiskeyjack_bot/questions/normalize.py
index bb00e16..1d84f42 100644
--- a/src/whiskeyjack_bot/questions/normalize.py
+++ b/src/whiskeyjack_bot/questions/normalize.py
@@ -75,6 +75,35 @@ def _sanitize(exc: ValidationError) -> NormalizationError:
     )
 
 
+def _group_parent_title(q: MetaculusQuestion) -> str | None:
+    """The group parent's post title, for a question that is a group member (M1-202).
+
+    Expansion sets a subquestion's ``question_text`` from the subquestion block and
+    drops the parent post's title, so a subquestion titled only with its option label
+    ("September 2024") carries no statement of what is being asked. The parent title
+    survives on the raw post payload the SDK retains, and is lifted back out here.
+
+    Only the title is taken. The payload itself is never carried onto the canonical
+    model: it contains the community-prediction aggregations, and the canonical
+    question is the forecaster's input boundary (community prediction is never a
+    forecaster input in v1).
+
+    Returns ``None`` for a non-group question, and for a group member whose payload
+    is absent -- a question rebuilt from a snapshot has no obligation to carry one.
+    """
+    if not q.question_ids_of_group:
+        return None
+    payload = getattr(q, "api_json", None)
+    if not isinstance(payload, dict):
+        return None
+    title = payload.get("title")
+    # A blank title is no more use than a missing one, and normalizing it here keeps
+    # the "is this self-describing" test downstream a simple None check.
+    if not isinstance(title, str) or not title.strip():
+        return None
+    return title
+
+
 def _common_fields(q: MetaculusQuestion) -> dict[str, Any]:
     """Read the fields shared by every supported type off the SDK object."""
     return {
@@ -102,6 +131,7 @@ def _common_fields(q: MetaculusQuestion) -> dict[str, Any]:
         ],
         "group_question_option": q.group_question_option,
         "question_ids_of_group": q.question_ids_of_group,
+        "group_parent_title": _group_parent_title(q),
     }
 
 
@@ -173,5 +203,31 @@ def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
 
 
 def normalize_questions(questions: list[MetaculusQuestion]) -> list[CanonicalQuestion]:
-    """Normalize a list of SDK questions; propagates the first failure."""
-    return [normalize_question(q) for q in questions]
+    """Normalize a list of SDK questions; propagates the first failure.
+
+    Enforces that ``question_id`` is unique across the batch (M1-202). Group
+    expansion is where this earns its keep: every subquestion of a group is built by
+    deep-copying the parent post, so siblings share ``post_id``, ``url`` and the
+    parent's framing fields, and ``question_id`` is the only thing telling them
+    apart. A duplicate here means either an expansion defect or the same question
+    fetched twice, and both would collide on the ledger's
+    ``UNIQUE (question_id, tournament_id, forecast_version)`` -- but only after a
+    forecast had been generated and paid for. Failing at the boundary is cheaper.
+    """
+    canonical = [normalize_question(q) for q in questions]
+
+    seen: set[int] = set()
+    duplicates = 0
+    for question in canonical:
+        if question.question_id in seen:
+            duplicates += 1
+        seen.add(question.question_id)
+    if duplicates:
+        # Count only. The colliding id is low-risk content, but the no-echo rule is
+        # unconditional and the softer reading of it has been a review finding.
+        raise NormalizationError(
+            f"question batch contains {duplicates} duplicate question id(s) "
+            "(ids withheld: an error message never echoes record content)"
+        )
+
+    return canonical
diff --git a/tests/fixtures/api_posts/group/minibench_group.json b/tests/fixtures/api_posts/group/minibench_group.json
new file mode 100644
index 0000000..c0dc322
--- /dev/null
+++ b/tests/fixtures/api_posts/group/minibench_group.json
@@ -0,0 +1,77 @@
+{
+  "id": 90004,
+  "title": "[SYNTHETIC FIXTURE] Will the example agency's monthly index exceed 3% in each of the following months?",
+  "published_at": "2026-07-02T09:30:00Z",
+  "nr_forecasters": 21,
+  "forecasts_count": 58,
+  "projects": {
+    "tournament": [
+      {"slug": "minibench", "name": "MiniBench"}
+    ],
+    "category": [
+      {"id": 47, "name": "Economics", "slug": "economics"}
+    ],
+    "default_project": {"id": 90900, "slug": "minibench", "name": "MiniBench"}
+  },
+  "group_of_questions": {
+    "description": "Synthetic fixture group for offline tests. Shaped like a MiniBench group question; never submitted anywhere. The framing below is stated once on the parent and overrides each subquestion.",
+    "resolution_criteria": "Each subquestion resolves YES if the example agency's published monthly index for that month exceeds 3.0%.",
+    "fine_print": "The first published value is used; subsequent revisions are ignored.",
+    "questions": [
+      {
+        "id": 91011,
+        "type": "binary",
+        "title": "September 2026",
+        "label": "September 2026",
+        "status": "open",
+        "open_time": "2026-07-02T09:30:00Z",
+        "scheduled_close_time": "2026-09-30T23:59:00Z",
+        "scheduled_resolve_time": "2026-10-15T12:00:00Z",
+        "cp_reveal_time": "2026-07-09T09:30:00Z",
+        "actual_resolve_time": null,
+        "resolution": null,
+        "include_bots_in_aggregates": true,
+        "question_weight": 0.5,
+        "description": "Subquestion-local description that the parent block overrides.",
+        "resolution_criteria": "Subquestion-local criteria that the parent block overrides.",
+        "fine_print": "Subquestion-local fine print that the parent block overrides.",
+        "unit": "%",
+        "my_forecasts": {"history": null}
+      },
+      {
+        "id": 91017,
+        "type": "binary",
+        "title": "[SYNTHETIC FIXTURE] Will the index exceed 3% in October 2026?",
+        "label": "October 2026",
+        "status": "open",
+        "open_time": "2026-07-02T09:30:00Z",
+        "scheduled_close_time": "2026-10-31T23:59:00Z",
+        "scheduled_resolve_time": "2026-11-16T12:00:00Z",
+        "cp_reveal_time": "2026-07-11T09:30:00Z",
+        "actual_resolve_time": null,
+        "resolution": null,
+        "include_bots_in_aggregates": true,
+        "question_weight": 1.0,
+        "unit": "%",
+        "my_forecasts": {"history": null}
+      },
+      {
+        "id": 91023,
+        "type": "binary",
+        "title": "November 2026",
+        "label": "November 2026",
+        "status": "open",
+        "open_time": "2026-07-04T16:45:00Z",
+        "scheduled_close_time": "2026-11-30T23:59:00Z",
+        "scheduled_resolve_time": "2026-12-14T12:00:00Z",
+        "cp_reveal_time": "2026-07-13T16:45:00Z",
+        "actual_resolve_time": null,
+        "resolution": null,
+        "include_bots_in_aggregates": true,
+        "question_weight": 0.75,
+        "unit": "%",
+        "my_forecasts": {"history": null}
+      }
+    ]
+  }
+}
diff --git a/tests/unit/test_groups.py b/tests/unit/test_groups.py
new file mode 100644
index 0000000..6f1e692
--- /dev/null
+++ b/tests/unit/test_groups.py
@@ -0,0 +1,315 @@
+"""M1-202 acceptance: unpacked fixtures produce one unique internal question per
+subquestion and no duplicate IDs.
+
+The criterion is written against a specific trap. Group expansion deep-copies the
+*parent post* once per subquestion, so every sibling shares ``post_id``, ``url`` and
+the parent's framing fields; only ``question_id`` tells them apart. These tests pin
+that ``question_id`` is the identity anchor, that the parent linkage and title
+survive, and that a duplicate id is refused at the normalization boundary rather
+than at the ledger's unique constraint (which is only reached after a forecast has
+been generated).
+"""
+
+import json
+import traceback
+from pathlib import Path
+from typing import Any
+
+import pytest
+from forecasting_tools.data_models.data_organizer import DataOrganizer
+from forecasting_tools.helpers.metaculus_client import MetaculusClient
+
+from whiskeyjack_bot.questions import (
+    NormalizationError,
+    UnsupportedQuestionTypeError,
+    is_group_post,
+    normalize_question,
+    normalize_questions,
+    unpack_group_post,
+)
+
+FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
+GROUP_POST = FIXTURES / "api_posts" / "group" / "minibench_group.json"
+
+PLANTED_SECRET = "privateFAKE123456"
+
+# Set at construction time, so two expansions of the same post legitimately differ.
+_CONSTRUCTION_TIME_FIELDS = {"date_accessed"}
+
+
+def raw_group_post() -> dict[str, Any]:
+    return json.loads(GROUP_POST.read_text(encoding="utf-8"))
+
+
+def canonical_group() -> list[Any]:
+    return normalize_questions(unpack_group_post(raw_group_post()))
+
+
+# --- acceptance -------------------------------------------------------------
+
+
+def test_unpacked_group_yields_one_unique_question_per_subquestion() -> None:
+    """The acceptance criterion, stated directly."""
+    post = raw_group_post()
+    subquestion_count = len(post["group_of_questions"]["questions"])
+    canonical = canonical_group()
+
+    assert len(canonical) == subquestion_count
+    ids = [q.question_id for q in canonical]
+    assert len(set(ids)) == len(ids)
+    assert set(ids) == {q["id"] for q in post["group_of_questions"]["questions"]}
+
+
+def test_siblings_share_the_post_but_not_the_question_id() -> None:
+    """Pins the exact collision the criterion is written against.
+
+    If a future refactor keys internal identity on ``post_id`` or ``url``, a whole
+    group collapses to a single record. This fails loudly when that happens.
+    """
+    canonical = canonical_group()
+
+    assert len({q.post_id for q in canonical}) == 1
+    assert len({q.url for q in canonical}) == 1
+    assert len({q.question_id for q in canonical}) == len(canonical)
+
+
+def test_parent_framing_overrides_every_subquestion() -> None:
+    """The resolution rules live once on the parent and govern every sibling.
+
+    The fixture's first subquestion carries its own description/criteria/fine print
+    precisely so this test can prove they are replaced rather than preserved.
+    """
+    post = raw_group_post()
+    group = post["group_of_questions"]
+    canonical = canonical_group()
+
+    assert {q.resolution_criteria for q in canonical} == {group["resolution_criteria"]}
+    assert {q.fine_print for q in canonical} == {group["fine_print"]}
+    assert {q.background_info for q in canonical} == {group["description"]}
+
+
+# --- parent identity --------------------------------------------------------
+
+
+def test_parent_linkage_and_title_survive_unpacking() -> None:
+    post = raw_group_post()
+    expected_ids = [q["id"] for q in post["group_of_questions"]["questions"]]
+    expected_labels = {q["label"] for q in post["group_of_questions"]["questions"]}
+    canonical = canonical_group()
+
+    for question in canonical:
+        assert question.question_ids_of_group == expected_ids
+        assert question.group_parent_title == post["title"]
+    assert {q.group_question_option for q in canonical} == expected_labels
+
+
+def test_group_parent_title_is_load_bearing_not_decorative() -> None:
+    """At least one subquestion is unforecastable without the parent title.
+
+    Metaculus titles some subquestions with only their option label ("September
+    2026"). Such a title states no question, so the parent's must be carried or the
+    forecaster receives a bare period name. The fixture contains one deliberately.
+    """
+    canonical = canonical_group()
+
+    bare = [q for q in canonical if q.title == q.group_question_option]
+    assert bare, "fixture no longer exercises the label-only-title case"
+    for question in bare:
+        assert question.group_parent_title
+        assert question.group_parent_title != question.title
+
+
+def test_non_group_questions_carry_no_group_fields() -> None:
+    """No accidental coupling: the group fields stay None off the group path."""
+    post = json.loads((FIXTURES / "api_posts" / "binary_post.json").read_text(encoding="utf-8"))
+    canonical = normalize_question(DataOrganizer.get_question_from_post_json(post))
+
+    assert canonical.group_question_option is None
+    assert canonical.question_ids_of_group is None
+    assert canonical.group_parent_title is None
+
+
+def test_group_parent_title_is_none_without_a_retained_payload() -> None:
+    """A question replayed from a snapshot need not carry the raw post payload.
+
+    The parent title is a best-effort recovery, not a required field: its absence
+    must degrade to None rather than raise.
+    """
+    questions = unpack_group_post(raw_group_post())
+    for question in questions:
+        question.api_json = {}
+
+    for canonical in normalize_questions(questions):
+        assert canonical.question_ids_of_group
+        assert canonical.group_parent_title is None
+
+
+# --- SDK drift --------------------------------------------------------------
+
+
+def test_our_unpacking_matches_the_pinned_sdk() -> None:
+    """Drift alarm for owning ~20 lines the SDK also implements.
+
+    We expand groups ourselves because the SDK's version is a private static method
+    on a network-bound client. That is only safe while the two agree, so this pins
+    them field-by-field on the same fixture and fails on an SDK bump that changes
+    expansion semantics.
+    """
+    ours = unpack_group_post(raw_group_post())
+    theirs = MetaculusClient._unpack_group_question(raw_group_post())
+
+    assert len(ours) == len(theirs)
+    for mine, sdk in zip(ours, theirs, strict=True):
+        mine_dump, sdk_dump = mine.model_dump(), sdk.model_dump()
+        # api_json is the raw post echoed back verbatim by both; comparing it adds
+        # nothing and dominates the diff on failure.
+        keys = (mine_dump.keys() | sdk_dump.keys()) - _CONSTRUCTION_TIME_FIELDS - {"api_json"}
+        for key in keys:
+            assert mine_dump.get(key) == sdk_dump.get(key), f"diverged on {key!r}"
+
+
+# --- duplicate rejection ----------------------------------------------------
+
+
+def test_duplicate_question_ids_are_refused() -> None:
+    """Refused at the boundary, not at the ledger's unique constraint.
+
+    A collision reaching the ledger is only discovered after a forecast has been
+    generated and paid for.
+    """
+    questions = unpack_group_post(raw_group_post())
+    questions[1].id_of_question = questions[0].id_of_question
+
+    with pytest.raises(NormalizationError) as excinfo:
+        normalize_questions(questions)
+
+    assert "duplicate" in str(excinfo.value)
+
+
+def test_duplicate_rejection_does_not_echo_ids_or_content() -> None:
+    questions = unpack_group_post(raw_group_post())
+    questions[1].id_of_question = questions[0].id_of_question
+    for question in questions:
+        question.question_text = PLANTED_SECRET
+
+    with pytest.raises(NormalizationError) as excinfo:
+        normalize_questions(questions)
+
+    rendered = str(excinfo.value) + "".join(traceback.format_exception(excinfo.value))
+    assert PLANTED_SECRET not in rendered
+    assert str(questions[0].id_of_question) not in rendered
+
+
+def test_unique_ids_still_normalize() -> None:
+    """The guard must not reject the ordinary case."""
+    assert len(canonical_group()) == 3
+
+
+# --- malformed posts --------------------------------------------------------
+
+
+@pytest.mark.parametrize(
+    "mutate",
+    [
+        pytest.param(lambda p: p.pop("group_of_questions"), id="no_group_block"),
+        pytest.param(lambda p: p.__setitem__("group_of_questions", []), id="group_not_a_dict"),
+        pytest.param(
+            lambda p: p["group_of_questions"].__setitem__("questions", []),
+            id="no_subquestions",
+        ),
+        pytest.param(
+            lambda p: p["group_of_questions"].__setitem__("questions", {"id": 1}),
+            id="subquestions_not_a_list",
+        ),
+        pytest.param(
+            lambda p: p["group_of_questions"].__setitem__("questions", ["not-a-dict"]),
+            id="subquestion_not_a_dict",
+        ),
+        pytest.param(
+            lambda p: p["group_of_questions"]["questions"][1].pop("id"),
+            id="subquestion_without_id",
+        ),
+        pytest.param(
+            lambda p: p["group_of_questions"]["questions"][1].pop("type"),
+            id="subquestion_without_type",
+        ),
+        pytest.param(
+            lambda p: p["group_of_questions"]["questions"][1].__setitem__("type", "made_up"),
+            id="unknown_subquestion_type",
+        ),
+    ],
+)
+def test_malformed_group_posts_raise_normalization_error(mutate: Any) -> None:
+    """Never a raw KeyError/ValueError/AssertionError.
+
+    Callers handle this package's own error type only; a raw exception escaping is
+    the defect class already found twice in review.
+    """
+    post = raw_group_post()
+    mutate(post)
+
+    with pytest.raises(NormalizationError):
+        unpack_group_post(post)
+
+
+def test_malformed_group_post_does_not_echo_content() -> None:
+    """The SDK's own ValueError interpolates the offending type string.
+
+    Every field is planted, not only the ones already known to leak: the narrow
+    version of this test is what let a High through on M1-301.
+    """
+    post = raw_group_post()
+    post["title"] = PLANTED_SECRET
+    post["group_of_questions"]["description"] = PLANTED_SECRET
+    post["group_of_questions"]["fine_print"] = PLANTED_SECRET
+    post["group_of_questions"]["resolution_criteria"] = PLANTED_SECRET
+    for subquestion in post["group_of_questions"]["questions"]:
+        subquestion["title"] = PLANTED_SECRET
+        subquestion["label"] = PLANTED_SECRET
+        subquestion["type"] = PLANTED_SECRET
+
+    with pytest.raises(NormalizationError) as excinfo:
+        unpack_group_post(post)
+
+    assert PLANTED_SECRET not in str(excinfo.value)
+    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
+
+
+def test_is_group_post_discriminates() -> None:
+    assert is_group_post(raw_group_post())
+    assert not is_group_post(
+        json.loads((FIXTURES / "api_posts" / "binary_post.json").read_text(encoding="utf-8"))
+    )
+
+
+def test_deferred_subquestion_types_are_refused_by_normalize_not_unpack() -> None:
+    """Type policy stays in one place (D21).
+
+    A well-formed date subquestion expands without complaint and is refused
+    downstream by ``normalize``, so the reason reported is the real one ("type not
+    supported in v1") rather than "malformed group". Refusal still happens before
+    any model or submission call.
+
+    The date payload is built out fully rather than by flipping the type string: the
+    SDK parses a subquestion against its declared type, so a binary-shaped block
+    labelled ``date`` fails at expansion and would test nothing about type policy.
+    """
+    post = raw_group_post()
+    subquestion = post["group_of_questions"]["questions"][1]
+    subquestion["type"] = "date"
+    subquestion["open_lower_bound"] = True
+    subquestion["open_upper_bound"] = True
+    # range_min/range_max are floated before being parsed as dates, so they are epoch
+    # seconds here: 2026-01-01 and 2026-12-31.
+    subquestion["scaling"] = {
+        "range_min": 1767225600.0,
+        "range_max": 1798761600.0,
+        "zero_point": None,
+    }
+
+    questions = unpack_group_post(post)
+    assert len(questions) == 3
+
+    with pytest.raises(UnsupportedQuestionTypeError) as excinfo:
+        normalize_questions(questions)
+    assert "date" in str(excinfo.value)
diff --git a/tests/unit/test_questions.py b/tests/unit/test_questions.py
index 3c4cc4c..4d2dc3e 100644
--- a/tests/unit/test_questions.py
+++ b/tests/unit/test_questions.py
@@ -251,6 +251,8 @@ def fake_sdk_question(**overrides: object) -> SimpleNamespace:
         "categories": [],
         "group_question_option": None,
         "question_ids_of_group": None,
+        # Read only for a group member, to recover the parent post's title (M1-202).
+        "api_json": {},
     }
     # Type-specific attrs are legitimately absent from the base (normalize reads them
     # only for their own type), so they are allowed per-type rather than globally: a
```
