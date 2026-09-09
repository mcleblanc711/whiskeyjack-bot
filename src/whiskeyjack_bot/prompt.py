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

**The probability range the prompt declares to the model is parsed and
cross-checked the same way (M1-407).** ``forecast.min_probability`` and
``forecast.max_probability`` bound what the *application* will accept back;
``prompts/forecaster.md`` states a range to the model in prose. Nothing compared
them, so a configuration outside the declared range asks for a probability the
prompt never permits and pays a repair turn to find out. The check reads the
prompt ``forecast.prompt_path`` actually names -- never a copy of its numbers,
which is the whole of the acceptance criterion. It is deliberately *not* the
same check as ``forecast.generate``'s spec-envelope preflight: see
``probability_bounds_problem``.

Error hygiene matches ``ConfigError``/``LedgerError``/``NormalizationError``: a
:class:`PromptError` never echoes file contents (a prompt can carry a
mistakenly pasted credential), and wrapping raises use ``from None`` so an
underlying exception -- ``UnicodeDecodeError`` in particular, whose text embeds
the offending bytes -- cannot reprint content through the cause chain.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# The single canonical semver rule for this project. Every version check --
# prompt H1, ``forecast.prompt_version`` in config -- compiles from this one
# string so the two cannot drift apart (they had: config used ``fullmatch`` on a
# looser pattern while this module used ``match`` + ``$``).
#
# ``(?:0|[1-9]\d*)`` rejects leading zeroes: ``01.1.0`` is not canonical SemVer,
# and accepting it lets ``01.1.0`` and ``1.1.0`` name the same prompt while
# comparing unequal against the ledger column.
_SEMVER = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"

# ``re.ASCII`` throughout: without it ``\d`` matches any Unicode decimal, so
# ``v١.١.٠`` parses as a version and reaches forecast_records.prompt_version as
# a string no operator can search for.
BARE_VERSION_RE = re.compile(_SEMVER, re.ASCII)
"""Config's bare form (``"1.1.0"``), no ``v`` prefix. Use with ``fullmatch``."""

# The version is declared once, in the H1 on the first line, as a trailing
# ``vMAJOR.MINOR.PATCH``:
#
#     # MiniBench forecaster prompt — v1.1.0
#
# Anchored to line 1 on purpose. The prompt body contains a fenced JSON example
# carrying ``"schema_version": "1.0.0"`` -- the *output record* schema version,
# an unrelated number that a document-wide search for a semver would match, and
# would keep matching, silently and wrongly, once the two versions diverge.
#
# Scanned with ``finditer`` rather than matched as one anchored pattern: an
# anchored ``.*v(...)$`` silently resolves ``v1.1.0 supersedes v2.0.0`` to
# 2.0.0, and non-greedy quantifiers do not fix it -- with a trailing anchor the
# engine just backtracks to the same last token. An H1 declaring two versions is
# drift, so ``parse_declared_version`` rejects it rather than picking a winner.
_H1_PREFIX_RE = re.compile(r"#[ \t]+\S")
_H1_VERSION_TOKEN_RE = re.compile(rf"\bv({_SEMVER})(?![\w.])", re.ASCII)

# M1-407. Unlike the version, the probability range is declared *in the body*
# and more than once -- ``prompts/forecaster.md`` v1.1.0 states it three times
# (binary prose guidance, the ``probability_yes`` bound, the multiple-choice
# per-option bound), because all three are things the model must be told where
# it reads them. So there is no single anchored line to match, and a
# document-wide scan for a decimal pair is exactly the mistake
# ``parse_declared_version``'s comment above describes: the body also carries a
# ``1e-6`` sum tolerance and a percentile ladder of decimals.
#
# The scan is therefore *scoped by line to lines that are about probability*.
# That is what keeps "percentile values must be non-decreasing" and
# "sum to 1 within ``1e-6``" out of the result, and it is a rule a custom prompt
# can satisfy by writing the sentence any operator would write anyway.
_PROBABILITY_LINE_RE = re.compile(r"probabilit", re.IGNORECASE)

# A digit is required before the point: ``.5`` is not a spelling this accepts,
# and the parser refuses rather than guessing at a prompt that uses one.
# Deliberately not bounded in length -- the matched text is never echoed, only
# the parsed ``float`` is, and every overlong digit string collapses to a short
# repr (or to ``inf``, which then fails the range check below).
_DECIMAL = r"\d+(?:\.\d+)?"
_DECLARED_RANGE_RE = re.compile(
    rf"\bbetween\s+({_DECIMAL})\s+and\s+({_DECIMAL})\b", re.ASCII | re.IGNORECASE
)


