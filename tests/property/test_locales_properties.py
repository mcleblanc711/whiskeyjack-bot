"""Invariants of the locale registry, detection and publisher origin (M1-356).

Four claims the module makes that a handful of examples cannot establish:

- **Validation is total and does not leak.** Over arbitrary payload shapes, the only
  exception is :class:`LocaleError`, and no message carries a planted value.
- **Detection is a function of the question and the registry's content, not the file's
  order.** Shuffling the locales in the registry changes nothing, and neither does a
  store-and-reload of the question.
- **Detection is total** over hostile question text.
- **Publisher origin is total, stays in its vocabulary, and does not depend on how a host is
  spelled** -- the root-dot, case and ``www.`` spellings ``canonical.host_identity`` folds.

Each property was checked against a mutant before it was trusted; ``docs/M1-NOTES.md``
lists the mutants and which property killed each.
"""

from __future__ import annotations

import copy
import json
from typing import Any, get_args

from hypothesis import given, strategies as st
from strategies import HOSTILE_TEXT, spelled

from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.locales import (
    LocaleError,
    PublisherOrigin,
    build_registry,
    detect,
    publisher_origin,
)

SECRET = "privateFAKE123456"
DIGEST = "0" * 64

_BASE: dict[str, Any] = {
    "schema_version": 1,
    "locales": [
        {
            "code": "IR",
            "triggers": ["Iran", "Iranian", "Strait of Hormuz", "Hormuz", "Gulf of Oman"],
            "languages": ["fa"],
            "cctlds": ["ir"],
            "local_domains": ["mehrnews.com", "tehrantimes.com"],
            "external_domains": ["iranintl.com", "bbc.com/persian"],
        },
        {
            "code": "OM",
            "triggers": ["Oman", "Omani", "Strait of Hormuz", "Hormuz", "Gulf of Oman"],
            "languages": ["ar"],
            "cctlds": ["om"],
            "local_domains": ["timesofoman.com"],
        },
        {"code": "CA", "triggers": ["Canada", "Ottawa"], "languages": ["en", "fr"]},
        {
            "code": "CA-QC",
            "parent": "CA",
            "triggers": ["Quebec", "Montreal"],
            "languages": ["fr"],
            "local_domains": ["ledevoir.com"],
        },
    ],
}
_FIELDS = [
    "code",
    "parent",
    "triggers",
    "languages",
    "cctlds",
    "local_domains",
    "external_domains",
    "native_terms",
]

# Values that are sometimes valid for some field and sometimes not, plus the planted secret
# in every container shape a field could hold it in.
_VALUES = st.one_of(
    HOSTILE_TEXT,
    st.just(SECRET),
    st.lists(st.one_of(HOSTILE_TEXT, st.just(SECRET)), max_size=3),
    st.dictionaries(st.one_of(HOSTILE_TEXT, st.just(SECRET)), HOSTILE_TEXT, max_size=2),
    st.integers(),
    st.none(),
    st.sampled_from(["IR", "fa", "ir", "mehrnews.com", "co.uk", "x.com/a b", ["Iran"]]),
)


@given(st.integers(0, 3), st.sampled_from(_FIELDS), _VALUES)
def test_validation_raises_only_its_own_error_and_never_echoes(
    index: int, field: str, value: Any
) -> None:
    payload = copy.deepcopy(_BASE)
    payload["locales"][index][field] = value
    try:
        build_registry(payload, sha256=DIGEST)
    except LocaleError as exc:
        assert SECRET not in str(exc)
        assert exc.__cause__ is None


@given(st.one_of(_VALUES, st.just(_BASE)))
def test_a_wrong_top_level_shape_raises_only_its_own_error(payload: Any) -> None:
    try:
        build_registry(payload, sha256=DIGEST)
    except LocaleError as exc:
        assert SECRET not in str(exc)


_PIECES = st.one_of(
    st.sampled_from(
        [
            "Iran",
            "IRAN's",
            "Iranic",
            "Strait of\nHormuz",
            "Gulf of Oman",
            "Omani",
            "Omaninvest",
            "QUEBEC",
            "Montreal",
            "Ottawa",
            "Canada",
            "the",
            "will",
            "?",
        ]
    ),
    HOSTILE_TEXT,
)
_TEXT = st.lists(_PIECES, min_size=1, max_size=8).map(" ".join)


def _question(title: str, parent: str | None, criteria: str | None) -> CanonicalBinaryQuestion:
    return CanonicalBinaryQuestion(
        question_id=1,
        post_id=1,
        title=title,
        group_parent_title=parent,
        resolution_criteria=criteria,
    )


