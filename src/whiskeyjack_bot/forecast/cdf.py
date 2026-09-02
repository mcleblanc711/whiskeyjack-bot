"""The numeric CDF conversion (M1-503).

``forecast/numeric.py`` validates the nine declared percentiles against everything true of
a percentile set **for any configuration** -- the exact levels, non-decreasing values,
closed bounds, ``zero_point``. It stops there and says why: the remaining rules are the
ones ``config.numeric_calibration`` decides, and ``NumericCalibrationConfig`` is a
*sibling* of ``ForecastConfig`` on ``AppConfig``, so a checker handed only the latter
cannot see them. This module is the other side of that line. It owns the 201-point count,
the PMF step cap, and everything the pinned SDK gates behind ``strict_validation`` and
``standardize_cdf``.

It is also the only thing in ``forecast/`` besides ``generate.py`` that reaches the
provider SDK, and that placement is deliberate rather than incidental --
:func:`whiskeyjack_bot.forecast.cdf.build_numeric_cdf` cannot live on the replay path.
See "Where this module may not live" below.

**What it produces.** ``[point.percentile for point in distribution.get_cdf()]`` -- the
cumulative probabilities, not the x-axis values. That is the array
``MetaculusClient.post_numeric_question_prediction`` takes and the array
``submission_live._require_cdf`` already validates on the way back in, so the two ends of
the wire agree by construction rather than by translation.

**Nothing is clamped, sorted, padded or renormalized by this module.** The SDK's own
standardization is a different thing and is disclosed rather than hidden: it is what
``numeric_calibration.use_forecasting_tools_standardization`` turns on, the committed
default is ``true``, and :class:`NumericCdf` records whether it ran. What this module
never does is edit an array the SDK produced in order to make it pass its own checks --
a CDF pulled inside a cap here is a distribution the ledger could not attribute to
anything.

Four rules this module applies, and why each is here rather than in the SDK:

- **Exactly ``expected_cdf_points`` values.** ``get_cdf`` returns ``self.cdf_size or 201``
  points, and ``cdf_size`` reaches us from a Metaculus payload through
  ``CanonicalNumericQuestion``, which types it as a plain ``int`` and defers the check by
  name: "calibration-time enforcement of the point count belongs to the validation epic
  (M1-503), not the model".
- **Every adjacent step at or below ``max_adjacent_pmf``.** This is the half of the
  acceptance criterion the package does **not** do for us, and it is worth being exact
  about why, because reading ``numeric_report.py`` casually suggests the opposite.
  ``_check_distribution_too_tall`` is the SDK's PMF cap; ``get_cdf`` re-validates its own
  output by constructing a ``NumericDistribution`` **without ``cdf_size``**, and that
  check runs only when ``len(percentiles) == self.cdf_size``. With ``cdf_size`` ``None``
  the comparison is false for every possible length, so the cap is skipped on the one
  array that gets submitted. Verified by execution, not by reading.
- **A closed bound pins its endpoint.** ``get_cdf``'s docstring puts
  ``cdf[0] = P(outcome < lower_bound)`` and ``cdf[-1] = P(outcome <= upper_bound)``, so a
  closed lower bound forces ``cdf[0] == 0.0`` and a closed upper bound forces
  ``cdf[-1] == 1.0``. Nothing in the SDK asserts it of the returned array.
- **Every value a finite float in [0, 1], non-decreasing.** ``submission_live._require_cdf``
  refuses an array that fails any of these, and it refuses it *after* a human has approved
  the forecast. Checking here makes it a repair turn instead.

**Four of the five calibration knobs are consumed here; the fifth is named rather than
ignored.** ``expected_cdf_points`` is the length rule and the ``cdf_size`` preflight,
``max_adjacent_pmf`` is the step cap, and ``strict_validation`` and
``use_forecasting_tools_standardization`` are passed to the SDK. ``calibration_profile`` is
``Literal["identity"]`` and this module applies **no** post-hoc transform to the array, so
the field has no consumer here and reading it would only imply one. That is stated rather
than left as an absence: a knob with no reader looks identical to a knob someone forgot,
and the whole point of the "nothing is clamped" rule above is that an operator can tell
which transformations exist. If a second profile is ever added it lands as its own row,
with the config owner's decision about what it may do to a submitted array.

**The minimum step is deliberately not re-checked.** ``_check_percentile_spacing``
enforces ``5e-05`` between adjacent points and ``get_cdf`` runs it against its own output,
so a violation arrives as an SDK failure this module translates. Restating ``5e-05`` would
copy a pinned constant into this repository, which is exactly what M1-405 refused to do
with the 0.25 wiggle factor and the 2x buffer so that a pin move has nothing here to
invalidate. The cap above is a different case: ``max_adjacent_pmf`` is **our** configured
value, not a transcription of theirs.

**Where this module may not live.** ``tests/unit/test_forecast_generate.py``'s import-graph
probe forbids ``forecasting_tools`` from ``forecast.schema``, ``binary``, ``multiple_choice``,
``numeric``, ``attribution``, ``validate``, ``inputs``, ``parse``, ``artifacts``,
``persist`` and ``replay``, and that probe *is* M1-406's acceptance criterion: a replay
must reproduce a stored forecast with zero provider calls, asserted as a property of the
import graph rather than of a mock count. ``NumericDistribution`` lives in
``forecasting_tools``. So this conversion cannot be a ``validate._TYPE_CHECKERS`` entry,
and the same conclusion follows a second time from the config boundary above -- the
registry hands a checker a ``ForecastConfig`` and nothing else.

That is why the composed entry point is not the seam here, and it is stated rather than
left to be inferred: ``forecast.validate`` remains the complete account of everything a
response must satisfy that is **SDK-free and ``ForecastConfig``-keyed**, and this module is
a second, SDK-bound gate that only ``forecast.generate`` runs. A reviewer reading
``validate.py`` alone would otherwise see a rule that looks absent, which is the seam
defect M1-506 exists to close.

Nothing here is named ``*_output_problems``, and that is load-bearing rather than a style
choice: ``test_no_output_checker_in_the_package_is_unreachable`` walks this package for
that suffix and requires every match to be registered in ``_TYPE_CHECKERS``. A conversion
gate that cannot be registered must not answer to the naming convention that means
"registered".

**No message renders a value.** Not the model's, not the question's, and above all not the
SDK's. Every failure inside ``NumericDistribution`` arrives as a ``pydantic.ValidationError``
whose text interpolates the offending percentiles, the bounds and the ``zero_point``, and
whose ``input_value`` carries the whole declared list. Those strings do not stop at the
repair turn: a response that fails twice puts them in
``ForecastGeneration.failure_problems``, which ``forecast/artifacts.py`` writes into the
persisted raw-output envelope. Question fields come from Metaculus payloads, which
CLAUDE.md classes as untrusted. So every SDK call in this module is fenced and re-raised
``from None``, and no problem string this module emits interpolates anything reached
through ``forecast``, ``question`` or the SDK. M1-405 took this as a round-1 blocking
finding; the argument is unchanged and the surface here is worse.

**Every SDK call here runs under a wall-clock bound (M1-514).** ``_standardize_cdf`` can
fail to *terminate* -- not raise, not run slowly, never return -- and until this item
nothing on the forecast path imposed a deadline, so one percentile set stopped the whole
run with no error, no failure event and no billing bound.
:func:`_standardization_can_converge` is M1-503's deterministic refusal of the two inputs
that were *found*; the bound is the general answer for the one that has not been. Both are
kept, and the division between them is the point: the guard is the fast path and produces a
repair turn the model can act on, while the bound is the backstop and produces a terminal
``timeout`` with no repair. See :func:`_bounded` for the mechanism and what it costs, and
:class:`_Expired` for why it is a ``BaseException``.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from math import isfinite
from types import FrameType
from typing import Any

from forecasting_tools import NumericDistribution, Percentile

from whiskeyjack_bot.config import MAX_CONVERSION_TIMEOUT_SECONDS, NumericCalibrationConfig
from whiskeyjack_bot.forecast.schema import (
    ForecastSchemaError,
    NumericForecastResponse,
)
from whiskeyjack_bot.questions.model import CanonicalNumericQuestion

_LOGGER = logging.getLogger(__name__)

# The field these problems are reported against. Every rule below is about the array the
# percentiles convert into, but the percentiles are the only thing the model can change,
# so a repair turn that named the CDF would name something the model was told not to
# produce ("Do not return a 201-value CDF", ``prompts/forecaster.md``).
_PERCENTILES_LOC = "final_prediction.percentiles"

# ``get_cdf`` falls back to this when ``cdf_size`` is unset. Not reachable through this
# module -- ``_require_question`` pins ``cdf_size`` to ``expected_cdf_points`` before
# anything converts -- but the evaluation grid must match ``get_cdf``'s either way.
_SDK_DEFAULT_CDF_SIZE = 201

# Stated as constants so no call site can interpolate a question field, a config value or
# an SDK message by accident. Each names the rule and the field it is about; none names a
# number reached through a question or a response. ``max_adjacent_pmf`` is configuration
# rather than content, but it is still not rendered: the model was never told the cap, and
# a repair turn quoting a number the prompt does not print is one the model cannot aim at
# any better than the rule alone -- ``attribution.py``'s side of M1-405's distinction.
_NOT_CONVERTIBLE = (
    "the declared percentiles do not describe a distribution the Metaculus CDF "
    "conversion accepts; widen the spread between adjacent percentile values "
    "(detail withheld: the conversion echoes the values it refused)"
)
_WRONG_LENGTH = (
    "the converted CDF did not come back at the configured length (offending input withheld)"
)
_NOT_IN_UNIT_INTERVAL = (
    "every converted CDF value must be a finite number between 0 and 1 (offending input withheld)"
)
_NOT_MONOTONE = "the converted CDF must be non-decreasing (offending input withheld)"
_STEP_TOO_TALL = (
    "the converted CDF concentrates more probability between two adjacent points than "
    "the configured maximum; widen the spread between adjacent percentile values "
    "(offending input withheld)"
)
_NOT_WELL_FORMED = (
    "the declared percentiles do not produce a well-formed cumulative distribution; "
    "return strictly increasing values spread more widely across the question's range "
    "(detail withheld: the conversion echoes the values it refused)"
)
_LOWER_ENDPOINT = (
    "the converted CDF must start at 0 for a question whose lower_bound is closed "
    "(offending input withheld)"
)
_UPPER_ENDPOINT = (
    "the converted CDF must end at 1 for a question whose upper_bound is closed "
    "(offending input withheld)"
)
_CONVERSION_TIMED_OUT = (
    "the numeric CDF conversion did not complete within the configured wall-clock bound "
    "(offending input withheld)"
)
_CANNOT_BE_BOUNDED = (
    "the numeric CDF conversion cannot be bounded in time on this thread; it is refused "
    "rather than run unbounded"
)

# What ``_bounded`` restores an already-expired outer timer to. ``setitimer(..., 0.0)``
# *disarms* a timer, so an outer deadline whose remaining time has run out during our
# conversion must be re-armed with something small and positive to fire immediately --
# handing back 0.0 would silently cancel the caller's own deadline instead.
_IMMEDIATE = 1e-6


class NumericCdfError(ForecastSchemaError):
    """A numeric CDF cannot be built, or was asked for in a way this module refuses.

    Subclasses :class:`ForecastSchemaError` for the reason ``BinaryOutputError`` argues at
    length and M1-403 round 1 settled: a caller that handles the forecast package's
    response failures as one type keeps working unchanged, while a caller that wants
    *this* module's boundary can name it without importing ``forecast.schema``.

    Carries the same sanitized ``problems`` list as its parent: a field path, a colon and a
    value-free message. Nothing here echoes model output, question fields or SDK text.
    """


class NumericCdfTimeoutError(NumericCdfError):
    """The conversion did not finish inside ``conversion_timeout_seconds`` (M1-514).

    Subclasses :class:`NumericCdfError` for the reason that class gives for subclassing
    ``ForecastSchemaError``: a caller handling the module's failures as one type keeps
    working unchanged, and a caller that needs *this* outcome can name it.

    ``forecast.generate`` is that caller. It needs the distinction because a timeout is the
    one conversion outcome that must **not** become a repair turn -- the model cannot fix a
    non-terminating scale search, so asking it to would buy a second billed call for
    nothing. It maps this to ``failure_code="timeout"`` and stops the attempt loop.
    """


class _Expired(BaseException):
    """Raised from the ``SIGALRM`` handler when the conversion's bound elapses.

    **``BaseException`` rather than ``Exception``, and that is load-bearing rather than
    fastidious.** :func:`_distribution`, :func:`_values` and
    :func:`_standardization_can_converge` each swallow ``Exception`` deliberately -- an SDK
    refusal is a problem with the *reply* and belongs in a repair turn, and the SDK's own
    messages quote the values, so they are caught broadly and never chained. An expiry
    raised as an ordinary exception would be caught by exactly those handlers and reported
    as ``_NOT_CONVERTIBLE``: a repair turn on a timeout, which spends the second call this
    module budgets on a condition no reply could repair, and loses the ``timeout``
    classification the ledger needs. Deriving from ``BaseException`` puts it outside every
    one of those handlers -- and outside any bare ``except Exception`` inside the pinned
    package too, which was verified by execution rather than read.

    Private and never raised past :func:`_bounded`, which converts it to
    :class:`NumericCdfTimeoutError` at the module boundary. A ``BaseException`` escaping
    into ``forecast.generate`` would bypass its attempt loop entirely.
    """


def can_bound_conversion() -> bool:
    """Whether this thread can install the conversion's deadline.

    ``signal.setitimer`` exists only on POSIX, and ``signal.signal`` raises ``ValueError``
    off the main thread of the main interpreter. Asked rather than caught so the answer is
    available to callers *before* they spend anything -- ``forecast.generate`` repeats this
    in its own preflight, before the first billable call.
    """
    return hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()


@contextmanager
def _bounded(seconds: float) -> Iterator[None]:
    """Run the enclosed SDK section under a wall-clock deadline, or raise.

    **Why a signal and not a worker thread.** A thread cannot be killed, so a bound built on
    ``Thread.join(timeout)`` bounds the *caller* and leaves the runaway loop spinning for the
    life of the process. Measured on this project's own workload, one such spinner makes
    ledger I/O about **1180x slower** -- 300 SQLite commits went from 4ms to 5.1s -- because
    a CPU-bound thread and a thread doing many short GIL round-trips convoy against each
    other. A run that "continues" at that rate has stopped, which is precisely the outcome
    M1-514's acceptance criterion is about. ``SIGALRM`` interrupts the loop, reclaims the
    CPU and leaves nothing behind; verified against the real non-terminating input, with the
    next conversion in the same process still taking its ordinary 7.7ms.

    **Why not a subprocess**, the other mechanism that can truly cancel the work:
    ``import forecasting_tools`` measures **10.6s**, paid again on every worker the pool has
    to replace after a kill, and the default start method here is ``fork``, which would
    inherit the ledger's open SQLite WAL connection into the child.

    **What it costs.** Installing a process-global signal handler and an interval timer for
    the duration of one conversion. Both the handler *and* the previous timer are restored,
    the latter with the elapsed time deducted, so a deadline the caller already owns is
    suspended for the conversion rather than destroyed by it -- that is what lets this nest
    under ``tests/conftest.py``'s own deadline fixture. ``setitimer`` returns the outer
    timer's true remaining time, which was measured rather than assumed.

    The caller is responsible for having checked :func:`can_bound_conversion`; this raises
    :class:`NumericCdfError` rather than running unbounded if it has not.
    """
    if not can_bound_conversion():
        raise NumericCdfError([f"{_PERCENTILES_LOC}: {_CANNOT_BE_BOUNDED}"])

    # ``armed`` gates the *handler*, not the timer, and it closes two different races that
    # a naive "install, then protect" ordering leaves open. Both were reachable; the second
    # was a round-1 blocking finding.
    #
    # **Teardown.** Disarming the timer does not retract a signal the kernel has already
    # delivered: it sits in CPython's pending-call flag until the eval loop next looks,
    # which may be after this context manager has begun cleaning up.
    #
    # **Handoff.** Between installing ``expire`` and replacing the caller's timer with ours,
    # any ``SIGALRM`` belongs to a deadline the *caller* already owned and may already be
    # due -- ``tests/conftest.py``'s own fixture is exactly such a caller. With the flag
    # raised from the start, that signal raised ``_Expired`` outside the protected region:
    # it escaped as a ``BaseException`` past ``forecast.generate``'s attempt loop, and the
    # cleanup never ran, so the caller's handler stayed clobbered by ours. That is the
    # stopped run this bound exists to prevent, reintroduced by the bound itself.
    #
    # So the flag starts **down**, and a signal arriving before we own the timer is handed
    # back to the caller's handler rather than swallowed or reinterpreted as our timeout.
    # It is raised before ``setitimer`` rather than after, because the opposite order leaves
    # a window in which *our* timer can fire and be delegated away -- a conversion that then
    # runs unbounded, which is a fail-open where this is merely a misattribution.
    armed = False
    installed = False
    previous_delay = previous_interval = 0.0
    # Read **before** installing, not from ``signal.signal``'s return value. The signal that
    # ``expire`` has to hand back can be delivered the instant the handler is installed,
    # which is before the assignment of that return value has run -- so binding it there
    # leaves ``expire`` looking at ``None`` in exactly the window it exists to cover, and
    # the caller's deadline is silently dropped instead of delegated. Measured: with the
    # return-value form the round-1 reproduction swallowed the caller's alarm.
    previous_handler: Any = signal.getsignal(signal.SIGALRM)

    def expire(signum: int, frame: FrameType | None) -> None:
        nonlocal armed
        if not armed:
            # Not ours. A non-callable previous handler is ``SIG_DFL``/``SIG_IGN`` or a
            # handler set from C; there is nothing to call, and killing the process on the
            # caller's behalf from inside a conversion is not this module's decision.
            if callable(previous_handler):
                previous_handler(signum, frame)
            return
        armed = False
        raise _Expired

    started = time.monotonic()
    try:
        signal.signal(signal.SIGALRM, expire)
        installed = True
        armed = True
        previous_delay, previous_interval = signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            armed = False
            if installed:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                # ``signal.signal`` returns ``None`` for a handler that was not set from
                # Python, and refuses ``None`` as an argument. ``SIG_DFL`` is the honest
                # restore for that case: it is what the C-level default already was.
                signal.signal(
                    signal.SIGALRM,
                    previous_handler if previous_handler is not None else signal.SIG_DFL,
                )
                if previous_delay:
                    remaining = previous_delay - (time.monotonic() - started)
                    signal.setitimer(
                        signal.ITIMER_REAL, max(remaining, _IMMEDIATE), previous_interval
                    )
    except _Expired:
        _LOGGER.warning(
            "numeric CDF conversion exceeded its wall-clock bound and was cut off (M1-514)"
        )
        # ``from None``: ``_Expired`` carries no text of its own, but the rule this module
        # applies to the SDK's exceptions applies to its own -- a rendered traceback is a
        # channel like any other, and this one would frame the SDK's locals.
        raise NumericCdfTimeoutError([f"{_PERCENTILES_LOC}: {_CONVERSION_TIMED_OUT}"]) from None


@dataclass(frozen=True)
class NumericCdf:
    """One converted numeric forecast: the submission array and how it was built.

    ``values`` is the array and the only thing a submission needs. The other three fields
    exist so that what happened during the conversion is **visible** rather than inferred
    from the configuration -- the record stores the percentiles the model returned, and
    without these a reader has no way to tell whether the CDF was built from those exact
    values.

    ``percentiles_used`` is what the SDK actually built from, as ``(level, value)`` pairs
    in the order it held them. It differs from the declared set when
    ``_check_and_update_repeating_values`` rewrites a repeated value -- see
    :func:`_percentiles_used` and M1-508.
    """

    values: tuple[float, ...]
    percentiles_used: tuple[tuple[float, float], ...]
    adjusted: bool
    standardized: bool


def _require_question(
    question: CanonicalNumericQuestion, calibration: NumericCalibrationConfig
) -> None:
    """Refuse what no percentile set could fix, before it becomes a repair turn.

    ``binary._require_config``'s precedent and its reason: a value object carries no memory
    of which validator built it, and an unsatisfiable one would fail *every* forecast
    through the repair loop -- two billed calls per question to reject something no model
    could have supplied. ``forecast.generate`` repeats these before anything is spent.

    Two rules, and both are about the question rather than the reply:

    - ``cdf_size`` must equal ``expected_cdf_points``. ``get_cdf`` returns
      ``self.cdf_size or 201`` points, so a question declaring another resolution produces
      an array of that length and ``submission_live._require_cdf`` refuses it -- after the
      forecast has been billed, recorded and approved. ``CanonicalNumericQuestion`` types
      the field as a plain ``int`` and defers this check here by name.
    - ``zero_point`` must be strictly below ``lower_bound``. ``_check_log_scaled_fields``
      runs unconditionally inside ``NumericDistribution``, so such a question cannot
      produce a distribution however the percentiles are drawn.

    The second restates what ``numeric._require_question`` already refuses, and the
    duplication is deliberate for ``binary._require_config``'s reason above: a caller that
    reached this module without running the composed validation would otherwise get an
    unsatisfiable repair turn, which is the failure M1-404's round-1 finding was about.
    Neither number is rendered.

    **M1-514 added two more, and they are about the caller rather than the question.**
    ``NumericCalibrationConfig`` bounds ``conversion_timeout_seconds`` at load, so a value
    above the ceiling means an ``AppConfig`` assembled some other way -- repeated here for
    the reason ``generate.py`` repeats ``MAX_MODEL_INVOCATIONS``: a config object carries no
    memory of which validator built it, and this is the bound that decides how long a
    non-terminating conversion may stop the run for. The deadline's availability is checked
    for the sharper reason: a thread that cannot install it would otherwise run the
    conversion *unbounded*, which is the bound silently absent rather than loudly refused.
    """
    if not isinstance(calibration, NumericCalibrationConfig):
        raise NumericCdfError(["numeric_calibration: must be a NumericCalibrationConfig"])
    if not isinstance(question, CanonicalNumericQuestion):
        raise NumericCdfError(["question: must be a canonical numeric question"])
    if not 0 < calibration.conversion_timeout_seconds <= MAX_CONVERSION_TIMEOUT_SECONDS:
        raise NumericCdfError(
            [
                "numeric_calibration: conversion_timeout_seconds is outside the wall-clock "
                "bound the conversion may be given (offending input withheld)"
            ]
        )
    if not can_bound_conversion():
        raise NumericCdfError([f"{_PERCENTILES_LOC}: {_CANNOT_BE_BOUNDED}"])
    if question.cdf_size != calibration.expected_cdf_points:
        raise NumericCdfError(
            [
                "question: cdf_size must equal numeric_calibration.expected_cdf_points "
                "(offending input withheld)"
            ]
        )
    if question.zero_point is not None and question.lower_bound <= question.zero_point:
        raise NumericCdfError(
            ["question: zero_point must be strictly below lower_bound for a log-scaled question"]
        )


def _declared(forecast: NumericForecastResponse) -> tuple[tuple[float, float], ...]:
    """The reply's percentiles as ``(level, value)`` pairs, in the order it returned them."""
    return tuple((point.percentile, point.value) for point in forecast.final_prediction.percentiles)


