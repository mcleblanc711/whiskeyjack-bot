# Cross-model review request — whiskeyjack-bot M1-203 (round 2)

You previously reviewed M1-203 of `whiskeyjack-bot` (defer unsupported question types) and
returned **changes requested** with two blocking findings against the error-hygiene guarantees.
Both are now fixed. Re-review the round-2 changes below with the same **stricter reading** —
assume the wrong reading and prove it can't happen from the diff. Do not rubber-stamp. Confirm
each finding is actually closed, or list what remains.

The full round-1 request (spec, decisions, risk areas A–H) is in
`GPT_REVIEW_REQUEST_M1-203.md`; this file covers only what changed in round 2.

## Project invariants that bear on this (from CLAUDE.md)

- An **error message** never echoes stored/file/field values, and sanitizing raises use
  `from None`.
- Every malformed shape must arrive as the module's own error type (`NormalizationError`) — a raw
  `AttributeError`/`KeyError`/`ValueError` escaping is a finding.
- Never print or persist secrets.
- Internal value objects are `@dataclass(frozen=True)`.
- Dispatch on the `question_type` literal, never `isinstance` — `DiscreteQuestion` subclasses
  `NumericQuestion` in the pinned SDK.

All four gates pass: `pytest` 336 (was 332), `ruff check`, `ruff format --check`,
`mypy --strict src`.

## The two blocking findings, and the fixes

**Finding 1 (was blocking) — `isinstance` gates leaked through subclass rendering.** A `str`
*subclass* whose value is a known tag passed the `_KNOWN_SDK_TYPES` membership check while its
`__str__`/`__repr__` rendered an attacker-controlled payload through the `%s` WARNING log and
`DeferralEvent.__repr__`; `IntEnum`/`int` subclasses in id slots did the same.

Fix: **exact-type gates** — `type(x) is str` in `_type_tag`/`_supported_type`, and
`type(v) is int and v > 0` in `_safe_int`. Anything not exactly the built-in type degrades to
`'unknown'`/`None`, so only a built-in's (payload-free) rendering can run. A `str`-subclass
valued `"binary"` is now deferred as unknown rather than normalized (stricter reading). Because a
frozen dataclass's annotations do not validate, **`DeferralEvent.__post_init__` now enforces the
invariant on the exported dataclass itself**, coercing every unsafe field to a safe module-owned
value regardless of how the event was constructed.

*Decision — coerce, not raise.* `__post_init__` coerces (chosen over raising) to match how ids
already degrade to `None`, to keep a diagnostic value from turning a deferral into a crash, and to
avoid events.py needing to own or lazy-import a sanitized exception to dodge the
events↔normalize circular import. If you still read "drop the ids" as the stronger option, that
remains a one-line change; the coercion makes a leak impossible either way.

**Finding 2 (was blocking) — reading `question_type` via the blanket-swallowing `_safe_attr` hid
malformed records.** A `question_type` getter that *raised* was swallowed into an
`unrecognized_type` deferral, hiding the defect. Fix: a dedicated `_read_question_type` converts a
failing getter into a constant-message `NormalizationError … from None` (aborting the batch, as a
malformed record must). The type is read **once** and threaded through classification, event
creation and the error message — no double getter call, no inconsistent result from a stateful
getter. `_safe_attr` now guards the optional *identity* reads only. `_build_canonical` was
extracted so the batch path reuses the single read instead of re-reading through
`normalize_question`.

## Pressure-test these specifically

1. Is there any remaining path by which a non-exact `str`/`int` (a subclass, an `IntEnum`, an
   object with `__index__`) reaches a rendered `DeferralEvent` field or the WARNING log?
2. Does `__post_init__` coercion (rather than raising) fully uphold the no-echo invariant for
   **any** directly-constructed event, including the `reason`↔`question_type` coherence repair
   (an 'unknown' tag forcing `reason='unrecognized_type'`)?
3. Does `_read_question_type` correctly distinguish a *raising* getter (malformed → abort) from a
   *readable-but-weird* value (`None`/`[]`/foreign `str` → deferred as unknown)? Verify the batch
   aborts on the former and still defers on the latter.
4. Is the single-read threading actually consistent across `normalize_question`, the batch loop,
   `_deferral_event`, and the error message — no residual second getter call, and no place a
   stateful getter could still be read twice?
5. Are the four new regression tests airtight, or do any pass vacuously?

## Output format

