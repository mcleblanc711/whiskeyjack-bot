# Cross-model review request — whiskeyjack-bot M1-203

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
this branch; suite went 323 → 332.

This is **M1-203**, the last item in the Question Normalization epic. M1-201 (merged)
defined the canonical question model and the SDK → canonical mapping. M1-202 (merged)
added group-question unpacking and batch-level duplicate-id enforcement.

The SDK is `forecasting-tools==0.2.92`, pinned and never floated. It is untyped
(mypy override).

## Authoritative spec

Backlog row `M1-203`, verbatim:

```
M1-203,Question Normalization,Reject unsupported types safely,
Flag date/conditional types as deferred instead of coercing or submitting.,
M1-201,High,Claude Code,
Unsupported types create a diagnostic event and make zero model/submission calls.,
S,Not Started,D21
```

Decision `D21`, verbatim: *"Defer date and conditional questions."* — Decided.
Rationale: *"Not required by current Summer template and adds validation complexity."*
Revisit trigger: *"MiniBench/current successor requires them."*

Decision `D20`: *"Support binary, multiple-choice and numeric in v1."*

Binding project constraints from `CLAUDE.md` that bear on this diff:

- **Error hygiene, non-negotiable:** *"an error message never echoes stored/file/field
  values"*, and sanitizing raises use `from None`. Every malformed shape must arrive as
  the module's own error type — a raw `AttributeError`/`KeyError`/`ValueError` escaping
  is a review finding (it has been, twice).
- **Never print or persist secrets.**
- **Never persist hidden chain-of-thought**; concise auditable rationale only.
- **Append-only ledger**; approval binds to an exact forecast hash.
- **If an acceptance criterion is ambiguous, implement the stricter reading and note it.**
- **`DiscreteQuestion` subclasses `NumericQuestion`** in the pinned SDK. Dispatch on the
  `question_type` literal, never `isinstance` — otherwise an unsupported type silently
  normalizes as numeric (*a wrong forecast, not an error*).
- Internal value objects are `@dataclass(frozen=True)`; validated boundary models are
  pydantic v2. Closed enums are module-level `Literal` aliases, not `enum.Enum`.

## The situation this diff addresses

**Half the acceptance criterion already held before this diff.** `normalize_question`
refused an unsupported tag *before reading any field*, so a `date` question could never
reach a model or a submission call. That was M1-201 behaviour and was already pinned.

Two things were actually missing:

1. **The batch aborted.** `normalize_questions` propagated the first failure, so a single
   deferred question **discarded the normalization of every supported question fetched
   alongside it**. On a tournament pull containing one date question, the batch returned
   nothing.
2. **"Diagnostic event" named a mechanism that existed nowhere in `src/`.** Neither
   `CODEX_HANDOFF.md` nor `CLAUDE_CODE_PROMPT.md` defines it. There is no diagnostics
   module, no event emitter, and the ledger has no writer API yet (that is M1-602, Not
   Started) — `ledger.py` exports only `connect()` and `initialize_ledger()`.

## What this diff does

- Adds `src/whiskeyjack_bot/questions/events.py`: `DeferralEvent` and
  `NormalizationResult`, both frozen dataclasses.
- Makes refusal **two-tier**. `normalize_question` (singular) still raises
  `UnsupportedQuestionTypeError` with a **byte-identical message**.
  `normalize_questions` (batch) skips the question, appends a `DeferralEvent`, and logs
  at WARNING.
- Changes `normalize_questions`' return type from `list[CanonicalQuestion]` to
  `NormalizationResult`.
- Extracts four defensive helpers: `_safe_attr`, `_type_tag`, `_supported_type`,
  `_safe_int`.
- Restricts the M1-202 duplicate-id check to **accepted** questions.

## Deliberate choices (challenge the rationale, but these are not omissions)

**1. The singular path still raises rather than returning an event.** It is the
type-policy chokepoint; the guarantee "an unsupported question can never reach a model"
is easiest to prove when the function *cannot return* one. Skipping is a batch policy.

**2. `normalize_question` does not log.** Logging *and* raising double-reports one
refusal to an operator.

**3. Ledger persistence deferred to M1-602.** No run or tournament context exists at this
layer to key a row on — nothing in `src/` even calls `normalize_questions` yet. Every
`*_events` table in `001_initial.sql` is FK-bound to `forecast_records`, which by
definition does not exist for a question refused before forecasting. A migration `003`
would also have collided with two live parallel branches (CLAUDE.md: migration numbers
are claimed globally).

**4. `NormalizationResult` is a dataclass, not a tuple.** `canonical, _ =
normalize_questions(...)` discards the deferrals in one character — the exact silence
this item exists to remove.

**5. Two `DeferralReason` values.** The `_KNOWN_SDK_TYPES` gate collapses an unvetted tag
to `'unknown'`, erasing the difference between "an SDK type we defer" (routine) and
"something arbitrary reached the tag slot" (means the object came from outside the SDK's
models). `reason` recovers that without echoing the value.

**6. Rejected: attaching the event to the exception** (`UnsupportedQuestionTypeError.event`),
so the batch could catch and read it. It puts state on a sanitized exception type whose
entire contract is "nothing but a safe string". Extracting `_type_tag`, shared by the
message and the event, gives the same drift protection without that.

## What to scrutinize (pressure-test these specifically)

### A. The no-echo reading — the highest-risk item in this diff

`DeferralEvent` carries `question_id` and `post_id`. **This is a deliberate reading of
CLAUDE.md's no-echo rule as scoped to error messages**, which is how the rule is written
("an *error message* never echoes stored/file/field values"). The argument: an event that
cannot say *which* question was deferred satisfies the criterion's words and fails its
purpose.

The claimed safety property is **by construction, not by promise**: `_safe_int` returns
`None` for anything that is not an `int` (and rejects `bool`, an `int` subclass), so
`DeferralEvent` holds **zero unvetted strings** — `reason` is a module literal,
`question_type` is `_KNOWN_SDK_TYPES`-gated, ids are `int | None`.

Please attack this directly:
- Is there **any** path by which a non-`int`, or attacker/accident-controlled content,
  reaches a `DeferralEvent` field? Consider `int` subclasses other than `bool`, objects
  with `__index__`, `IntEnum`, and anything whose `__repr__` or `__str__` is attacker-
  influenced.
