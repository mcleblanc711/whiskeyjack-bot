"""M2-709: every artifact writer creates its file through one atomic helper.

Three modules write an artifact, and all three need the same race-sensitive sequence --
``mkstemp`` in the destination's own directory, write, ``flush``, ``fsync``, then
``os.link`` into place, because ``link`` fails with ``EEXIST`` rather than clobbering.
``submission_gateway`` spelled it a second time, privately, because it differs from the
other two in two places: an existing destination whose **bytes match** is a no-op there
rather than a collision, and its error type is ``GatewayError`` rather than
``ArtifactError``. Both are now parameters of
:func:`whiskeyjack_bot.artifacts.write_new_file`.

**The witness is two-part, and neither half is sufficient.** This is M1-608's lesson in a
different shape: a parity test with no witness outside the program cannot detect a change
to the shared thing.

- ``submission_gateway.write_new_file is artifacts.write_new_file`` proves the *name* is
  the shared one. It does **not** prove the writers call it -- a re-added private
  ``_write_or_confirm`` leaves that assertion passing untouched, because the import is
  still there.
- Patching ``write_new_file`` in each consumer's own namespace and asserting the spy ran
  proves the writer routes through a function of that name in that module. On its own it
  does not prove that name is the shared one.

Together they say what the criterion asks: the shared helper *is used by* each writer. Both
were run against a deliberately un-rewired gateway and observed to fail.

Failures are driven by real conditions -- a destination that already exists -- rather than
by monkeypatching, following ``test_research_persist.py``. The one exception is
:func:`test_the_link_arm_closes_the_race_the_pre_check_cannot`, which defeats the
``refuse`` pre-check on purpose: the pre-check is not what makes the write safe, and the
arm underneath it is unreachable in a single-process test without simulating the concurrent
writer it exists for.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from whiskeyjack_bot import artifacts as shared
from whiskeyjack_bot import submission_gateway
from whiskeyjack_bot.artifacts import ArtifactError, write_new_file
from whiskeyjack_bot.forecast import artifacts as forecast_artifacts
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.research import artifacts as research_artifacts
from whiskeyjack_bot.submission import submission_key
from whiskeyjack_bot.submission_gateway import (
    DryRunSubmissionGateway,
    GatewayError,
    SubmissionRequest,
    payload_sha256,
    write_dry_run_artifact,
    write_live_artifact,
)

WHEN = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
QUESTION = 42

# Low-entropy on purpose: a realistic-looking secret would trip the repository's gitleaks
# full-history scan on every unrelated PR (docs/LESSONS.md).
PLANTED = "privateFAKE123456"

PAYLOAD: dict[str, Any] = {"probability_yes": 0.37, "comment": PLANTED}


# --- the four writers, driven through one description each ---------------------------


@dataclass(frozen=True)
class Writer:
    """One artifact writer, and what the shared helper does on its behalf."""

    name: str
    module: ModuleType
    error: type[Exception]
    on_existing: str
    # (root, variant) -> the relative path written. The same variant must produce
    # byte-identical content at the same path; a different variant must produce
    # different content at the *same* path, which is what makes the EEXIST arm reachable.
    write: Callable[[Path, int], str]


def _write_research(root: Path, variant: int) -> str:
    relative = research_artifacts.write_raw_responses(
        root,
        retrieval_run_id="run-1",
        question_id=QUESTION,
        provider="asknews",
        raw_responses=[{"articles": [{"title": PLANTED}], "variant": variant}],
        written_at_utc=WHEN,
        retain=True,
    )
    assert relative is not None
    return relative


def _settings() -> ModelSettings:
    return ModelSettings(
        provider="openrouter",
        name="openrouter/test-model",
        temperature=0.1,
        max_output_tokens=2048,
        timeout_seconds=60.0,
        allowed_tries=2,
        prompt_version="1.1.0",
        prompt_sha256="b" * 64,
    )


def _write_forecast(root: Path, variant: int) -> str:
    generation = ForecastGeneration(
        forecast=None,
        settings=_settings(),
        sources=(),
        request=f"the rendered reasoning packet {PLANTED}",
        raw_responses=(json.dumps({"probability": 0.5, "variant": variant}),),
        invocations=1,
        repair_attempted=False,
        cost_usd=0.25,
        failure_code=None,
        failure_problems=(),
    )
    relative = forecast_artifacts.write_raw_model_output(
        root,
        attempt_id="attempt-1",
        question_id=QUESTION,
        generation=generation,
        written_at_utc=WHEN,
        retain=True,
        secret_env_var_names=(),
    )
    assert relative is not None
    return relative


def _key() -> str:
    return submission_key(
        tournament_id="minibench",
        question_id=QUESTION,
        forecast_version=1,
        request_payload_sha256=payload_sha256(PAYLOAD),
    )


def _receipt(variant: int) -> Any:
    """A dry-run receipt whose *envelope* varies without its path varying.

    The path is derived from the idempotency key, which is derived from the payload -- so
    changing the payload moves the destination and would not test anything. The clock is
    what varies instead, exactly as ``test_a_clock_change_alone_makes_the_artifact_disagree``
    does: the receipt is part of the envelope, so a second dry run at a different instant is
    a different body at the same content-derived path.
    """
    at = WHEN + timedelta(seconds=variant)
    gateway = DryRunSubmissionGateway(artifact_root=None, clock=lambda: at)
    return gateway.submit(
        SubmissionRequest(
            forecast_record_id="rec-1",
            question_id=QUESTION,
            idempotency_key=_key(),
            payload=PAYLOAD,
        )
    )


def _write_dry_run(root: Path, variant: int) -> str:
    return write_dry_run_artifact(
        root, receipt=_receipt(variant), question_id=QUESTION, payload=PAYLOAD
    )


def _write_live(root: Path, variant: int) -> str:
    return write_live_artifact(
        root,
        receipt=replace(_receipt(variant), mode="live"),
        question_id=QUESTION,
        payload=PAYLOAD,
    )


WRITERS = (
    Writer("research", research_artifacts, ArtifactError, "refuse", _write_research),
    Writer("forecast", forecast_artifacts, ArtifactError, "refuse", _write_forecast),
    Writer("dry_run", submission_gateway, GatewayError, "confirm_identical", _write_dry_run),
    Writer("live", submission_gateway, GatewayError, "confirm_identical", _write_live),
)

_IDS = [writer.name for writer in WRITERS]


# --- the wiring witness, both halves --------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [research_artifacts, forecast_artifacts, submission_gateway],
    ids=["research", "forecast", "submission_gateway"],
)
def test_every_writer_module_binds_the_shared_helper(module: ModuleType) -> None:
    """Half one: the name each writer calls is this one function object.

    Not sufficient on its own -- see the module docstring -- which is why
    `test_every_writer_calls_the_helper_it_bound` sits underneath it.
    """
    assert module.write_new_file is shared.write_new_file


@pytest.mark.parametrize("writer", WRITERS, ids=_IDS)
def test_every_writer_calls_the_helper_it_bound(
    writer: Writer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half two: the file is created by that call, with this writer's two parameters.

    The spy repeats the real signature's defaults, so `research` and `forecast` -- which
    pass neither `on_existing` nor `error` -- are measured on the values that actually
    take effect rather than on what they happened to spell.
    """
    calls: list[dict[str, Any]] = []

    def spy(
        destination: Path,
        payload: bytes,
        *,
        what: str,
        on_existing: str = "refuse",
        error: Callable[[str], Exception] = ArtifactError,
    ) -> None:
        calls.append(
            {
                "destination": destination,
                "payload": payload,
                "what": what,
                "on_existing": on_existing,
                "error": error,
            }
        )

    monkeypatch.setattr(writer.module, "write_new_file", spy)
    relative = writer.write(tmp_path, 0)

    assert len(calls) == 1, "the writer created its file some other way"
    call = calls[0]
    assert call["destination"] == tmp_path / relative
    assert call["on_existing"] == writer.on_existing
    assert call["error"] is writer.error
    # `what` is the caller's own literal and is what the shared messages name.
    assert isinstance(call["what"], str) and call["what"].strip()
    # And nothing reached the disk, because the only thing that writes is the spy.
    assert not (tmp_path / relative).exists()


