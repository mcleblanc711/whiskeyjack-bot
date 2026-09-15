"""M4-801: classifying a Metaculus post payload into a resolution observation.

The classifier is pure, so it is tested against payloads directly. The committed post
fixtures are re-resolved by ``resolution_rows.post_payload``; the one real resolved payload
(`withheld_minibench_45321.json`, captured 2026-09-14 with this project's bot token) is the
masked shape Metaculus returns for a question the account did not predict on.

The last section drives the pinned SDK's own ``get_question_by_post_id`` with ``requests.get``
stubbed, so a resolved payload the SDK cannot parse would fail here rather than at the first
live poll.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import requests
from forecasting_tools.helpers.metaculus_client import MetaculusClient

from resolution_rows import FIXTURES, RESOLVED_VALUE, kind_payload, post_payload
from whiskeyjack_bot.resolution import (
    DEFINITIVE_KINDS,
    RESOLUTION_KINDS,
    SCORABLE_KINDS,
    ResolutionError,
    ResolutionObservation,
    canonical_json,
    classify_resolution,
    observation_from_snapshot,
)
from whiskeyjack_bot.resolution_ingest import ResolutionFetchError, sdk_post_fetcher

TYPES = ("binary", "multiple_choice", "numeric", "discrete")
KINDS = ("resolved", "annulled", "ambiguous", "withheld", "unresolved")
POST_ID = 45556
QUESTION_ID = 45747
# Low-entropy by convention (gitleaks scans every branch), and distinctive enough that a
# substring match cannot be a coincidence.
SENTINEL = "LEAKCANARY7"
REAL_WITHHELD = FIXTURES / "resolution" / "withheld_minibench_45321.json"


def _classify(post: object, question_type: str = "binary", question_id: int = QUESTION_ID) -> Any:
    return classify_resolution(post, question_id=question_id, question_type=question_type)


def _post(question_type: str = "binary", **changes: Any) -> dict[str, Any]:
    return post_payload(question_type, post_id=POST_ID, question_id=QUESTION_ID, **changes)


# ── the partition ────────────────────────────────────────────────────────────


def test_the_vocabularies_are_the_ones_the_migration_names() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "src/whiskeyjack_bot/migrations/014_resolution_ingestion.sql"
    ).read_text(encoding="utf-8")
    assert set(KINDS) == RESOLUTION_KINDS
    assert "NOT IN ('resolved', 'annulled', 'ambiguous', 'withheld', 'unresolved')" in migration
    assert SCORABLE_KINDS == {"resolved"}
    assert DEFINITIVE_KINDS == {"resolved", "annulled", "ambiguous"}


@pytest.mark.parametrize("question_type", TYPES)
@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_classifies_for_every_type(question_type: str, kind: str) -> None:
    post = kind_payload(question_type, kind, post_id=POST_ID, question_id=QUESTION_ID)
    observation = _classify(post, question_type)
    assert observation.kind == kind
    assert observation.scorable is (kind == "resolved")
    assert observation.definitive is (kind in ("resolved", "annulled", "ambiguous"))
    assert observation.outcome == (RESOLVED_VALUE[question_type] if kind == "resolved" else None)
    assert (observation.question_id, observation.post_id) == (QUESTION_ID, POST_ID)


@pytest.mark.parametrize("kind", ["annulled", "ambiguous"])
def test_a_cancellation_is_never_scorable_and_carries_no_outcome(kind: str) -> None:
    observation = _classify(_post(resolution=kind))
    assert observation.kind == kind
    assert not observation.scorable
    assert observation.outcome is None


@pytest.mark.parametrize("question_type", ["numeric", "discrete"])
@pytest.mark.parametrize("token", ["above_upper_bound", "below_lower_bound"])
def test_an_out_of_bounds_continuous_outcome_is_resolved(question_type: str, token: str) -> None:
    observation = _classify(_post(question_type, resolution=token), question_type)
    assert (observation.kind, observation.outcome, observation.scorable) == (
        "resolved",
        token,
        True,
    )


@pytest.mark.parametrize("question_type", ["binary", "multiple_choice"])
@pytest.mark.parametrize("token", ["above_upper_bound", "below_lower_bound"])
def test_an_out_of_bounds_token_is_refused_where_the_type_has_no_bounds(
    question_type: str, token: str
) -> None:
    with pytest.raises(ResolutionError):
        _classify(_post(question_type, resolution=token), question_type)


def test_the_real_masked_payload_is_withheld() -> None:
    """The shape every resolved question the bot did not predict on comes back in."""
    post = json.loads(REAL_WITHHELD.read_text(encoding="utf-8"))
    observation = classify_resolution(post, question_id=45510, question_type="binary")
    assert observation.kind == "withheld"
    assert observation.platform_status == "resolved"
    assert not observation.scorable and not observation.definitive
    assert observation.resolution_set_time == "2026-09-05T18:46:05.395332+00:00"


def test_a_question_that_is_not_resolved_classifies_as_unresolved() -> None:
    for status in ("open", "closed", "upcoming"):
        assert _classify(_post(status=status, resolution=None)).kind == "unresolved"


def test_a_group_post_is_resolved_by_subquestion_id() -> None:
    post = json.loads((FIXTURES / "group" / "minibench_group.json").read_text(encoding="utf-8"))
    members = post["group_of_questions"]["questions"]
    for member, resolution in zip(members, ("yes", "no", "annulled"), strict=True):
        member["status"] = "resolved"
        member["resolution"] = resolution
    kinds = {member["id"]: _classify(post, "binary", member["id"]) for member in members}
    assert [kinds[m["id"]].outcome for m in members] == ["yes", "no", None]
    assert [kinds[m["id"]].kind for m in members] == ["resolved", "resolved", "annulled"]


def test_the_sdk_group_unpacking_shape_still_resolves_by_id() -> None:
    """The SDK hands back the whole post with one subquestion copied under `question`."""
    post = json.loads((FIXTURES / "group" / "minibench_group.json").read_text(encoding="utf-8"))
    members = post["group_of_questions"]["questions"]
    for member in members:
        member["status"] = "resolved"
        member["resolution"] = "no"
    members[1]["resolution"] = "yes"
    unpacked = copy.deepcopy(post)
    unpacked["question"] = copy.deepcopy(members[0])  # the SDK's copy for the first member
    assert _classify(unpacked, "binary", members[1]["id"]).outcome == "yes"
    assert _classify(unpacked, "binary", members[0]["id"]).outcome == "no"


# ── dispatch on the literal, never isinstance ────────────────────────────────


def test_a_discrete_payload_does_not_resolve_a_numeric_record() -> None:
    """`DiscreteQuestion` subclasses `NumericQuestion`; the literal must still decide."""
    with pytest.raises(ResolutionError, match="type does not match"):
        _classify(_post("discrete"), "numeric")
    with pytest.raises(ResolutionError, match="type does not match"):
        _classify(_post("numeric"), "discrete")


def test_an_unsupported_record_type_is_refused() -> None:
    with pytest.raises(ResolutionError, match="not a supported question type"):
        _classify(_post(), "date")


# ── malformed and inconsistent shapes ────────────────────────────────────────


def _mutated(change: Any) -> dict[str, Any]:
    post = _post()
    change(post)
    return post


MALFORMED = {
    "not a dict": lambda: [],
    "no post id": lambda: _mutated(lambda p: p.pop("id")),
    "bool post id": lambda: _mutated(lambda p: p.__setitem__("id", True)),
    "no question": lambda: _mutated(lambda p: p.pop("question")),
    "question is a list": lambda: _mutated(lambda p: p.__setitem__("question", [])),
    "other question id": lambda: _mutated(lambda p: p["question"].__setitem__("id", 1)),
    "bool question id": lambda: _mutated(lambda p: p["question"].__setitem__("id", True)),
    "unknown status": lambda: _mutated(lambda p: p["question"].__setitem__("status", "gone")),
    "status not text": lambda: _mutated(lambda p: p["question"].__setitem__("status", 3)),
    "no resolution key": lambda: _mutated(lambda p: p["question"].pop("resolution")),
    "resolution is a number": lambda: _mutated(
        lambda p: p["question"].__setitem__("resolution", 1)
    ),
    "resolution is a bool": lambda: _mutated(
        lambda p: p["question"].__setitem__("resolution", True)
    ),
    "resolution is empty": lambda: _mutated(lambda p: p["question"].__setitem__("resolution", "")),
    "binary maybe": lambda: _mutated(lambda p: p["question"].__setitem__("resolution", "maybe")),
    "binary upper-case": lambda: _mutated(lambda p: p["question"].__setitem__("resolution", "YES")),
    "resolution on a closed question": lambda: _mutated(
        lambda p: p["question"].__setitem__("status", "closed")
    ),
    "naive timestamp": lambda: _mutated(
        lambda p: p["question"].__setitem__("actual_resolve_time", "2026-09-17T12:00:00")
    ),
    "garbage timestamp": lambda: _mutated(
        lambda p: p["question"].__setitem__("resolution_set_time", "yesterday")
    ),
    "timestamp not text": lambda: _mutated(
        lambda p: p["question"].__setitem__("resolution_set_time", 1726574400)
    ),
    "lone surrogate": lambda: _mutated(
        lambda p: p["question"].__setitem__("resolution", "yes\ud800")
    ),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_every_malformed_shape_arrives_as_a_resolution_error(name: str) -> None:
    with pytest.raises(ResolutionError):
        _classify(MALFORMED[name]())


@pytest.mark.parametrize(
    "value",
    [
        "nan",
        "NaN",
        "inf",
        "-infinity",
        "1e999",
        " 1.5",
        "1.5 ",
        "+1",
        "1_000",
        "0x10",
        "01",
        ".5",
        "5.",
    ],
)
@pytest.mark.parametrize("question_type", ["numeric", "discrete"])
def test_a_continuous_outcome_float_would_accept_is_still_refused(
    question_type: str, value: str
) -> None:
    """`float()` takes most of these; the SDK's `typed_resolution` would have too."""
    with pytest.raises(ResolutionError, match="finite decimal"):
        _classify(_post(question_type, resolution=value), question_type)