- The event is logged and its `repr` may reach a log or a traceback. Is the "zero
  unvetted strings" claim actually airtight, or only nearly so?
- Is the scoped reading of the rule defensible, or is this eroding a constraint the
  project calls non-negotiable? **I would rather be told to drop the ids than ship a
  leak.** Note the precedent: `normalize_questions`' duplicate-id error deliberately
  withholds ids (the softer reading there was a past review finding) — that error is
  unchanged here, but tell me if the two positions are inconsistent.

### B. `_safe_attr` swallowing `Exception`

```python
try:
    return getattr(q, attribute, None)
except Exception:
    return None
```

Rationale: `getattr`'s default only suppresses `AttributeError`; a property whose getter
raises `ValueError` would escape the module's error boundary and turn a clean deferral
into a crash.

- Is a blanket `except Exception` on a read path defensible here, or does it hide real
  defects? It is used on the *refusal* path only — verify that from the diff rather than
  taking my word.
- Does it mask a genuine SDK contract violation that should surface loudly?
- Note the interaction flagged in **D** below.

### C. Does the batch path actually make zero model/submission calls?

The claim is that `_supported_type` + `_deferral_event` read only `question_type`,
`id_of_question`, `id_of_post` — no content field, no model construction. Verify from the
diff. In particular verify the loop cannot fall through to `normalize_question` for a
deferred type.

### D. The tripwire test's exception type

`tripwire_question` arms every content attribute to raise, and raises
`_ContentFieldRead(BaseException)` — **deliberately not `AssertionError`**, because
`_safe_attr` swallows `Exception`, so an `AssertionError` tripwire would be caught and
the test would pass **vacuously** the moment anyone routed a content read through that
helper. I verified this by mutation (injecting `_safe_attr(q, "resolution_criteria")`
into the deferral path fails the test).

- Is `_CONTENT_ATTRS` actually complete? A missing attribute is an untested read.
- Is raising `BaseException` in a test acceptable practice here, or is there a better
  construction?

### E. The stricter-reading split

Only a deferred *type* is skipped. A malformed *supported*-type question and a duplicate
`question_id` still raise and abort the batch.

- Is that the right reading of D21, or should a malformed record also become an event?
  I argue reporting a real defect as "deferred" hides it behind a diagnostic that says
  the opposite.
- **Duplicate-id scoping:** the check now runs over accepted questions only. Is excluding
  deferred questions correct? My argument: the check protects the ledger's
  `UNIQUE (question_id, tournament_id, forecast_version)`, and a deferred question has no
  canonical model and never reaches the ledger.

### F. The logging decision

`logging_setup.py` is untouched. Values are interpolated into the log **message** with
lazy `%` args, because `record.getMessage()` is redacted twice (a filter rewrites the
message, and `JsonFormatter` redacts every string field it serializes).

I rejected an `extra`-field passthrough as *worse*: `JsonFormatter.format`'s redaction is
`isinstance(value, str)` over **top-level values only**, so a dict or list arriving via
`extra` would sail past it untouched — a new leak class in the one module that must not
have one.

- Is that reading of `JsonFormatter` correct? (It is in the diff context, not the diff.)
- The deferral log line is JSON-with-a-string-message, so a machine consumer needs a
  regex until M1-602. Is that an acceptable cost or a design smell?
- `test_deferral_log_record_is_not_a_leak_vector` attaches a raw `logging.Handler` and
  monkeypatches `emit`. Is that sound, and does it leak handler state across tests? (It
  removes the handler in a `finally`.)

### G. Breaking API change

`normalize_questions` changed its return type. Both callers are tests; nothing in `src/`
consumes it. Argument: this is the cheapest the change will ever be. Is there a
migration/compat concern I have dismissed too easily?

### H. Behaviour preservation

The `UnsupportedQuestionTypeError` message is claimed byte-identical, and the
`isinstance`-before-membership ordering (the unhashable-tag guard) is claimed preserved
through the `_supported_type` refactor. **Verify both from the diff** — the refactor moved
that logic, and a regression here reintroduces a raw `TypeError` escaping the boundary.

Also verify the `DiscreteQuestion`/`isinstance` trap is still avoided end-to-end: dispatch
must key on the tag, never `isinstance`.

## Known risks I am declaring up front

1. **The no-echo scoped reading (A) is the thing most likely to be wrong.** I have argued
   it, but I am not confident enough to be defensive about it.
2. **The "diagnostic event" mechanism was unspecified**, so its shape is my judgement, not
   the spec's. If you think a ledger row was required by the criterion *now* rather than
   at M1-602, say so plainly — that is a scope question I would rather resolve in review.
3. **No production caller exists**, so the behavioural check for this item *is* the test
   suite. There is no end-to-end path to exercise until the pipeline lands.
4. Comprehensive golden valid/invalid fixture coverage is **Codex's T-901** and is
   deliberately not in this diff.

## Output format

Findings as: **Severity (Blocking / High / Medium / Nit) — file:line — what's wrong — why
it matters — suggested fix.** Then an overall verdict. If you approve, justify why each of
A–H is actually safe rather than merely plausible.

---

# Full branch diff (`git diff origin/master...feat/m1-203-defer-unsupported-types`)

