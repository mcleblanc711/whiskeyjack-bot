"""M2-709: the shared artifact writer's contract, fuzzed over both of its policies.

`whiskeyjack_bot/artifacts.py` had no property pass before this item, and M2-709 gives it
two new parameters -- the EEXIST policy and the exception to raise -- whose whole purpose is
that two callers disagree about them. A parameter that decides whether a file is
**overwritten** is worth fuzzing rather than sampling.

**The strategy draws the pre-existing state rather than hoping for it.** Random bytes never
collide, so a `st.binary()` draw for the existing file would make every assertion about the
identical-content arm vacuous -- the top recurring defect in this project's property tests.
`existing` is therefore drawn as one of three *modes* and derived from the payload, which
makes all three arms reachable on every run, and the mutation pass in `docs/M2-NOTES.md`
confirms each one is actually load-bearing.

**Where the strategy stops, and why that is not an oversight.** The destination path is
well-formed here. A lone surrogate in `artifact_root` makes `Path.mkdir` raise a raw
`UnicodeEncodeError` -- a `ValueError`, not an `OSError` -- which escapes every writer as
something other than the module's own error type. That is real, reproduced, **pre-existing
in all three writers on master**, and filed as `M1-322`; it is the writer-side twin of the
reader-side defect `M1-314` closed. It is pinned as a strict xfail in
`tests/unit/test_shared_artifact_writer.py` rather than fixed here, because changing what a
merged and reviewed writer does with a path is the behaviour-change-to-merged-code this
project files a row for -- the same convention `M2-709` itself was filed under.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import get_args

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings

from whiskeyjack_bot.artifacts import ArtifactError, ExistingFilePolicy, write_new_file
from whiskeyjack_bot.submission_gateway import GatewayError

# Low-entropy on purpose; see docs/LESSONS.md on the gitleaks full-history scan. One marker
# per body, so "no content reached the message" is an assertion rather than an inspection.
PLANTED = b"privateFAKE123456"
PLANTED_EXISTING = b"privateFAKE654321"

_ERRORS: tuple[type[Exception], ...] = (ArtifactError, GatewayError)
_WHATS = ("retrieval artifact", "raw model output", "submission artifact")

# What the destination already holds. Drawn as a mode and derived from the payload, because
# two independent binary draws are never equal and every assertion about the
# identical-content arm would then be about a case the strategy cannot reach.
_MODES = ("absent", "same", "different")


@st.composite
def _cases(draw: st.DrawFn) -> tuple[bytes, str, str, type[Exception], str]:
    tail = draw(st.binary(max_size=48))
    return (
        PLANTED + tail,
        draw(st.sampled_from(_MODES)),
        draw(st.sampled_from(get_args(ExistingFilePolicy))),
        draw(st.sampled_from(_ERRORS)),
        draw(st.sampled_from(_WHATS)),
    )


def _prepare(root: Path, mode: str, body: bytes) -> Path:
    destination = root / "kind" / "42" / "artifact.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "same":
        destination.write_bytes(body)
    elif mode == "different":
        destination.write_bytes(PLANTED_EXISTING + body)
    return destination


def _accepted(mode: str, policy: str) -> bool:
    """Whether the write is expected to return rather than raise.

    Spelled as the policy contract itself rather than as a copy of the implementation's
    branches: a destination that is absent is always written, and an existing one is
    accepted only when the caller said identical content is a no-op *and* it is identical.
    """
    return mode == "absent" or (mode == "same" and policy == "confirm_identical")


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@given(case=_cases())
def test_the_policy_contract_holds_and_only_the_callers_error_escapes(
    tmp_path_factory: pytest.TempPathFactory,
    case: tuple[bytes, str, str, type[Exception], str],
) -> None:
    """Four claims at once, because they are four readings of one call.

    - Nothing but the exception the caller supplied ever escapes. A raw `OSError` or
      `ValueError` reaching a caller is a review finding in this project.
    - `refuse` never replaces existing content, and neither does `confirm_identical`
      when the content disagrees.
    - The destination's bytes after the call are the ones the contract says.
    - No content reaches the message, and the path always does (the M1-401 carve-out).
    """
    body, mode, policy, error, what = case
    root = tmp_path_factory.mktemp("artifacts")
    destination = _prepare(root, mode, body)
    before = destination.read_bytes() if destination.exists() else None

    raised: Exception | None = None
    try:
        write_new_file(destination, body, what=what, on_existing=policy, error=error)
    except error as exc:  # noqa: B902 -- the caller's own type is exactly what may escape
        raised = exc
    # Any other exception propagates out of the test and fails it, which is the point:
    # a narrow `except` here is the assertion, not an omission.

    if _accepted(mode, policy):
        assert raised is None
        assert destination.read_bytes() == body
    else:
        assert raised is not None
        message = str(raised)
        assert str(destination) in message
        assert PLANTED.decode() not in message
        assert PLANTED_EXISTING.decode() not in message
        # The pre-existing bytes are exactly as they were: never overwritten.
        assert destination.read_bytes() == before

    # The failure mode is a stray temp file, never a half-written artifact -- and on every
    # arm the temp file is cleaned up too.
    assert [p.name for p in destination.parent.iterdir()] == [destination.name]


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=100)
@given(
    body=st.binary(max_size=32),
    policy=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.text(max_size=8),
        st.lists(st.text(max_size=4), max_size=2),
    ),
    error=st.sampled_from(_ERRORS),
)
def test_an_unrecognized_policy_never_writes_anything(
    tmp_path_factory: pytest.TempPathFactory,
    body: bytes,
    policy: object,
    error: type[Exception],
) -> None:
    """Any value but the two members is refused, with the caller's error, before any I/O.

    The failure this guards is not a bad message: it is an unrecognized policy falling
    through into whichever branch happens to be last and silently turning "never overwrite"
    into "sometimes".
    """
    root = tmp_path_factory.mktemp("artifacts")
    destination = root / "kind" / "42" / "artifact.json"
    if policy in get_args(ExistingFilePolicy):
        pytest.skip("a recognized policy; covered by the property above")
    with pytest.raises(error, match="on_existing must be one of"):
        write_new_file(
            destination,
            body,
            what="artifact",
            on_existing=policy,  # type: ignore[arg-type]
            error=error,
        )
    # Nothing was created, not even the directory: the check is before the first syscall.
    assert not destination.parent.exists()


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=100)
@given(body=st.binary(max_size=64), policy=st.sampled_from(get_args(ExistingFilePolicy)))
def test_a_write_into_a_path_blocked_by_a_file_is_the_callers_error(
    tmp_path_factory: pytest.TempPathFactory, body: bytes, policy: str
) -> None:
    """A reachable local I/O failure -- a plain file where a directory must go -- arrives
    as the caller's error rather than the `NotADirectoryError` underneath it, on both
    policies. `errno` differs between the arms of this call; the type a caller handles
    must not."""
    root = tmp_path_factory.mktemp("artifacts")
    blocker = root / "blocker"
    blocker.write_bytes(b"not a directory")
    raise_as: Callable[[str], Exception] = GatewayError
    with pytest.raises(GatewayError) as excinfo:
        write_new_file(
            blocker / "sub" / "a.json", body, what="artifact", on_existing=policy, error=raise_as
        )
    assert str(blocker / "sub") in str(excinfo.value)
