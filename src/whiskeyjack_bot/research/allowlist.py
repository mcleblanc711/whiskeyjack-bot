"""X account allowlist loader (M1-308).

Loads and validates ``config/x_accounts.yaml`` -- a provided, committed, curated list of
primary-source X accounts the M1-307 xAI X Search adapter will constrain its queries to and
assign :class:`~whiskeyjack_bot.research.model.ReliabilityTag` values from. This module owns
structural validation only; handle verification against live X is the owner's job (A-1106).

``reliability_tag`` reuses :data:`whiskeyjack_bot.research.model.ReliabilityTag` rather than
restating its values -- that Literal's own comment says as much, and a second copy of the closed
set would be one more place for the two to drift.

``domains`` is deliberately **not** validated against the taxonomy the YAML file's header comment
documents. That taxonomy is documentation for humans adding entries, not a schema: encoding it
here as a second, code-level source of truth would drift the moment someone edits the comment
without touching this module. The acceptance criterion this module satisfies is "non-empty
domains", not "domains drawn from a closed set" -- so only non-emptiness (of the list, and of each
element) is enforced.

Error hygiene matches every other module: :class:`AllowlistError` never echoes stored/file/field
values, and :func:`load_allowlist` renders only the *path* it was given (the carve-out this
project applies uniformly to filesystem paths, alongside ``config.py``, ``ledger.py``,
``metaculus/snapshots.py``, ``prompt.py`` and ``env_verify.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from whiskeyjack_bot.config import _StrictModel
from whiskeyjack_bot.research.model import ReliabilityTag


class AllowlistEntry(_StrictModel):
    """One curated account: ``{username, display_name, reliability_tag, domains, notes?}``."""

    username: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    reliability_tag: ReliabilityTag
    domains: list[str] = Field(min_length=1)
    notes: str | None = None

    @field_validator("domains")
    @classmethod
    def _domains_are_non_blank(cls, v: list[str]) -> list[str]:
        # list-level min_length catches an empty list but not domains: [""] -- a blank
        # element would silently never match any question domain, which is worth
        # rejecting at load time rather than leaving a dead entry in the allowlist.
        if any(not domain.strip() for domain in v):
            raise ValueError("domains entries must be non-blank")
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
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid account allowlist:\n" + "\n".join(f"  - {p}" for p in problems))


# Substituted for any error-location part that did not come from the schema. Same
# convention as research.model._sanitize / config._sanitize_validation_error.
_WITHHELD = "<withheld>"

# loc parts may name a field on either model: _AllowlistFile ("accounts") or the nested
# AllowlistEntry ("username", "reliability_tag", ...). Both are schema-authored, so both
# are allowed through; nothing else is.
_KNOWN_FIELDS = set(_AllowlistFile.model_fields) | set(AllowlistEntry.model_fields)


def _sanitize(exc: ValidationError) -> AllowlistError:
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        parts = [
            str(part) if isinstance(part, int) or part in _KNOWN_FIELDS else _WITHHELD
            for part in err["loc"]
        ]
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
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AllowlistError(
            [f"cannot read account allowlist {path}: {exc.strerror or exc}"]
        ) from exc
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
