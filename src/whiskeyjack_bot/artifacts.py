"""Filesystem primitives every artifact kind shares (M1-406).

``research/artifacts.py`` (M1-306) established what an artifact is and how one is written:
a versioned JSON envelope, created atomically, **never** overwritten, named by a path
component derived from an identifier and stored relative to ``storage.artifact_root``.
M1-406 adds a second kind -- raw model output -- under its own namespace, and the three
rules that make an artifact evidence rather than a cache are the same for both:

- **Never overwrite.** An artifact is the record that a paid call happened; replacing one
  destroys evidence. Creation is atomic against a concurrent writer (write a temp file,
  then ``os.link`` it into place, which fails rather than clobbers), so a crash leaves at
  most a stray temp file and never a half-written artifact.
- **The stored path is relative to the artifact root**, so a ledger stays readable after
  the artifact directory moves, or is opened on another machine.
- **An identifier that becomes a path component is constrained**, before any I/O.

This module is those rules, factored out so the two writers cannot drift apart. It is a
pure extraction: every function below is ``research/artifacts.py``'s, with the noun it
names in its messages passed in rather than hard-coded.

**One error type for both kinds.** :class:`ArtifactError` lives here and
``research.artifacts`` re-exports it, so its public name and every existing caller are
unchanged. That follows the project's own precedent rather than inventing one --
``research/persist.py`` reuses ``StoreError`` and ``forecast/store.py`` reuses
``ForecastRecordError``, both on the grounds that a module adding no failure mode of its
own should not mint a second type for callers to handle.

Purely local file I/O: no network access on any path through here.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# An identifier becomes a path component, so it is constrained to characters that cannot
# escape the artifact root or name a directory entry with a meaning of its own. The
# operator is not the adversary here (see CLAUDE.md's threat boundary) -- this refuses a
# *caller mistake*, before any I/O, rather than an attack: an id carrying a separator
# would write outside the tree the ledger's relative paths are resolved in.
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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


def write_new_file(destination: Path, payload: bytes, *, what: str) -> None:
    """Create ``destination`` with ``payload``, atomically, never overwriting.

    A temp file in the destination's own directory is written and fsynced, then
    ``os.link`` moves it into place -- ``link`` fails with ``EEXIST`` rather than
    replacing, so "do not overwrite" is atomic against a concurrent writer instead of a
    check that can be raced. ``os.replace`` would have been the usual atomic rename and is
    exactly wrong here: it clobbers.

    The failure mode is a stray temp file, never a half-written artifact.

    ``what`` names the artifact kind for the message and is the caller's own literal.
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ArtifactError(f"cannot create artifact directory {destination.parent}") from None
    if destination.exists():
        # Reported before the write attempt so the common case has a message that says
        # what happened; the link below is what actually makes it safe.
        raise ArtifactError(f"{what} already exists and is never overwritten: {destination}")
    handle, temp_name = -1, ""
    try:
        handle, temp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
        with os.fdopen(handle, "wb") as stream:
            handle = -1  # fdopen took ownership; the finally below must not close it twice.
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_name, destination)
    except FileExistsError:
        raise ArtifactError(
            f"{what} already exists and is never overwritten: {destination}"
        ) from None
    except OSError:
        raise ArtifactError(f"cannot write {what} {destination}") from None
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
