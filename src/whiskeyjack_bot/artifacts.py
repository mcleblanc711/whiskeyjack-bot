"""Filesystem primitives every artifact kind shares (M1-406).

``research/artifacts.py`` (M1-306) established what an artifact is and how one is written:
a versioned JSON envelope, created atomically, **never** overwritten, named by a path
component derived from an identifier and stored relative to ``storage.artifact_root``.
M1-406 adds a second kind -- raw model output -- under its own namespace, and M2-709 brings
in a third, the submission artifacts ``submission_gateway`` writes. The three rules that
make an artifact evidence rather than a cache are the same for all of them:

- **Never overwrite.** An artifact is the record that a paid call happened; replacing one
  destroys evidence. Creation is atomic against a concurrent writer (write a temp file,
  then ``os.link`` it into place, which fails rather than clobbers), so a crash leaves at
  most a stray temp file and never a half-written artifact.
- **The stored path is relative to the artifact root**, so a ledger stays readable after
  the artifact directory moves, or is opened on another machine.
- **An identifier that becomes a path component is constrained**, before any I/O.

This module is those rules, factored out so the writers cannot drift apart. M1-406's
extraction was a pure one -- every function below was ``research/artifacts.py``'s, with the
noun it names in its messages passed in rather than hard-coded. **M2-709 brought the third
writer in.** ``submission_gateway`` spelled the same ``mkstemp``/``fsync``/``os.link``
sequence a second time, privately, because it differs from the other two in exactly two
places; a second copy of a race-sensitive write is what M2-709 was filed to stop. Both
differences are parameters here rather than a second body:

- **What an existing destination means** (``on_existing``, see :data:`ExistingFilePolicy`).
  A retrieval or model-output artifact records that a paid call happened, so an existing
  file is always a collision. A submission artifact's path is derived from the idempotency
  key, which is derived from the payload -- so an existing file whose **bytes match** means
  the identical dry run was performed before, and refusing it would make the one mode whose
  whole purpose is repeatability un-repeatable. Only a byte *disagreement* raises there,
  and a disagreement at a content-derived path means something outside the writer put it
  there.
- **Which exception the caller's own callers handle** (``error``).

**One error type for the artifact kinds, and an injectable one for the third writer.**
:class:`ArtifactError` lives here and ``research.artifacts`` re-exports it, so its public
name and every existing caller are unchanged. That follows the project's own precedent
rather than inventing one -- ``research/persist.py`` reuses ``StoreError`` and
``forecast/store.py`` reuses ``ForecastRecordError``, both on the grounds that a module
adding no failure mode of its own should not mint a second type for callers to handle.
``submission_gateway`` cannot join that arrangement: ``GatewayError`` subclasses
``submission.SubmissionError`` so a caller already handling the submission seam's error type
handles it too, and ``ArtifactError`` is a bare ``Exception`` -- neither class can absorb
the other without widening what an existing ``except`` catches. So the exception to raise is
the ``error`` parameter, defaulting to :class:`ArtifactError`. Every failure arm below
raises it, which is what lets each writer keep its own error type **without relaying another
module's message text** -- the alternative (catch ``ArtifactError``, re-raise
``GatewayError(str(exc))``) would couple the gateway to this module's hygiene rule for
every message it ever adds.

Purely local file I/O: no network access on any path through here.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal, get_args

# An identifier becomes a path component, so it is constrained to characters that cannot
# escape the artifact root or name a directory entry with a meaning of its own. The
# operator is not the adversary here (see CLAUDE.md's threat boundary) -- this refuses a
# *caller mistake*, before any I/O, rather than an attack: an id carrying a separator
# would write outside the tree the ledger's relative paths are resolved in.
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# What an existing destination means to :func:`write_new_file`. A closed vocabulary as a
# module-level ``Literal`` alias rather than an ``enum.Enum``, per the project convention,
# and validated at runtime with ``get_args`` at the top of the function.
ExistingFilePolicy = Literal["refuse", "confirm_identical"]


class ArtifactError(Exception):
    """An artifact cannot be written or read back.

    Same hygiene rule as ``SnapshotError``: the message never echoes a provider response
    body or a model reply, and sanitizing raises use ``from None`` so an underlying
    exception cannot reprint one through its text or a rendered traceback. Filesystem
    paths are the settled M1-401 carve-out and are rendered.
    """


def require_safe_component(value: object, *, field: str) -> str:
    """Return ``value`` if it is safe to use as one path component.

    ``field`` is the caller's own literal -- a field name, never content -- so naming it
    in the message tells an operator which input was refused without echoing the input.
    """
    if type(value) is not str or not _SAFE_COMPONENT_RE.match(value):
        # No value in the message: an identifier is row content. The rule it broke is
        # this module's own literal and is safe to state.
        raise ArtifactError(
            f"{field} must be 1-128 characters of [A-Za-z0-9._-] starting alphanumeric: "
            "it becomes a path component (offending input withheld)"
        )
    return value


def require_int(value: object, field: str) -> int:
    """Return ``value`` if it is exactly an ``int``.

    ``type() is int`` rather than ``isinstance``: ``bool`` subclasses ``int``, and ``True``
    would otherwise become the directory ``"1"``.
    """
    if type(value) is not int:
        raise ArtifactError(f"{field} must be an int")
    return value


def write_new_file(
    destination: Path,
    payload: bytes,
    *,
    what: str,
    on_existing: ExistingFilePolicy = "refuse",
    error: Callable[[str], Exception] = ArtifactError,
) -> None:
    """Create ``destination`` with ``payload``, atomically, never overwriting.

    A temp file in the destination's own directory is written and fsynced, then
    ``os.link`` moves it into place -- ``link`` fails with ``EEXIST`` rather than
    replacing, so "do not overwrite" is atomic against a concurrent writer instead of a
    check that can be raced. ``os.replace`` would have been the usual atomic rename and is
    exactly wrong here: it clobbers.

    The failure mode is a stray temp file, never a half-written artifact.

    ``what`` names the artifact kind for the message and is the caller's own literal.

    ``on_existing`` says what an existing destination *means* -- see
    :data:`ExistingFilePolicy` and the module docstring. The two arms differ in one more
    way than the obvious one: ``"refuse"`` also checks **before** writing anything, so the
    common case gets a message that says what happened (the link is still what makes it
    safe). ``"confirm_identical"`` deliberately does not pre-check. It lets the link fail
    and compares the bytes, which is one syscall sequence with no window between a check
    and the write, and it is the sequence ``submission_gateway._write_or_confirm`` ran
    before M2-709 folded it in here.

    ``error`` is the exception the caller's own callers handle; every failure arm raises
    it. That is what lets a writer keep its own error type without relaying this module's
    message text.
    """
    if on_existing not in get_args(ExistingFilePolicy):
        # Before any I/O, and not decoration: an unrecognized policy falling through into
        # whichever branch happens to be last would silently turn "never overwrite" into
        # "sometimes". The accepted values are this module's own literals.
        raise error(
            f"on_existing must be one of {get_args(ExistingFilePolicy)} (offending input withheld)"
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise error(f"cannot create the directory for {what} {destination.parent}") from None
    if on_existing == "refuse" and destination.exists():
        # Reported before the write attempt so the common case has a message that says
        # what happened; the link below is what actually makes it safe.
        raise error(f"{what} already exists and is never overwritten: {destination}")
    handle, temp_name = -1, ""
    try:
        handle, temp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
        with os.fdopen(handle, "wb") as stream:
            handle = -1  # fdopen took ownership; the finally below must not close it twice.
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, destination)
        except FileExistsError:
            if on_existing == "refuse":
                raise error(
                    f"{what} already exists and is never overwritten: {destination}"
                ) from None
            _confirm_identical(destination, payload, what=what, error=error)
    except OSError:
        raise error(f"cannot write {what} {destination}") from None
    finally:
        if handle != -1:
            os.close(handle)
        if temp_name:
            # The link either succeeded (the content now has two names) or failed (the
            # temp file is garbage). Either way the temp name goes.
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _confirm_identical(
    destination: Path, payload: bytes, *, what: str, error: Callable[[str], Exception]
) -> None:
    """Accept an existing file whose bytes match ``payload``; raise if they do not.

    Only reachable under ``on_existing="confirm_identical"``, where the destination name is
    derived from the content it holds -- so identical bytes mean the identical write was
    performed before, and that is a no-op rather than a collision. A *disagreement* at a
    content-derived path means something outside this writer put the file there, and the
    comparison is what turns that into a refusal rather than a silent overwrite.

    On ``submission_gateway``'s live path an existing destination should be unreachable at
    all -- the path is derived from an idempotency key ``submission.require_key_unused``
    has just declared unspent -- which is why reaching here at all is worth refusing on.
    """
    try:
        existing = destination.read_bytes()
    except OSError:
        raise error(
            f"a {what} already exists at {destination} and could not be read back to "
            "confirm it records the same content"
        ) from None
    if existing != payload:
        # Names neither body: both are caller-supplied content.
        raise error(
            f"a different {what} already exists at {destination} and is never overwritten "
            "(detail withheld: the differing content is not named)"
        )
