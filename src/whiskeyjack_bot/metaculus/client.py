"""Single construction point for the forecasting-tools MetaculusClient (M0-101, M2-704).

Everything that talks to Metaculus goes through :func:`build_client`; nothing
else in the codebase may instantiate ``MetaculusClient`` directly. The token
is read from the configured environment variable at construction time, passed
to the SDK, and never stored, logged, or echoed by this module. Constructing
the SDK client performs no network I/O (verified against the pinned
forecasting-tools==0.2.92 source).

**M2-704 added :class:`SingleAttemptPoster`, and it is the only place in the tree that
knows the pinned SDK retries.** ``MetaculusClient._post_question_prediction`` carries
``@retry_with_exponential_backoff()`` (``max_retries=3``) whose ``retry_on_exceptions`` is
``requests.exceptions.RequestException`` -- which ``HTTPError`` subclasses. Measured
against ``forecasting-tools==0.2.92`` with ``requests.post`` stubbed: **four POSTs on a
timeout, four on a 400.** A timed-out post that actually landed is re-posted three more
times under one idempotency key with no refetch in between, which is the blind retry
M2-704's acceptance criterion forbids, arriving from inside the dependency.

The line this module draws is **reads may retry, writes must not**. A GET is idempotent
and its retry is kept exactly as the SDK ships it; the POST is made through the
undecorated function the decorator wrapped, so exactly one request is sent and the real
exception propagates. That is a *guarded* dependency on a private name, which is what
``M2-705``'s acceptance criterion contemplates ("no private package method dependency
without a guard") -- and it is not the dependency D28 rejected, which was on a private
method to *capture a response body*. Nothing here reads a response; it only declines to
have the request repeated. The guards are :func:`_assert_single_post_is_reachable` at
import and ``tests/unit/test_metaculus_poster.py``, which drives the real SDK class with a
counted stub and asserts four-without / one-with. A version bump that changes the shape is
a red build, not a silent return to four posts.
"""

from __future__ import annotations

import inspect
import os
import threading
import types
import weakref
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.config import AppConfig

if TYPE_CHECKING:  # pragma: no cover - import-time typing only, never at runtime
    from whiskeyjack_bot.submission_live import MetaculusPoster


class MissingCredentialError(Exception):
    """A required credential environment variable is unset.

    Raised before any network attempt; the message names the variable and
    never contains a value.
    """

    def __init__(self, env_var_name: str):
        self.env_var_name = env_var_name
        super().__init__(
            f"environment variable {env_var_name} is not set; "
            "set it in the environment (never in config or code)"
        )


def build_client(config: AppConfig) -> MetaculusClient:
    """Construct the one configured MetaculusClient.

    Raises :class:`MissingCredentialError` when the configured token variable
    is unset or empty — callers on fixture-only paths must not call this at
    all, so reaching here without a token is always an operator error worth
    failing loudly on.
    """
    token_env = config.metaculus.token_env
    token = os.environ.get(token_env)
    if not token:
        raise MissingCredentialError(token_env)
    return MetaculusClient(
        base_url=config.metaculus.base_url,
        timeout=int(config.metaculus.request_timeout_seconds),
        sleep_seconds_between_requests=config.metaculus.request_spacing_seconds,
        sleep_jitter_seconds=config.metaculus.request_jitter_seconds,
        token=token,
    )


class PosterContractError(Exception):
    """The pinned SDK no longer has the shape :class:`SingleAttemptPoster` depends on.

    Raised at **import** rather than at first use, for the reason
    ``submission._assert_prefix_matches_version`` gives: a guard only a test enforces is a
    guard the next module to import this one does not have. Failing to import is the
    correct outcome -- the alternative is a build that silently posts four times.

    Same hygiene rule as the rest of the package: the message names only this module's own
    expectations about the dependency, never a value.
    """


# The undecorated function `@retry_with_exponential_backoff()` wrapped. `functools.wraps`
# sets `__wrapped__`, so this is the documented way back to the original -- not a reach
# into the decorator's closure.
_RAW_POST_PREDICTION: Any = getattr(MetaculusClient._post_question_prediction, "__wrapped__", None)

