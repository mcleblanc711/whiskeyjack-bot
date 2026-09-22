"""Replay a real stored research packet and prove it is not re-purchased (M1-330).

M1-326 stopped a deterministic research verdict being bought a second time, and the tests
it shipped prove that over a *constructed* packet -- the synthetic question 91001, with
articles generated stale or future on demand. Its notes and review request described a test
driving Cup question 45452's stored packet; that test was described before it was written
and then not written, and ``docs/M1-NOTES.md`` records the correction. This module is it.

Two things are replayed here that the synthetic tests cannot reach.

**Real retrieved evidence.** ``tests/fixtures/research/cup_45452_asknews_run.json`` holds
provider response bodies AskNews really returned on 2026-09-08, for the question whose
re-purchase loop burned 33 billed calls over 17 retrievals in nine hours and exhausted the
month's quota. They are driven through the real adapter -- ``SearchResponse.model_validate``,
``asknews._to_document``, ``validate_document`` -- so what reaches the gate is a packet the
system parsed, not one a test built. ``scripts/regenerate_replay_fixture.py`` derived the
fixture and records which of the run's fourteen articles it kept and on what rule; this
module deliberately does not import it, so the assertion is a live parse of frozen bytes
rather than a generator checked against itself.

**Checkpoint expiry.** ``test_deterministic_refusal_is_recorded_and_never_re_purchased`` in
``test_tournament.py`` polls twice at wall clock, so both polls fall inside *both* 1800 s
windows -- ``research_checkpoint`` (``pipeline_live.py:396``) and ``question_started``
(``tournament.py:709``). Inside them a second poll is cheap whether or not M1-326's gate
exists, so that test cannot separate the gate from ordinary reuse. The acceptance criterion
says "after checkpoint expiry", and that is what ``ADVANCE`` below is for.

**Why the replayed verdict is ``stale_evidence``.** The live ledger holds exactly one
``research_failed`` row for this question, ``no_evidence`` at 2026-09-09T01:25:19Z -- the poll
where the AskNews call failed and Exa retained nothing. The seventeen refusals before it
predate M1-326 and raised straight past the recorder, leaving no row at all; they came from
the branch that demanded a document from the resolution authority the question names
(``results.cik.bg``), and **M1-327 demoted that branch** -- it records an ``evidence_gap``
now and refuses nothing. So this packet correctly does *not* refuse at the instant it was
retrieved, which :func:`test_the_same_stored_packet_is_forecastable_when_it_was_retrieved`
asserts, and the replay is pinned into the window where the question's own real evidence has
aged out while the question is still open. Both instants are real dates off this question's
own calendar: retrieved 2026-09-08, closes 2026-10-24T22:59Z, thirty-day freshness window.

Nothing here touches ``src/``, the network, or the submission path.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from asknews_sdk.dto.news import SearchResponse
from forecasting_tools import DataOrganizer

from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot import tournament_state
from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.questions.normalize import normalize_question
from whiskeyjack_bot.research.asknews import _to_document
from whiskeyjack_bot.research.model import validate_document, validate_run
from whiskeyjack_bot.research.packet import ResearchPacket, build_packet
from whiskeyjack_bot.research.quality import (
    missing_source_domains,
    quality_problem,
    relevant,
    usable,
)
from whiskeyjack_bot.tournament import run_once
from whiskeyjack_bot.tournament_state import enable, events, question_fingerprint, spending

from tests.unit.test_pipeline_live import config as base_config
from tests.unit.test_tournament import Model, Platform

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "research"

PROJECT = 32977
ACCOUNT = 42

_RUN = json.loads((FIXTURES / "cup_45452_asknews_run.json").read_text(encoding="utf-8"))
_PROVENANCE = _RUN["provenance"]

QUESTION_ID: int = _PROVENANCE["question_id"]
RETRIEVAL_RUN_ID: str = _PROVENANCE["retrieval_run_id"]

# Read off the fixture rather than transcribed, so a regenerated fixture cannot leave the
# instant this module pins pointing at a retrieval that no longer exists.
RETRIEVED_AT = datetime.fromisoformat(_PROVENANCE["captured_at_utc"])

# Inside the question's own real calendar: every stored article is dated on or before
# 2026-09-08, the freshness window is thirty days, and the question closes 2026-10-24T22:59Z.
# So here the evidence has aged out and the question is still open -- the exact state in
# which re-buying research to re-derive a refusal is pure waste.
AGED_OUT = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

# Past both 1800 s windows. 31 minutes, not 30: at exactly 1800 the comparisons are `<=`.
ADVANCE = timedelta(minutes=31)


def stored_bodies() -> list[dict[str, Any]]:
    """The provider response bodies, in the order AskNews returned them."""
    return [dict(body) for body in _RUN["raw_responses"]]


def stored_question() -> Any:
    raw = json.loads((FIXTURES / "cup_45452_post.json").read_text(encoding="utf-8"))
    return normalize_question(DataOrganizer.get_question_from_post_json(raw))


def stored_packet() -> ResearchPacket:
    """The packet the stored bodies really parse to, through the real adapter.

    Deduplicated on the ledger's own ``UNIQUE (retrieval_run_id, canonical_url,
    content_sha256)`` key, because that is what makes this the packet the ledger would
    hold rather than a list of everything the provider said.
    """
    documents = []
    seen: set[tuple[str, str, str]] = set()
    for body in stored_bodies():
        for article in SearchResponse.model_validate(body).as_dicts or []:
            document = validate_document(
                _to_document(article, retrieval_run_id=RETRIEVAL_RUN_ID, retrieved_at=RETRIEVED_AT)
            )
            key = (document.retrieval_run_id, document.canonical_url, document.content_sha256)
            if key in seen:
                continue
            seen.add(key)
            documents.append(document)
    run = validate_run(
        {
            "retrieval_run_id": RETRIEVAL_RUN_ID,
            "question_id": QUESTION_ID,
            "provider": "asknews",
            "started_at_utc": RETRIEVED_AT.isoformat(),
            "completed_at_utc": RETRIEVED_AT.isoformat(),
        }
    )
    return build_packet(QUESTION_ID, (run,), tuple(documents))


# --- the fakes: stored bytes in, counters out -----------------------------------------


def _refuse(**kwargs: Any) -> Any:
    """Armed on the second poll: reaching the provider at all is the defect."""
    raise AssertionError("a deterministic verdict was re-purchased")


class StoredNews:
    """The AskNews SDK, replaying the stored bodies in call order.

    ``calls`` is the **billed-call counter** this module asserts on, and the reason it is
    here rather than on ``research_runs`` is that run rows are wrong in both directions:
    ``started_at_utc`` is the *pinned* ``now``, so distinct timestamps undercount
    retrievals, and ``durable.py``'s within-window dedup serves a run without billing it,
    so the row count overcounts calls. Three of question 45452's twenty real run rows were
    served that way. A counter on the client counts exactly what would leave the machine.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self.news = self
        self.raw = raw
        self.calls = 0
        self.bodies = stored_bodies()

    def search_news(self, **kwargs: Any) -> SearchResponse:
        self.calls += 1
        # Each strategy replays its own stored body. Indexing rather than cycling: if the
        # adapter ever asks for more than the run recorded, that is a change in how many
        # calls one retrieval costs, and it should surface here rather than be papered over.
        index = (self.calls - 1) % len(self.bodies)
        return SearchResponse.model_validate(self.bodies[index])


