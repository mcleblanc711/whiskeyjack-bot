"""What a lens document adds over baseline research, and which lens documents are one report (M1-356).

The local-source lens (D53) is worth paying for only if it finds something baseline research
did not. This module measures that, and it measures it **conservatively in one direction on
purpose**: when in doubt, two documents are treated as one report. The failure it guards
against is the one the request names: "Multiple publications repeating one original report
do not constitute independent confirmation." The 2026-10-16 probe produced the textbook case
-- one AFP story about a tanker fire, returned in English, Arabic, Spanish, Portuguese and
French. Five rows, one report.

Two questions, answered separately:

- :func:`classify` -- for each lens document, **exactly one** class against the baseline:

  * ``same_url``: the baseline already holds this canonical URL.
  * ``same_content``: the baseline holds a document with this ``content_sha256``.
  * ``republication``: the baseline holds a near-duplicate of it (see
    :func:`near_duplicate`).
  * ``unassessed``: none of the above, and there is no English title to compare. This is
    every untranslated Exa document today. It is reported as its own class rather than
    folded into ``novel``, because "we could not tell" and "it is new" are different claims
    and the lens report must not inflate the second with the first.
  * ``novel``: none of the above, with an English title that matches nothing in the baseline.

  First match wins, in that order: the cheaper, more certain identity outranks the
  heuristic one.

- :func:`clusters` -- the lens documents grouped into reports. Near-duplicate lens documents
  are joined, transitively, and each cluster names one **representative**: the earliest
  published, with a total-order tiebreak. A cluster of five wire copies is one report however
  many languages it arrived in.

**Near-duplicate rule** (both constants are this module's and are pinned by tests):

- English-title token Jaccard ≥ :data:`TITLE_JACCARD`; or
- the same publisher, published within :data:`WIRE_WINDOW` of each other, with Jaccard
  ≥ :data:`WIRE_JACCARD`. This second rule exists because AskNews translates each language's
  copy of a wire story separately, and its translations differ in wording. On the probe's AFP
  copies the pairwise Jaccard was around 0.4, below any threshold safe to apply across
  publishers.

Claim-level novelty -- a new claim against a new publication of an old one -- is **not**
attempted. Titles cannot carry it, and pretending otherwise would put a guess in the ledger's
evidence statistics. That is M1-360.

Pure and deterministic: no I/O, results independent of input order (property-tested), and
:class:`OverlapError` is the only exception that leaves the module.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from whiskeyjack_bot.research.dedup import dedup_key
from whiskeyjack_bot.research.model import ResearchDocument

OverlapClass = Literal["same_url", "same_content", "republication", "novel", "unassessed"]

# Two titles from different publishers are one report at or above this overlap.
TITLE_JACCARD = 0.6
# Two titles from the same publisher, close in time, are one report at or above this overlap.
WIRE_JACCARD = 0.3
WIRE_WINDOW = timedelta(hours=12)

# Title words too common in news headlines to say two headlines are about one thing.
_STOP = frozenset(
    "the and for with from that this into over after amid says said will about than its are "
    "was has have not new".split()
)

Key = tuple[str, str, str]


class OverlapError(Exception):
    """A caller passed something this module cannot compare. Never echoes document text."""


@dataclass(frozen=True)
class Comparable:
    """The part of a document overlap is decided on.

    ``english_title`` is supplied by the caller rather than read off ``document.title``
    because only the caller knows which language a title is in: AskNews documents store
    AskNews's English translation in ``title``, while an Exa document's ``title`` is
    whatever the page said. Passing ``None`` is the honest answer for an untranslated
    document, and it makes that document ``unassessed`` rather than ``novel``.
    """

    key: Key
    canonical_url: str
    content_sha256: str
    publisher: str | None
    published_at_utc: datetime | None
    english_title: str | None


def comparable(document: ResearchDocument, *, english_title: str | None) -> Comparable:
    """Build a :class:`Comparable`; raises :class:`OverlapError`."""
    if not isinstance(document, ResearchDocument):
        raise OverlapError("document must be a ResearchDocument")
    if english_title is not None and type(english_title) is not str:
        raise OverlapError("english_title must be a string or None")
    return Comparable(
        key=dedup_key(document),
        canonical_url=document.canonical_url,
        content_sha256=document.content_sha256,
        publisher=document.publisher,
        published_at_utc=document.published_at_utc,
        english_title=english_title,
    )


def title_tokens(title: str | None) -> frozenset[str]:
    """The comparable words of a title: NFKC, case-folded, alphanumeric runs of 3+, no stop words."""
    if title is None:
        return frozenset()
    words = re.findall(r"\w+", unicodedata.normalize("NFKC", title).casefold())
    return frozenset(word for word in words if len(word) >= 3 and word not in _STOP)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _same_publisher(left: Comparable, right: Comparable) -> bool:
    if left.publisher is None or right.publisher is None:
        return False
    return (
        unicodedata.normalize("NFKC", left.publisher).casefold().strip()
        == unicodedata.normalize("NFKC", right.publisher).casefold().strip()
    )


def _within_wire_window(left: Comparable, right: Comparable) -> bool:
    if left.published_at_utc is None or right.published_at_utc is None:
        return False
    return abs(left.published_at_utc - right.published_at_utc) <= WIRE_WINDOW


def near_duplicate(left: Comparable, right: Comparable) -> bool:
    """True when two documents are one report under this module's rule. Symmetric."""
    if left.canonical_url == right.canonical_url or left.content_sha256 == right.content_sha256:
        return True
    score = _jaccard(title_tokens(left.english_title), title_tokens(right.english_title))
    if score >= TITLE_JACCARD:
        return True
    return (
        score >= WIRE_JACCARD and _same_publisher(left, right) and _within_wire_window(left, right)
    )