# The signature that function must have for the shadowing below to be a pass-through rather
# than a re-implementation. `post_binary_question_prediction` and its two siblings build
# `forecast_payload` and call it with exactly these two arguments.
_EXPECTED_POST_PARAMETERS = ("self", "question_id", "forecast_payload")


def _assert_single_post_is_reachable() -> None:
    """Fail at import unless exactly one post can still be made through the public methods.

    Three checks, and each one closes a different way the guard could quietly stop working:

    1. ``__wrapped__`` exists -- the retry decorator is still applied and still uses
       ``functools.wraps``. If a future version drops the decorator entirely this fails,
       which is the right outcome: the guard's premise would be gone and its absence should
       be a decision, not a discovery.
    2. It is not the decorated attribute itself, so the unwrapping is real.
    3. Its parameters are the ones the public post methods pass. A renamed or re-ordered
       parameter would make the shadowed call a different call, and a *silently* different
       call is the failure mode this whole adapter exists to prevent.
    """
    if _RAW_POST_PREDICTION is None:
        raise PosterContractError(
            "the pinned forecasting-tools MetaculusClient no longer exposes an unwrapped "
            "_post_question_prediction; a single-attempt post cannot be guaranteed"
        )
    if _RAW_POST_PREDICTION is MetaculusClient._post_question_prediction:
        raise PosterContractError(
            "the pinned forecasting-tools MetaculusClient's _post_question_prediction is "
            "its own __wrapped__, so unwrapping it removes no retry"
        )
    try:
        parameters = tuple(inspect.signature(_RAW_POST_PREDICTION).parameters)
    except (TypeError, ValueError):  # pragma: no cover - a builtin would land here
        raise PosterContractError(
            "the pinned forecasting-tools MetaculusClient's unwrapped "
            "_post_question_prediction has no readable signature"
        ) from None
    if parameters != _EXPECTED_POST_PARAMETERS:
        raise PosterContractError(
            "the pinned forecasting-tools MetaculusClient's unwrapped "
            f"_post_question_prediction takes {parameters!r}, not "
            f"{_EXPECTED_POST_PARAMETERS!r}; the single-attempt guard cannot be applied"
        )


_assert_single_post_is_reachable()


# The override window shadows an attribute on the *client*, so the mutual exclusion that
# makes it safe has to be keyed on the client too. A per-adapter lock does not do it: two
# `SingleAttemptPoster`s over one `MetaculusClient` each hold their own, so B's `finally`
# can restore the class method while A is still inside its window -- and A's post then
# resolves the decorated attribute and is retried four times. That is the exact blind
# retry this class exists to prevent, and it was found by M2-704 round-1 cross-model
# review (reproduced: one logical post, four POSTs).
#
# A `WeakKeyDictionary` so a client that goes out of scope takes its lock with it. It is
# guarded by a plain module lock because `setdefault` on a `WeakKeyDictionary` is not
# atomic under free-threading, and two adapters constructed concurrently over one client
# must not each be handed a different lock -- which would reopen the very race this
# closes.
_CLIENT_LOCKS: MutableMapping[MetaculusClient, threading.RLock] = weakref.WeakKeyDictionary()
_CLIENT_LOCKS_GUARD = threading.Lock()


def _lock_for_client(client: MetaculusClient) -> threading.RLock:
    """The one lock guarding every override window on *client*, created on first use."""
    with _CLIENT_LOCKS_GUARD:
        existing = _CLIENT_LOCKS.get(client)
        if existing is None:
            existing = threading.RLock()
            _CLIENT_LOCKS[client] = existing
        return existing