class CountingPlatform(Platform):
    """``Platform``, plus a count of Metaculus refetches.

    A third witness: M1-326's skip sits ahead of ``MetaculusSubmissionGateway.observe`` as
    well as ahead of retrieval, so a poll that declines for free does not refetch either.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        super().__init__(raw)
        self.refetches = 0

    def get_question_by_post_id(self, post_id: int) -> Any:
        self.refetches += 1
        return super().get_question_by_post_id(post_id)


class Replay:
    """One pinned-clock tournament, over the stored question and the stored bodies."""

    def __init__(self, conn: Any, config: Any, clock: dict[str, datetime]) -> None:
        self.conn = conn
        self.config = config
        self.clock = clock
        raw = json.loads((FIXTURES / "cup_45452_post.json").read_text(encoding="utf-8"))
        raw["projects"]["default_project"]["id"] = PROJECT
        # `scheduled_close_time` is deliberately NOT rewritten. The whole point is that the
        # question's own recorded close date leaves a real window in which its own recorded
        # evidence has aged out.
        self.raw = raw
        self.platform = CountingPlatform(raw)
        self.news = StoredNews(raw)
        self.model = Model(raw)

    @property
    def now(self) -> datetime:
        return self.clock["now"]

    def advance(self, delta: timedelta) -> None:
        self.clock["now"] = self.clock["now"] + delta

    def poll(self) -> dict[str, Any]:
        return run_once(
            self.conn,
            self.config,
            client=self.platform,
            poster=self.platform,
            news_client=self.news,
            web_client=object(),
            forecaster=self.model,
        )

    def blocks(self) -> list[dict[str, Any]]:
        return events(self.conn, "question_blocked", f"{PROJECT}:{QUESTION_ID}")

    def reserved(self) -> int:
        """Microdollars held or settled against this activation -- the ledger's own witness."""
        actual, held = spending(self.conn, f"{ACCOUNT}:{PROJECT}")
        return actual + held

    def failure_rows(self) -> list[tuple[str, str]]:
        return [
            (row[0], row[1])
            for row in self.conn.execute(
                "SELECT event_type, detail_code FROM pipeline_failure_events ORDER BY rowid"
            )
        ]