class PromptError(Exception):
    """The forecaster prompt cannot be loaded, parsed or version-verified.

    Same hygiene rule as ``ConfigError``: the message never echoes the prompt's
    contents, and wrapping raises use ``from None`` so an underlying exception
    cannot reprint a line of the file through its text or a rendered traceback.
    """


@dataclass(frozen=True)
class DeclaredProbabilityBounds:
    """The probability range the prompt states to the model (M1-407).

    Two floats parsed out of the prompt body, never the matched text. Both are
    safe in the repr for the reason ``LoadedPrompt.version`` is: each has
    already matched a strict decimal pattern and been range-checked, so neither
    can carry arbitrary file content.
    """

    low: float
    high: float


@dataclass(frozen=True)
class LoadedPrompt:
    """A verified prompt: its declared version, raw-byte digest and text.

    ``text`` is excluded from the repr. The module's error paths are sanitized,
    but the value object was not: a traceback frame, a failed assertion or a log
    line rendering this dataclass printed the whole prompt -- including any
    mistakenly pasted credential, the same hazard the module docstring names.
    ``version``, ``sha256`` and ``bounds`` stay in the repr; all three are safe
    by construction (a matched semver, a hex digest, two range-checked floats)
    and a repr without them is useless.
    """

    version: str
    sha256: str
    text: str = field(repr=False)
    bounds: DeclaredProbabilityBounds