@pytest.mark.parametrize("value", ["0", "-0.0", "3.0", "77289125.94957079", "1e-05", "-12.5E+3"])
def test_the_decimals_metaculus_stores_are_accepted(value: str) -> None:
    assert _classify(_post("numeric", resolution=value), "numeric").outcome == value


def test_a_multiple_choice_label_must_be_one_the_question_has_had() -> None:
    with pytest.raises(ResolutionError, match="not one of the question's options"):
        _classify(_post("multiple_choice", resolution="Option Gamma"), "multiple_choice")
    # A label added after the question opened is judged against every label it ever had.
    post = _post("multiple_choice", resolution="Option Gamma")
    post["question"]["all_options_ever"] = ["Option Alpha", "Option Beta", "Option Gamma", "Other"]
    assert _classify(post, "multiple_choice").outcome == "Option Gamma"


def test_a_multiple_choice_payload_with_no_labels_is_refused() -> None:
    post = _post("multiple_choice")
    post["question"]["options"] = None
    post["question"].pop("all_options_ever", None)
    with pytest.raises(ResolutionError, match="no option labels"):
        _classify(post, "multiple_choice")


def test_a_group_listing_the_question_twice_is_refused() -> None:
    post = json.loads((FIXTURES / "group" / "minibench_group.json").read_text(encoding="utf-8"))
    members = post["group_of_questions"]["questions"]
    members.append(copy.deepcopy(members[0]))
    with pytest.raises(ResolutionError, match="more than once"):
        _classify(post, "binary", members[0]["id"])


