# Cross-model review request — whiskeyjack-bot M1-203 (round 3)

You reviewed M1-203 of `whiskeyjack-bot` (defer unsupported question types) in round 2 and
returned **changes requested** with one **Medium** diagnostic-coherence issue plus one **Nit**,
after confirming both original blockers were genuinely closed. Both are now addressed. Re-review
the round-3 changes below with the same **stricter reading** — assume the wrong reading and prove
it can't happen from the diff. Do not rubber-stamp. Confirm each finding is actually closed, or
list what remains.

The full round-1 request (spec, decisions, risk areas A–H) is in
`GPT_REVIEW_REQUEST_M1-203.md`; the round-2 request + the closed blockers are in
`GPT_REVIEW_REQUEST_M1-203_R2.md`. This file covers only what changed in round 3.

## Project invariants that bear on this (from CLAUDE.md)

- An **error message** never echoes stored/file/field values, and sanitizing raises use
  `from None`.
- Every malformed shape must arrive as the module's own error type (`NormalizationError`) — a raw
  `AttributeError`/`KeyError`/`ValueError` escaping is a finding.
- Internal value objects are `@dataclass(frozen=True)`; a value object exported from the package
  upholds its own guarantees (annotations do not validate).
- Dispatch on the `question_type` literal, never `isinstance` — `DiscreteQuestion` subclasses
  `NumericQuestion` in the pinned SDK.

All four gates pass: `pytest` 351 (was 336), `ruff check`, `ruff format --check`,
`mypy --strict src`.

## The two round-2 findings, and the fixes

**Finding 1 (was Medium) — `DeferralEvent.__post_init__` did not fully enforce reason/type
coherence.** Direct construction still accepted contradictory events:
`reason="unrecognized_type", question_type="date"` (a known deferred type misclassified as
unrecognized) and `reason="deferred_v1_type", question_type="binary"` (a *supported* type claimed
as deferred). The exported value object promises to enforce its own invariant, yet `reason` was
partly trusted from the caller.

Fix: **`reason` is now derived exclusively from the canonicalized `question_type`; the constructor
argument is never trusted.** A coherent deferral describes exactly one thing — a type the SDK
vouches for (`_KNOWN_SDK_TYPES`) that v1 does **not** support (`_SUPPORTED_TYPES`) — and is kept
with `reason='deferred_v1_type'`. Everything else — a non-`str`, a `str` subclass, an unvetted
tag, or even a *supported* type (never deferred) — collapses to `question_type='unknown'`,
`reason='unrecognized_type'`. `reason` is defaulted, and the exact-type (`type(x) is str`) no-echo
gate the round-2 blockers added is preserved. The reason-derivation now lives in **one place**:
`normalize._deferral_event` no longer computes or passes `reason`, so the exception text and the
event cannot drift.

- `("date","unrecognized_type")` → `("date","deferred_v1_type")` — mismatch #1 corrected.
- `("binary","deferred_v1_type")` → `("unknown","unrecognized_type")` — mismatch #2 collapsed.

*Decision — `_SUPPORTED_TYPES` is derived in `events.py` from `config.SupportedQuestionType`, not
imported from `normalize.py`.* `normalize` imports *from* `events`, so importing back would be
circular; `config` imports nothing from `questions` (verified), and it is already the single source
of truth for the D20 supported set that `normalize._SUPPORTED_TYPES` also derives from.

**Finding 2 (was Nit) — the read-once guarantee was implemented but not regression-tested.** None
of the four round-2 tests failed if a second successful `question_type` read were reintroduced.

Fix: a `_CountingQuestionType` wrapper whose `question_type` getter returns a real tag once and
**raises on any second access**, exercised on the batch defer path, the batch accept path, and both
`normalize_question` branches — a reintroduced second read either raises or (if swallowed
elsewhere) changes the tag away from the asserted one, so each test fails. Plus a parametrization
over `None`, `[]`, and a foreign built-in `str` pinning the readable-but-weird → deferred
distinction (vs. a *raising* getter → abort).

## Pressure-test these specifically

1. Is `reason` now derived **exclusively** from the canonicalized `question_type`, with no path
   (direct construction or `_deferral_event`) by which a caller-supplied `reason` survives
   contradicting the type? Cross-check the parametrized table against `__post_init__`.