@st.composite
def _questions(draw: st.DrawFn) -> CanonicalBinaryQuestion:
    # Built through the real model, so a title the model refuses is never generated as a
    # question: detect's contract is over validated questions plus its own type gate.
    while True:
        title = draw(_TEXT)
        if title.strip() and "\ud800" not in title:
            break
    parent = draw(st.none() | _TEXT)
    criteria = draw(st.none() | _TEXT)
    try:
        return _question(title, parent, criteria)
    except Exception:
        return _question("Iran Hormuz", parent=None, criteria=None)


@given(_questions(), st.permutations(range(4)))
def test_detection_does_not_depend_on_registry_order(
    question: CanonicalBinaryQuestion, order: list[int]
) -> None:
    payload = copy.deepcopy(_BASE)
    reordered = copy.deepcopy(_BASE)
    reordered["locales"] = [payload["locales"][i] for i in order]
    assert detect(question, build_registry(payload, sha256=DIGEST)) == detect(
        question, build_registry(reordered, sha256=DIGEST)
    )


@given(_questions())
def test_detection_survives_a_store_and_reload_of_the_question(
    question: CanonicalBinaryQuestion,
) -> None:
    registry = build_registry(_BASE, sha256=DIGEST)
    stored = json.dumps(question.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
    reloaded = CanonicalBinaryQuestion.model_validate(json.loads(stored))
    assert detect(question, registry) == detect(reloaded, registry)


@given(_questions())
def test_every_match_names_triggers_that_belong_to_its_locale(
    question: CanonicalBinaryQuestion,
) -> None:
    registry = build_registry(_BASE, sha256=DIGEST)
    found = detect(question, registry)
    assert len({match.code for match in found}) == len(found)
    for match in found:
        entry = registry.get(match.code)
        assert entry is not None and match.triggers
        assert set(match.triggers) <= set(entry.triggers)


_HOSTS = st.sampled_from(
    ["mehrnews.com", "en.mehrnews.com", "irna.ir", "iranintl.com", "bbc.com", "example.org"]
)
_ROOT_DOTS = st.sampled_from(["", ".", "。", "．", "｡"])
_WWW = st.sampled_from(["", "www.", "WWW."])
_PATHS = st.sampled_from(["/", "/persian/x", "/persian", "/news/1", "/persianfood"])


@given(_HOSTS, _WWW, _WWW, _ROOT_DOTS, _ROOT_DOTS, st.booleans(), _PATHS)
def test_origin_does_not_depend_on_how_the_host_is_spelled(
    host: str, www_a: str, www_b: str, dot_a: str, dot_b: str, upper: bool, path: str
) -> None:
    iran = build_registry(_BASE, sha256=DIGEST).get("IR")
    assert iran is not None
    spelled = (www_b + host).upper() if upper else www_b + host
    left = f"https://{www_a}{host}{dot_a}{path}"
    right = f"https://{spelled}{dot_b}{path}"
    assert publisher_origin(left, iran) == publisher_origin(right, iran)


# Authorities ``urlsplit`` itself raises on. Hostile text behind ``https://`` almost never
# spells one, so without this pool the guard in ``_host_and_path`` was unreachable and a
# mutant removing it survived the property.
_BROKEN_AUTHORITIES = st.sampled_from(
    ["https://[abc/x", "http://[::1", "https://[v1.x]/", "https://a]b/", "https://[::1]:99999/"]
)


@given(
    st.one_of(
        HOSTILE_TEXT,
        HOSTILE_TEXT.map("https://{}".format),
        _BROKEN_AUTHORITIES,
        st.integers(),
        st.none(),
    )
)
def test_origin_is_total_and_stays_in_its_vocabulary(url: Any) -> None:
    iran = build_registry(_BASE, sha256=DIGEST).get("IR")
    assert iran is not None
    assert publisher_origin(url, iran) in get_args(PublisherOrigin)


# --- Round 1, B1: surrogate-pair spellings ---------------------------------------------
#
# A letter can be written three ways that JSON replay does not keep apart: plain ASCII, a
# Mathematical Bold scalar (U+1D400 block, which NFKC folds to the plain letter), and that
# scalar's UTF-16 surrogate-pair spelling. ``json.dumps(ensure_ascii=True)`` escapes the last
# two identically and ``json.loads`` recombines the pair, so a stored question can read back
# with different characters from the one that was detected on. The properties above drew no
# such letters, which is why round 1 had to find this.


_SPELLED_CRITERIA = st.lists(
    st.sampled_from(["Iran", "Oman", "Strait of Hormuz", "Quebec", "the", "officials"]).flatmap(
        spelled
    ),
    min_size=1,
    max_size=4,
).map(" ".join)


@given(_SPELLED_CRITERIA)
def test_detection_survives_reload_of_surrogate_pair_spellings(criteria: str) -> None:
    registry = build_registry(_BASE, sha256=DIGEST)
    question = _question("Will the strait close?", None, criteria)
    stored = json.dumps(question.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
    reloaded = CanonicalBinaryQuestion.model_validate(json.loads(stored))
    assert detect(question, registry) == detect(reloaded, registry)