def _require(items: Iterable[Comparable], name: str) -> tuple[Comparable, ...]:
    try:
        result = tuple(items)
    except TypeError:
        raise OverlapError(f"{name} must be an iterable of Comparable") from None
    if any(not isinstance(item, Comparable) for item in result):
        raise OverlapError(f"{name} must contain only Comparable values")
    return result


def classify(
    baseline: Iterable[Comparable], lens: Iterable[Comparable]
) -> Mapping[Key, OverlapClass]:
    """One class for every lens document, keyed by its dedup key."""
    base = _require(baseline, "baseline")
    found = _require(lens, "lens")
    urls = {item.canonical_url for item in base}
    hashes = {item.content_sha256 for item in base}
    result: dict[Key, OverlapClass] = {}
    for item in found:
        if item.canonical_url in urls:
            verdict: OverlapClass = "same_url"
        elif item.content_sha256 in hashes:
            verdict = "same_content"
        elif any(near_duplicate(item, other) for other in base):
            verdict = "republication"
        elif not title_tokens(item.english_title):
            verdict = "unassessed"
        else:
            verdict = "novel"
        # Two lens documents can share a dedup key only across runs, which the key's run id
        # forbids, so a repeat here is a caller error rather than a tie to break.
        if item.key in result:
            raise OverlapError("lens documents must have distinct dedup keys")
        result[item.key] = verdict
    return result


def _representative_order(
    item: Comparable,
) -> tuple[bool, datetime | str, str, str, tuple[str, str, str]]:
    # Undated documents sort after dated ones; the empty string stands in for "no date" so
    # the tuple never compares a datetime with None.
    dated = item.published_at_utc is not None
    when: datetime | str = item.published_at_utc if item.published_at_utc is not None else ""
    return (not dated, when, item.canonical_url, item.content_sha256, item.key)


def clusters(lens: Iterable[Comparable]) -> tuple[tuple[Key, ...], ...]:
    """Lens documents grouped into reports, each representative first.

    Union-find over :func:`near_duplicate`, so grouping is transitive and does not depend on
    the order documents arrive in. Within a cluster, members are ordered by the
    representative order (earliest published, then URL, hash and key). Clusters are ordered
    by their representatives.
    """
    items = _require(lens, "lens")
    keys = [item.key for item in items]
    if len(set(keys)) != len(keys):
        raise OverlapError("lens documents must have distinct dedup keys")
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if near_duplicate(items[left], items[right]):
                root_left, root_right = find(left), find(right)
                if root_left != root_right:
                    parent[max(root_left, root_right)] = min(root_left, root_right)

    groups: dict[int, list[Comparable]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    ordered = [sorted(group, key=_representative_order) for group in groups.values()]
    ordered.sort(key=lambda group: _representative_order(group[0]))
    return tuple(tuple(item.key for item in group) for group in ordered)


def independent_reports(
    baseline: Sequence[Comparable], lens: Sequence[Comparable]
) -> tuple[tuple[Key, ...], ...]:
    """Clusters that are reports the baseline does not already hold.

    The lens report's headline number. A cluster is one report however many lens documents
    it holds, and it counts only when **at least one member is** ``novel`` **and no member
    is** ``same_url``, ``same_content`` or ``republication``. The second half is the
    conservative direction this module chooses everywhere: if any copy of a report is already
    in the baseline, the report is, whichever copy happens to be the representative. A
    cluster of nothing but ``unassessed`` documents is not counted here either; the lens
    report counts those separately, as what they are.
    """
    verdicts = classify(baseline, lens)
    held = {"same_url", "same_content", "republication"}
    return tuple(
        group
        for group in clusters(lens)
        if any(verdicts[key] == "novel" for key in group)
        and not any(verdicts[key] in held for key in group)
    )
