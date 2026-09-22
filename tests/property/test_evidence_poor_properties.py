"""M1-349 properties: the empty-retrieval classification, and the marker's refusal message.

The classification property measures its own reach (the vacuous-property memory): it counts
the examples that land on "the primary FAILED and the fallback answered EMPTY", which is the
branch the item exists for, and fails if the strategy never produced one.
"""

from __future__ import annotations

import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.pipeline_live import any_provider_failed, evidence_poor_reason
from whiskeyjack_bot.research.orchestrate import ProviderRun
from whiskeyjack_bot.tournament_state import TournamentError, append


def _run(provider: str, failed: bool) -> ProviderRun:
    return ProviderRun(
        retrieval_run_id=f"run-{uuid.uuid4().hex}",
        provider=provider,
        documents_retained=0,
        provider_failed=failed,
        artifact_outcome="written",
        artifact_error=None,
        cost_usd=None,
        fallback_reasons=(),
    )


# An empty retrieval: the primary always ran; the fallback may not have (it can be
# refused, or not needed). Either may have failed.
_chains = st.tuples(st.booleans(), st.one_of(st.none(), st.booleans()))


def test_an_empty_retrieval_is_classified_by_the_owner_table() -> None:
    reached: Counter[str] = Counter()

    @given(chain=_chains, fallback_enabled=st.booleans(), final_attempt=st.booleans())
    @settings(max_examples=300, deadline=None)
    def check(chain: tuple[bool, bool | None], fallback_enabled: bool, final_attempt: bool) -> None:
        primary_failed, fallback_failed = chain
        runs = [_run("asknews", primary_failed)]
        if fallback_failed is not None:
            runs.append(_run("exa", fallback_failed))
        if primary_failed and fallback_failed is False:
            reached["primary failed, fallback empty"] += 1

        failed = any_provider_failed(runs)
        # The oracle, written from the criterion rather than from the code: an empty
        # result is an outage when ANY provider failed, whichever one answered last.
        assert failed == (primary_failed or bool(fallback_failed))
        reason = evidence_poor_reason(
            provider_failed=failed,
            evidence_poor_fallback=fallback_enabled,
            final_attempt=final_attempt,
        )
        if not fallback_enabled:
            expected = None
        elif not failed:
            expected = "no_documents"
        elif final_attempt:
            expected = "provider_failed_exhausted"
        else:
            expected = None
        assert reason == expected
        if expected is not None:
            reached[expected] += 1

    check()
    assert reached["primary failed, fallback empty"] > 0, "the item's own branch was never drawn"
    assert reached["no_documents"] > 0 and reached["provider_failed_exhausted"] > 0


@pytest.fixture(scope="module")
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("evidence-poor") / "ledger.sqlite3"
    initialize_ledger(path)
    return path


@given(planted=st.text(min_size=1, max_size=80).filter(lambda text: text != "a" * 64))
@settings(max_examples=100, deadline=None)
def test_a_mismatched_marker_is_refused_without_echoing_its_value(
    ledger: Path, planted: str
) -> None:
    conn = connect(ledger)
    try:
        record = SimpleNamespace(record_id=f"wj-{uuid.uuid4().hex}")
        append(
            conn,
            "evidence_gap",
            record.record_id,
            {"code": "evidence_poor", "forecast_sha256": planted},
        )
        with mock.patch.object(whiskeyjack_tournament, "record_sha256", lambda _record: "a" * 64):
            with pytest.raises(TournamentError) as raised:
                whiskeyjack_tournament.is_evidence_poor(conn, record)
        # Equality with a constant is the strongest no-leak statement there is: whatever
        # the planted value, nothing of it can be in the message.
        assert str(raised.value) == "evidence-poor marker does not match the forecast hash"
    finally:
        conn.close()
