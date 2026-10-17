"""Properties of the shadow arm's one pure function, ``lens_run_ids`` (M1-359).

``lens_run_ids`` reads a ``local_lens_checkpoint`` payload back out of the journal -- a value
read back out of the ledger, so untrusted -- and picks the lens runs a shadow forecast may
read. The properties:

1. It never raises, on any JSON at all.
2. It equals an independent oracle: in order, distinct, exactly the calls that are a dict
   with a non-empty ``str`` run id, an exact-``int`` positive ``documents``, ``interrupted``
   exactly ``False`` and ``skipped`` exactly ``None``.
3. It is replay-stable through the persisted form (``json.dumps`` -> ``json.loads``), which is
   how the checkpoint reaches it.
4. The strategy reaches both branches -- runs kept and runs refused -- measured, not assumed
   (LESSONS.md lesson 5).

Filed off M1-359's round-1 review as the standing convention's missing pass.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.forecast.shadow import lens_run_ids

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | HOSTILE_TEXT,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=6,
)


_NEAR_MISSES: dict[str, list[Any]] = {
    "retrieval_run_id": ["", None, 7],
    "documents": [0, -1, True, 1.0, "1", None],
    "interrupted": [True, 0, None, "no"],
    "skipped": ["no_web_client", False, ""],
}


@st.composite
def _call(draw: st.DrawFn) -> Any:
    """A lens call entry: junk one time in ten; otherwise a well-formed, kept call, which half
    the time has exactly **one** field replaced by a near miss or deleted. One field at a time
    is the point: a near miss is only observable when everything else would have kept the run
    (the first draft varied every field at once, and a ``bool`` count and a missing
    ``skipped`` key both survived mutation)."""
    if draw(st.integers(0, 9)) == 0:
        return draw(_JSON)
    call: dict[str, Any] = {
        # Mostly unique, so a near miss is not hidden behind an earlier kept copy of its id;
        # a repeated id one time in five, so the de-duplication is still exercised.
        "retrieval_run_id": draw(
            st.sampled_from(["run-a", "run-b"])
            if draw(st.integers(0, 4)) == 0
            else (st.uuids().map(str) | HOSTILE_TEXT)
        ),
        "documents": draw(st.integers(1, 9)),
        "interrupted": False,
        "skipped": None,
    }
    if draw(st.integers(0, 2)) != 0:  # two in three
        field = draw(st.sampled_from(sorted(_NEAR_MISSES)))
        if draw(st.integers(0, 4)) == 0:
            del call[field]
        else:
            call[field] = draw(st.sampled_from(_NEAR_MISSES[field]))
    return call


@st.composite
def _checkpoints(draw: st.DrawFn) -> dict[str, Any]:
    """Eight in ten a non-empty list of calls; otherwise a dict of junk, or junk under
    ``calls``. Weighted by an integer draw, because ``st.one_of`` does not weight by
    repetition and the call-list branch is where every keep/refuse decision is made."""
    branch = draw(st.integers(0, 9))
    if branch < 8:
        return {"calls": draw(st.lists(_call(), min_size=1, max_size=6))}
    if branch == 8:
        return draw(st.dictionaries(st.text(max_size=6), _JSON, max_size=3))
    return {"calls": draw(_JSON)}


_CHECKPOINTS = _checkpoints()


def _oracle(checkpoint: dict[str, Any]) -> tuple[str, ...]:
    calls = checkpoint.get("calls")
    kept: list[str] = []
    if not isinstance(calls, list):
        return ()
    for call in calls:
        if not isinstance(call, dict):
            continue
        run = call.get("retrieval_run_id")
        documents = call.get("documents")
        if not (isinstance(run, str) and run != ""):
            continue
        if isinstance(documents, bool) or not isinstance(documents, int) or documents <= 0:
            continue
        if call.get("interrupted") is not False or "skipped" not in call:
            continue
        if call["skipped"] is not None or run in kept:
            continue
        kept.append(run)
    return tuple(kept)


@given(_CHECKPOINTS)
def test_lens_run_ids_never_raises_and_equals_the_oracle(checkpoint: dict[str, Any]) -> None:
    found = lens_run_ids(checkpoint)
    assert found == _oracle(checkpoint)
    assert len(set(found)) == len(found)


@given(_CHECKPOINTS)
def test_lens_run_ids_is_replay_stable_through_the_persisted_form(
    checkpoint: dict[str, Any],
) -> None:
    try:
        persisted = json.loads(json.dumps(checkpoint))
    except (ValueError, TypeError):
        return
    # Compared in the persisted form: a surrogate pair held as two code units reads back as one
    # character (M1-306), and the journal only ever hands this function the persisted form.
    assert list(lens_run_ids(persisted)) == json.loads(json.dumps(list(lens_run_ids(checkpoint))))


def test_the_checkpoint_strategy_reaches_both_branches() -> None:
    hits: Counter[str] = Counter()

    @settings(max_examples=400, database=None, derandomize=True)
    @given(_CHECKPOINTS)
    def draw(checkpoint: dict[str, Any]) -> None:
        found = lens_run_ids(checkpoint)
        calls = checkpoint.get("calls")
        total = len(calls) if isinstance(calls, list) else 0
        hits["kept"] += len(found)
        hits["refused"] += total - len(found)
        hits["some kept"] += bool(found)
        for call in calls if isinstance(calls, list) else []:
            if isinstance(call, dict):
                for field in _NEAR_MISSES:
                    if field not in call:
                        hits[f"{field} missing"] += 1
                    elif call[field] in _NEAR_MISSES[field] and type(call[field]) is type(
                        _NEAR_MISSES[field][0]
                    ):
                        hits[f"{field} near miss"] += 1
                if call.get("documents") is True:
                    hits["documents bool"] += 1

    draw()
    assert hits["kept"] >= 100 and hits["refused"] >= 100 and hits["some kept"] >= 50, hits
    # Each near miss is only observable one field at a time, so each must be drawn often.
    for field in _NEAR_MISSES:
        assert hits[f"{field} missing"] >= 5, (field, hits)
        assert hits[f"{field} near miss"] >= 15, (field, hits)
    assert hits["documents bool"] >= 5, hits
