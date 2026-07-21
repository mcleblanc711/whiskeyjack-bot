"""Expand a Metaculus group post into one question per subquestion (M1-202).

A group question arrives from the API as a single *post* carrying a
``group_of_questions`` block, not as a question type: ``group_of_questions`` is a
post-level tag and never appears in ``question_type``, so there is no
``GroupQuestion`` model in the pinned SDK and nothing downstream of
:mod:`whiskeyjack_bot.questions.normalize` ever sees a group as such. Expansion is
purely a fetch-time concern.

On the live path the SDK already expands groups: ``MetaculusClient`` does it inside
``get_all_open_questions_from_tournament`` when ``group_question_mode`` is
``"unpack_subquestions"`` (the committed default, see ``config.MetaculusConfig``),
so :func:`whiskeyjack_bot.questions.normalize.normalize_question` receives
subquestions already separated. :func:`unpack_group_post` exists because the SDK's
own expansion is reachable only through a private static method on a
network-bound client class; owning a public, offline seam is what lets a raw group
post be exercised as a fixture. ``test_groups.py`` pins our output against the
SDK's on the same fixture, so a semantic change on an SDK bump fails loudly rather
than silently diverging.

**The identity trap this module exists to make testable.** Expansion deep-copies the
*parent post* once per subquestion and swaps in that subquestion's block. Every
sibling therefore shares ``post_id`` and ``page_url`` (the URL is built from the post
id), and shares the parent's ``fine_print``/``description``/``resolution_criteria``,
which overwrite the subquestion's own. Only ``id_of_question`` distinguishes them.
Any identity keyed on the post would collapse a whole group to one record -- which is
why ``question_id`` is the canonical anchor, matching the ledger's
``UNIQUE (question_id, tournament_id, forecast_version)``. Batch-level enforcement of
that uniqueness lives in ``normalize.normalize_questions``.

Error hygiene matches the rest of the package: a malformed post raises
:class:`~whiskeyjack_bot.questions.normalize.NormalizationError` with a constant,
value-free message and ``from None``, so neither a raw ``KeyError``/``ValueError``
nor an SDK message interpolating post content can reach a caller.
"""

from __future__ import annotations

import copy
from typing import Any

from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import MetaculusQuestion

from whiskeyjack_bot.questions.normalize import NormalizationError

# Fields the parent group block overrides on every subquestion. The subquestion
# blocks carry only their own titles and options; the shared framing lives once on
# the parent, so an un-overridden subquestion would reach the forecaster without the
# resolution rules that actually govern it.
_PARENT_OVERRIDES = ("fine_print", "description", "resolution_criteria")


def is_group_post(post_json: dict[str, Any]) -> bool:
    """Whether a raw API post carries a group block and needs expanding."""
    return isinstance(post_json, dict) and isinstance(post_json.get("group_of_questions"), dict)


def unpack_group_post(post_json: dict[str, Any]) -> list[MetaculusQuestion]:
    """Expand one group post into one SDK question per subquestion.

    Mirrors the pinned SDK's expansion semantics: the parent's framing fields
    overwrite each subquestion's, and every resulting question carries the full
    sibling id list in ``question_ids_of_group``.

    Raises :class:`NormalizationError` if the post is not a well-formed group post.
    Deferred subquestion *types* are not rejected here -- a ``date`` subquestion
    expands fine and is refused later by ``normalize_question`` (D21), keeping type
    policy in one place.
    """
    if not is_group_post(post_json):
        raise NormalizationError("post is not a group question post (no group_of_questions block)")

    group_json: dict[str, Any] = post_json["group_of_questions"]
    question_jsons = group_json.get("questions")
    if not isinstance(question_jsons, list) or not question_jsons:
        raise NormalizationError("group question post has no subquestions")
    if not all(isinstance(q, dict) for q in question_jsons):
        raise NormalizationError("group question post has a malformed subquestion block")

    try:
        question_ids: list[int] = [q["id"] for q in question_jsons]
    except KeyError:
        # Constant message + from None: the KeyError text is safe, but the rule is
        # unconditional and the subquestion dict must not reach a traceback.
        raise NormalizationError("group subquestion is missing its question id") from None

    questions: list[MetaculusQuestion] = []
    for question_json in question_jsons:
        subquestion = copy.deepcopy(question_json)
        # Deviation from the SDK, which indexes these keys directly and raises
        # KeyError when a group block omits one. The tolerance is scoped to *absent*
        # keys: a key the parent carries explicitly as null still overwrites the
        # subquestion's own value with None, matching the SDK. That is intended --
        # an explicit null is the parent stating the field is empty for the whole
        # group, which is not the same as the parent not addressing it at all.
        for key in _PARENT_OVERRIDES:
            if key in group_json:
                subquestion[key] = group_json[key]

        subquestion_post = copy.deepcopy(post_json)
        subquestion_post["question"] = subquestion

        try:
            question = DataOrganizer.get_question_from_post_json(subquestion_post)
        except Exception:
            # Deliberately broad: the SDK signals a bad post with AssertionError,
            # KeyError, a ValueError whose text interpolates the offending type
            # string, or a pydantic ValidationError echoing input values. All of
            # them are unvetted content, and a caller handling only
            # NormalizationError would otherwise see a raw exception escape.
            raise NormalizationError(
                "group subquestion could not be parsed as a question "
                "(detail withheld: it can echo post contents)"
            ) from None

        question.question_ids_of_group = question_ids.copy()
        questions.append(question)

    return questions
