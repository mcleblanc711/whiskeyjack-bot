"""Canonicalize a question's persisted form for hashing, never for storage (M1-331).

M1-326's gate skips a question whose recorded research verdict is deterministic, keyed on
``tournament_state.digest(question.model_dump(mode="json"))``. That ``digest`` sorts dict
*keys* only (``json.dumps(..., sort_keys=True)``); it does nothing to list *elements*. Every
list-valued field reachable from :class:`~whiskeyjack_bot.questions.model.CanonicalQuestion`'s
persisted form -- ``tournament_slugs``, ``source_categories``, ``question_ids_of_group`` on the
shared base, and ``options`` on the multiple-choice leaf -- is a membership set carried through
from the pinned SDK with no sort applied anywhere in :mod:`whiskeyjack_bot.questions.normalize`,
so its order is whatever one API response happened to return. If two polls of the same question
return one of these lists in a different order, the digest changes, M1-326's block silently
stops matching, and the question is re-researched at full price -- the exact failure M1-326
exists to prevent, reintroduced through the key rather than the gate.

Every field here is classified **unordered**, each on its own evidence rather than by category:

* ``tournament_slugs`` / ``source_categories`` -- already treated as order-insensitive platform
  metadata elsewhere in this codebase: ``submission_policy.py``'s live-question equality check
  excludes both, calling them "platform metadata unrelated to the resolution contract."
  ``source_categories`` sorts on ``(id, name, slug)``, not ``id`` alone (M1-331 round-1 review):
  ``CanonicalQuestion``'s schema does not require ``id`` to be unique within the list, and a bare
  ``id`` key makes ``sorted``'s stability leak the *original* relative order back in for any tie
  -- two categories sharing one ``id`` but differing in ``name``/``slug`` would then still permute
  the fingerprint. The full tuple is unique whenever the categories themselves differ in any
  field; two categories that agree on all three are equal in every sense this function cares
  about, so which position either ends up in is genuinely irrelevant.
* ``question_ids_of_group`` -- built in raw API-payload order (``questions/groups.py``) and never
  indexed against anything; it names group-sibling membership, not a sequence.
* ``options`` -- verified by reading ``forecast/multiple_choice.py``, whose own docstring states
  order is irrelevant to every rule there ("the response may answer in any order"), backed by a
  named test. The forecast response and the submission payload both match an option by its
  *label*, never by position.

Canonicalizing here, ahead of the hash, rather than on the stored question itself, is what keeps
replayability intact: the persisted ``CanonicalQuestion`` -- what a forecast record and a
research packet were actually generated from -- is untouched. Only the bytes handed to
``tournament_state.digest`` change. Pure: no network, no wall-clock, and no mutation of the
``CanonicalQuestion`` instance -- ``model_dump`` already returns a fresh dict, so this returns a
second fresh dict built from it.
"""

from __future__ import annotations

from typing import Any

from whiskeyjack_bot.questions.model import CanonicalQuestion


def canonicalize_for_fingerprint(question: CanonicalQuestion) -> dict[str, Any]:
    """Return ``question``'s persisted form with every unordered list sorted.

    Every other field is passed through unchanged. Sorting an already-sorted list is a no-op,
    so this changes nothing for a question whose API-returned order already happens to be
    canonical -- see the property suite's fixed-point test and the item's live-ledger count for
    how often that already holds.
    """
    data = question.model_dump(mode="json")
    data["tournament_slugs"] = sorted(data["tournament_slugs"])
    data["source_categories"] = sorted(
        data["source_categories"],
        key=lambda category: (category["id"], category["name"], category["slug"] or ""),
    )
    question_ids_of_group = data.get("question_ids_of_group")
    if question_ids_of_group is not None:
        data["question_ids_of_group"] = sorted(question_ids_of_group)
    options = data.get("options")
    if options is not None:
        data["options"] = sorted(options)
    return data
