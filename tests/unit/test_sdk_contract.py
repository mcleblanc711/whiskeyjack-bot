"""M2-705: every reach into a pinned third party's shape, and a guard on each one.

The acceptance criterion's second clause is *"no private package method dependency without
a guard"*, and taking it literally is what this module is. It is deliberately **not**
scoped to the submission seam: the criterion's words are unqualified, and the repository's
one genuine private-*method* call is in `forecast/cdf.py`, nowhere near a submission.

**The hazard class, stated once so the enumeration has a rule.** `docs/LESSONS.md` #6 says
that when a finding names one exception type from a third-party parser, you enumerate its
siblings by execution rather than fixing the instance. The sibling relation here is not
"is private" -- `MetaculusQuestion.api_json` is a declared public pydantic field and is one
of the least guarded things in the tree. It is:

    a reach into a pinned third party's shape whose failure mode, if the pin moves,
    is a *wrong or degraded answer* rather than an exception.

Every one of those is enumerated in `THIRD_PARTY_REACHES` below, and
`test_no_new_reach_escapes_this_table` fails when a new one appears in `src/`. That is what
makes the criterion a gate rather than a claim -- the same reason `check-migrations.sh`
exists next to `docs/TRACKS.md`'s advisory migration column.

**A guard is a test that fails when the shape is gone.** Not a `getattr` default: those are
in `src/` on purpose, because production must degrade rather than raise after a post has
been made, and a default that quietly answers `None` is exactly why the drift needs to be
caught *here* instead. So every guard below is paired with a mutation of itself -- the
attribute taken away, the guard asserted to fail. A guard that still passes against a
package that no longer has the attribute is worse than no guard, because it is a claim.

**What this module does not do.** It changes nothing in `src/`. Two guards could have been
import-time assertions in the module that owns the reach, the way
`metaculus.client._assert_single_post_is_reachable` is; that is written up as a rejected
alternative in `docs/M2-NOTES.md` and the short version is that `submission_live.py`
deliberately imports nothing from `forecasting_tools`, and that turning an already-merged
module's silent degradation into an import-time refusal is a behaviour change no criterion
on this branch asks for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from forecasting_tools import NumericDistribution, Percentile
from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import (
    BinaryQuestion,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    QuestionState,
)
from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.forecast.cdf import _standardization_can_converge
from whiskeyjack_bot.research.transport import apply_connection_retries
from whiskeyjack_bot.submission_live import read_my_forecasts

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "whiskeyjack_bot"
API_POSTS = REPO_ROOT / "tests" / "fixtures" / "api_posts"


def _requires(present: bool, what: str) -> None:
    """The shared shape of every guard here, so the mutation half can drive it too.

    Split out rather than inlined as a bare ``assert`` because a guard you cannot point a
    stub at is a guard you cannot prove fails.
    """
    if not present:
        raise AssertionError(
            f"the pinned third-party no longer provides {what}; this repository depends on "
            "it, so the pin move is a decision to make rather than a behaviour to discover"
        )


# ── the enumeration ──────────────────────────────────────────────────────────

# Keyed on ``(module under src/, attribute name)`` -- stable when code moves within a file,
# which line numbers are not. The value says what the reach is and what holds it down.
#
# Entries whose guard is "not third party" are the ones the scan below picks up because
# they are syntactically identical: an optional field read off one of *this project's* own
# dataclasses. They are listed rather than filtered out by a heuristic, because a heuristic
# that decided what counted would be the silent-skip failure `docs/LESSONS.md` #7 is about.
THIRD_PARTY_REACHES: dict[tuple[str, str], str] = {
    # --- forecasting-tools: guarded here, by this module ---
    ("submission_live.py", "api_json"): "test_both_readers_agree_on_one_forecast_history",
    ("questions/normalize.py", "api_json"): "test_both_readers_agree_on_one_forecast_history",
    ("submission_policy.py", "api_json"): (
        "test_the_numeric_scaling_shape_submission_policy_reads_is_the_sdks plus "
        "test_the_project_membership_shape_submission_policy_reads_is_the_sdks"
    ),
    ("submission_live.py", "state"): "test_the_open_state_is_the_string_the_refusal_compares",
    ("submission_live.py", "value"): "test_the_open_state_is_the_string_the_refusal_compares",
    ("cli.py", "question_type"): "test_the_question_type_attribute_exists_on_the_real_classes",
    # --- forecasting-tools: guarded elsewhere, before this item ---
    ("metaculus/client.py", "__wrapped__"): (
        "metaculus.client._assert_single_post_is_reachable, at import, plus "
        "tests/unit/test_metaculus_poster.py's three contract tests"
    ),
    ("submission_live.py", "__module__"): (
        "tests/unit/test_metaculus_poster.py::test_the_error_vocabulary_is_pinned_to_the_"
        "real_classes"
    ),
    ("submission_live.py", "__cause__"): (
        "tests/unit/test_metaculus_poster.py::test_the_http_status_is_recoverable_through_"
        "the_cause_chain"
    ),
    ("submission_live.py", "response"): "same, via http_details",
    ("submission_live.py", "status_code"): "same, via http_details",
    ("submission_live.py", "text"): "same, via http_details",
    ("submission_live.py", "headers"): "same, via http_details",
    ("forecast/generate.py", "model"): (
        "tests/unit/test_forecast_generate.py drives a real GeneralLlm; `model` is a "
        "constructor parameter the SDK names in its own signature"
    ),
    # --- httpx / httpcore ---
    ("research/transport.py", "_pool"): "test_the_connection_pool_already_has_the_field_we_set",
    # --- asknews ---
    ("research/asknews.py", "name"): "test_the_asknews_author_still_carries_a_name",
    ("forecast/generate.py", "last_cost"): (
        "not third party -- SolClient's own attribute; the pinned GeneralLlm has no "
        "last_cost at all (confirmed by execution), so the getattr default is what every "
        "other Forecaster implementation actually takes"
    ),
    # --- not third party: this project's own optional dataclass fields ---
    ("forecast/artifacts.py", "request"): "not third party",
    ("forecast/artifacts.py", "raw_responses"): "not third party",
    ("forecast/artifacts.py", "invocations"): "not third party",
    ("forecast/artifacts.py", "repair_attempted"): "not third party",
    ("forecast/artifacts.py", "failure_code"): "not third party",
    ("forecast/artifacts.py", "failure_problems"): "not third party",
    ("forecast/artifacts.py", "settings"): "not third party",
    ("forecast/artifacts.py", "cost_usd"): "not third party",
    ("forecast/record.py", "forecast"): "not third party",
    ("forecast/record.py", "settings"): "not third party",
    ("forecast/record.py", "sources"): "not third party",
    ("forecast/validate.py", "question_type"): "not third party",
    ("logging_setup.py", "_whiskeyjack"): "not third party -- our own marker on our own handler",
}

_GETATTR = re.compile(r'getattr\(\s*[^,]+?\s*,\s*"([A-Za-z_][A-Za-z0-9_]*)"')


def _scan_reaches() -> set[tuple[str, str]]:
    """Every ``getattr(x, "literal")`` in ``src/``, as ``(module, attribute)``."""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        for name in _GETATTR.findall(path.read_text(encoding="utf-8")):
            found.add((module, name))
    return found


def test_no_new_reach_escapes_this_table() -> None:
    """**The gate.** A new guarded attribute read cannot be added without deciding about it.

    This is the half of the acceptance criterion that has to keep holding after the branch
    merges. Without it the table above is a list someone wrote once, and the next
    `getattr(question, "something", None)` ships with no guard and nothing to say so.

    If this fails, the fix is to add the row -- naming the guard, or `"not third party"`
    with the reason -- not to widen the regex.
    """
    scanned = _scan_reaches()
    undeclared = scanned - set(THIRD_PARTY_REACHES)
    assert not undeclared, (
        f"new guarded attribute reads with no row in THIRD_PARTY_REACHES: "
        f"{sorted(undeclared)}. Add a row naming the guard that fails when the shape is "
        "gone, or 'not third party' with the reason."
    )
    stale = set(THIRD_PARTY_REACHES) - scanned
    assert not stale, (
        f"THIRD_PARTY_REACHES names reads that no longer exist: {sorted(stale)}. A table "
        "that keeps claiming to cover deleted code hides the day it stops covering live code."
    )


def test_the_scan_would_notice_a_new_reach() -> None:
    """The gate above is only worth its line count if its scanner can see one.

    `docs/LESSONS.md` #5: check the test can fail. A regex that matched nothing would make
    `test_no_new_reach_escapes_this_table` pass forever, and it is the exact shape of
    "assertion that cannot fire" this project has been bitten by.
    """
    assert _GETATTR.findall('getattr(question, "api_json", None)') == ["api_json"]
    assert _GETATTR.findall("getattr(question, name, None)") == [], "only literals are scannable"
    # And the live scan is not accidentally empty.
    assert len(_scan_reaches()) >= 20


# ── forecasting-tools: the one genuine private *method* ──────────────────────


def _convergent_distribution() -> NumericDistribution:
    """A well-formed, comfortably convergent distribution the guard should accept."""
    return NumericDistribution(
        declared_percentiles=[
            Percentile(percentile=0.1, value=10.0),
            Percentile(percentile=0.5, value=50.0),
            Percentile(percentile=0.9, value=90.0),
        ],
        open_upper_bound=False,
        open_lower_bound=False,
        upper_bound=100.0,
        lower_bound=0.0,
        zero_point=None,
    )


def test_the_private_cdf_probe_is_still_there_and_still_answers() -> None:
    """`NumericDistribution._get_cdf_at` -- the repository's one private-method call.

    `forecast/cdf.py:543` calls it directly, inside `except Exception: return False`, to
    decide whether the SDK's standardization can converge before handing it an array.

    **The drift failure mode, established by execution and not by reading** -- see the
    mutation test below. It is *not* a silent no-op: the swallow turns a rename into
    `False`, and `False` means refuse, so a pin move would refuse **every** numeric
    forecast while reporting a non-convergence that is not what happened. Fail-closed, but
    with a diagnosis that points at the forecast instead of at the dependency. This guard
    exists to make the real cause legible on the day it happens.
    """
    _requires(hasattr(NumericDistribution, "_get_cdf_at"), "NumericDistribution._get_cdf_at")
    curve = [_convergent_distribution()._get_cdf_at(step / 200) for step in range(201)]
    assert all(isinstance(height, float) for height in curve)
    assert all(first <= second for first, second in zip(curve, curve[1:], strict=False))
    assert curve[-1] > curve[0]
    # And the guard that calls it reaches a positive answer, so the call is live rather
    # than swallowed on a good input.
    assert _standardization_can_converge(_convergent_distribution()) is True


def test_removing_the_private_cdf_probe_makes_the_convergence_guard_refuse_everything() -> None:
    """The mutation, and the evidence for the docstring above.

    A stand-in with no `_get_cdf_at` reaches the `except Exception` arm, and
    `_standardization_can_converge` answers `False` -- which its one caller reads as
    "refuse this forecast". Asserted rather than reasoned about, because the reasoning is
    what the plan for this item got wrong before running it.
    """

    class _WithoutTheProbe:
        cdf_size = 201

    assert _standardization_can_converge(_WithoutTheProbe()) is False  # type: ignore[arg-type]
    with pytest.raises(AssertionError):
        _requires(hasattr(_WithoutTheProbe, "_get_cdf_at"), "NumericDistribution._get_cdf_at")


# ── forecasting-tools: the refetch's shape ───────────────────────────────────


def _binary_post_with_history(entries: list[dict[str, Any]], *, aggregations: bool) -> dict:
    """The committed binary post, with a forecast history and optionally aggregations.

    `aggregations` is a parameter because the SDK's reader needs it and ours does not,
    which is one of the two findings below rather than a detail of the fixture.
    """
    post = json.loads((API_POSTS / "binary_post.json").read_text(encoding="utf-8"))
    post["question"]["my_forecasts"] = {
        "history": entries,
        "latest": entries[-1] if entries else None,
    }
    if aggregations:
        post["question"]["aggregations"] = {"recency_weighted": {"latest": {"centers": [0.4]}}}
    return post


HISTORY = [
    {"start_time": 1_000_000.0, "end_time": None, "forecast_values": [0.7, 0.3]},
    {"start_time": 1_000_100.0, "end_time": None, "forecast_values": [0.63, 0.37]},
]


def test_both_readers_agree_on_one_forecast_history() -> None:
    """`api_json["question"]["my_forecasts"]["history"]`, read twice from the same bytes.

    `submission_live.read_my_forecasts` walks that path by hand; the SDK's own
    `BinaryQuestion.from_metaculus_api_json` walks it too, into `previous_forecasts`. Two
    independent readers of one payload is a measurement of the shape. Every existing test
    of `read_my_forecasts` drives a hand-written `FakeQuestion` whose `api_json` we wrote
    to match the assumption, which cannot detect the assumption being wrong.

    The keys are the ones the SDK itself names -- `start_time` and `forecast_values`
    (`questions.py:343`) -- so a schema change that renamed either would move both readers
    together *only* if it also changed the SDK. That is precisely the drift this catches:
    ours would go blind while the SDK's kept working, or the reverse.
    """
    post = _binary_post_with_history(HISTORY, aggregations=True)
    question = DataOrganizer.get_question_from_post_json(post)
    assert isinstance(question, BinaryQuestion)

    ours = read_my_forecasts(question)
    assert ours is not None, "the raw path must read the real object the SDK built"
    theirs = question.previous_forecasts
    assert theirs is not None, "the SDK's own reader found the same history"

    assert len(ours.entries) == len(theirs) == len(HISTORY)
    for mine, sdk in zip(ours.entries, theirs, strict=True):
        assert mine.start_time == sdk.timestamp.timestamp()
        assert mine.values[1] == pytest.approx(sdk.prediction_in_decimal)


def test_the_sdk_reader_loses_the_history_when_an_unrelated_key_is_missing() -> None:
    """**Why `submission_live` reads the raw path, restated as a second reason.**

    The module docstring gives one: `MultipleChoiceQuestion` never populates
    `previous_forecasts`. Here is another, found while writing this guard --
    `BinaryQuestion.from_metaculus_api_json` reads `aggregations` and `my_forecasts` inside
    **one** `try`, so a post with no aggregation block yields `previous_forecasts is None`
    even though its forecast history is right there and perfectly readable.

    Every committed fixture in `tests/fixtures/api_posts/` is such a post. A verification
    built on the SDK's field would therefore have gone blind on a condition that has
    nothing to do with forecasts at all.
    """
    post = _binary_post_with_history(HISTORY, aggregations=False)
    question = DataOrganizer.get_question_from_post_json(post)
    assert isinstance(question, BinaryQuestion)

    assert question.previous_forecasts is None, "the SDK's reader gave up"
    ours = read_my_forecasts(question)
    assert ours is not None and len(ours.entries) == len(HISTORY), "the raw path did not"


def _numeric_question_from_fixture() -> Any:
    post = json.loads((API_POSTS / "numeric_post.json").read_text(encoding="utf-8"))
    return DataOrganizer.get_question_from_post_json(post)


def test_the_numeric_scaling_shape_submission_policy_reads_is_the_sdks() -> None:
    """`submission_policy.before_post` reads `api_json["question"]["scaling"]` by hand.

    A third reach into `api_json`, alongside the two `test_both_readers_agree_on_one_
    forecast_history` already guards -- but a different sub-path, so that test passing
    says nothing about this one. The refusal it backs (`live numeric bounds are
    unreadable; nothing was posted`) exists specifically because this shape is read off a
    hand-written `FakeQuestion` everywhere else in the test suite (`test_cli_submit.py`),
    which by construction cannot detect the assumption being wrong.
    """
    question = _numeric_question_from_fixture()
    raw = getattr(question, "api_json", None)
    _requires(raw is not None, "api_json on a real NumericQuestion")
    inner = raw.get("question", {}) if isinstance(raw, dict) else {}
    scaling = inner.get("scaling", {})
    assert isinstance(scaling, dict)
    assert {"range_min", "range_max", "zero_point", "inbound_outcome_count"} <= scaling.keys()
    assert all(type(inner.get(k)) is bool for k in ("open_lower_bound", "open_upper_bound"))


def test_removing_a_scaling_key_makes_submission_policys_own_check_refuse() -> None:
    """The mutation: reproduce `before_post`'s exact condition, one key short.

    Copied rather than imported because the real condition lives inline inside a closure
    factory (`build_before_post`) that needs a live ledger connection and activation to
    construct -- this asserts the boolean expression it evaluates would flip, which is
    the property that matters for a guard.
    """
    inner = {
        "scaling": {"range_min": 0.0, "range_max": 1.0, "zero_point": None},
        "open_lower_bound": False,
        "open_upper_bound": True,
    }
    scaling = inner.get("scaling", {})
    unreadable = (
        not isinstance(scaling, dict)
        or not {"range_min", "range_max", "zero_point", "inbound_outcome_count"} <= scaling.keys()
        or any(type(inner.get(k)) is not bool for k in ("open_lower_bound", "open_upper_bound"))
    )
    assert unreadable, "missing inbound_outcome_count must trip the refusal"


def test_the_project_membership_shape_submission_policy_reads_is_the_sdks() -> None:
    """`submission_policy.before_post` also reads `api_json["projects"]` by hand.

    The activated-project check (`live question is not in the activated project`) walks
    `projects.values()` looking for a dict carrying `id`. On a real MiniBench post that id
    lives on `projects["default_project"]`, not on the `"tournament"` list entries (those
    carry only `slug`/`name`) -- a detail no hand-written double would surface unless it
    happened to be written to match, which is exactly what every existing caller does.
    """
    question = _numeric_question_from_fixture()
    api = getattr(question, "api_json", None)
    _requires(api is not None, "api_json on a real NumericQuestion")
    projects = api.get("projects", {}) if isinstance(api, dict) else {}
    memberships = [
        p
        for values in projects.values()
        for p in (values if isinstance(values, list) else [values])
        if isinstance(p, dict)
    ]
    with_id = [p for p in memberships if "id" in p]
    _requires(bool(with_id), "at least one projects entry carrying an id")
    assert isinstance(with_id[0]["id"], int)


def test_the_multiple_choice_reader_still_populates_nothing() -> None:
    """The documented deviation, pinned so a future SDK fixing it is news rather than luck."""
    post = json.loads((API_POSTS / "multiple_choice_post.json").read_text(encoding="utf-8"))
    post["question"]["my_forecasts"] = {"history": HISTORY, "latest": HISTORY[-1]}
    question = DataOrganizer.get_question_from_post_json(post)
    assert isinstance(question, MultipleChoiceQuestion)
    assert question.previous_forecasts is None
    ours = read_my_forecasts(question)
    assert ours is not None and len(ours.entries) == len(HISTORY)


def test_api_json_is_a_declared_field_and_survives_construction() -> None:
    """`api_json` is public and declared, which is the reason this reach is defensible.

    Worth pinning explicitly because the acceptance criterion is about *private* method
    dependencies and this one reads as if it might be. It is not: it is a `Field` on the
    base model, and the SDK stores the whole post in it.
    """
    _requires("api_json" in MetaculusQuestion.model_fields, "MetaculusQuestion.api_json")
    question = DataOrganizer.get_question_from_post_json(
        _binary_post_with_history(HISTORY, aggregations=True)
    )
    assert isinstance(question.api_json, dict)
    assert question.api_json["question"]["my_forecasts"]["history"] == HISTORY


def test_removing_api_json_makes_the_reader_answer_unreadable() -> None:
    """The mutation: without the field, every refetch is `unreadable`, forever and silently.

    That is the worst outcome `submission_live` names -- *"an honest post recorded as
    uncertain forever"* -- and it arrives with no exception anywhere, which is why the
    guard above has to be a test rather than a `getattr` default.
    """

    class _WithoutApiJson:
        id_of_question = 91001
        id_of_post = 90001

    assert read_my_forecasts(_WithoutApiJson()) is None
    with pytest.raises(AssertionError):
        _requires("api_json" in {"id_of_question"}, "MetaculusQuestion.api_json")


def test_the_identity_fields_the_refetch_compares_are_declared() -> None:
    """`id_of_question`/`id_of_post`, read to prove a refetch describes the right question.

    If either stopped existing, `_observe_with_detail`'s identity check would compare
    `None` against an int, treat every refetch as describing a different question, and
    resolve to `unreadable` -- the same silent permanent uncertainty as above.
    """
    for name in ("id_of_question", "id_of_post"):
        _requires(name in MetaculusQuestion.model_fields, f"MetaculusQuestion.{name}")


def test_the_question_type_attribute_exists_on_the_real_classes() -> None:
    """`cli.py` prints `question_type` off SDK questions; a rename would print a class name.

    Cosmetic on its own -- the fallback is `type(q).__name__` -- but it is the same reach
    and the table above is only honest if it covers the cosmetic ones too.
    """
    for question in (
        DataOrganizer.get_question_from_post_json(
            json.loads((API_POSTS / f"{name}_post.json").read_text(encoding="utf-8"))
        )
        for name in ("binary", "numeric", "multiple_choice")
    ):
        _requires(
            isinstance(getattr(question, "question_type", None), str),
            f"a string question_type on {type(question).__name__}",
        )


def test_the_open_state_is_the_string_the_refusal_compares() -> None:
    """`_require_open` refuses anything whose `state.value` is not exactly `"open"`.

    So two things have to stay true, and neither is documented anywhere but in the SDK's
    source: `QuestionState` is a value-carrying enum, and the open member's value is that
    lowercase word. If the enum were re-spelled, the pre-post refusal would stop refusing
    closed questions -- and the post would then be rejected by the platform, spending an
    idempotency key to learn what this check exists to know for free.
    """
    _requires(hasattr(QuestionState, "OPEN"), "QuestionState.OPEN")
    assert QuestionState.OPEN.value == "open"
    # The other members matter too: each is a state the refusal must catch.
    assert {member.value for member in QuestionState} == {
        "upcoming",
        "open",
        "resolved",
        "closed",
    }


def test_the_rotating_tournament_alias_still_reads_minibench() -> None:
    """`MetaculusClient.CURRENT_MINIBENCH_ID`, which `metaculus/fetch.py` treats as rotating.

    `docs/backlog/verified-facts.csv` records the value as `minibench`, checked against
    GitHub on 2026-07-09. This checks the installed package instead, which is the thing
    actually running. A rotation is not a bug -- `resolve_tournament_id` handles the
    disagreement deliberately, per D31 -- but it must not be something only a log line
    mentions.
    """
    _requires(hasattr(MetaculusClient, "CURRENT_MINIBENCH_ID"), "CURRENT_MINIBENCH_ID")
    assert MetaculusClient.CURRENT_MINIBENCH_ID == "minibench"


# ── httpx / httpcore ─────────────────────────────────────────────────────────


def test_the_connection_pool_already_has_the_field_we_set() -> None:
    """`research/transport.py` writes `_pool._retries`; nothing checked httpcore reads it.

    The existing coverage (`test_asknews.py`, `test_exa.py`) asserts `transport._pool.
    _retries == 7`, which proves *our write landed* -- it would pass just as happily
    against a field httpcore renamed and no longer consults, because we would then be
    creating the attribute rather than overwriting it.

    So the guard is that the attribute is **already there** on a freshly built pool, before
    anyone writes to it. Creating a new one is the silent failure: zero retries, no error,
    and a metered provider that stops retrying connection failures.
    """
    client = httpx.Client()
    try:
        pool = getattr(client._transport, "_pool", None)
        _requires(pool is not None, "httpx.Client's default transport exposing _pool")
        assert "_retries" in vars(pool), (
            "httpcore's connection pool no longer stores _retries, so apply_connection_"
            "retries would create a field nothing reads instead of setting one that works"
        )
        apply_connection_retries(client, 7)
        assert pool._retries == 7
    finally:
        client.close()


def test_a_client_with_no_pool_is_still_a_no_op() -> None:
    """The documented tolerance, kept honest: a custom transport has no `_pool`."""
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    try:
        apply_connection_retries(client, 7)  # must not raise
    finally:
        client.close()


# ── asknews ──────────────────────────────────────────────────────────────────


def test_the_asknews_author_still_carries_a_name() -> None:
    """`research/asknews.py` reads `author.name` and deliberately reads nothing else.

    The neighbouring field is `email`, personal data that must never reach the ledger, so
    this reach is narrow on purpose. A rename would silently drop every byline; a
    *reshuffle* that put an address in `name` would be worse, which is why the field the
    module refuses is pinned here alongside the one it takes.
    """
    from asknews_sdk.dto.base import Author

    fields = Author.model_fields
    _requires("name" in fields, "asknews_sdk.dto.base.Author.name")
    assert "email" in fields, (
        "the field research/asknews.py deliberately does not read is gone; the narrowness "
        "of that read was a decision about personal data and needs re-taking"
    )
