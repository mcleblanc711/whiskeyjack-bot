"""Invariants of the lens's pure planning and locale-row building (M1-358).

- ``plan`` is total over validated questions, never exceeds its cap, names only registry
  locales with local publishers, and does not depend on the registry file's order.
- Every planned call asks only for bare hosts the locale lists as local -- never an external
  or path-prefixed entry -- which is the "locality is the publisher" rule in executable form.
- ``locale_rows`` is total over arbitrary (untrusted) provider bodies: it never raises, every
  row it returns validated, and every row describes a document it was given.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from hypothesis import given, strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.lens import LensCall, locale_rows, plan
from whiskeyjack_bot.research.locales import build_registry
from whiskeyjack_bot.research.model import validate_document

REPO = Path(__file__).resolve().parents[2]
_PAYLOAD = yaml.safe_load((REPO / "config" / "locales.yaml").read_text("utf-8"))
_PIECES = st.sampled_from(
    [
        "Iran", "Strait of Hormuz", "Oman", "UAE", "Russia", "Kyiv", "Taiwan", "Quebec",
        "Canada", "Israel", "Saudi", "US CPI", "the", "will", "close", "?",
    ]
)  # fmt: skip
_TITLES = st.lists(_PIECES, min_size=1, max_size=6).map(" ".join)


def _registry(order: list[int] | None = None) -> Any:
    payload = copy.deepcopy(_PAYLOAD)
    if order is not None:
        payload["locales"] = [payload["locales"][i] for i in order]
    return build_registry(payload, sha256="0" * 64)


@given(
    _TITLES,
    st.integers(1, 4),
    st.permutations(list(range(len(_PAYLOAD["locales"])))),
)
def test_plan_is_capped_local_and_independent_of_registry_order(
    title: str, cap: int, order: list[int]
) -> None:
    question = CanonicalBinaryQuestion(question_id=1, post_id=1, title=title)
    registry = _registry()
    calls = plan(question, registry, max_calls=cap)
    assert len(calls) <= cap
    assert calls == plan(question, _registry(order), max_calls=cap)
    for call in calls:
        entry = registry.get(call.locale_code)
        assert entry is not None and call.domains
        assert set(call.domains) <= set(entry.local_domains)
        assert all("/" not in domain for domain in call.domains)
        assert not set(call.domains) & set(entry.external_domains)
        assert call.query.strip()
    assert len({call.locale_code for call in calls}) == len(calls)


_SCALARS = st.one_of(st.none(), st.integers(), st.booleans(), HOSTILE_TEXT)
_ARTICLE = st.fixed_dictionaries(
    {},
    optional={
        "article_url": st.sampled_from(["https://timesofoman.com/a", "https://x.example/b"])
        | _SCALARS,
        "language": _SCALARS,
        "country": _SCALARS,
        "title": _SCALARS,
        "eng_title": _SCALARS,
        "crawl_date": _SCALARS | st.just("2026-10-15T09:00:00Z") | st.just("2026-10-15"),
        "reporting_voice": _SCALARS,
        "provocative": _SCALARS,
        "classification": _SCALARS | st.lists(HOSTILE_TEXT, max_size=2),
    },
)
_BODIES = st.lists(
    st.one_of(
        st.fixed_dictionaries({"as_dicts": st.lists(st.one_of(_ARTICLE, _SCALARS), max_size=4)}),
        st.fixed_dictionaries({"as_dicts": _SCALARS}),
        st.dictionaries(HOSTILE_TEXT, _SCALARS, max_size=2),
    ),
    max_size=3,
)
_NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
_DOCUMENTS = [
    validate_document(
        {
            "retrieval_run_id": "run-1",
            "original_url": url,
            "canonical_url": url,
            "title": "t",
            "retrieved_at_utc": _NOW,
            "source_type": "news",
            "provenance": "direct_api",
            "content_sha256": f"{index:064x}",
        }
    )
    for index, url in enumerate(["https://timesofoman.com/a", "https://x.example/b"])
]


@given(_BODIES, st.sampled_from(["asknews", "exa"]))
def test_locale_rows_never_raise_and_describe_only_given_documents(
    bodies: list[Any], provider: str
) -> None:
    entry = _registry().get("OM")
    assert entry is not None
    call = LensCall("OM", provider, "q", "question", ("timesofoman.com",))  # type: ignore[arg-type]
    rows = locale_rows(call, entry, _DOCUMENTS, bodies)
    keys = {(d.retrieval_run_id, d.canonical_url, d.content_sha256) for d in _DOCUMENTS}
    assert {row.key() for row in rows} <= keys
    assert len({row.key() for row in rows}) == len(rows)
    for row in rows:
        for text in (row.language, row.native_title, row.english_title):
            if text is not None:
                text.encode("utf-8")  # storable: never a lone surrogate
        assert row.publisher_origin == (
            "local" if row.canonical_url.startswith("https://timesofoman.com") else "unclassified"
        )