def _percentiles_used(distribution: NumericDistribution) -> tuple[tuple[float, float], ...]:
    """What the distribution actually holds after construction.

    This is not always what it was handed. ``_check_and_update_repeating_values`` runs
    inside the ``model_validator``, so on construction and before ``get_cdf`` is ever
    called, and it **replaces** ``declared_percentiles`` with a new list in which every
    repeated value has been nudged -- by ``1e-6`` when the value is strictly inside the
    bounds, by ``1e-10`` when it is at or outside one.

    Worth stating precisely, because ``docs/M1-NOTES.md`` and ``forecast/numeric.py`` both
    described this as mutating the list *in place*, and it does not: it builds fresh
    ``Percentile`` objects into a fresh list and rebinds the attribute, leaving the
    caller's list and the caller's objects untouched. Both prose claims are corrected on
    this branch. The distinction matters here, because "compare what we handed it against
    what it holds" is only a sound way to detect the nudge if our own copy survives it.

    It runs only under ``strict_validation``; with that off, the tie reaches ``get_cdf``
    unchanged and this returns the declared set.
    """
    return tuple((point.percentile, point.value) for point in distribution.declared_percentiles)


def _standardization_can_converge(distribution: NumericDistribution) -> bool:
    """Whether ``_standardize_cdf``'s scale search can terminate for this distribution.

    **A liveness guard, not a quality rule.** ``_standardize_cdf`` finds its scale with

    .. code-block:: python

        lo = hi = scale = 1.0
        while capped_sum(hi) < 1.0:
            hi *= 1.2

    where ``capped_sum`` sums ``min(cap, scale * pmf)`` over the interior points. If any
    interior PMF entry is **negative** that sum falls without bound as ``scale`` grows
    rather than rising: ``hi`` reaches ``inf``, ``inf * 1.2`` is still ``inf``, and
    ``capped_sum(inf)`` is ``-inf``, still below ``1.0``. The loop has no exit. It does not
    raise and it is not slow -- it never returns, and nothing on the forecast path sets a
    deadline, so the whole run stops.

    A negative interior PMF means the cumulative distribution decreased somewhere, and two
    unrelated inputs reach that from replies ``numeric_output_problems`` accepts:

    - a repeated value at or beyond a bound. ``_check_and_update_repeating_values``
      rewrites it **onto** the bound (plus ~1e-10) while leaving an unrepeated value
      further out untouched, so the set the conversion interpolates is no longer
      non-decreasing;
    - a log-scaled question whose values span many orders of magnitude, where the
      interpolation loses order on its own with no rewrite involved.

    Enumerating triggers was tried first and the second was found only after the first was
    closed, which is the argument for guarding the mechanism instead.

    **What this evaluates.** ``get_cdf`` builds its curve as ``_get_cdf_at`` over
    ``i / (cdf_size - 1)``, then standardizes, then re-validates. This runs only the first
    of those three, on the distribution the conversion will actually use -- so the tie
    rewrite has already happened and the log scaling is the SDK's own. It deliberately does
    **not** reuse ``standardize_cdf=False`` as the probe: an unstandardized curve has flat
    regions that fail the SDK's own ``5e-05`` spacing re-validation, so that probe refuses
    ordinary open-bounded forecasts, which is a fix bought by rejecting good ones.

    **Why a non-decreasing curve is then safe**, which is what keeps this a guard rather
    than a guess: ``apply_minimum`` is affine and increasing in the height plus a strictly
    increasing term in position, so an already-increasing curve stays increasing through it
    and every interior PMF entry is non-negative. ``capped_sum`` is then non-decreasing in
    ``scale`` and tends to ``pmf[0] + pmf[-1] + cap * (interior points with mass)`` with
    ``cap`` at ``0.19``, which clears ``1.0`` unless nearly every interior step is zero --
    and a curve that flat is what ``_check_percentile_spacing`` already refuses. The
    strictly-positive span is required for the same reason: ``_standardize_cdf`` divides by
    it, and a zero span yields ``inf``/``nan`` rather than a distribution.

    No epsilon, cap or bound arithmetic is transcribed here; a pin move changes what this
    *runs*. The general deadline on the conversion path is M1-514 -- this refuses what is
    reachable today, deterministically, before any array exists.
    """
    size = distribution.cdf_size or _SDK_DEFAULT_CDF_SIZE
    try:
        curve = [distribution._get_cdf_at(i / (size - 1)) for i in range(size)]
    except Exception:
        # Same rule and reason as ``_distribution`` and ``_values``: a third-party call
        # whose failure modes are not this repository's to enumerate, and whose messages
        # quote the values. Swallowed, never chained.
        return False
    if not all(isfinite(height) for height in curve):
        return False
    if not all(first <= second for first, second in pairwise(curve)):
        return False
    return bool(curve[-1] > curve[0])