# --- the EEXIST path of every writer, through the shared code -------------------------


@pytest.mark.parametrize("writer", WRITERS, ids=_IDS)
def test_a_second_write_of_different_content_is_refused_by_every_writer(
    writer: Writer, tmp_path: Path
) -> None:
    """The rule both policies share: a disagreement at one path is never overwritten."""
    relative = writer.write(tmp_path, 0)
    original = (tmp_path / relative).read_bytes()

    with pytest.raises(writer.error) as excinfo:
        writer.write(tmp_path, 1)

    message = str(excinfo.value)
    assert "never overwritten" in message
    # Paths are rendered (the settled M1-401 carve-out); content never is.
    assert str(tmp_path / relative) in message
    assert PLANTED not in message
    assert (tmp_path / relative).read_bytes() == original


@pytest.mark.parametrize("writer", WRITERS, ids=_IDS)
def test_the_policies_disagree_about_identical_content_and_that_is_the_point(
    writer: Writer, tmp_path: Path
) -> None:
    """The one deliberate difference, asserted as a difference rather than assumed.

    A retrieval or model-output artifact records that a paid call happened, so a second
    file at the same path is a collision whatever it contains. A submission artifact's path
    is derived from the payload, so identical bytes mean the identical dry run was
    performed before -- and refusing that would make the one mode whose entire purpose is
    repeatability un-repeatable.
    """
    relative = writer.write(tmp_path, 0)
    original = (tmp_path / relative).read_bytes()

    if writer.on_existing == "refuse":
        with pytest.raises(writer.error, match="never overwritten"):
            writer.write(tmp_path, 0)
    else:
        assert writer.write(tmp_path, 0) == relative

    assert (tmp_path / relative).read_bytes() == original
    # Either way the temp file is gone: the artifact is the only entry in its directory.
    assert [p.name for p in (tmp_path / relative).parent.iterdir()] == [Path(relative).name]


