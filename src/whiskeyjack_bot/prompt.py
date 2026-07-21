"""Forecaster-prompt loading, version verification and hashing (M1-401).

Every forecast record stores the prompt version and the prompt's SHA-256
(``forecast_records.prompt_version`` / ``prompt_sha256``, both ``NOT NULL``
since migration 001). Those two columns are the only link between a stored
forecast and the exact instructions that produced it, and they cannot be
reconstructed after the fact -- which is why D04 requires them from the very
first forecast rather than as a later schema addition.

**The digest is over the file's raw bytes, with no normalization of any kind.**
This module deliberately does *not* reuse
:func:`whiskeyjack_bot.research.hashing.content_sha256`. That function's pinned
rule collapses whitespace runs and applies Unicode NFC, which is correct for
research documents -- two renderings of the same article are the same evidence
-- and wrong for a prompt. Reflowing a prompt changes what the model actually
sees, so a reflow must produce a new hash. The acceptance criterion is
"changed *bytes* produce a new hash", and only a raw-byte digest satisfies it.
The precedent followed here is the migration checksum in :mod:`ledger`, which
hashes ``read_bytes()`` before any decoding for the same reason.

Like the normalization rule in ``research.hashing``, **changing this rule
breaks replay**: forecasts already in the ledger keep their old digests, so a
re-run over an unchanged prompt would no longer match them. If it must ever
change, it changes as a new versioned function alongside this one.

The declared version is parsed from the prompt's H1 and cross-checked against
``forecast.prompt_version`` in config. The two disagreeing is a hard error and
never a coercion: a prompt whose declared version does not match the version
that will be recorded against every forecast is exactly the drift D04 exists to
catch.

Error hygiene matches ``ConfigError``/``LedgerError``/``NormalizationError``: a
:class:`PromptError` never echoes file contents (a prompt can carry a
mistakenly pasted credential), and wrapping raises use ``from None`` so an
underlying exception -- ``UnicodeDecodeError`` in particular, whose text embeds
the offending bytes -- cannot reprint content through the cause chain.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# The version is declared once, in the H1 on the first line, as a trailing
# ``vMAJOR.MINOR.PATCH``:
#
#     # MiniBench forecaster prompt — v1.1.0
#
# Anchored to line 1 on purpose. The prompt body contains a fenced JSON example
# carrying ``"schema_version": "1.0.0"`` -- the *output record* schema version,
# an unrelated number that a document-wide search for a semver would match, and
# would keep matching, silently and wrongly, once the two versions diverge.
_H1_VERSION_RE = re.compile(r"^#\s+\S.*\bv(\d+\.\d+\.\d+)\s*$")

# Config stores the bare form (``"1.1.0"``); the prompt H1 carries the ``v``
# prefix. Bare is canonical -- it is what reaches the ledger column -- so the
# parser strips the prefix rather than the config growing one.
_BARE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class PromptError(Exception):
    """The forecaster prompt cannot be loaded, parsed or version-verified.

    Same hygiene rule as ``ConfigError``: the message never echoes the prompt's
    contents, and wrapping raises use ``from None`` so an underlying exception
    cannot reprint a line of the file through its text or a rendered traceback.
    """


@dataclass(frozen=True)
class LoadedPrompt:
    """A verified prompt: its declared version, raw-byte digest and text."""

    version: str
    sha256: str
    text: str


def prompt_sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data`` -- raw bytes, unnormalized."""
    return hashlib.sha256(data).hexdigest()


def parse_declared_version(text: str) -> str:
    """Return the bare version declared in the prompt's H1, ``v`` prefix stripped.

    Raises :class:`PromptError` if the first line is not an H1 declaring one.
    """
    first_line = text.split("\n", 1)[0].rstrip("\r")
    match = _H1_VERSION_RE.match(first_line)
    if match is None:
        # Constant message: the offending line is prompt content.
        raise PromptError(
            "prompt does not declare a version: the first line must be an H1 ending in "
            "'vMAJOR.MINOR.PATCH' (line withheld: it can echo prompt contents)"
        )
    return match.group(1)


def load_prompt(path: Path, expected_version: str) -> LoadedPrompt:
    """Load the prompt at ``path``, verifying its declared version and hashing it.

    ``expected_version`` is ``forecast.prompt_version`` from config, in bare
    form. A mismatch against the prompt's own H1 raises :class:`PromptError`.
    """
    if _BARE_VERSION_RE.match(expected_version) is None:
        # Checked here, not just in config, because the mismatch message below
        # echoes this value: it must be provably a bare semver first, or an
        # arbitrary caller-supplied string reaches a diagnostic.
        raise PromptError(
            "forecast.prompt_version must be a bare MAJOR.MINOR.PATCH version with no "
            "'v' prefix (value withheld)"
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        # from None: OSError's text carries the path, and a caller that only
        # handles PromptError must not receive a raw OSError either way.
        raise PromptError(
            f"cannot read forecaster prompt {path}: {exc.strerror or 'unreadable'}"
        ) from None

    # Hash before decoding: the digest is defined over the bytes on disk, so a
    # file that fails to decode still has a well-defined identity, and no
    # decoding step can sit between the file and its recorded hash.
    digest = prompt_sha256(data)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # from None: UnicodeDecodeError's message embeds the offending bytes.
        raise PromptError(
            f"forecaster prompt {path} is not valid UTF-8 "
            "(detail withheld: it can echo prompt contents)"
        ) from None

    declared = parse_declared_version(text)
    if declared != expected_version:
        # Both versions are safe to echo: each has already matched a strict
        # semver pattern, so neither can carry arbitrary file content.
        raise PromptError(
            f"forecaster prompt declares version {declared} but forecast.prompt_version "
            f"is {expected_version}; every forecast would be attributed to a prompt "
            "version it was not generated from (D04)"
        )

    return LoadedPrompt(version=declared, sha256=digest, text=text)