@pytest.fixture
def days(tmp_path: Path) -> int:
    """The configured freshness window, read rather than transcribed.

    Hard-coding 30 here would let a change to ``retrieval.freshness_days_default``
    decouple these assertions from the window the pipeline actually applies, while both
    kept passing.
    """
    return int(base_config.__wrapped__(tmp_path).retrieval.freshness_days_default)


@pytest.fixture
def replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    clock = {"now": AGED_OUT}
    # Both bindings: `run_once` reads `utcnow` through the `tournament` module per question
    # (`tournament.py:601`), while `pipeline_live` and `Budget` read it off `tournament_state`.
    monkeypatch.setattr(tournament_state, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(whiskeyjack_tournament, "utcnow", lambda: clock["now"])

    config = base_config.__wrapped__(tmp_path)
    data = config.model_dump(mode="json")
    prompt = tmp_path / "forecaster.md"
    prompt.write_bytes(config.forecast.prompt_path.read_bytes())
    data["forecast"]["prompt_path"] = str(prompt)
    data["metaculus"]["tournament"].update(id=PROJECT, use_sdk_current_id=False)
    data["model"].update(
        name=Model.model, max_output_tokens=6000, timeout_seconds=120, temperature=None
    )
    data["submission"].update(
        enabled=True, dry_run=False, no_submit=False, post_private_reasoning_comment=True
    )
    data["retrieval"]["primary"]["retries"] = 0
    data["retrieval"]["fallback"]["retries"] = 0
    config = validate_config_data(data)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    case = Replay(conn, config, clock)
    enable(
        conn,
        config,
        account_id=ACCOUNT,
        project_id=PROJECT,
        starts=clock["now"] - timedelta(minutes=1),
        ends=clock["now"] + timedelta(days=1),
    )
    yield case
    conn.close()


# --- the fixture reaches the branch, and only because of freshness ---------------------


def test_the_same_stored_packet_is_forecastable_when_it_was_retrieved(days: int) -> None:
    """The non-vacuity control, and it is the load-bearing test in this module.

    This project's top recurring defect is a test whose inputs never reach the branch the
    assertion is about -- M1-327's replay property compared ``None == None`` over every one
    of its fixtures. A replay that refuses could refuse because the packet is empty, because
    nothing in it is relevant, or because the fixture is junk, and all three look identical
    from the refusal alone.

    So the *same* packet and the *same* question are judged at two instants. At the instant
    the evidence was really retrieved there is no refusal at all: real documents, really
    usable, really about this question. Only the clock differs at
    :func:`test_a_deterministic_verdict_over_real_evidence_is_never_re_purchased`.
    """
    packet = stored_packet()
    question = stored_question()

    assert quality_problem(packet, question, RETRIEVED_AT, days) is None

    usable_then = [d for d in packet.documents if usable(d, question, RETRIEVED_AT, days)]
    assert usable_then, "the stored evidence must be usable when it was stored"
    # Mixed, not uniformly fresh. Without a document that had already aged out at retrieval
    # time, the later refusal would be consistent with "any stale document refuses" -- which
    # is not the rule, and is not what M1-326 gates on.
    assert len(usable_then) < len(packet.documents), "the stored packet must be mixed"


def test_only_freshness_changes_between_the_two_instants(days: int) -> None:
    """What moved is the window, not the evidence.

    Relevance is asserted at *both* instants because it is the other way this refusal could
    be reached: a packet of documents about something else would also refuse, and would say
    nothing about M1-326. ``relevant`` does not read the clock, so this is a statement about
    the fixture rather than about the function -- which is the point, since the fixture is
    what a future regeneration could quietly change.
    """
    packet = stored_packet()
    question = stored_question()

    for now in (RETRIEVED_AT, AGED_OUT):
        assert all(relevant(d, question) for d in packet.documents), now.isoformat()

    problem = quality_problem(packet, question, AGED_OUT, days)
    assert problem is not None
    assert problem.code == "stale_evidence"
    assert not any(usable(d, question, AGED_OUT, days) for d in packet.documents)
    # `stale_evidence`, not `no_evidence`: the packet is not empty, which is what makes this
    # the aged-out branch rather than the nothing-retrieved one.
    assert packet.documents


def test_the_stored_bodies_parse_to_the_packet_the_run_recorded() -> None:
    """The fixture is evidence, so what it parses to is asserted, not assumed.

    **This is a statement about the stored FIXTURE, not about today's adapter.** The run it
    was captured from made two AskNews calls -- two strategies per query, as the adapter
    issued them before M1-352 dropped the historical pass -- and stored one article under
    both. Carrying that duplicate is deliberate: it is what keeps the ledger's
    ``UNIQUE (retrieval_run_id, canonical_url, content_sha256)`` collapse in the replay
    instead of leaving it to the real run. A recorded run is immutable, so this count must
    NOT follow the adapter.
    """
    packet = stored_packet()
    articles = sum(len(body["as_dicts"]) for body in stored_bodies())

    assert len(stored_bodies()) == 2, "the recorded run made two calls, as the adapter did then"
    assert articles == _PROVENANCE["articles_kept"]
    assert len(packet.documents) < articles, "the run's dedup collapse must survive reduction"
    assert {d.retrieval_run_id for d in packet.documents} == {RETRIEVAL_RUN_ID}
    assert stored_question().question_id == QUESTION_ID


def test_an_empty_packet_reads_as_no_evidence_not_stale_evidence(days: int) -> None:
    """The discriminator between the two codes the gate stores.

    ``stale_evidence`` and ``no_evidence`` are both deterministic and both block, so a test
    that only checked "it blocked" could not tell which branch ran. Pinning the empty case
    here is what lets the pipeline test's ``stale_evidence`` mean *documents exist and have
    aged out* rather than merely *nothing usable*.

    This is also where the live ledger's one real ``research_failed`` row belongs. That row
    -- ``no_evidence``, 2026-09-09T01:25:19Z -- was written by ``pipeline_live``'s
    ``packet is None`` branch, because the last run of that retrieval was Exa succeeding
    with zero documents; it never reached ``quality_problem`` at all. So it is asserted on
    the pure function rather than replayed through the pipeline, where it would exercise a
    different branch and say nothing about M1-326.
    """
    packet = stored_packet()
    empty = ResearchPacket(question_id=packet.question_id, runs=packet.runs, documents=())
    problem = quality_problem(empty, stored_question(), AGED_OUT, days)
    assert problem is not None
    assert problem.code == "no_evidence"


def test_the_committed_bodies_round_trip_through_the_pinned_sdk() -> None:
    """The bodies are still shaped like what the pinned SDK parses and emits.

    **This proves schema and serialization compatibility, and nothing about historical
    fidelity.** Round-tripping is a fixed point of *any* schema-valid body: edit an article
    summary to something AskNews never wrote and this assertion still passes. Round 1
    demonstrated exactly that, and an earlier version of this docstring claimed the
    assertion "rules out a hand-edited body" -- the same overstated-evidence defect that
    created this backlog item, so it is corrected here rather than quietly dropped.

    What it does establish is worth keeping. Reducing the articles and nulling
    ``authors[].email`` did not take the bodies outside the SDK's own schema, and the key
    comes back as ``null`` rather than disappearing -- so the committed form is exactly what
    the adapter parses at replay time, not a shape that happens to work because the SDK is
    lenient today.

    **Historical fidelity is established elsewhere, and cannot be established here**, because
    the source artifacts are deliberately not committed. It is
    ``scripts/regenerate_replay_fixture.py --check``, which re-derives both fixtures from the
    operator's stored Cup tree read-only and exits non-zero if either has drifted.
    """
    for body in stored_bodies():
        parsed = SearchResponse.model_validate(body)
        assert parsed.model_dump(mode="json", warnings=False) == body


def test_the_committed_fixtures_carry_no_personal_data() -> None:
    """A fixture is forever, and CI scans full history on every branch.

    The stored bodies arrived carrying working email addresses for the journalists who wrote
    the articles, and the stored post carried the operator's own forecast history and account
    id. None of it is needed to replay a verdict. This guards the next regeneration, not this
    one -- "restore the full packet" is a one-line change and this is what refuses it.
    """
    for path in sorted(FIXTURES.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), path.name

    run = json.loads((FIXTURES / "cup_45452_asknews_run.json").read_text(encoding="utf-8"))
    for body in run["raw_responses"]:
        for article in body["as_dicts"]:
            assert all(author.get("email") is None for author in article.get("authors") or [])

    post = json.loads((FIXTURES / "cup_45452_post.json").read_text(encoding="utf-8"))
    assert post["question"]["my_forecasts"] == {"history": None}
    assert not {"author_id", "author_username", "vote", "private_note"} & set(post)
    # The community prediction is never a forecaster input in v1; a fixture is not an input,
    # but shipping one invites it.
    assert "aggregations" not in post["question"]


def test_a_named_resolution_authority_is_recorded_and_not_refused(days: int) -> None:
    """M1-327 against the question the argument was settled on.

    This packet holds no document from ``results.cik.bg`` -- the Bulgarian election
    commission's results portal, which cannot have news coverage before the election it
    exists to report -- and never could: zero of the 196 documents the live run stored came
    from it. Before M1-327 that refused the forecast, seventeen times, at full price. It is
    recorded now, and here that is checked against the real evidence rather than a
    constructed stand-in.
    """
    packet = stored_packet()
    question = stored_question()
    assert missing_source_domains(packet, question, RETRIEVED_AT, days) == ("results.cik.bg",)
    # And it is not the reason for the later refusal: at the retrieval instant the gap is
    # already there and `quality_problem` still returns None.
    assert quality_problem(packet, question, RETRIEVED_AT, days) is None


# --- the replay: a deterministic verdict costs nothing the second time ----------------


def test_a_deterministic_verdict_over_real_evidence_is_never_re_purchased(replay: Any) -> None:
    """The acceptance criterion, over evidence the system really retrieved.

    The first poll buys the retrieval and records the refusal; the clock then moves past
    **both** 1800 s windows and the second poll must cost nothing.

    **What the assertions below are and are not.** ``news.calls``, the reserved budget and
    ``heartbeat["blocked"]`` are not three independent witnesses -- the skip ``continue``s
    before the Metaculus refetch, before the clients are built and before the budget context
    exists, so all three are readings of one branch and any mutant that breaks it breaks all
    three. They are kept because each says something different about *what did not happen*
    (no call left the client, no money was reserved, and the skip that fired was this one
    rather than the close-time or hold skip), not because they corroborate each other.

    What makes the zero mean anything is
    :func:`test_without_the_gate_the_same_second_poll_buys_the_same_evidence_again`, which
    establishes by execution that an unblocked second poll at exactly this clock, on exactly
    this fixture, does bill. Without it every number here is equally consistent with a warm
    cache.

    The counter is on the client because there is no ledger-side alternative:
    ``calls_attempted`` reaches ``RetrievalOutcome.unpriced_calls``, but the refusal branch
    at ``pipeline_live.py`` builds its ``QuestionOutcome`` without it, and ``tournament.py``
    discards the outcome. ``research_runs`` rows are wrong in both directions and are never
    asserted on -- see :class:`StoredNews`, and note that the Exa fallback opens a run per
    retrieval that it never completes.
    """
    first = replay.poll()
    assert first["heartbeat"]["failures"] == 1
    assert replay.news.calls == 1, "one retrieval is one AskNews call since M1-352"
    billed, refetched = replay.news.calls, replay.platform.refetches

    # 0.025 for the `latest news` strategy, in microdollars. It was 150_000 until M1-352
    # dropped the `news knowledge` pass (0.125). Exact, not `> 0`: it pins what was bought,
    # and that the Exa fallback -- which this question's named resolution authority does
    # trigger -- billed nothing.
    assert replay.reserved() == 25_000

    # `stale_evidence`, not merely "some deterministic code": it pins the branch to the one
    # an aged-out packet takes, which an empty packet could not reach.
    assert replay.failure_rows() == [("research_failed", "stale_evidence")]
    blocks = replay.blocks()
    assert len(blocks) == 1
    assert blocks[0]["reason"] == "deterministic_verdict"
    assert blocks[0]["detail_code"] == "stale_evidence"
    assert blocks[0]["fingerprint"] == question_fingerprint(stored_question())

    replay.advance(ADVANCE)
    # A trap, not a count: if the gate lets this through, the failure names the defect at the
    # call site instead of arriving as an off-by-two at the bottom of the test.
    replay.news.search_news = _refuse  # type: ignore[method-assign]
    second = replay.poll()

    assert replay.news.calls == billed, "a deterministic verdict must never be re-purchased"
    assert replay.reserved() == 25_000, "declining must reserve nothing"
    assert replay.platform.refetches == refetched, "the skip precedes the Metaculus refetch"
    assert replay.model.calls == replay.platform.posts == 0
    assert second["heartbeat"]["blocked"] == 1
    assert second["heartbeat"]["failures"] == 0, "declining is not failing"
    # Read, not re-derived: one audit row and one block, still.
    assert len(replay.blocks()) == 1
    assert replay.failure_rows() == [("research_failed", "stale_evidence")]


def test_without_the_gate_the_same_second_poll_buys_the_same_evidence_again(
    replay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control, and the test the one above is worthless without.

    Everything is held identical -- same fixture, same pinned clock, same 31-minute advance,
    same question and so the same fingerprint and the same provider request -- and one
    constant is emptied, so the verdict is no longer classified as deterministic and no
    block is recorded. The calls must come back.

    That establishes three things the replay test assumes: that 31 minutes really does clear
    both 1800 s windows for *this* fixture at *this* clock, that the counter can move at all,
    and that the second retrieval is genuinely re-billed rather than served from
    ``durable.py``'s journal. It is also the live behaviour of question 45452 reproduced --
    the loop that bought the same refusal seventeen times over nine hours.
    """
    monkeypatch.setattr(whiskeyjack_tournament, "DETERMINISTIC_FAILURE_CODES", frozenset())

    replay.poll()
    assert replay.news.calls == 1
    assert replay.reserved() == 25_000
    assert replay.blocks() == [], "the verdict is no longer classified as deterministic"

    replay.advance(ADVANCE)
    again = replay.poll()

    assert replay.news.calls == 2, (
        "with nothing blocking it, the advance must reach the provider again -- otherwise "
        "the zero in the replay test is a warm cache rather than the gate; two retrievals, "
        "one call each since M1-352"
    )
    assert replay.reserved() == 50_000, "the second retrieval must really be re-billed"
    assert again["heartbeat"]["failures"] == 1
    assert len(replay.failure_rows()) == 2, "the same verdict was re-derived at full price"


def test_a_second_poll_inside_the_window_proves_nothing_about_the_gate(
    replay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why this module exists, pinned as an executable fact.

    The same control as above, advanced only ten minutes. With the gate disabled the second
    poll still bills nothing -- ``question_started`` rewinds ``now`` and
    ``research_checkpoint`` replays the stored runs. So a two-poll test that does not cross
    the windows shows zero extra calls whether or not M1-326 exists, which is exactly what
    the shipped gate test does. Recorded here so it cannot be reintroduced by accident.
    """
    monkeypatch.setattr(whiskeyjack_tournament, "DETERMINISTIC_FAILURE_CODES", frozenset())

    replay.poll()
    assert replay.news.calls == 1

    replay.advance(timedelta(minutes=10))
    replay.poll()

    assert replay.news.calls == 1, (
        "inside the window a second poll is free with or without the gate"
    )
    assert replay.reserved() == 25_000


def test_an_edited_question_is_re_qualified_and_re_purchased(replay: Any) -> None:
    """The block is keyed on the fingerprint, and an edited question must re-qualify.

    Distinct from the positive control above, which holds the fingerprint fixed and empties
    the classification: this one holds the classification fixed and changes the fingerprint.
    Between them they pin both halves of the key. It is deliberately *not* used as the
    cache-defeat control, because editing the title also changes the query the adapter
    sends, so it would move ``durable``'s request digest as well as the clock and could not
    isolate either.
    """
    replay.poll()
    billed = replay.news.calls
    assert billed == 1
    assert len(replay.blocks()) == 1

    replay.raw["question"]["title"] += " (revised)"
    replay.advance(ADVANCE)
    third = replay.poll()

    assert replay.news.calls > billed, "an edited question must re-qualify"
    assert third["heartbeat"].get("blocked", 0) == 0
    # It refuses again -- the evidence is still aged out -- but under the new fingerprint,
    # rather than merging into the block recorded for the old one.
    assert len({block["fingerprint"] for block in replay.blocks()}) == 2