class SingleAttemptPoster:
    """A Metaculus client that posts **once** per call and refetches with the SDK's retry.

    Satisfies ``submission_live.MetaculusPoster``. The three post methods are pass-throughs
    to the SDK's public ones -- so every bound and every payload shape those enforce still
    applies, and this is not the narrow HTTP adapter M2-705 spikes -- wrapped in a window
    where ``_post_question_prediction`` resolves to the function the retry decorator wraps
    rather than to the decorator. Instance attributes shadow class attributes, so the
    public method's own ``self._post_question_prediction(...)`` finds it.

    ``get_question_by_post_id`` is a plain pass-through **with its retry intact**. A GET is
    idempotent, retrying it is free of consequence, and it is what makes the "refetch could
    not be performed" case rare enough to be an edge rather than a routine outcome.

    The window is held under a lock. The pipeline is single-threaded today
    (``run_limits.max_parallel_questions`` is 1), so this is not fixing a live bug -- it is
    that a shadow-and-restore window shared between two callers would restore the class
    method while the other was still inside it, and the cost of preventing that is one
    lock.
    """

    def __init__(self, client: MetaculusClient) -> None:
        if not isinstance(client, MetaculusClient):
            raise PosterContractError("client must be a MetaculusClient")
        self._client = client
        self._lock = _lock_for_client(client)

    def post_binary_question_prediction(
        self, question_id: int, prediction_in_decimal: float
    ) -> None:
        with self._single_attempt():
            self._client.post_binary_question_prediction(question_id, prediction_in_decimal)

    def post_numeric_question_prediction(self, question_id: int, cdf_values: list[float]) -> None:
        with self._single_attempt():
            self._client.post_numeric_question_prediction(question_id, cdf_values)

    def post_multiple_choice_question_prediction(
        self, question_id: int, options_with_probabilities: dict[str, float]
    ) -> None:
        with self._single_attempt():
            self._client.post_multiple_choice_question_prediction(
                question_id, options_with_probabilities
            )

    def get_question_by_post_id(self, post_id: int) -> object:
        return self._client.get_question_by_post_id(post_id)

    @contextmanager
    def _single_attempt(self) -> Iterator[None]:
        """Bind the undecorated post for the duration of one call, then unbind it.

        ``setattr``/``delattr`` by name rather than attribute syntax: the target is a bound
        method on a third-party class, and naming it as an attribute would be a type error
        for what is deliberately a runtime shadow.

        The lock is shared by every adapter over this client (:func:`_lock_for_client`),
        not owned by this adapter: the attribute being shadowed belongs to the client, so
        anything less lets one adapter's ``finally`` restore the class method while another
        is still inside its window -- and that adapter's post is then retried four times.

        The ``finally`` restores the attribute's **exact prior state** rather than
        unconditionally deleting it: whatever was in the instance ``__dict__`` on the way in
        is what is there on the way out, and if nothing was, nothing is. Deleting
        unconditionally was correct only while no window could ever nest inside another, and
        that is a property of the lock rather than of this function -- so the restore does
        not depend on it. ``delattr`` stays guarded because a caller who reached in and
        removed it first must not turn a completed post into an exception.
        """
        with self._lock:
            sentinel = object()
            previous: object = self._client.__dict__.get("_post_question_prediction", sentinel)
            setattr(
                self._client,
                "_post_question_prediction",
                types.MethodType(_RAW_POST_PREDICTION, self._client),
            )
            try:
                yield
            finally:
                if previous is sentinel:
                    try:
                        delattr(self._client, "_post_question_prediction")
                    except AttributeError:  # pragma: no cover - only if a caller removed it
                        pass
                else:
                    setattr(self._client, "_post_question_prediction", previous)


def build_poster(config: AppConfig) -> SingleAttemptPoster:
    """Construct the one configured single-attempt poster.

    The seam the CLI calls. It goes through :func:`build_client`, so the token still has
    exactly one construction point and the ``MissingCredentialError`` contract is unchanged.
    """
    return SingleAttemptPoster(build_client(config))


if TYPE_CHECKING:  # pragma: no cover - a static conformance check, never executed

    def _poster_conforms(poster: SingleAttemptPoster) -> MetaculusPoster:
        """Fail ``mypy --strict`` if the adapter drifts from the protocol it promises.

        In ``src`` rather than in a test because the gate type-checks ``src`` only, and a
        protocol conformance nobody checks is a protocol nobody keeps.
        """
        return poster