def _distribution(
    forecast: NumericForecastResponse,
    calibration: NumericCalibrationConfig,
    question: CanonicalNumericQuestion,
    *,
    standardize: bool | None = None,
) -> NumericDistribution | None:
    """Construct the distribution, or ``None`` if the SDK refuses these percentiles.

    **Constructed field by field rather than through ``NumericDistribution.from_question``,
    and that is a deviation from the backlog row's wording.** ``from_question`` takes an
    SDK ``NumericQuestion | DateQuestion``. That object does not survive
    ``questions/normalize.py``: the canonical model is what the ledger stores and what any
    later conversion from a stored record has, so reaching ``from_question`` would mean
    fabricating an SDK question from the canonical one purely to have its fields read back
    out. It also hardcodes ``strict_validation`` to the model default, so it cannot honour
    ``numeric_calibration.strict_validation``; and it branches on
    ``isinstance(question, DateQuestion)``, the ``isinstance``-on-an-SDK-type shape this
    project has a standing gotcha about.

    The field mapping is therefore ours, and it is **pinned rather than asserted**:
    ``test_the_field_mapping_is_the_one_from_question_would_have_made`` builds a real SDK
    ``NumericQuestion``, calls ``from_question``, and compares the two distributions field
    for field, so a pin move that changes the mapping fails visibly instead of drifting.

    ``nominal_lower_bound``/``nominal_upper_bound`` are not passed because
    ``NumericDistribution`` has no such fields; ``from_question`` does not read them either.

    Returns ``None`` rather than raising, because a percentile set the SDK refuses is a
    problem with the *reply* and belongs in a repair turn. The exception is swallowed
    entirely and never chained: it is a ``pydantic.ValidationError`` whose text and
    ``input_value`` both carry the declared percentiles and the question's bounds.
    """
    try:
        return NumericDistribution(
            declared_percentiles=[
                Percentile(percentile=level, value=value) for level, value in _declared(forecast)
            ],
            open_upper_bound=question.open_upper_bound,
            open_lower_bound=question.open_lower_bound,
            upper_bound=question.upper_bound,
            lower_bound=question.lower_bound,
            zero_point=question.zero_point,
            cdf_size=question.cdf_size,
            standardize_cdf=(
                calibration.use_forecasting_tools_standardization
                if standardize is None
                else standardize
            ),
            strict_validation=calibration.strict_validation,
        )
    except Exception:
        # Broad and scoped to the one call, the M1-308 round-7 rule. Every validator in
        # ``numeric_report.py`` raises ``ValueError``, which pydantic re-raises as
        # ``ValidationError``; ``Percentile``'s own validator can raise before the
        # distribution is even reached; and the constructor is a third-party call whose
        # failure modes are not this repository's to enumerate. ``from None`` is implicit
        # -- nothing is re-raised.
        return None