def test_no_refusal_echoes_the_payload() -> None:
    """Every message names a rule or field; none reprints what the platform sent."""
    planted = {
        "label": _post("multiple_choice", resolution=SENTINEL),
        "binary": _post(resolution=SENTINEL),
        "status": _mutated(lambda p: p["question"].__setitem__("status", SENTINEL)),
        "timestamp": _mutated(lambda p: p["question"].__setitem__("actual_resolve_time", SENTINEL)),
        "type": _mutated(lambda p: p["question"].__setitem__("type", SENTINEL)),
    }
    for name, post in planted.items():
        question_type = "multiple_choice" if name == "label" else "binary"
        # Vacuity guard: the sentinel really is in the payload the classifier reads.
        assert SENTINEL in json.dumps(post), name
        with pytest.raises(ResolutionError) as caught:
            _classify(post, question_type)
        assert SENTINEL not in str(caught.value), name


# ── the persisted form ───────────────────────────────────────────────────────


def test_a_stored_snapshot_re_validates_to_the_same_observation_and_digest() -> None:
    for question_type in TYPES:
        for kind in KINDS:
            post = kind_payload(question_type, kind, post_id=POST_ID, question_id=QUESTION_ID)
            observation = _classify(post, question_type)
            replayed = observation_from_snapshot(observation.snapshot_json())
            assert replayed == observation
            assert replayed.observation_sha256 == observation.observation_sha256