def prompt_sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data`` -- raw bytes, unnormalized."""
    return hashlib.sha256(data).hexdigest()


def parse_declared_version(text: str) -> str:
    """Return the bare version declared in the prompt's H1, ``v`` prefix stripped.

    Raises :class:`PromptError` if the first line is not an H1 declaring exactly
    one version, as its trailing token.
    """
    first_line = text.split("\n", 1)[0].rstrip("\r")

    # All three messages below are constant text: the offending line is prompt
    # content and must never reach a diagnostic.
    malformed = PromptError(
        "prompt does not declare a version: the first line must be an H1 ending in "
        "'vMAJOR.MINOR.PATCH' (line withheld: it can echo prompt contents)"
    )
    if _H1_PREFIX_RE.match(first_line) is None:
        raise malformed

    matches = list(_H1_VERSION_TOKEN_RE.finditer(first_line))
    if not matches:
        raise malformed
    if len(matches) > 1:
        raise PromptError(
            "prompt H1 declares more than one version; exactly one is required so the "
            "version recorded against every forecast is unambiguous (D04) "
            "(line withheld: it can echo prompt contents)"
        )

    only = matches[0]
    if first_line[only.end() :].strip():
        # The version must be the trailing token, not buried mid-heading.
        raise malformed
    return only.group(1)


def parse_declared_probability_bounds(text: str) -> DeclaredProbabilityBounds:
    """Return the probability range the prompt declares to the model (M1-407).

    Every line that is about probability is scanned for ``between <low> and
    <high>``; **every match found must agree**, and at least one is required.

    Both of those are the stricter reading, and both follow
    ``parse_declared_version``. A prompt whose three statements of the range
    disagree is drift, and picking a winner -- even the narrowest one -- would
    silently accept a prompt that tells the model two different things where it
    reads them. A prompt that declares no range at all cannot be checked
    against, and "reported at startup" is not satisfied by guessing that the
    default applies: an operator who points ``forecast.prompt_path`` at a prompt
    stating no range gets told so, once, before anything is spent.

    Raises :class:`PromptError` and nothing else. No message echoes a line: the
    only file-derived values that reach one are parsed ``float``s.
    """
    found: list[tuple[float, float]] = []
    for line in text.splitlines():
        if _PROBABILITY_LINE_RE.search(line) is None:
            continue
        for match in _DECLARED_RANGE_RE.finditer(line):
            # float() cannot raise on this pattern: it is digits with at most
            # one point. An overlong run becomes ``inf`` and fails the range
            # check below rather than escaping as an OverflowError.
            found.append((float(match.group(1)), float(match.group(2))))

    if not found:
        raise PromptError(
            "forecaster prompt declares no probability range: at least one line about "
            "probability must state it as 'between <low> and <high>', so a configured "
            "bound can be checked against the prompt rather than against a copy of its "
            "numbers (M1-407) (lines withheld: they can echo prompt contents)"
        )

    low, high = found[0]
    if any(pair != (low, high) for pair in found[1:]):
        raise PromptError(
            "forecaster prompt declares more than one probability range and they disagree; "
            "exactly one range is required so the bound checked here is the bound the model "
            "is told (lines withheld: they can echo prompt contents)"
        )
    if not 0.0 <= low < high <= 1.0:
        # Echoing the parsed pair follows the version mismatch below: each value
        # has matched a strict pattern, so neither can carry arbitrary content,
        # and a message without them names no fixable defect.
        raise PromptError(
            f"forecaster prompt declares the probability range {low!r} to {high!r}, which "
            "cannot bound a probability; 0 <= low < high <= 1 is required"
        )
    return DeclaredProbabilityBounds(low=low, high=high)


def probability_bounds_problem(
    bounds: DeclaredProbabilityBounds, *, min_probability: float, max_probability: float
) -> str | None:
    """Return a sanitized problem string if config falls outside ``bounds`` (M1-407).

    ``None`` means the configured pair is contained in the range the prompt
    declares. This is **containment, not equality**: a configuration narrower
    than the prompt is accepted, because the acceptance criterion is about a
    config falling *outside* the declared range, and a narrower one is a bound
    ``forecast.binary``'s repair turn already states to the model.

    **This is not ``forecast.generate``'s envelope preflight and must not be
    fused with it.** That check asks whether the configured pair is inside the
    ``0.001``-``0.999`` the *submission path* will accept, and its numbers come
    from ``config.PROBABILITY_BOUND_FLOOR``/``CEILING`` for that reason. This
    one asks whether the configured pair is inside the range *the loaded prompt
    states to the model*. The two agree today only because the shipped prompt
    happens to print the spec's endpoints; a custom prompt separates them
    immediately. Collapsing them is the mistake ``bounds.py``'s docstring
    describes for ``MAX_ACTOR_LENGTH`` and ``MAX_IDENTIFIER_LENGTH``, and giving
    the submission envelope one owner is a different open row (M1-513).

    The declared pair is named and the configured pair is withheld, which is
    exactly what ``forecast.multiple_choice``'s envelope diagnostic already
    does: the declared values are file-derived but strictly matched, while
    whether a *configured* value may be rendered at all is open (M1-509).
    """
    if bounds.low <= min_probability and max_probability <= bounds.high:
        return None
    return (
        f"forecast.min_probability/forecast.max_probability fall outside the {bounds.low!r} "
        f"to {bounds.high!r} range the loaded forecaster prompt declares to the model, so "
        "every forecast would be asked for a probability the prompt does not permit "
        "(configured pair withheld)"
    )


def load_prompt(
    path: Path,
    expected_version: str,
    *,
    min_probability: float,
    max_probability: float,
) -> LoadedPrompt:
    """Load the prompt at ``path``, verifying its declared version and hashing it.

    ``expected_version`` is ``forecast.prompt_version`` from config, in bare
    form. A mismatch against the prompt's own H1 raises :class:`PromptError`.

    ``min_probability``/``max_probability`` are ``forecast.min_probability`` and
    ``forecast.max_probability``. They are **required keyword arguments and not
    a separate function** on purpose (M1-407): every path that reaches a
    billable call loads the prompt through here -- both pipelines,
    ``verify-env`` and the acceptance harness -- so binding the cross-check to
    the load makes it inherited by construction rather than remembered at each
    site. A caller that has no configuration to check against does not exist;
    one that appears must say what it is checking.
    """
    # Exact-type, and range-checked before use: these two reach ``<=``
    # comparisons below, so a str or a NaN would escape this module as a raw
    # TypeError or pass a comparison silently -- and every malformed shape must
    # arrive as this module's own error type. ``ForecastConfig`` already
    # guarantees floats; this is the AppConfig-assembled-some-other-way case
    # ``forecast.generate`` repeats its own preflights for.
    for value in (min_probability, max_probability):
        if type(value) is not float or not 0.0 <= value <= 1.0:
            raise PromptError(
                "forecast.min_probability and forecast.max_probability must be floats "
                "between 0 and 1 (values withheld)"
            )

    # fullmatch, not match: ``match`` + ``$`` accepts a terminal newline, so
    # "1.1.0\n" passed this guard and then reached the mismatch diagnostic below
    # -- exactly the unvalidated-value-in-a-message case the guard exists for.
    if BARE_VERSION_RE.fullmatch(expected_version) is None:
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

    # Version first, deliberately: M1-401's checks keep the precedence they had,
    # so a prompt that fails both reports the version drift D04 exists to catch
    # rather than a new message. One PromptError per load either way -- the
    # first failure wins, here and in ``verify-env``.
    declared = parse_declared_version(text)
    if declared != expected_version:
        # Both versions are safe to echo: each has already matched a strict
        # semver pattern, so neither can carry arbitrary file content.
        raise PromptError(
            f"forecaster prompt declares version {declared} but forecast.prompt_version "
            f"is {expected_version}; every forecast would be attributed to a prompt "
            "version it was not generated from (D04)"
        )

    bounds = parse_declared_probability_bounds(text)
    problem = probability_bounds_problem(
        bounds, min_probability=min_probability, max_probability=max_probability
    )
    if problem is not None:
        raise PromptError(problem)

    return LoadedPrompt(version=declared, sha256=digest, text=text, bounds=bounds)