def _values(distribution: NumericDistribution) -> tuple[float, ...] | None:
    """The submission array, or ``None`` if the conversion refuses this distribution.

    ``point.percentile``, not ``point.value``: ``get_cdf`` returns ``(x, height)`` pairs
    and the wire takes the heights. ``MetaculusClient.post_numeric_question_prediction``
    posts exactly this list under ``continuous_cdf``.

    ``+ 0.0`` normalizes ``-0.0`` to ``0.0`` and is the identity on every other float.
    That is a change of representation and never of value -- the two compare equal -- and
    it is here because ``docs/M2-NOTES.md`` names this as a standing risk that "becomes
    reachable when M1-503's CDF arrays exist": the two render differently, so they hash
    differently, so two arrays an operator would call equal derive two idempotency keys
    and two submissions. M2-NOTES declined to normalize floats *inside* the replay-critical
    hash rule, and this does not: it is the producer declining to emit a spelling the
    hasher would have to special-case.

    ``get_cdf`` re-validates its own output, and that is where the minimum-step rule
    (``5e-05`` between adjacent points) is enforced. A violation arrives here as a refusal
    rather than as a message, for the same reason as :func:`_distribution`.
    """
    try:
        cdf = distribution.get_cdf()
    except Exception:
        # Same rule and same reason as ``_distribution``: ``get_cdf`` raises a bare
        # ``ValueError`` from its interpolation helpers and a ``ValidationError`` from the
        # distribution it builds to re-validate itself, and both quote the values.
        return None
    return tuple(point.percentile + 0.0 for point in cdf)