2. Does collapsing a *supported* type (`binary`/`multiple_choice`/`numeric`) to
   `unknown`/`unrecognized_type` match the intended semantics — i.e. is there any legitimate path
   that constructs a `DeferralEvent` with a supported type that this now silently rewrites and
   should instead have been prevented upstream? (Confirm `_deferral_event` is only reached after
   `_supported_type` returned `None`, so `tag` is never a supported type there.)
3. Is the `events → config` import genuinely acyclic, and is deriving `_SUPPORTED_TYPES` from
   `config.SupportedQuestionType` in two modules (`events` and `normalize`) a real single-source
   or a latent drift risk?
4. Are the new read-once tests airtight — does each actually fail if a second successful read is
   reintroduced, or can any pass vacuously (e.g. the accept path never re-reading regardless)?
5. Does `_CountingQuestionType.__getattr__` delegation interact safely with the accept path's
   field reads (no attribute masking, no recursion, `_ns` set before first delegation)?

## Output format

Findings as: **Severity (Blocking / High / Medium / Nit) — file:line — what's wrong — why it
matters — suggested fix.** Then an overall verdict. If you approve, justify why each finding is
actually closed rather than merely plausibly closed.

---

# Round-3 diff (`git diff ffb8399..HEAD -- src/ tests/`, uncommitted at time of writing)