Findings as: **Severity (Blocking / High / Medium / Nit) — file:line — what's wrong — why it
matters — suggested fix.** Then an overall verdict. If you approve, justify why each finding is
actually closed rather than merely plausibly closed.

---

# Round-2 diff (`git diff 5c55a8b..ac96af3 -- src/ tests/`)

```diff
diff --git a/src/whiskeyjack_bot/questions/events.py b/src/whiskeyjack_bot/questions/events.py
index d7dfba0..77dc609 100644
--- a/src/whiskeyjack_bot/questions/events.py
+++ b/src/whiskeyjack_bot/questions/events.py
@@ -19,7 +19,9 @@ without widening the no-echo surface the rest of the package maintains.
 from __future__ import annotations
 
 from dataclasses import dataclass
-from typing import Literal
+from typing import Literal, get_args
+
+from forecasting_tools.data_models.questions import QuestionBasicType
 
 from whiskeyjack_bot.questions.model import CanonicalQuestion
 
@@ -30,6 +32,12 @@ from whiskeyjack_bot.questions.model import CanonicalQuestion
 # distinction is recoverable here without echoing the value.
 DeferralReason = Literal["deferred_v1_type", "unrecognized_type"]
 
+# The exact-type/membership sets ``__post_init__`` canonicalizes against. The SDK's
+# own six-value tag enum is the only vocabulary safe to name; anything else renders
+# as 'unknown'. ``frozenset`` so membership is a hash lookup, not a linear scan.
+_KNOWN_SDK_TYPES: frozenset[str] = frozenset(get_args(QuestionBasicType))
+_REASONS: frozenset[str] = frozenset(get_args(DeferralReason))
+
 
 @dataclass(frozen=True)
 class DeferralEvent:
@@ -39,9 +47,18 @@ class DeferralEvent:
     are ``None`` when the question object does not expose an integer there. They
     are carried at all -- unlike the duplicate-id error, which withholds ids --
     because an operator cannot act on "one question was deferred". The no-echo rule
-    guards against a credential surfacing through free-text field values; the
-    ``int`` gate in ``normalize._safe_int`` makes that impossible here by
-    construction rather than by promise.
+    guards against a credential surfacing through free-text field values.
+
+    The no-echo invariant is enforced here in ``__post_init__``, not only by the
+    ``normalize`` helpers that usually build the event: a frozen dataclass's field
+    annotations do not validate, so a value object exported from the package must
+    uphold the guarantee itself. ``__post_init__`` coerces every field to a safe,
+    module-owned value using **exact-type** checks -- ``type(x) is str``/``int``,
+    not ``isinstance`` -- because a ``str``/``int`` *subclass* (or an ``IntEnum``)
+    can carry attacker-controlled ``__str__``/``__repr__`` whose value slips past a
+    membership check and renders through the log or this dataclass's repr. Anything
+    that is not exactly the built-in type degrades to ``'unknown'``/``None``, so a
+    leak is impossible by construction rather than by promise.
     """
 
     reason: DeferralReason
@@ -50,6 +67,24 @@ class DeferralEvent:
     question_id: int | None = None
     post_id: int | None = None
 
+    def __post_init__(self) -> None:
+        # Coerce, don't raise: a diagnostic value degrades to a safe placeholder the
+        # same way ids already drop to None, rather than turning a deferral into a
+        # crash. Exact-type checks so only a built-in str/int -- whose rendering can
+        # carry no attacker payload -- is ever kept.
+        if type(self.question_type) is not str or self.question_type not in _KNOWN_SDK_TYPES:
+            object.__setattr__(self, "question_type", "unknown")
+        if type(self.reason) is not str or self.reason not in _REASONS:
+            object.__setattr__(self, "reason", "unrecognized_type")
+        # An 'unknown' tag means the SDK never vouched for this record's type, so the
+        # reason must record that it was unrecognized rather than a routine deferral.
+        if self.question_type == "unknown":
+            object.__setattr__(self, "reason", "unrecognized_type")
+        for field in ("question_id", "post_id"):
+            value = getattr(self, field)
+            if value is not None and (type(value) is not int or value <= 0):
+                object.__setattr__(self, field, None)
+
 
 @dataclass(frozen=True)
 class NormalizationResult:
diff --git a/src/whiskeyjack_bot/questions/normalize.py b/src/whiskeyjack_bot/questions/normalize.py
index 6971a9e..2087596 100644
--- a/src/whiskeyjack_bot/questions/normalize.py
+++ b/src/whiskeyjack_bot/questions/normalize.py
@@ -118,12 +118,15 @@ def _group_parent_title(q: MetaculusQuestion) -> str | None:
 
 
 def _safe_attr(q: MetaculusQuestion, attribute: str) -> object:
-    """Read one attribute with nothing allowed to escape.
+    """Read one *optional identity* attribute with nothing allowed to escape.
 
     ``getattr``'s default only suppresses ``AttributeError``; a property whose
     getter raises anything else would escape this module's error boundary. These
-    reads happen on the *refusal* path, where an escaping exception would turn a
-    clean deferral into a crash -- and the value is unusable either way.
+    reads happen on the *refusal* path for fields whose absence is tolerable
+    (``id_of_question``/``id_of_post``): an escaping exception would turn a clean
+    deferral into a crash, and the value is unusable either way. It is **not** used
+    for ``question_type`` -- a type that cannot be read is a malformed record, not
+    a missing id (see :func:`_read_question_type`).
     """
     try:
         return getattr(q, attribute, None)
@@ -131,51 +134,79 @@ def _safe_attr(q: MetaculusQuestion, attribute: str) -> object:
         return None
 
 
+def _read_question_type(q: MetaculusQuestion) -> object:
+    """Read ``question_type`` once, turning a failing getter into our own error.
+
+    The type is not optional: a ``question_type`` property that *raises* is a
+    malformed record or an SDK contract violation, and the project rule is that
+    every malformed shape arrives as a :class:`NormalizationError` rather than a
+    raw exception -- or, worse, being swallowed into a deferral that hides the
+    defect. Constant message + ``from None``: the getter's exception can carry the
+    object's field values, so it must not resurface through the text or the cause
+    chain. Best-effort swallowing is reserved for the optional identity reads.
+    """
+    try:
+        return q.question_type
+    except Exception:
+        raise NormalizationError(
+            "cannot read question type (detail withheld: it can echo question contents)"
+        ) from None
+
+
 def _type_tag(question_type: object) -> str:
     """Render the type tag for an error message or a diagnostic event.
 
-    One helper for both so the exception text and the event cannot drift: a tag
-    outside the SDK's own enum reached that slot from outside the SDK's models and
-    is unvetted content under the no-echo rule.
+    One helper for both so the exception text and the event cannot drift. Exact
+    ``type(...) is str`` rather than ``isinstance``: a ``str`` *subclass* can pass
+    a membership check on its value while its ``__str__``/``__repr__`` renders an
+    unvetted payload, so anything that is not exactly ``str`` -- and any value
+    outside the SDK's own enum -- renders as the module-owned literal ``'unknown'``.
     """
-    if isinstance(question_type, str) and question_type in _KNOWN_SDK_TYPES:
+    if type(question_type) is str and question_type in _KNOWN_SDK_TYPES:
         return question_type
     return "unknown"
 
 
-def _supported_type(q: MetaculusQuestion) -> str | None:
-    """The question's type if v1 supports it (D20), else ``None``.
+def _supported_type(question_type: object) -> str | None:
+    """The (already-read) type if v1 supports it (D20), else ``None``.
 
-    isinstance before membership: an unhashable tag (a list, say) would raise a raw
-    ``TypeError`` out of the frozenset test itself, escaping the error boundary.
+    Exact ``type(...) is str`` for the same reason as :func:`_type_tag`: a ``str``
+    subclass is unvetted, so a subclass valued ``"binary"`` is deferred as unknown
+    rather than normalized. The exact-type test also runs before membership, so an
+    unhashable tag (a list, say) never reaches the frozenset test that would raise
+    a raw ``TypeError`` out of the error boundary.
     """
-    question_type = _safe_attr(q, "question_type")
-    if isinstance(question_type, str) and question_type in _SUPPORTED_TYPES:
+    if type(question_type) is str and question_type in _SUPPORTED_TYPES:
         return question_type
     return None
 
 
 def _safe_int(q: MetaculusQuestion, attribute: str) -> int | None:
-    """Read one integer identity field, or ``None`` if it is not an integer.
+    """Read one integer identity field, or ``None`` if it is not a usable id.
 
     The int gate is the no-echo guarantee for :class:`DeferralEvent`: a string in
     an id slot -- which could be a mistakenly stored credential -- is dropped
-    rather than carried. ``bool`` is excluded explicitly since it subclasses
-    ``int``, and a ``True`` identity is a defect rather than an id.
+    rather than carried. Exact ``type(value) is int`` rather than ``isinstance``:
+    it rejects ``bool`` (a ``True`` identity is a defect), an ``IntEnum`` (whose
+    repr embeds its class/member name), and any other ``int`` subclass with an
+    attacker-controlled ``__repr__``. A non-positive id is a defect, not an id.
     """
     value = _safe_attr(q, attribute)
-    if isinstance(value, bool) or not isinstance(value, int):
+    if type(value) is not int or value <= 0:
         return None
     return value
 
 
-def _deferral_event(q: MetaculusQuestion) -> DeferralEvent:
-    """Describe a question deferred under D21. Reads identity only.
+def _deferral_event(q: MetaculusQuestion, question_type: object) -> DeferralEvent:
+    """Describe a question deferred under D21 from its already-read type.
 
-    No content field is touched, so a deferred question reaches no model and no
-    submission call -- and nothing that could carry a secret reaches the event.
+    Takes the single ``question_type`` read threaded from the caller so a stateful
+    getter cannot yield a different tag here than the one classification saw. Reads
+    identity only -- no content field is touched, so a deferred question reaches no
+    model and no submission call, and nothing that could carry a secret reaches the
+    event. ``DeferralEvent`` re-canonicalizes every field regardless (see events.py).
     """
-    tag = _type_tag(_safe_attr(q, "question_type"))
+    tag = _type_tag(question_type)
     return DeferralEvent(
         reason="deferred_v1_type" if tag != "unknown" else "unrecognized_type",
         question_type=tag,
@@ -218,23 +249,35 @@ def _common_fields(q: MetaculusQuestion) -> dict[str, Any]:
 def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
     """Map one SDK question onto its canonical model.
 
-    Raises :class:`UnsupportedQuestionTypeError` for deferred types (D21) and
-    :class:`NormalizationError` if a supported type fails canonical validation.
+    Raises :class:`UnsupportedQuestionTypeError` for deferred types (D21),
+    :class:`NormalizationError` if a supported type fails canonical validation, and
+    :class:`NormalizationError` if ``question_type`` itself cannot be read.
 
     The singular path still *raises* on a deferred type, deliberately: it is the
     type-policy chokepoint, and "an unsupported question can never reach a model"
     is easiest to guarantee when this function cannot return one. Skipping with a
     diagnostic event is a batch policy and lives on :func:`normalize_questions`.
     """
-    question_type = _supported_type(q)
-    if question_type is None:
+    question_type = _read_question_type(q)
+    supported = _supported_type(question_type)
+    if supported is None:
         # Refused before any field is read, so an unsupported type can never
-        # reach a model or submission call (D21).
-        tag = _type_tag(_safe_attr(q, "question_type"))
+        # reach a model or submission call (D21). The tag is rendered from the same
+        # single read used for classification.
         raise UnsupportedQuestionTypeError(
-            f"question type {tag!r} is not supported in v1 (binary, multiple_choice, numeric only)"
+            f"question type {_type_tag(question_type)!r} is not supported in v1 "
+            "(binary, multiple_choice, numeric only)"
         )
+    return _build_canonical(q, supported)
+
 
+def _build_canonical(q: MetaculusQuestion, question_type: str) -> CanonicalQuestion:
+    """Construct the canonical model for an already-classified supported type.
+
+    Split out so the batch path (:func:`normalize_questions`) can build an accepted
+    question from the single ``question_type`` read it already has, rather than
+    re-reading it through :func:`normalize_question`.
+    """
     # Field reads are fenced separately from model construction: a TypeError raised
     # while building a canonical model is our bug, and must stay visible rather than
     # being reported as a malformed input record.
@@ -291,10 +334,11 @@ def normalize_questions(questions: list[MetaculusQuestion]) -> NormalizationResu
     batch's supported questions still normalize.
 
     **Everything else still aborts.** A *supported*-type question that fails
-    canonical validation, and a duplicate ``question_id``, both raise
-    :class:`NormalizationError`. D21 defers date and conditional questions; it does
-    not make malformed records survivable, and the stricter reading of an ambiguous
-    criterion is the project rule.
+    canonical validation, a duplicate ``question_id``, and a ``question_type`` that
+    cannot even be read (a failing getter -- a malformed record, not a deferrable
+    type) all raise :class:`NormalizationError`. D21 defers date and conditional
+    questions; it does not make malformed records survivable, and the stricter
+    reading of an ambiguous criterion is the project rule.
 
     ``question_id`` uniqueness (M1-202) is enforced over the **accepted** questions
     only. Group expansion is where that check earns its keep: every subquestion of a
@@ -310,8 +354,14 @@ def normalize_questions(questions: list[MetaculusQuestion]) -> NormalizationResu
     deferrals: list[DeferralEvent] = []
 
     for question in questions:
-        if _supported_type(question) is None:
-            event = _deferral_event(question)
+        # Read the type once: a getter that raises is a malformed record and aborts
+        # the batch (D21 defers unsupported *types*, it does not swallow malformed
+        # records), and threading the single read on avoids a stateful getter
+        # classifying and describing the same question inconsistently.
+        question_type = _read_question_type(question)
+        supported = _supported_type(question_type)
+        if supported is None:
+            event = _deferral_event(question, question_type)
             deferrals.append(event)
             # Logged inside the loop so deferrals stay visible even when a later
             # question aborts the batch. Interpolated into the message rather than
@@ -327,7 +377,7 @@ def normalize_questions(questions: list[MetaculusQuestion]) -> NormalizationResu
                 event.post_id,
             )
             continue
-        accepted.append(normalize_question(question))
+        accepted.append(_build_canonical(question, supported))
 
     seen: set[int] = set()
     duplicates = 0
diff --git a/tests/unit/test_questions.py b/tests/unit/test_questions.py
index 3e2a4c0..3abec28 100644
--- a/tests/unit/test_questions.py
+++ b/tests/unit/test_questions.py
@@ -7,13 +7,14 @@ golden records are Codex's T-901; this suite covers the model + mapping only.
 """
 
 import dataclasses
+import enum
 import json
 import logging
 import traceback
 from datetime import datetime, timezone
 from pathlib import Path
 from types import SimpleNamespace
-from typing import Any
+from typing import Any, get_args
 
 import pytest
 from forecasting_tools.data_models.data_organizer import DataOrganizer
@@ -23,6 +24,7 @@ from forecasting_tools.data_models.questions import (
     DiscreteQuestion,
     MetaculusQuestion,
     NumericQuestion,
+    QuestionBasicType,
 )
 from pydantic import ValidationError
 
@@ -32,6 +34,7 @@ from whiskeyjack_bot.questions import (
     CanonicalMultipleChoiceQuestion,
     CanonicalNumericQuestion,
     CanonicalQuestionAdapter,
+    DeferralEvent,
     NormalizationError,
     NormalizationResult,
     SourceCategory,
@@ -43,6 +46,10 @@ from whiskeyjack_bot.questions import (
 FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
 API_POSTS = FIXTURES / "api_posts"
 
+# The SDK's own tag enum, used to prove a str subclass's *value* would pass a
+# membership gate even though its rendering is unvetted.
+_KNOWN_TAG_SET = frozenset(get_args(QuestionBasicType))
+
 
 def load_fixture_questions() -> list[MetaculusQuestion]:
     posts = sorted(API_POSTS.glob("*_post.json"))
@@ -776,6 +783,153 @@ def test_deferral_log_record_is_not_a_leak_vector() -> None:
     assert PLANTED_SECRET not in rendered
 
 
+# --- subclass rendering / exact-type gates (GPT round-2 findings) -----------
+
+
+class _EvilStr(str):
+    """A ``str`` subclass whose value is vetted but whose rendering leaks.
+
+    ``isinstance(x, str)`` accepts it and a membership check passes on its value,
+    yet ``%s``/``repr`` invoke the overridden methods -- the exact bypass the
+    exact-type gates close.
+    """
+
+    def __repr__(self) -> str:
+        return PLANTED_SECRET
+
+    def __str__(self) -> str:
+        return PLANTED_SECRET
+
+
+class _EvilInt(int):
+    """An ``int`` subclass with the same attacker-controlled rendering."""
+
+    def __repr__(self) -> str:
+        return PLANTED_SECRET
+
+    def __str__(self) -> str:
+        return PLANTED_SECRET
+
+
+def test_str_subclass_tag_is_not_carried_into_event_or_log() -> None:
+    """A ``str`` subclass valued as a known tag renders 'unknown', not its payload.
+
+    Its value ``"date"`` would pass any membership check, but ``type() is str`` is
+    false, so the tag drops to the module-owned literal and the leaking ``__str__``/
+    ``__repr__`` never runs on a carried value -- in the event or the WARNING record.
+    """
+    tag = _EvilStr("date")
+    assert tag in _KNOWN_TAG_SET  # sanity: value alone would slip a membership gate
+
+    logger = logging.getLogger("whiskeyjack_bot.questions.normalize")
+    records: list[logging.LogRecord] = []
+    handler = logging.Handler()
+    handler.emit = records.append  # type: ignore[method-assign]
+    logger.addHandler(handler)
+    try:
+        result = normalize_questions([fake_sdk_question(question_type=tag)])  # type: ignore[list-item]
+    finally:
+        logger.removeHandler(handler)
+
+    (event,) = result.deferrals
+    assert event.question_type == "unknown"
+    assert event.reason == "unrecognized_type"
+    assert PLANTED_SECRET not in repr(event)
+
+    (record,) = [r for r in records if r.levelno == logging.WARNING]
+    assert PLANTED_SECRET not in JsonFormatter([]).format(record)
+
+    # The singular path renders the same 'unknown' and never echoes the payload.
+    with pytest.raises(UnsupportedQuestionTypeError) as excinfo:
+
+        class _EvilTag:
+            question_type = tag
+
+        normalize_question(_EvilTag())  # type: ignore[arg-type]
+    assert "unknown" in str(excinfo.value)
+    assert PLANTED_SECRET not in str(excinfo.value)
+    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
+
+
+def test_int_subclass_and_intenum_ids_are_withheld() -> None:
+    """An ``int`` subclass or ``IntEnum`` in an id slot is dropped, not carried.
+
+    ``isinstance(x, int)`` accepts both; an ``IntEnum``'s repr embeds its class and
+    member name, and an ``int`` subclass can override rendering outright. Exact
+    ``type(v) is int`` rejects them, so ``question_id``/``post_id`` become ``None``.
+    """
+    secret_enum = enum.IntEnum("_QType", {PLANTED_SECRET: 91001})
+    member = secret_enum[PLANTED_SECRET]
+    assert PLANTED_SECRET in repr(member)  # sanity: the IntEnum leak vector is real
+
+    result = normalize_questions(
+        [
+            fake_sdk_question(  # type: ignore[list-item]
+                question_type="date",
+                id_of_question=_EvilInt(91001),
+                id_of_post=member,
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
+def test_deferral_event_enforces_no_echo_on_direct_construction() -> None:
+    """The exported dataclass upholds the invariant itself, not only via helpers.
+
+    Field annotations do not validate, so ``__post_init__`` coerces every unsafe
+    field -- a ``str`` subclass reason/tag, an ``IntEnum`` id -- to a safe,
+    module-owned value regardless of how the event was built.
+    """
+    secret_enum = enum.IntEnum("_QType", {PLANTED_SECRET: 91001})
+
+    event = DeferralEvent(
+        reason=_EvilStr("deferred_v1_type"),  # type: ignore[arg-type]
+        question_type=_EvilStr("date"),
+        question_id=secret_enum[PLANTED_SECRET],
+        post_id=_EvilInt(90001),
+    )
+
+    assert event.question_type == "unknown"
+    assert event.reason == "unrecognized_type"
+    assert event.question_id is None
+    assert event.post_id is None
+    assert PLANTED_SECRET not in repr(event)
+
+
+def test_unreadable_question_type_aborts_as_normalization_error() -> None:
+    """A ``question_type`` getter that raises is a malformed record, not a deferral.
+
+    Swallowing it into an 'unrecognized_type' deferral would hide the defect; the
+    project rule is that every malformed shape arrives as the module's own error.
+    The getter's exception can echo field values, so it must surface in neither the
+    message nor the traceback.
+    """
+
+    class _RaisingType:
+        id_of_question = 91001
+        id_of_post = 90001
+
+        @property
+        def question_type(self) -> str:
+            raise ValueError(PLANTED_SECRET)
+
+    for call in (
+        lambda: normalize_question(_RaisingType()),  # type: ignore[arg-type]
+        lambda: normalize_questions([_RaisingType()]),  # type: ignore[list-item]
+    ):
+        with pytest.raises(NormalizationError) as excinfo:
+            call()
+        # A malformed read must abort, not be classified as an unsupported type.
+        assert not isinstance(excinfo.value, UnsupportedQuestionTypeError)
+        assert PLANTED_SECRET not in str(excinfo.value)
+        assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
+
+
 def test_duplicate_check_ignores_deferred_questions() -> None:
     """A deferred question has no canonical model and never reaches the ledger.
 
```
