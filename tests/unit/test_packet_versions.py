"""M1-357: a packet without locale rows still hashes under the pre-019 1.0.0 rule, byte for byte.

The digests below were computed by the 1.0.0 code on master `eeeb1d8`, before this item
touched ``research/packet.py``. They are literals on purpose: a parity test computed by the
code under test moves with that code, and a hash rule is the one thing that must not move
(``packet.py``: "if the rule must change it changes as a new versioned function alongside
this one, never as an edit to this one"). The same check was run over every forecast record
in a copy of the live ledger migrated to 019; see ``docs/M1-NOTES.md`` § M1-357.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from whiskeyjack_bot.research.model import validate_document, validate_run
from whiskeyjack_bot.research.packet import (
    PACKET_LOCALES_SCHEMA_VERSION,
    PACKET_SCHEMA_VERSION,
    ResearchPacket,
    build_packet,
    canonical_packet_json,
    packet_sha256,
)

WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

GOLDEN_1_0_0 = {
    "empty_runs_only": "4733978361b72aea83456ff2c0c748d33ce7e1ca4b62b2d72cde482a8dcf3e2e",
    "one_run_two_docs": "673a18dc3bc54f13054af5472980b52b07352ee2e3f37774693eedaa1b05e53e",
    "two_runs": "a93d3b944c8aec171310220ce1f654e42d7b42d9d4deeb0fef8c37ef7e14140d",
}


def _packets() -> dict[str, ResearchPacket]:
    run_a = validate_run(
        {
            "retrieval_run_id": "run-a",
            "question_id": 101,
            "provider": "asknews",
            "provider_config": {"strategy": "latest news", "hours_back": 720},
            "queries": ["iran strait of hormuz"],
            "started_at_utc": WHEN,
            "completed_at_utc": WHEN,
            "cost_usd": 0.025,
            "documents_dropped": 0,
            "duplicates_collapsed": 1,
            "raw_response_path": "research/101/run-a.json",
        }
    )
    run_b = validate_run(
        {
            "retrieval_run_id": "run-b",
            "question_id": 101,
            "provider": "exa",
            "queries": ["q1", "q2"],
            "started_at_utc": WHEN,
            "completed_at_utc": WHEN,
            "cost_usd": 0.007,
            "error_summary": None,
            "freshness_cutoff_utc": WHEN,
        }
    )

    def doc(run: str, index: int, **overrides: object) -> object:
        payload = {
            "retrieval_run_id": run,
            "original_url": f"https://www.mehrnews.com/news/{index}",
            "canonical_url": f"https://mehrnews.com/news/{index}",
            "title": f"Hormuz report {index}",
            "retrieved_at_utc": WHEN,
            "published_at_utc": WHEN,
            "source_type": "news",
            "provenance": "direct_api",
            "content_sha256": f"{index:064x}",
            "snippet": "تنگه هرمز",
            "publisher": "Mehr",
            "raw_artifact_path": "x/y.json",
            "document_id": f"doc-{run}-{index}",
        }
        payload.update(overrides)
        return validate_document(payload)

    return {
        "empty_runs_only": build_packet(101, [run_a], []),
        "one_run_two_docs": build_packet(101, [run_a], [doc("run-a", 1), doc("run-a", 2)]),
        "two_runs": build_packet(
            101, [run_b, run_a], [doc("run-b", 3, source_type="web"), doc("run-a", 1)]
        ),
    }


@pytest.mark.parametrize("name", sorted(GOLDEN_1_0_0))
def test_a_packet_without_locales_keeps_its_pre_019_digest(name: str) -> None:
    packet = _packets()[name]
    assert packet.locales == ()
    assert packet_sha256(packet) == GOLDEN_1_0_0[name]
    assert json.loads(canonical_packet_json(packet))["packet_schema_version"] == "1.0.0"


def test_the_two_versions_are_distinct_constants() -> None:
    assert (PACKET_SCHEMA_VERSION, PACKET_LOCALES_SCHEMA_VERSION) == ("1.0.0", "1.1.0")