```diff
diff --git a/src/whiskeyjack_bot/questions/events.py b/src/whiskeyjack_bot/questions/events.py
index 77dc609..7ddefb1 100644
--- a/src/whiskeyjack_bot/questions/events.py
+++ b/src/whiskeyjack_bot/questions/events.py
@@ -10,10 +10,12 @@ the ledger writers (M1-602): there is no run or tournament context at this layer
 key a row on, and the event exists to be returned to the caller and logged.
 
 The event is built to carry **no unvetted string**. ``reason`` is one of the two
-literals below, ``question_type`` is a member of the SDK's own tag enum or
-``'unknown'``, and both identity fields are gated to ``int`` -- a non-integer id
-becomes ``None`` rather than being carried. That is what lets the event be logged
-without widening the no-echo surface the rest of the package maintains.
+literals below and is **derived**, not accepted from the caller: it follows
+deterministically from the canonicalized ``question_type`` (see ``__post_init__``).
+``question_type`` is a member of the SDK's own tag enum that v1 does not support, or
+``'unknown'``; both identity fields are gated to ``int`` -- a non-integer id becomes
+``None`` rather than being carried. That is what lets the event be logged without
+widening the no-echo surface the rest of the package maintains.
 """
 
 from __future__ import annotations
@@ -23,20 +25,27 @@ from typing import Literal, get_args
 
 from forecasting_tools.data_models.questions import QuestionBasicType
 
+from whiskeyjack_bot.config import SupportedQuestionType
 from whiskeyjack_bot.questions.model import CanonicalQuestion
 
 # Two reasons rather than one: the ``_KNOWN_SDK_TYPES`` gate renders an unvetted tag
 # as 'unknown', which erases the difference between "a type the SDK defines and we
 # defer" and "something arbitrary reached the tag slot". The first is routine; the
 # second means a question object came from outside the SDK's own models. The
-# distinction is recoverable here without echoing the value.
+# distinction is recoverable here without echoing the value, and is drawn from the
+# canonicalized type alone rather than trusted from the constructor.
 DeferralReason = Literal["deferred_v1_type", "unrecognized_type"]
 
 # The exact-type/membership sets ``__post_init__`` canonicalizes against. The SDK's
 # own six-value tag enum is the only vocabulary safe to name; anything else renders
 # as 'unknown'. ``frozenset`` so membership is a hash lookup, not a linear scan.
 _KNOWN_SDK_TYPES: frozenset[str] = frozenset(get_args(QuestionBasicType))
-_REASONS: frozenset[str] = frozenset(get_args(DeferralReason))
+# The v1-supported types (D20). A supported type is *never* deferred, so a
+# ``DeferralEvent`` naming one is incoherent and collapses to 'unknown'. Derived from
+# config directly -- ``normalize`` imports *from* this module, so importing its
+# ``_SUPPORTED_TYPES`` back would be circular; ``config`` imports nothing from
+# ``questions``.
+_SUPPORTED_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))
 
 
 @dataclass(frozen=True)
@@ -49,36 +58,50 @@ class DeferralEvent:
     because an operator cannot act on "one question was deferred". The no-echo rule
     guards against a credential surfacing through free-text field values.
 
-    The no-echo invariant is enforced here in ``__post_init__``, not only by the
-    ``normalize`` helpers that usually build the event: a frozen dataclass's field
-    annotations do not validate, so a value object exported from the package must
-    uphold the guarantee itself. ``__post_init__`` coerces every field to a safe,
-    module-owned value using **exact-type** checks -- ``type(x) is str``/``int``,
-    not ``isinstance`` -- because a ``str``/``int`` *subclass* (or an ``IntEnum``)
-    can carry attacker-controlled ``__str__``/``__repr__`` whose value slips past a
-    membership check and renders through the log or this dataclass's repr. Anything
-    that is not exactly the built-in type degrades to ``'unknown'``/``None``, so a
-    leak is impossible by construction rather than by promise.
+    Both the no-echo invariant *and* reason/type coherence are enforced here in
+    ``__post_init__``, not only by the ``normalize`` helpers that usually build the
+    event: a frozen dataclass's field annotations do not validate, so a value object
+    exported from the package must uphold its own guarantees. ``reason`` is **not
+    trusted from the caller** -- it is derived from the canonicalized
+    ``question_type`` alone, so a contradictory ``(reason, question_type)`` pair
+    passed to the constructor cannot survive.
+
+    A coherent deferral describes exactly one thing: a type the SDK vouches for (a
+    member of ``_KNOWN_SDK_TYPES``) that v1 does **not** support. Such a type is kept
+    with ``reason='deferred_v1_type'``. Everything else -- a non-``str``, a ``str``
+    *subclass*, an unvetted tag, or even a *supported* type (which is never deferred)
+    -- collapses to ``question_type='unknown'``, ``reason='unrecognized_type'``.
+
+    The type gate is **exact** -- ``type(x) is str``/``int``, not ``isinstance`` --
+    because a ``str``/``int`` *subclass* (or an ``IntEnum``) can carry
+    attacker-controlled ``__str__``/``__repr__`` whose value slips past a membership
+    check and renders through the log or this dataclass's repr. Anything not exactly
+    the built-in type degrades to ``'unknown'``/``None``, so a leak is impossible by
+    construction rather than by promise; the same coercion (not a raise) keeps a
+    diagnostic value from turning into a crash.
     """
 
-    reason: DeferralReason
-    # A ``QuestionBasicType`` member, or 'unknown' for an unvetted tag.
+    # A ``QuestionBasicType`` member (deferred by v1), or 'unknown' for an unvetted
+    # or incoherent tag.
     question_type: str
+    # Derived in ``__post_init__`` from ``question_type``; the constructor value is
+    # ignored. Defaulted so callers need not supply it.
+    reason: DeferralReason = "unrecognized_type"
     question_id: int | None = None
     post_id: int | None = None
 
     def __post_init__(self) -> None:
-        # Coerce, don't raise: a diagnostic value degrades to a safe placeholder the
-        # same way ids already drop to None, rather than turning a deferral into a
-        # crash. Exact-type checks so only a built-in str/int -- whose rendering can
-        # carry no attacker payload -- is ever kept.
-        if type(self.question_type) is not str or self.question_type not in _KNOWN_SDK_TYPES:
+        # Reason follows the canonicalized type, so a contradictory pair cannot
+        # survive. A coherent deferral is a KNOWN SDK type that v1 does not support;
+        # a supported type is never deferred and an unvetted tag was never vouched
+        # for, so both collapse to the unknown/unrecognized pair. Exact-type check so
+        # only a built-in str -- whose rendering carries no attacker payload -- is
+        # ever kept.
+        qt = self.question_type
+        if type(qt) is str and qt in _KNOWN_SDK_TYPES and qt not in _SUPPORTED_TYPES:
+            object.__setattr__(self, "reason", "deferred_v1_type")
+        else:
             object.__setattr__(self, "question_type", "unknown")
-        if type(self.reason) is not str or self.reason not in _REASONS:
-            object.__setattr__(self, "reason", "unrecognized_type")
-        # An 'unknown' tag means the SDK never vouched for this record's type, so the
-        # reason must record that it was unrecognized rather than a routine deferral.
-        if self.question_type == "unknown":
             object.__setattr__(self, "reason", "unrecognized_type")
         for field in ("question_id", "post_id"):
             value = getattr(self, field)
diff --git a/src/whiskeyjack_bot/questions/normalize.py b/src/whiskeyjack_bot/questions/normalize.py
index 2087596..26f099c 100644
--- a/src/whiskeyjack_bot/questions/normalize.py
+++ b/src/whiskeyjack_bot/questions/normalize.py
@@ -205,11 +205,15 @@ def _deferral_event(q: MetaculusQuestion, question_type: object) -> DeferralEven
     identity only -- no content field is touched, so a deferred question reaches no
     model and no submission call, and nothing that could carry a secret reaches the
     event. ``DeferralEvent`` re-canonicalizes every field regardless (see events.py).
+
+    ``reason`` is not passed: the event derives it from the canonicalized type alone,
+    so reason/type coherence lives in one place. ``tag`` here is only ever a
+    known-unsupported type or 'unknown' (this runs after ``_supported_type`` returned
+    ``None``), which is exactly what the event maps to ``deferred_v1_type`` /
+    ``unrecognized_type`` respectively -- identical to the value it would derive.
     """
-    tag = _type_tag(question_type)
     return DeferralEvent(
-        reason="deferred_v1_type" if tag != "unknown" else "unrecognized_type",
-        question_type=tag,
+        question_type=_type_tag(question_type),
         question_id=_safe_int(q, "id_of_question"),
         post_id=_safe_int(q, "id_of_post"),
     )
diff --git a/tests/unit/test_questions.py b/tests/unit/test_questions.py
index 3abec28..97b1290 100644
--- a/tests/unit/test_questions.py
+++ b/tests/unit/test_questions.py
@@ -901,6 +901,46 @@ def test_deferral_event_enforces_no_echo_on_direct_construction() -> None:
     assert PLANTED_SECRET not in repr(event)
 
 
+@pytest.mark.parametrize(
+    ("in_type", "in_reason", "out_type", "out_reason"),
+    [
+        # A KNOWN SDK type v1 does not support: kept, reason derived as a routine
+        # deferral -- regardless of the reason the constructor was handed.
+        ("date", "deferred_v1_type", "date", "deferred_v1_type"),
+        ("conditional", "deferred_v1_type", "conditional", "deferred_v1_type"),
+        ("discrete", "deferred_v1_type", "discrete", "deferred_v1_type"),
+        # Mismatch #1: a known deferred type mislabeled 'unrecognized_type' is
+        # corrected -- reason follows the type, not the caller.
+        ("date", "unrecognized_type", "date", "deferred_v1_type"),
+        # Mismatch #2: a *supported* type is never deferred, so naming one collapses
+        # the whole event to the unknown/unrecognized pair.
+        ("binary", "deferred_v1_type", "unknown", "unrecognized_type"),
+        ("multiple_choice", "deferred_v1_type", "unknown", "unrecognized_type"),
+        ("numeric", "unrecognized_type", "unknown", "unrecognized_type"),
+        # An unvetted tag the SDK never defined.
+        ("totally_made_up", "deferred_v1_type", "unknown", "unrecognized_type"),
+        ("unknown", "deferred_v1_type", "unknown", "unrecognized_type"),
+    ],
+)
+def test_deferral_event_reason_is_derived_from_type(
+    in_type: str, in_reason: str, out_type: str, out_reason: str
+) -> None:
+    """``reason`` follows the canonicalized type; a contradictory pair cannot survive.
+
+    The exported value object enforces its own invariant: a deferral describes a
+    known SDK type v1 does not support (``deferred_v1_type``), and anything else --
+    a supported type, which is never deferred, or an unvetted tag -- collapses to
+    ``question_type='unknown'``, ``reason='unrecognized_type'``. The constructor's
+    ``reason`` argument is not trusted.
+    """
+    event = DeferralEvent(
+        question_type=in_type,
+        reason=in_reason,  # type: ignore[arg-type]
+    )
+    assert event.question_type == out_type
+    assert event.reason == out_reason
+
+
 def test_unreadable_question_type_aborts_as_normalization_error() -> None:
     """A ``question_type`` getter that raises is a malformed record, not a deferral.
 
@@ -930,6 +970,102 @@ def test_unreadable_question_type_aborts_as_normalization_error() -> None:
         assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
 
 
+class _CountingQuestionType:
+    """Wraps an SDK-shaped namespace, counting each ``question_type`` read.
+
+    A path that honors the read-once contract touches ``question_type`` exactly
+    once. A reintroduced *second* successful read is the regression this pins: the
+    second access raises, so it either surfaces as an error or (if swallowed on some
+    other path) changes the tag away from the one asserted -- either way the outcome
+    assertions fail. Every other attribute delegates to the wrapped namespace.
+    """
+
+    def __init__(self, namespace: SimpleNamespace, tag: str) -> None:
+        self._ns = namespace
+        self._tag = tag
+        self.reads = 0
+
+    @property
+    def question_type(self) -> str:
+        self.reads += 1
+        if self.reads > 1:
+            raise AssertionError("question_type read more than once")
+        return self._tag
+
+    def __getattr__(self, name: str) -> object:
+        # Only reached for names not set on the instance (``question_type`` is a
+        # class property, so it never lands here); ``_ns`` is set first in __init__.
+        return getattr(self._ns, name)
+
+
+def test_batch_path_reads_question_type_once_when_deferring() -> None:
+    """The batch defer path classifies and describes from a single read.
+
+    Threading the one read through ``_deferral_event`` (rather than re-reading it)
+    is what stops a stateful getter from classifying and describing the same
+    question inconsistently.
+    """
+    q = _CountingQuestionType(fake_sdk_question(question_type="binary"), tag="date")
+
+    result = normalize_questions([q])  # type: ignore[list-item]
+
+    (event,) = result.deferrals
+    assert not result.questions
+    assert event.question_type == "date"
+    assert event.reason == "deferred_v1_type"
+    assert event.question_id == 91001
+    assert event.post_id == 90001
+    assert q.reads == 1
+
+
+def test_batch_path_reads_question_type_once_when_accepting() -> None:
+    """The batch accept path builds the canonical model from a single read."""
+    q = _CountingQuestionType(fake_sdk_question(question_type="binary"), tag="binary")
+
+    result = normalize_questions([q])  # type: ignore[list-item]
+
+    (canonical,) = result.questions
+    assert not result.deferrals
+    assert isinstance(canonical, CanonicalBinaryQuestion)
+    assert q.reads == 1
+
+
+def test_singular_path_reads_question_type_once() -> None:
+    """``normalize_question`` reads the type once on both the accept and refuse paths."""
+    accepted = _CountingQuestionType(fake_sdk_question(question_type="binary"), tag="binary")
+    canonical = normalize_question(accepted)  # type: ignore[arg-type]
+    assert isinstance(canonical, CanonicalBinaryQuestion)
+    assert accepted.reads == 1
+
+    refused = _CountingQuestionType(fake_sdk_question(question_type="binary"), tag="date")
+    with pytest.raises(UnsupportedQuestionTypeError):
+        normalize_question(refused)  # type: ignore[arg-type]
+    assert refused.reads == 1
+
+
+@pytest.mark.parametrize("tag", [None, [], "totally_made_up"])
+def test_batch_defers_readable_but_unusable_type(tag: object) -> None:
+    """A readable-but-weird type defers; only a *raising* getter aborts the batch.
+
+    ``None``, an (unhashable) list, and a foreign built-in string are all read
+    without error, so they are deferred as ``unknown``/``unrecognized_type`` rather
+    than raising -- the distinction from
+    ``test_unreadable_question_type_aborts_as_normalization_error``, where the getter
+    itself raises. The list case also pins that the exact-type gate runs before the
+    frozenset membership test that would otherwise choke on an unhashable tag.
+    """
+    q = SimpleNamespace(question_type=tag, id_of_question=91001, id_of_post=90001)
+
+    result = normalize_questions([q])  # type: ignore[list-item]
+
+    (event,) = result.deferrals
+    assert not result.questions
+    assert event.question_type == "unknown"
+    assert event.reason == "unrecognized_type"
+    assert event.question_id == 91001
+    assert event.post_id == 90001
+
+
 def test_duplicate_check_ignores_deferred_questions() -> None:
     """A deferred question has no canonical model and never reaches the ledger.
 
```