def test_a_snapshot_whose_kind_does_not_follow_from_its_resolution_is_refused() -> None:
    observation = _classify(_post(resolution="annulled"))
    forged = json.loads(observation.snapshot_json())
    forged["kind"] = "resolved"
    forged["outcome"] = "yes"
    with pytest.raises(ResolutionError):
        observation_from_snapshot(canonical_json(forged))


def test_the_digest_separates_a_null_from_an_empty_optional_field() -> None:
    """The M1-331 lesson: a key over an optional field must keep None and "" apart."""
    base = _classify(_post()).model_dump()
    with_null = ResolutionObservation.model_validate({**base, "actual_resolve_time": None})
    assert with_null.observation_sha256 != _classify(_post()).observation_sha256
    with pytest.raises(Exception):
        ResolutionObservation.model_validate({**base, "actual_resolve_time": ""})


def test_canonical_json_refuses_what_has_no_json_form() -> None:
    for bad in (float("nan"), {1: object()}, {"a": float("inf")}):
        with pytest.raises(ResolutionError):
            canonical_json(bad)
    deep: Any = []
    for _ in range(100_000):
        deep = [deep]
    with pytest.raises(ResolutionError):
        canonical_json(deep)


def test_a_surrogate_in_the_source_is_stored_as_an_escape() -> None:
    text = canonical_json({"title": "bad \ud800 text"})
    assert text.isascii() and "\\ud800" in text


# ── the SDK fetch adapter, against the pinned SDK's own parse ─────────────────


class _Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status_code = status
        self.ok = status < 400
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()
        self.reason = "OK"

    def json(self) -> object:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


@pytest.fixture()
def client() -> MetaculusClient:
    built = MetaculusClient(token="fake-token-for-tests")
    built.sleep_time_between_requests_min = 0.0
    built.sleep_jitter_seconds = 0.0
    return built


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import forecasting_tools.util.misc as misc

    monkeypatch.setattr(misc.time, "sleep", lambda _seconds: None)


def _payloads() -> dict[str, tuple[dict[str, Any], int, str]]:
    cases: dict[str, tuple[dict[str, Any], int, str]] = {}
    for question_type in TYPES:
        for kind in KINDS:
            cases[f"{question_type}-{kind}"] = (
                kind_payload(question_type, kind, post_id=POST_ID, question_id=QUESTION_ID),
                QUESTION_ID,
                question_type,
            )
    cases["real-withheld"] = (
        json.loads(REAL_WITHHELD.read_text(encoding="utf-8")),
        45510,
        "binary",
    )
    group = json.loads((FIXTURES / "group" / "minibench_group.json").read_text(encoding="utf-8"))
    for member in group["group_of_questions"]["questions"]:
        member["status"] = "resolved"
        member["resolution"] = "yes"
    cases["group-resolved"] = (group, group["group_of_questions"]["questions"][1]["id"], "binary")
    return cases


@pytest.mark.parametrize("case", sorted(_payloads()))
def test_the_pinned_sdk_parses_every_resolution_shape_and_the_payload_survives(
    case: str, client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, question_id, question_type = _payloads()[case]
    calls: list[str] = []

    def fake_get(url: str, *args: Any, **kwargs: Any) -> _Response:
        calls.append(url)
        return _Response(payload)

    def refuse_post(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("resolution ingestion must never POST")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", refuse_post)
    fetched = sdk_post_fetcher(client)(payload["id"])
    assert len(calls) == 1 and calls[0].endswith(f"/posts/{payload['id']}/")
    observation = classify_resolution(fetched, question_id=question_id, question_type=question_type)
    assert observation == classify_resolution(
        payload, question_id=question_id, question_type=question_type
    )


def test_a_failed_fetch_is_a_fetch_error_that_withholds_the_sdk_text(
    client: MetaculusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get(url: str, *args: Any, **kwargs: Any) -> Any:
        raise requests.exceptions.ConnectionError(f"https://x.invalid/{SENTINEL}")

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ResolutionFetchError) as caught:
        sdk_post_fetcher(client)(POST_ID)
    assert SENTINEL not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