def _array_problems(
    values: tuple[float, ...],
    calibration: NumericCalibrationConfig,
    question: CanonicalNumericQuestion,
) -> list[str]:
    """Every problem with a converted array, in a stable order.

    The order is length, membership of the unit interval, monotonicity, the step cap, then
    the two endpoint rules -- and every rule is reported rather than the first one, because
    the complete account in one repair turn is worth more than the second billed call a
    staged check would cost. ``numeric_output_problems`` makes the same argument.

    The unit-interval rule is **unreachable through the pinned SDK** and is not pretending
    otherwise: ``Percentile``'s own validator refuses a ``percentile`` outside ``[0, 1]``
    and refuses NaN, and an infinity fails the same comparison. It is kept because it is
    the contract ``submission_live._require_cdf`` enforces on the way back in, and a pin
    move that loosened ``Percentile`` would otherwise produce an array refused *after* a
    human approved the forecast. The test says it is unreachable rather than inventing a
    draw that reaches it.
    """
    problems: list[str] = []
    if len(values) != calibration.expected_cdf_points:
        problems.append(f"{_PERCENTILES_LOC}: {_WRONG_LENGTH}")
    if not all(isfinite(value) and 0.0 <= value <= 1.0 for value in values):
        problems.append(f"{_PERCENTILES_LOC}: {_NOT_IN_UNIT_INTERVAL}")
        # The three rules below all compare adjacent values, and a non-finite member makes
        # every one of those comparisons meaningless rather than false. Reported and
        # stopped, so the repair turn says the one true thing about this array.
        return problems
    if not all(first <= second for first, second in pairwise(values)):
        problems.append(f"{_PERCENTILES_LOC}: {_NOT_MONOTONE}")
    if any(second - first > calibration.max_adjacent_pmf for first, second in pairwise(values)):
        problems.append(f"{_PERCENTILES_LOC}: {_STEP_TOO_TALL}")
    if not question.open_lower_bound and values and values[0] != 0.0:
        problems.append(f"{_PERCENTILES_LOC}: {_LOWER_ENDPOINT}")
    if not question.open_upper_bound and values and values[-1] != 1.0:
        problems.append(f"{_PERCENTILES_LOC}: {_UPPER_ENDPOINT}")
    return problems