```diff
diff --git a/docs/M1-202-NOTES.md b/docs/M1-202-NOTES.md
index 170d39b..9f8f378 100644
--- a/docs/M1-202-NOTES.md
+++ b/docs/M1-202-NOTES.md
@@ -156,3 +156,129 @@ that glob would break **every** existing question test.
 Per M1-201's review history, the fixture varies its subquestion ids, timestamps and question
 weights rather than reusing shared constants — shared-constant vacuity was a repeat finding
 there, and this fixture's entire purpose is that siblings differ.
+
+## M1-203 — Rejecting unsupported types safely
+
+Acceptance: *unsupported types create a diagnostic event and make zero model/submission
+calls.*
+
+### What the criterion is actually guarding against
+
+Half of it already held. `normalize_question` refused an unsupported tag before reading any
+field, so a `date` question could never reach a model or a submission call — that is D21's
+real safety property, and it was pinned from M1-201.
+
+The live defect was the other half. `normalize_questions` propagated the first failure, so a
+single deferred question **discarded the normalization of every supported question fetched
+alongside it**. On a tournament pull containing one date question, the batch returned
+nothing. And "diagnostic event" named a mechanism that existed nowhere in `src/` — neither
+`CODEX_HANDOFF.md` nor `CLAUDE_CODE_PROMPT.md` defines it.
+
+### Delivered
+
+- `questions/events.py` — `DeferralEvent` and `NormalizationResult`, frozen dataclasses.
+- Refusal is now **two-tier**. `normalize_question` (singular) still raises
+  `UnsupportedQuestionTypeError`, message byte-identical. `normalize_questions` (batch)
+  skips, records a `DeferralEvent` and logs at WARNING.
+- `normalize_questions` returns `NormalizationResult`, not `list[CanonicalQuestion]`.
+- Four defensive helpers (`_safe_attr`, `_type_tag`, `_supported_type`, `_safe_int`).
+- 332 tests (up from 323).
+
+### Decision — the event is an in-process value, not a ledger row
+
+The obvious reading of "diagnostic event" is a ledger row. It was rejected for now, on three
+grounds: M1-602 owns ledger writers and is Not Started; there is **no run or tournament
+context at this layer** to key a row on (nothing in `src/` even calls `normalize_questions`
+yet); and every `*_events` table in `001_initial.sql` is FK-bound to `forecast_records`,
+which by definition does not exist for a question refused before forecasting.
+
+A migration `003` would also have collided with two live parallel branches — CLAUDE.md's
+"migration numbers are claimed globally" gotcha.
+
+`DeferralEvent` is shaped so M1-602 can persist it later without rework, and lives in its own
+module importing only `model.py`, so a ledger writer can import it without dragging in the SDK.
+
+### Decision — the event carries `question_id`/`post_id`; the int gate is why that is safe
+
+This is the one deliberate reading of the no-echo rule as **scoped**, and it should be the
+first thing a reviewer pressure-tests.
+
+CLAUDE.md's rule is written about error messages: *"an error message never echoes
+stored/file/field values."* A `DeferralEvent` is not an error message; it is the diagnostic
+artifact the criterion demands. An event that says "3 questions were deferred" without saying
+*which* satisfies the criterion's words and fails its purpose — an operator cannot act on it.
+
+Carrying identity is safe **by construction, not by promise**. `_safe_int` returns `None` for
+anything that is not an `int` (and rejects `bool`, an `int` subclass). So the event contains
+**zero unvetted strings**: `reason` is a module literal, `question_type` is
+`_KNOWN_SDK_TYPES`-gated or `'unknown'`, and both ids are `int | None`. Hand it an object whose
+`id_of_question` is a leaked credential and the event carries `None` —
+`test_deferral_withholds_non_integer_identity` pins exactly that.
+
+**The duplicate-id error is unchanged and still withholds ids.** That is an error message
+interpolating into free prose, the softer reading there was already a review finding, and this
+does not reopen it.
+
+### Decision — `logging_setup.py` was not touched
+
+Values are interpolated into the log **message** with lazy `%` args, because
+`record.getMessage()` is already redacted twice (filter + formatter).
+
+An `extra`-field passthrough would have been *worse*, not just bigger: the redaction
+comprehension in `JsonFormatter.format` is `isinstance(value, str)` over **top-level values
+only**, so a dict or list arriving via `extra` sails past it untouched — a new leak class in
+the one module that must not have one.
+
+Cost, stated plainly: the record is JSON with a string message, so a machine consumer needs a
+regex until M1-602 gives deferrals a real row. Accepted — the machine-readable form today is
+the returned `DeferralEvent`.
+
+`test_deferral_log_record_is_not_a_leak_vector` renders a real record through the real
+`JsonFormatter` rather than asserting on `caplog.text`, since the formatter is what production
+writes.
+
+### Decision — only a deferred *type* is skipped
+
+The stricter reading (CLAUDE.md rule 4). A malformed *supported*-type question and a duplicate
+`question_id` both still raise and abort the batch. D21 defers date and conditional questions;
+it does not make malformed records survivable, and reporting a real defect as "deferred" would
+hide it behind a diagnostic that says the opposite.
+
+Uniqueness is checked over **accepted** questions only: a deferred question has no canonical
+model and never reaches the ledger, so it is not part of the contract that check protects.
+
+### Note — the tripwire test derives from `BaseException` deliberately
+
+`tripwire_question` arms every content attribute to raise on access, making explicit a
+guarantee the older `_OnlyTag` tests held only by accident (an object exposing just a tag
+proves "nothing crashed", not "nothing was read").
+
+It raises `_ContentFieldRead(BaseException)`, **not** `AssertionError`. `_safe_attr` swallows
+`Exception` by design, so an `AssertionError` tripwire would be caught and the test would pass
+vacuously the moment anyone routed a content read through that helper. This was verified by
+mutation: injecting `_safe_attr(q, "resolution_criteria")` into the deferral path fails the
+test.
+
+### Rejected — attaching the event to the exception
+
+`UnsupportedQuestionTypeError.event`, so the batch could catch and read it. It guarantees
+message/event agreement at one construction site, but puts state on a sanitized exception type
+whose entire contract is "nothing but a safe string". Extracting `_type_tag`, shared by both
+the message and the event, delivers the same drift protection without that.
+
+### Deferred (do not read the absence as an omission)
+
+- **Ledger persistence of deferrals → M1-602**, when writers and a run context exist.
+- **Structured `extra` fields on log records** — deliberately not built; see above.
+- **Golden valid/invalid fixture coverage remains Codex's T-901.** The tests here are the
+  minimum to stay honest, and none of them is a golden-record suite.
+
+### Standing note — no production caller yet
+
+Nothing in `src/` calls `normalize_questions`; both callers are tests. That is why the return
+type changed *now* rather than later — it is the cheapest this change will ever be. It also
+means the behavioural check for M1-203 **is** the test suite, with no end-to-end path to
+exercise until the pipeline lands.
+
+The M1-202 bullet above saying a deferred subquestion "still aborts the whole batch … is
+M1-203" is left as written: these notes are a historical record, and this section supersedes it.
diff --git a/docs/backlog/backlog.csv b/docs/backlog/backlog.csv
index c4ebe21..15caf90 100644
--- a/docs/backlog/backlog.csv
+++ b/docs/backlog/backlog.csv
@@ -10,8 +10,8 @@ M0-102,Metaculus Integration,Fetch current MiniBench questions,Fetch open questi
 M0-103,Metaculus Integration,Save and reload question snapshots,Persist normalized API snapshots for deterministic replay and tests.,M0-102,High,Claude Code,Saved snapshot round-trips without network access and retains question/post/tournament IDs.,S,Done,D12
 M0-104,Metaculus Integration,Add bot-testing-area fixture path,Support smoke-test targeting without changing MiniBench config.,M0-102,High,Claude Code,CLI can target `bot-testing-area` explicitly; production MiniBench remains unchanged.,S,Done,https://github.com/Metaculus/metac-bot-template/blob/main/main.py
 M1-201,Question Normalization,Define canonical question model,Map package question objects into a stable internal schema.,M0-103,Critical,Claude Code,"Golden binary, multiple-choice and numeric fixtures validate and retain resolution fine print.",M,Done,D20; D21
-M1-202,Question Normalization,Handle group-question unpacking,Preserve group parent identity while processing each subquestion as a forecastable record.,M1-201,High,Claude Code,Unpacked fixtures produce one unique internal question per subquestion and no duplicate IDs.,M,In Review,https://github.com/Metaculus/forecasting-tools
-M1-203,Question Normalization,Reject unsupported types safely,Flag date/conditional types as deferred instead of coercing or submitting.,M1-201,High,Claude Code,Unsupported types create a diagnostic event and make zero model/submission calls.,S,Not Started,D21
+M1-202,Question Normalization,Handle group-question unpacking,Preserve group parent identity while processing each subquestion as a forecastable record.,M1-201,High,Claude Code,Unpacked fixtures produce one unique internal question per subquestion and no duplicate IDs.,M,Done,https://github.com/Metaculus/forecasting-tools
+M1-203,Question Normalization,Reject unsupported types safely,Flag date/conditional types as deferred instead of coercing or submitting.,M1-201,High,Claude Code,Unsupported types create a diagnostic event and make zero model/submission calls.,S,In Review,D21
 M1-301,Retrieval,Define research-document schema,"Normalize provider results with stable source IDs, URLs, timestamps, hashes and reliability tags.",M0-005,Critical,Claude Code,"Pydantic schema preserves original URL, published/retrieved times and raw artifact reference.",M,Not Started,D17-D19
 M1-302,Retrieval,Implement AskNews adapter,Retrieve current and historical news while retaining article-level provenance.,M1-301,Critical,Claude Code,Mocked call returns normalized documents; missing credentials fail before a paid call.,M,Not Started,https://docs.asknews.app/
 M1-303,Retrieval,Implement Exa fallback,Use Exa only when AskNews fails or when official-source/web retrieval is required.,M1-301,High,Claude Code,Configured fallback records why it ran and preserves citations; no silent provider switching.,M,Not Started,https://exa.ai/pricing
diff --git a/src/whiskeyjack_bot/questions/__init__.py b/src/whiskeyjack_bot/questions/__init__.py
index d846c3a..ed53e12 100644
--- a/src/whiskeyjack_bot/questions/__init__.py
+++ b/src/whiskeyjack_bot/questions/__init__.py
@@ -1,5 +1,10 @@
 """Canonical question schema and normalization from the pinned SDK models."""
 
+from whiskeyjack_bot.questions.events import (
+    DeferralEvent,
+    DeferralReason,
+    NormalizationResult,
+)
 from whiskeyjack_bot.questions.groups import is_group_post, unpack_group_post
 from whiskeyjack_bot.questions.model import (
     CanonicalBinaryQuestion,
@@ -22,7 +27,10 @@ __all__ = [
     "CanonicalNumericQuestion",
     "CanonicalQuestion",
     "CanonicalQuestionAdapter",
+    "DeferralEvent",
+    "DeferralReason",
     "NormalizationError",
+    "NormalizationResult",
     "SourceCategory",
     "UnsupportedQuestionTypeError",
     "is_group_post",
diff --git a/src/whiskeyjack_bot/questions/events.py b/src/whiskeyjack_bot/questions/events.py
new file mode 100644
index 0000000..d7dfba0
--- /dev/null
+++ b/src/whiskeyjack_bot/questions/events.py
@@ -0,0 +1,65 @@
+"""Diagnostic value objects for question normalization (M1-203).
+
+A question whose type is deferred in v1 (D21) does not abort a batch and does not
+raise to the caller: :func:`~whiskeyjack_bot.questions.normalize.normalize_questions`
+skips it and records a :class:`DeferralEvent` on its result, so the batch's
+supported questions still normalize.
+
+**These are in-process values, not ledger rows.** Persisting a deferral belongs to
+the ledger writers (M1-602): there is no run or tournament context at this layer to
+key a row on, and the event exists to be returned to the caller and logged.
+
+The event is built to carry **no unvetted string**. ``reason`` is one of the two
+literals below, ``question_type`` is a member of the SDK's own tag enum or
+``'unknown'``, and both identity fields are gated to ``int`` -- a non-integer id
+becomes ``None`` rather than being carried. That is what lets the event be logged
+without widening the no-echo surface the rest of the package maintains.
+"""
+
+from __future__ import annotations
+
+from dataclasses import dataclass
+from typing import Literal
+
+from whiskeyjack_bot.questions.model import CanonicalQuestion
+
+# Two reasons rather than one: the ``_KNOWN_SDK_TYPES`` gate renders an unvetted tag
+# as 'unknown', which erases the difference between "a type the SDK defines and we
+# defer" and "something arbitrary reached the tag slot". The first is routine; the
+# second means a question object came from outside the SDK's own models. The
+# distinction is recoverable here without echoing the value.
+DeferralReason = Literal["deferred_v1_type", "unrecognized_type"]
+
+
+@dataclass(frozen=True)
+class DeferralEvent:
+    """One question skipped because its type is deferred in v1 (D21).
+
+    Identity is best-effort and read defensively: ``question_id`` and ``post_id``
+    are ``None`` when the question object does not expose an integer there. They
+    are carried at all -- unlike the duplicate-id error, which withholds ids --
+    because an operator cannot act on "one question was deferred". The no-echo rule
+    guards against a credential surfacing through free-text field values; the
+    ``int`` gate in ``normalize._safe_int`` makes that impossible here by
+    construction rather than by promise.
+    """
+
+    reason: DeferralReason
+    # A ``QuestionBasicType`` member, or 'unknown' for an unvetted tag.
+    question_type: str
+    question_id: int | None = None
+    post_id: int | None = None
+
+
+@dataclass(frozen=True)
+class NormalizationResult:
+    """The outcome of normalizing a batch: what was accepted, and what was deferred.
+
+    A value object rather than a ``(questions, deferrals)`` tuple, deliberately:
+    tuple-returning invites ``canonical, _ = normalize_questions(...)``, which
+    discards the deferrals in one character -- the exact silence M1-203 exists to
+    remove. Named access makes dropping them a choice.
+    """
+
+    questions: tuple[CanonicalQuestion, ...]
+    deferrals: tuple[DeferralEvent, ...] = ()
diff --git a/src/whiskeyjack_bot/questions/model.py b/src/whiskeyjack_bot/questions/model.py
index 67bc536..3e34e54 100644
--- a/src/whiskeyjack_bot/questions/model.py
+++ b/src/whiskeyjack_bot/questions/model.py
@@ -10,9 +10,10 @@ objects onto these models.
 
 Scope is fixed by decisions D20 (support binary, multiple-choice and numeric in
 v1) and D21 (defer date and conditional). Only the three supported types have a
-canonical model here; rejecting the deferred types is
-:mod:`whiskeyjack_bot.questions.normalize`'s job (and, as a diagnostic event,
-M1-203's).
+canonical model here; refusing the deferred types is
+:mod:`whiskeyjack_bot.questions.normalize`'s job, either by raising or -- on the
+batch path -- by skipping and recording a
+:class:`~whiskeyjack_bot.questions.events.DeferralEvent` (M1-203).
 
 The models are strict (``extra="forbid"``, reusing ``config._StrictModel``) so a
 malformed record fails validation loudly -- that is the M1-201 acceptance
diff --git a/src/whiskeyjack_bot/questions/normalize.py b/src/whiskeyjack_bot/questions/normalize.py
index 1d84f42..6971a9e 100644
--- a/src/whiskeyjack_bot/questions/normalize.py
+++ b/src/whiskeyjack_bot/questions/normalize.py
@@ -11,9 +11,18 @@ Type dispatch keys on the SDK's ``question_type`` literal rather than
 ``isinstance``: ``DiscreteQuestion`` subclasses ``NumericQuestion`` in the SDK,
 so an ``isinstance(q, NumericQuestion)`` test would silently swallow the
 unsupported ``discrete`` type. Only the three v1 types (D20) map; ``date``,
-``conditional``, ``discrete`` and anything else are refused with
-:class:`UnsupportedQuestionTypeError` (D21). Turning that refusal into a logged
-diagnostic event -- rather than an exception the caller must catch -- is M1-203.
+``conditional``, ``discrete`` and anything else are deferred (D21).
+
+Refusal is two-tier (M1-203). :func:`normalize_question` -- the single-question
+path, and the type-policy chokepoint -- *raises*
+:class:`UnsupportedQuestionTypeError`, so it can never return a question of a
+deferred type. :func:`normalize_questions` -- the batch path -- instead *skips*
+such a question, records a
+:class:`~whiskeyjack_bot.questions.events.DeferralEvent` on its result and logs
+it, so one deferred question no longer throws away the normalization of every
+supported question fetched alongside it. Everything else still aborts the batch:
+D21 defers date and conditional questions, it does not make malformed records
+survivable.
 
 Error hygiene matches ``ConfigError``/``SnapshotError``/``LedgerError``: a
 :class:`NormalizationError` never echoes field values (a mistakenly stored
@@ -22,12 +31,14 @@ secret must not surface), and sanitizing raises use ``from None``.
 
 from __future__ import annotations
 
+import logging
 from typing import Any, get_args
 
 from forecasting_tools.data_models.questions import MetaculusQuestion, QuestionBasicType
 from pydantic import ValidationError
 
 from whiskeyjack_bot.config import SupportedQuestionType
+from whiskeyjack_bot.questions.events import DeferralEvent, NormalizationResult
 from whiskeyjack_bot.questions.model import (
     CanonicalBinaryQuestion,
     CanonicalMultipleChoiceQuestion,
@@ -35,6 +46,8 @@ from whiskeyjack_bot.questions.model import (
     CanonicalQuestion,
 )
 
+logger = logging.getLogger(__name__)
+
 # Derived from the single source of truth in config (D20), so adding a type
 # there cannot leave this dispatch silently out of step.
 _SUPPORTED_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))
@@ -104,6 +117,73 @@ def _group_parent_title(q: MetaculusQuestion) -> str | None:
     return title
 
 
+def _safe_attr(q: MetaculusQuestion, attribute: str) -> object:
+    """Read one attribute with nothing allowed to escape.
+
+    ``getattr``'s default only suppresses ``AttributeError``; a property whose
+    getter raises anything else would escape this module's error boundary. These
+    reads happen on the *refusal* path, where an escaping exception would turn a
+    clean deferral into a crash -- and the value is unusable either way.
+    """
+    try:
+        return getattr(q, attribute, None)
+    except Exception:
+        return None
+
+
+def _type_tag(question_type: object) -> str:
+    """Render the type tag for an error message or a diagnostic event.
+
+    One helper for both so the exception text and the event cannot drift: a tag
+    outside the SDK's own enum reached that slot from outside the SDK's models and
+    is unvetted content under the no-echo rule.
+    """
+    if isinstance(question_type, str) and question_type in _KNOWN_SDK_TYPES:
+        return question_type
+    return "unknown"
+
+
+def _supported_type(q: MetaculusQuestion) -> str | None:
+    """The question's type if v1 supports it (D20), else ``None``.
+
+    isinstance before membership: an unhashable tag (a list, say) would raise a raw
+    ``TypeError`` out of the frozenset test itself, escaping the error boundary.
+    """
+    question_type = _safe_attr(q, "question_type")
+    if isinstance(question_type, str) and question_type in _SUPPORTED_TYPES:
+        return question_type
+    return None
+
+
+def _safe_int(q: MetaculusQuestion, attribute: str) -> int | None:
+    """Read one integer identity field, or ``None`` if it is not an integer.
+
+    The int gate is the no-echo guarantee for :class:`DeferralEvent`: a string in
+    an id slot -- which could be a mistakenly stored credential -- is dropped
+    rather than carried. ``bool`` is excluded explicitly since it subclasses
+    ``int``, and a ``True`` identity is a defect rather than an id.
+    """
+    value = _safe_attr(q, attribute)
+    if isinstance(value, bool) or not isinstance(value, int):
+        return None
+    return value
+
+
+def _deferral_event(q: MetaculusQuestion) -> DeferralEvent:
+    """Describe a question deferred under D21. Reads identity only.
+
+    No content field is touched, so a deferred question reaches no model and no
+    submission call -- and nothing that could carry a secret reaches the event.
+    """
+    tag = _type_tag(_safe_attr(q, "question_type"))
+    return DeferralEvent(
+        reason="deferred_v1_type" if tag != "unknown" else "unrecognized_type",
+        question_type=tag,
+        question_id=_safe_int(q, "id_of_question"),
+        post_id=_safe_int(q, "id_of_post"),
+    )
+
+
 def _common_fields(q: MetaculusQuestion) -> dict[str, Any]:
     """Read the fields shared by every supported type off the SDK object."""
     return {
@@ -140,18 +220,17 @@ def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
 
     Raises :class:`UnsupportedQuestionTypeError` for deferred types (D21) and
     :class:`NormalizationError` if a supported type fails canonical validation.
+
+    The singular path still *raises* on a deferred type, deliberately: it is the
+    type-policy chokepoint, and "an unsupported question can never reach a model"
+    is easiest to guarantee when this function cannot return one. Skipping with a
+    diagnostic event is a batch policy and lives on :func:`normalize_questions`.
     """
-    question_type = getattr(q, "question_type", None)
-    # isinstance before membership: an unhashable tag (a list, say) would raise a raw
-    # TypeError out of the frozenset test itself, escaping the boundary below.
-    if not isinstance(question_type, str) or question_type not in _SUPPORTED_TYPES:
+    question_type = _supported_type(q)
+    if question_type is None:
         # Refused before any field is read, so an unsupported type can never
         # reach a model or submission call (D21).
-        tag = (
-            question_type
-            if isinstance(question_type, str) and question_type in _KNOWN_SDK_TYPES
-            else "unknown"
-        )
+        tag = _type_tag(_safe_attr(q, "question_type"))
         raise UnsupportedQuestionTypeError(
             f"question type {tag!r} is not supported in v1 (binary, multiple_choice, numeric only)"
         )
@@ -202,32 +281,69 @@ def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
     raise AssertionError("unreachable: unhandled supported question type")
 
 
-def normalize_questions(questions: list[MetaculusQuestion]) -> list[CanonicalQuestion]:
-    """Normalize a list of SDK questions; propagates the first failure.
+def normalize_questions(questions: list[MetaculusQuestion]) -> NormalizationResult:
+    """Normalize a batch: defer unsupported types, propagate real failures (M1-203).
 
-    Enforces that ``question_id`` is unique across the batch (M1-202). Group
-    expansion is where this earns its keep: every subquestion of a group is built by
-    deep-copying the parent post, so siblings share ``post_id``, ``url`` and the
-    parent's framing fields, and ``question_id`` is the only thing telling them
-    apart. A duplicate here means either an expansion defect or the same question
-    fetched twice, and both would collide on the ledger's
+    A question whose type is deferred in v1 (D21) does **not** abort the batch. It
+    is skipped before any field is read, recorded as a
+    :class:`~whiskeyjack_bot.questions.events.DeferralEvent` on the result and
+    logged at WARNING -- so it makes zero model and zero submission calls while the
+    batch's supported questions still normalize.
+
+    **Everything else still aborts.** A *supported*-type question that fails
+    canonical validation, and a duplicate ``question_id``, both raise
+    :class:`NormalizationError`. D21 defers date and conditional questions; it does
+    not make malformed records survivable, and the stricter reading of an ambiguous
+    criterion is the project rule.
+
+    ``question_id`` uniqueness (M1-202) is enforced over the **accepted** questions
+    only. Group expansion is where that check earns its keep: every subquestion of a
+    group is built by deep-copying the parent post, so siblings share ``post_id``,
+    ``url`` and the parent's framing fields, and ``question_id`` is the only thing
+    telling them apart. A duplicate means either an expansion defect or the same
+    question fetched twice, and both would collide on the ledger's
     ``UNIQUE (question_id, tournament_id, forecast_version)`` -- but only after a
-    forecast had been generated and paid for. Failing at the boundary is cheaper.
+    forecast had been generated and paid for. A deferred question has no canonical
+    model and never reaches the ledger, so it is not part of that check.
     """
-    canonical = [normalize_question(q) for q in questions]
+    accepted: list[CanonicalQuestion] = []
+    deferrals: list[DeferralEvent] = []
+
+    for question in questions:
+        if _supported_type(question) is None:
+            event = _deferral_event(question)
+            deferrals.append(event)
+            # Logged inside the loop so deferrals stay visible even when a later
+            # question aborts the batch. Interpolated into the message rather than
+            # passed via ``extra``: JsonFormatter builds a fixed payload with no
+            # structured-field passthrough, and the message is a field it redacts,
+            # so every value here is already inside the redaction path.
+            logger.warning(
+                "deferring unsupported question type (D21): reason=%s question_type=%s "
+                "question_id=%s post_id=%s",
+                event.reason,
+                event.question_type,
+                event.question_id,
+                event.post_id,
+            )
+            continue
+        accepted.append(normalize_question(question))
 
     seen: set[int] = set()
     duplicates = 0
-    for question in canonical:
-        if question.question_id in seen:
+    for canonical in accepted:
+        if canonical.question_id in seen:
             duplicates += 1
-        seen.add(question.question_id)
+        seen.add(canonical.question_id)
     if duplicates:
         # Count only. The colliding id is low-risk content, but the no-echo rule is
-        # unconditional and the softer reading of it has been a review finding.
+        # unconditional for an error message and the softer reading of it has been a
+        # review finding. (DeferralEvent does carry ids: it is a diagnostic value
+        # rather than an error message, and its int gate makes a leak impossible --
+        # see events.py.)
         raise NormalizationError(
             f"question batch contains {duplicates} duplicate question id(s) "
             "(ids withheld: an error message never echoes record content)"
         )
 
-    return canonical
+    return NormalizationResult(questions=tuple(accepted), deferrals=tuple(deferrals))
diff --git a/tests/unit/test_groups.py b/tests/unit/test_groups.py
index ceab274..713a2d3 100644
--- a/tests/unit/test_groups.py
+++ b/tests/unit/test_groups.py
@@ -42,7 +42,7 @@ def raw_group_post() -> dict[str, Any]:
 
 
 def canonical_group() -> list[Any]:
-    return normalize_questions(unpack_group_post(raw_group_post()))
+    return list(normalize_questions(unpack_group_post(raw_group_post())).questions)
 
 
 # --- acceptance -------------------------------------------------------------
@@ -159,7 +159,7 @@ def test_group_parent_title_is_none_without_a_retained_payload() -> None:
     for question in questions:
         question.api_json = {}
 
-    for canonical in normalize_questions(questions):
+    for canonical in normalize_questions(questions).questions:
         assert canonical.question_ids_of_group
         assert canonical.group_parent_title is None
 
@@ -330,6 +330,14 @@ def test_deferred_subquestion_types_are_refused_by_normalize_not_unpack() -> Non
     questions = unpack_group_post(post)
     assert len(questions) == 3
 
+    # The batch defers it (M1-203) rather than aborting: the group's other two
+    # subquestions are forecastable and must survive their sibling's type.
+    result = normalize_questions(questions)
+    assert len(result.questions) == 2
+    assert len(result.deferrals) == 1
+    assert result.deferrals[0].question_type == "date"
+
+    # The singular path is still the chokepoint: it cannot return a deferred type.
     with pytest.raises(UnsupportedQuestionTypeError) as excinfo:
-        normalize_questions(questions)
+        normalize_question(questions[1])
     assert "date" in str(excinfo.value)
diff --git a/tests/unit/test_questions.py b/tests/unit/test_questions.py
index 4d2dc3e..3e2a4c0 100644
--- a/tests/unit/test_questions.py
+++ b/tests/unit/test_questions.py
@@ -6,7 +6,9 @@ question can never reach a model or submission call. Comprehensive valid/invalid
 golden records are Codex's T-901; this suite covers the model + mapping only.
 """
 
+import dataclasses
 import json
+import logging
 import traceback
 from datetime import datetime, timezone
 from pathlib import Path
@@ -24,12 +26,14 @@ from forecasting_tools.data_models.questions import (
 )
 from pydantic import ValidationError
 
+from whiskeyjack_bot.logging_setup import JsonFormatter
 from whiskeyjack_bot.questions import (
     CanonicalBinaryQuestion,
     CanonicalMultipleChoiceQuestion,
     CanonicalNumericQuestion,
     CanonicalQuestionAdapter,
     NormalizationError,
+    NormalizationResult,
     SourceCategory,
     UnsupportedQuestionTypeError,
     normalize_question,
@@ -53,11 +57,11 @@ def raw_post(name: str) -> dict[str, Any]:
 
 
 def normalized_by_type() -> dict[str, Any]:
-    return {q.qtype: q for q in normalize_questions(load_fixture_questions())}
+    return {q.qtype: q for q in normalize_questions(load_fixture_questions()).questions}
 
 
 def test_fixtures_normalize_to_expected_canonical_types() -> None:
-    canonical = normalize_questions(load_fixture_questions())
+    canonical = normalize_questions(load_fixture_questions()).questions
     assert {type(q) for q in canonical} == {
         CanonicalBinaryQuestion,
         CanonicalMultipleChoiceQuestion,
@@ -138,7 +142,7 @@ def test_numeric_bounds_preserved() -> None:
 
 
 def test_canonical_questions_round_trip_through_the_union_adapter() -> None:
-    for canonical in normalize_questions(load_fixture_questions()):
+    for canonical in normalize_questions(load_fixture_questions()).questions:
         restored = CanonicalQuestionAdapter.validate_python(canonical.model_dump())
         assert restored == canonical
         assert type(restored) is type(canonical)
@@ -484,9 +488,32 @@ def test_unsupported_error_is_a_normalization_error() -> None:
     assert issubclass(UnsupportedQuestionTypeError, NormalizationError)
 
 
-def test_normalize_questions_propagates_the_first_failure() -> None:
-    with pytest.raises(UnsupportedQuestionTypeError):
-        normalize_questions([*load_fixture_questions(), _synthetic_date_question()])
+def test_a_deferred_type_does_not_abort_the_batch() -> None:
+    """One deferred question no longer discards the batch it arrived in (M1-203).
+
+    Before M1-203 the batch propagated the first failure, so a single date question
+    in a tournament pull threw away the normalization of every supported question
+    fetched alongside it.
+    """
+    result = normalize_questions([*load_fixture_questions(), _synthetic_date_question()])
+
+    assert len(result.questions) == 3
+    assert len(result.deferrals) == 1
+    assert result.deferrals[0].question_type == "date"
+    assert result.deferrals[0].reason == "deferred_v1_type"
+
+
+def test_a_malformed_supported_question_still_aborts_the_batch() -> None:
+    """D21 defers date and conditional; it does not make malformed records survivable.
+
+    The stricter reading of the M1-203 criterion: only a deferred *type* is skipped.
+    A numeric question missing the bounds its own type declares is a real defect, and
+    silently dropping it would hide it behind a diagnostic that says "deferred".
+    """
+    malformed = fake_sdk_question(question_type="numeric")
+
+    with pytest.raises(NormalizationError, match="does not expose the fields"):
+        normalize_questions([_synthetic_date_question(), malformed])
 
 
 # --- schema integrity: finite floats ----------------------------------------
@@ -587,3 +614,186 @@ def test_option_validation_errors_never_echo_the_labels() -> None:
         )
     assert PLANTED_SECRET not in str(excinfo.value)
     assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
+
+
+# --- deferral events (M1-203) -----------------------------------------------
+
+# Every attribute normalize reads for *content*, as opposed to identity. The
+# deferral path may read id_of_question/id_of_post; touching anything here means a
+# deferred question got further into normalization than D21 allows.
+_CONTENT_ATTRS = (
+    "page_url",
+    "question_text",
+    "background_info",
+    "resolution_criteria",
+    "fine_print",
+    "unit_of_measure",
+    "open_time",
+    "close_time",
+    "scheduled_resolution_time",
+    "tournament_slugs",
+    "question_weight",
+    "categories",
+    "group_question_option",
+    "question_ids_of_group",
+    "api_json",
+    "options",
+    "option_is_instance_of",
+    "lower_bound",
+    "upper_bound",
+    "open_lower_bound",
+    "open_upper_bound",
+    "zero_point",
+    "cdf_size",
+    "nominal_lower_bound",
+    "nominal_upper_bound",
+)
+
+
+class _ContentFieldRead(BaseException):
+    """Raised by a tripwire attribute. Deliberately **not** an ``Exception``.
+
+    ``normalize._safe_attr`` swallows ``Exception`` by design, so a tripwire raising
+    ``AssertionError`` would be caught and the test would pass vacuously the moment
+    someone routed a content read through that helper. Deriving from
+    ``BaseException`` puts the tripwire outside every ``except Exception`` in the
+    module under test, which is the only way it can actually fail the build.
+    """
+
+
+def tripwire_question(tag: object) -> object:
+    """A question whose every content attribute raises when read.
+
+    The explicit form of a guarantee the ``_OnlyTag`` tests hold only by accident:
+    an object exposing just a tag proves "nothing crashed", not "nothing was read".
+    Here identity is readable and content is armed, so the distinction is tested
+    rather than assumed.
+    """
+
+    def _boom(self: object) -> object:
+        raise _ContentFieldRead("normalization read a content field of a deferred question")
+
+    namespace: dict[str, object] = {
+        "question_type": tag,
+        "id_of_question": 91001,
+        "id_of_post": 90001,
+    }
+    for name in _CONTENT_ATTRS:
+        namespace[name] = property(_boom)
+    return type("_Tripwire", (), namespace)()
+
+
+def test_deferral_carries_integer_identity() -> None:
+    """An operator cannot act on "one question was deferred"; the event says which."""
+    result = normalize_questions([fake_sdk_question(question_type="date")])  # type: ignore[list-item]
+
+    assert len(result.questions) == 0
+    (event,) = result.deferrals
+    assert event.question_id == 91001
+    assert event.post_id == 90001
+
+
+def test_deferral_withholds_non_integer_identity() -> None:
+    """The int gate is what makes carrying identity safe under the no-echo rule.
+
+    A credential mistakenly stored in an id slot is dropped rather than carried, so
+    the event holds no unvetted string by construction rather than by promise.
+    """
+    result = normalize_questions(
+        [
+            fake_sdk_question(  # type: ignore[list-item]
+                question_type="date",
+                id_of_question=PLANTED_SECRET,
+                id_of_post=PLANTED_SECRET,
+            )
+        ]
+    )
+
+    (event,) = result.deferrals
+    assert event.question_id is None
+    assert event.post_id is None
+    assert PLANTED_SECRET not in repr(event)
+
+
+def test_deferral_event_names_only_known_sdk_types() -> None:
+    """Same gate as the error message, and the reason records that it fired.
+
+    Collapsing an unvetted tag to 'unknown' would otherwise erase the difference
+    between a type the SDK defines and something arbitrary in the tag slot.
+    """
+
+    class _ForeignTag:
+        question_type = PLANTED_SECRET
+        id_of_question = 91001
+        id_of_post = 90001
+
+    result = normalize_questions([_ForeignTag()])  # type: ignore[list-item]
+
+    (event,) = result.deferrals
+    assert event.question_type == "unknown"
+    assert event.reason == "unrecognized_type"
+    assert PLANTED_SECRET not in repr(event)
+
+
+def test_deferral_reads_no_content_field() -> None:
+    """Zero model and zero submission calls, enforced at the field-access level."""
+    result = normalize_questions([tripwire_question("date")])  # type: ignore[list-item]
+
+    assert len(result.questions) == 0
+    assert result.deferrals[0].question_type == "date"
+
+
+def test_deferral_log_record_is_not_a_leak_vector() -> None:
+    """The event is logged, and the rendered record carries no question content.
+
+    Rendered through the real ``JsonFormatter`` rather than asserting on
+    ``caplog.text``: the formatter is what production writes, and the values are
+    interpolated into the message precisely because that field is redacted.
+    """
+    question = fake_sdk_question(
+        question_type="date",
+        resolution_criteria=PLANTED_SECRET,
+        question_text=PLANTED_SECRET,
+    )
+
+    logger = logging.getLogger("whiskeyjack_bot.questions.normalize")
+    records: list[logging.LogRecord] = []
+    handler = logging.Handler()
+    handler.emit = records.append  # type: ignore[method-assign]
+    logger.addHandler(handler)
+    try:
+        normalize_questions([question])  # type: ignore[list-item]
+    finally:
+        logger.removeHandler(handler)
+
+    (record,) = [r for r in records if r.levelno == logging.WARNING]
+    rendered = JsonFormatter([]).format(record)
+    payload = json.loads(rendered)
+
+    assert payload["level"] == "WARNING"
+    assert "91001" in payload["message"]
+    assert "date" in payload["message"]
+    assert PLANTED_SECRET not in rendered
+
+
+def test_duplicate_check_ignores_deferred_questions() -> None:
+    """A deferred question has no canonical model and never reaches the ledger.
+
+    The uniqueness check exists to protect the ledger's
+    ``UNIQUE (question_id, tournament_id, forecast_version)``; a deferred question
+    sharing an id with an accepted one is not a collision.
+    """
+    accepted = fake_sdk_question(question_type="binary", id_of_question=91001)
+    deferred = fake_sdk_question(question_type="date", id_of_question=91001)
+
+    result = normalize_questions([accepted, deferred])  # type: ignore[list-item]
+
+    assert len(result.questions) == 1
+    assert len(result.deferrals) == 1
+
+
+def test_normalization_result_is_frozen() -> None:
+    """A value object, per the project convention for internal results."""
+    result = NormalizationResult(questions=(), deferrals=())
+    with pytest.raises(dataclasses.FrozenInstanceError):
+        result.questions = ()  # type: ignore[misc]
diff --git a/whiskeyjack-bot-v1-backlog.xlsx b/whiskeyjack-bot-v1-backlog.xlsx
index d2a5a07..a8f0055 100644
Binary files a/whiskeyjack-bot-v1-backlog.xlsx and b/whiskeyjack-bot-v1-backlog.xlsx differ
```