def test_the_link_arm_closes_the_race_the_pre_check_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive `os.link`'s own EEXIST under both policies, with the pre-check defeated.

    The `refuse` pre-check reports the common case with a better message and is *not* what
    makes the write safe; the arm underneath it is what survives a concurrent writer
    creating the destination after the check. A single-process test cannot reach it without
    simulating that writer, which is what patching `Path.exists` does here -- a reachable
    condition (two commands, one path), not a hostile one.
    """
    monkeypatch.setattr(Path, "exists", lambda self: False)

    refused = tmp_path / "refused" / "a.json"
    refused.parent.mkdir(parents=True)
    refused.write_bytes(b"first")
    with pytest.raises(ArtifactError, match="never overwritten"):
        write_new_file(refused, b"second", what="retrieval artifact")
    assert refused.read_bytes() == b"first"

    confirmed = tmp_path / "confirmed" / "a.json"
    confirmed.parent.mkdir(parents=True)
    confirmed.write_bytes(b"first")
    # Identical bytes: the link fails, the comparison agrees, and nothing is written.
    write_new_file(
        confirmed,
        b"first",
        what="submission artifact",
        on_existing="confirm_identical",
        error=GatewayError,
    )
    assert confirmed.read_bytes() == b"first"
    with pytest.raises(GatewayError, match="never overwritten"):
        write_new_file(
            confirmed,
            b"second",
            what="submission artifact",
            on_existing="confirm_identical",
            error=GatewayError,
        )
    assert confirmed.read_bytes() == b"first"
    # No temp file survives either arm.
    for parent in (refused.parent, confirmed.parent):
        assert [p.name for p in parent.iterdir()] == ["a.json"]


# --- the shared helper's own parameters -----------------------------------------------


@pytest.mark.parametrize("policy", [None, "clobber", "REFUSE", "", 0, True])
def test_an_unrecognized_policy_is_refused_before_any_io(policy: Any, tmp_path: Path) -> None:
    """An unrecognized policy must not fall through into whichever branch is last.

    Refused with the *caller's* error type, and before the destination's directory is
    created -- which is what makes "before any I/O" an assertion rather than a claim.
    """
    destination = tmp_path / "never" / "made" / "a.json"
    with pytest.raises(GatewayError, match="on_existing must be one of"):
        write_new_file(
            destination,
            b"body",
            what="submission artifact",
            on_existing=policy,
            error=GatewayError,
        )
    assert not destination.parent.exists()


def test_every_failure_arm_raises_the_error_the_caller_supplied(tmp_path: Path) -> None:
    """`GatewayError` is not an `ArtifactError` and never becomes one, and vice versa.

    The two classes are unrelated -- `GatewayError` subclasses `submission.SubmissionError`
    so the submission seam's callers catch it, `ArtifactError` is a bare `Exception` -- so
    the shared helper cannot pick one. Each arm is driven with a real condition.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    for error in (ArtifactError, GatewayError):
        # mkdir onto a plain file
        with pytest.raises(error) as directory:
            write_new_file(blocker / "sub" / "a.json", b"body", what="artifact", error=error)
        assert str(blocker / "sub") in str(directory.value)

        # the pre-check on an existing destination
        existing = tmp_path / f"{error.__name__}.json"
        existing.write_bytes(b"first")
        with pytest.raises(error, match="never overwritten"):
            write_new_file(existing, b"second", what="artifact", error=error)

        # the confirm-identical comparison
        with pytest.raises(error, match="never overwritten"):
            write_new_file(
                existing,
                b"second",
                what="artifact",
                on_existing="confirm_identical",
                error=error,
            )

    assert not issubclass(GatewayError, ArtifactError)
    assert not issubclass(ArtifactError, GatewayError)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN DEFECT, filed as M1-322 and pre-existing on master in all three writers: a "
        "lone surrogate in artifact_root makes Path.mkdir raise a raw UnicodeEncodeError "
        "-- a ValueError, not an OSError -- so it escapes `except OSError` and reaches the "
        "caller as something other than the module's own error type, which is a review "
        "finding in this project. It is the writer-side twin of the reader-side defect "
        "M1-314 closed, and the fix is M1-314's: `except ValueError` raising the caller's "
        "error with the path withheld, since interpolating it is itself the failing "
        "operation. Not fixed here -- changing what a merged, reviewed writer does with a "
        "path is the behaviour-change-to-merged-code this project files a row for, the "
        "same convention M2-709 itself was filed under. Strict, so the day M1-322 lands "
        "this test turns red and gets deleted rather than quietly passing."
    ),
)
def test_a_lone_surrogate_in_the_artifact_root_arrives_as_this_modules_error(
    tmp_path: Path,
) -> None:
    """Not a hostile operator: `artifact_root` is operator configuration, and this is the
    ordinary local-I/O failure class the threat boundary keeps in scope. `\\udcc3` would
    round-trip -- it is a surrogateescape for a real byte -- so the case is `\\ud800`,
    which has no byte behind it and cannot be encoded for the syscall at all."""
    root = tmp_path / "ro\ud800ot"
    with pytest.raises(ArtifactError):
        write_new_file(root / "a.json", b"body", what="retrieval artifact")