def numeric_cdf_or_problems(
    forecast: NumericForecastResponse,
    calibration: NumericCalibrationConfig,
    question: CanonicalNumericQuestion,
) -> tuple[NumericCdf | None, list[str]]:
    """Convert one numeric response, or report why it cannot be converted.

    ``parse._parse``'s shape, for its reason: inside the attempt loop a problem is a repair
    turn rather than an error, and the caller needs the value *and* the problems from one
    pass rather than converting twice. An empty problem list with a ``None`` CDF is
    impossible, and a CDF with a non-empty list is impossible.

    A non-empty list means this reply cannot be submitted as it stands. Each string is a
    schema-authored field path, a colon and a value-free message -- safe to log, to store,
    and to send back to the model as a repair turn, in the shape ``generate._repair_turn``
    renders and ``generate._classify`` reads as ``schema_invalid``, with no change on
    either side.

    **This assumes M1-405's checks have already run.** ``forecast.parse`` applies them
    inside the same loop and before this, so by the time a response reaches here its levels
    are exactly the declared nine, its values are non-decreasing and inside any closed
    bound, and no value sits below the ``zero_point``. Nothing here restates them; what
    this module adds is everything ``numeric_calibration`` decides. The one exception is
    :func:`_require_question`, which restates the two *question* facts no reply could
    repair.

    Raises :class:`NumericCdfError` only for a caller mistake -- a response, a calibration
    config or a question of the wrong type, a question no percentile set could satisfy, or a
    thread on which the wall-clock bound cannot be installed. Those are not problems with
    the model's output and must never become a repair turn.

    Raises :class:`NumericCdfTimeoutError` -- a subclass, so a caller handling the parent
    needs no change -- when the SDK section does not finish inside
    ``conversion_timeout_seconds``. That one *is* about this reply, and it is still not a
    repair turn: see the class for why.
    """
    if not isinstance(forecast, NumericForecastResponse):
        # Exact category, not a duck-typed read: a response of another question type has no
        # percentiles, and a non-response has no fields at all. Both are caller mistakes,
        # and neither is something to ask the model to fix.
        raise NumericCdfError(["forecast: must be a numeric forecast response"])
    _require_question(question, calibration)

    # Everything that reaches the SDK, and nothing that does not: ``_require_question``
    # above and ``_array_problems`` below are ours and pure, so bounding them would only
    # widen the window in which a caller's own deadline is suspended. M1-514.
    with _bounded(calibration.conversion_timeout_seconds):
        distribution = _distribution(forecast, calibration, question)
        if distribution is None:
            return None, [f"{_PERCENTILES_LOC}: {_NOT_CONVERTIBLE}"]
        used = _percentiles_used(distribution)
        if calibration.use_forecasting_tools_standardization and not _standardization_can_converge(
            distribution
        ):
            # The liveness guard, and it must stay ahead of ``_values``: ``get_cdf`` does
            # not reject the inputs below, it fails to terminate on them. See
            # :func:`_standardization_can_converge` for the mechanism and why the guard is
            # on that rather than on a list of triggers. Skipped when standardization is
            # off, because then ``_values`` *is* the unstandardized path and the loop that
            # hangs is never reached -- probing first would just convert twice for nothing.
            #
            # It stays even though ``_bounded`` now covers the same failure, because the
            # two outcomes differ: this is deterministic, costs one interpolation, and
            # gives the model a repair turn, where the bound costs the whole deadline and
            # gives the question a terminal ``timeout``. Fast path first, backstop second.
            return None, [f"{_PERCENTILES_LOC}: {_NOT_WELL_FORMED}"]
        values = _values(distribution)
    if values is None:
        return None, [f"{_PERCENTILES_LOC}: {_NOT_CONVERTIBLE}"]
    problems = _array_problems(values, calibration, question)
    if problems:
        return None, problems

    adjusted = used != _declared(forecast)
    if adjusted:
        # The count, never the values. M1-508 is the row that decides what a tie may cost;
        # what this item owes it is that the divergence is observable rather than silent,
        # and ``NumericCdf.percentiles_used`` is where a caller reads it. A log line naming
        # the values would put model output into the operator's logs for a case that is not
        # even an error.
        _LOGGER.info(
            "numeric CDF built from %d adjusted percentile value(s) (see M1-508)",
            sum(1 for before, after in zip(_declared(forecast), used) if before != after),
        )
    return (
        NumericCdf(
            values=values,
            percentiles_used=used,
            adjusted=adjusted,
            standardized=calibration.use_forecasting_tools_standardization,
        ),
        [],
    )


def build_numeric_cdf(
    forecast: NumericForecastResponse,
    calibration: NumericCalibrationConfig,
    question: CanonicalNumericQuestion,
) -> NumericCdf:
    """Return the converted CDF, or raise with the sanitized problems.

    The entry point for a caller holding a response it cannot repair -- a payload build, or
    a conversion pass over a stored record. ``forecast.generate`` uses
    :func:`numeric_cdf_or_problems` instead, because inside the attempt loop a problem is a
    repair turn rather than an error.

    Nothing this module returns is clamped, sorted, truncated or padded. The SDK's
    standardization is disclosed on :attr:`NumericCdf.standardized` rather than hidden, and
    it is the only transformation in the path; an array this module edited to satisfy its
    own checks would be a distribution the ledger could not attribute to anything.
    """
    cdf, problems = numeric_cdf_or_problems(forecast, calibration, question)
    if cdf is None:
        raise NumericCdfError(problems)
    return cdf
