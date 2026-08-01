"""X account allowlist loader (M1-308).

Loads and validates ``config/x_accounts.yaml`` -- a provided, committed, curated list of
primary-source X accounts the M1-307 xAI X Search adapter will constrain its queries to and
assign :class:`~whiskeyjack_bot.research.model.ReliabilityTag` values from. This module owns
structural validation only; handle verification against live X is the owner's job (A-1106).

``reliability_tag`` reuses :data:`whiskeyjack_bot.research.model.ReliabilityTag` rather than
restating its values -- that Literal's own comment says as much, and a second copy of the closed
set would be one more place for the two to drift.

``username`` is validated against the X handle rule (1-15 characters of ``A-Za-z0-9_``) rather
than merely being non-blank, because it is not free text: it is the key for both the
case-insensitive uniqueness check and :meth:`AccountAllowlist.lookup_by_username`. A value that
cannot round-trip through its own lookup is a dead entry that *fails open* -- the M1-307 adapter
finds no match and falls back to the ``unverified_social`` default for an account the operator
believed was tagged ``official_primary``. ``" BLS_gov "`` used to validate, store padded, count as
distinct from ``"BLS_gov"`` for uniqueness, and never be found again; so did ``"@BLS_gov"`` and a
handle with an interior space or a zero-width character. One charset predicate closes the class
instead of the one instance, and it accepts every entry of the committed file unchanged.

``domains`` is the opposite case: free-form, so only the hazard that mirrors the username one is
closed. :meth:`AccountAllowlist.match_domain` compares exactly, so a blank or whitespace-padded
element matches no question domain and the tag is dead weight; both are rejected, and nothing
else about the value is. In particular ``domains`` is deliberately **not** validated against the
taxonomy the YAML file's header comment documents. That taxonomy is documentation for humans
adding entries, not a schema: encoding it here as a second, code-level source of truth would
drift the moment someone edits the comment without touching this module. The acceptance criterion
this module satisfies is "non-empty domains", not "domains drawn from a closed set".

Error hygiene matches every other module: :class:`AllowlistError` never echoes stored/file/field
values, and :func:`load_allowlist` renders only the *path* it was given (the carve-out this
project applies uniformly to filesystem paths, alongside ``config.py``, ``ledger.py``,
``metaculus/snapshots.py``, ``prompt.py`` and ``env_verify.py``). That extends to a validation
error's *location*, not just its input: under ``extra="forbid"`` the location of an unexpected
key **is** that key, so a part of ``loc`` survives only if the schema authored it -- a declared
field name, or an index under a list-valued field. An ``int`` is not self-evidently an index;
see :func:`_sanitize`.

:class:`AllowlistError` also carries ``is_filesystem_error``, set only when the file could not
be read at all. Everything else raised here -- bad UTF-8, malformed YAML, a schema violation --
is the file's *content* failing to validate, which ``env_verify.py`` and ``cli.py`` route to
config-invalid rather than environment-missing (see ``env_verify.load_and_verify_account_allowlist``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_origin

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from whiskeyjack_bot.config import _StrictModel
from whiskeyjack_bot.research.model import ReliabilityTag

# The X handle rule. Matched with re.fullmatch, never a ``$`` anchor: ``$`` also matches
# just before a trailing newline, so "BLS_gov\n" would pass a "$"-anchored pattern and
# reach lookup_by_username() as an unfindable key -- the same greedy-anchor trap M1-401 hit.
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


class AllowlistEntry(_StrictModel):
    """One curated account: ``{username, display_name, reliability_tag, domains, notes?}``."""

    username: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    reliability_tag: ReliabilityTag
    domains: list[str] = Field(min_length=1)
    notes: str | None = None

    @field_validator("username")
    @classmethod
    def _username_is_a_handle(cls, v: str) -> str:
        # min_length=1 lets through "   ", " BLS_gov ", "@BLS_gov" and "BLS gov" -- none of
        # which can identify an X account, and each of which would be stored as written,
        # counted as its own entry by the uniqueness check, and then never returned by
        # lookup_by_username(). The handle rule rejects all of them at load time.
        if _HANDLE.fullmatch(v) is None:
            raise ValueError("username must be 1-15 characters of A-Z, a-z, 0-9 or _")
        return v

    @field_validator("domains")
    @classmethod
    def _domains_are_non_blank(cls, v: list[str]) -> list[str]:
        # list-level min_length catches an empty list but not domains: [""] or [" econ_data "].
        # match_domain() compares exactly, so a blank or padded element silently never
        # matches any question domain -- a dead tag, worth rejecting at load time.
        if any(not domain.strip() or domain != domain.strip() for domain in v):
            raise ValueError(
                "domains entries must be non-blank and free of leading/trailing whitespace"
            )
        return v


class _AllowlistFile(_StrictModel):
    """The top-level shape of ``config/x_accounts.yaml``: ``{accounts: [...]}``."""

    accounts: list[AllowlistEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _usernames_are_unique_case_insensitively(self) -> _AllowlistFile:
        seen: dict[str, int] = {}
        problems: list[str] = []
        for index, entry in enumerate(self.accounts):
            key = entry.username.casefold()
            first = seen.get(key)
            if first is None:
                seen[key] = index
            else:
                # Indices only -- never the username itself. A collision is a config
                # problem, and this module's error messages never echo entry content.
                problems.append(
                    f"accounts[{index}] duplicates the username already used by "
                    f"accounts[{first}] (case-insensitive)"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class AllowlistError(Exception):
    """The account allowlist failed validation, with entry content withheld.

    Same hygiene rule as ``ConfigError``/``ResearchSchemaError``: pydantic renders the
    offending input in its message, and an allowlist entry is operator-edited content, so
    consumers print this exception and never a raw ``ValidationError``.

    ``is_filesystem_error`` distinguishes "the file could not be read" (a filesystem/
    environment concern -- retrying later or fixing permissions can resolve it) from every
    other case here, which is the file's *content* failing to validate (a config concern,
    per this item's acceptance criterion). Only the ``read_bytes()`` failure in
    :func:`load_allowlist` sets it; decode, parse and schema failures are content failures.
    """

    def __init__(self, problems: list[str], *, is_filesystem_error: bool = False):
        self.problems = problems
        self.is_filesystem_error = is_filesystem_error
        super().__init__("invalid account allowlist:\n" + "\n".join(f"  - {p}" for p in problems))


# Substituted for any error-location part that did not come from the schema. Same
# convention as research.model._sanitize / config._sanitize_validation_error.
_WITHHELD = "<withheld>"

# loc parts may name a field on either model: _AllowlistFile ("accounts") or the nested
# AllowlistEntry ("username", "reliability_tag", ...). Both are schema-authored, so both
# are allowed through; nothing else is.
_KNOWN_FIELDS = set(_AllowlistFile.model_fields) | set(AllowlistEntry.model_fields)

# The list-valued fields, derived rather than restated: a hardcoded {"accounts", "domains"}
# drifts the moment a field is added, and this set decides what gets *rendered*. Derived by
# annotation, so a later ``list[str] | None`` field (origin UnionType, not list) drops out
# and its indices are withheld -- over-redacting, which is the fail-safe direction.
_SEQUENCE_FIELDS = {
    name
    for model in (_AllowlistFile, AllowlistEntry)
    for name, info in model.model_fields.items()
    if get_origin(info.annotation) is list
}


def _sanitize(exc: ValidationError) -> AllowlistError:
    """Render a ValidationError with every file-controlled fragment removed.

    An ``int`` in ``loc`` is a list index only when the part before it names a list-valued
    field. Anywhere else it is a *mapping key lifted from the file*: pydantic's
    ``invalid_key`` error sets ``loc`` to the key itself, and an unquoted numeric YAML key
    parses as an int, so ``987654321: x`` used to be echoed verbatim by a sanitizer that
    trusted every int as an index (round-5 review finding 1). Withholding all ints would
    close it too, but ``accounts.<withheld>.username`` cannot be acted on against a
    46-entry file, and the index is schema-authored, not content.

    String parts need no such positional test: a key that is not a declared field name is
    withheld outright, which already covers ``extra="forbid"`` reporting an unknown key.
    """
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        parts: list[str] = []
        previous: str | int = ""
        for part in err["loc"]:
            if isinstance(part, int):
                parts.append(str(part) if previous in _SEQUENCE_FIELDS else _WITHHELD)
            else:
                parts.append(part if part in _KNOWN_FIELDS else _WITHHELD)
            previous = part
        location = ".".join(parts) or "<root>"
        problems.append(f"{location}: {err['msg']}")
    return AllowlistError(problems)


@dataclass(frozen=True)
class AccountAllowlist:
    """A validated, queryable allowlist."""

    entries: tuple[AllowlistEntry, ...]

    def lookup_by_username(self, username: str) -> AllowlistEntry | None:
        """Case-insensitive lookup. Uniqueness is enforced at load time, so at most one match."""
        key = username.casefold()
        for entry in self.entries:
            if entry.username.casefold() == key:
                return entry
        return None

    def match_domain(self, domain: str) -> tuple[AllowlistEntry, ...]:
        """Every entry tagged with exactly ``domain``, in file order."""
        return tuple(entry for entry in self.entries if domain in entry.domains)


def load_allowlist(path: Path | str) -> AccountAllowlist:
    """Load and validate an account allowlist YAML file; raises AllowlistError on failure."""
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        # from None: OSError's cause chain must not ride along into a formatted
        # traceback (same rule prompt.py's identical read-failure translation follows);
        # the message already carries exc.strerror, so nothing is lost.
        raise AllowlistError(
            [f"cannot read account allowlist {path}: {exc.strerror or exc}"],
            is_filesystem_error=True,
        ) from None
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # from None: UnicodeDecodeError's message embeds the offending bytes.
        raise AllowlistError(
            [
                f"account allowlist {path} is not valid UTF-8 "
                "(detail withheld: it can echo file contents)"
            ]
        ) from None
    try:
        data: Any = yaml.safe_load(raw_text)
    except yaml.MarkedYAMLError as exc:
        # PyYAML's message embeds a snippet of the offending source line, so it is
        # withheld the same way config.load_config withholds it.
        mark = exc.problem_mark or exc.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise AllowlistError(
            [
                f"account allowlist {path} is not valid YAML{where} "
                "(parser detail withheld: it can echo file contents)"
            ]
        ) from None
    except yaml.YAMLError:
        raise AllowlistError(
            [
                f"account allowlist {path} is not valid YAML "
                "(parser detail withheld: it can echo file contents)"
            ]
        ) from None
    if not isinstance(data, dict):
        raise AllowlistError(
            [f"account allowlist {path} must contain a YAML mapping at the top level"]
        )
    try:
        parsed = _AllowlistFile.model_validate(data)
    except ValidationError as exc:
        raise _sanitize(exc) from None
    return AccountAllowlist(entries=tuple(parsed.accounts))
