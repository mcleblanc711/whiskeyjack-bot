"""Locale registry, geographic detection and publisher origin for the local-source lens (M1-356).

The local-source lens (D53) looks for evidence from **publishers based in the places a
question is about**. This module answers the two questions that precede any paid call, and
answers them without one:

- **Which places is this question about?** :func:`detect` matches the registry's English
  triggers against the question. It is pure and deterministic, so the same question and the
  same registry always select the same locales. That is what lets a lens run record *why* it
  ran and a replay agree.
- **Is this document's publisher local to that place?** :func:`publisher_origin` reads the
  document's URL against the registry's curated domain lists and the locale's country-code
  TLDs, and **nothing else**. Two shortcuts are refused, both on evidence from the
  2026-10-16 probe (``docs/LOCAL-LENS-NOTES.md`` § 2):

  * **Language is not locality.** A Persian query to Exa returned Iranian publishers
    alongside BBC Persian and Iran International, both London-based. The owner's correction
    was explicit: the target is Iranian sources, not Persian ones.
  * **A provider's country tag is not locality.** AskNews's ``countries=["IR"]`` returned
    AFP copy, a French agency, because the tag describes the story.

  A URL that no list covers is ``unclassified``, never guessed at.

The registry is ``config/locales.yaml``. It is deliberately **not** ``AppConfig``: a new
config field moves ``config_sha256`` and retires the live activation, and this module has
no paid caller yet. Its identity is :attr:`LocaleRegistry.sha256` over the file's raw bytes,
the rule ``prompt.py`` uses for the forecaster prompt, so a lens run can record exactly which
registry selected its locales.

Error hygiene is the project's: :class:`LocaleError` never echoes file content. The path is
rendered (the M1-401 carve-out), and validation problems go through
:func:`validation_errors.sanitized_problems` so that a file-controlled key or value cannot
reach a message.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from publicsuffixlist import PublicSuffixList

from whiskeyjack_bot.config import _StrictModel
from whiskeyjack_bot.questions.model import _CanonicalQuestionBase
from whiskeyjack_bot.research.canonical import host_identity
from whiskeyjack_bot.validation_errors import authored_error, sanitized_problems

if TYPE_CHECKING:
    from whiskeyjack_bot.questions.model import CanonicalQuestion

# Where a document's publisher sits relative to one locale. ``external`` is an outlet based
# outside the locale but aimed at it: exile, diaspora, or a foreign state's language service.
PublisherOrigin = Literal["local", "external", "unclassified"]

REGISTRY_SCHEMA_VERSION = 1

# ISO 3166-1 alpha-2, optionally with an ISO 3166-2 subdivision suffix (``CA-QC``).
_CODE = re.compile(r"[A-Z]{2}(?:-[A-Z0-9]{1,3})?")
# ISO 639-1. AskNews takes the bare two-letter form; ``en-US`` is a response value, not a
# request value, and the registry describes requests.
_LANGUAGE = re.compile(r"[a-z]{2}")
_CCTLD = re.compile(r"[a-z]{2}")
# A domain entry is a lowercase ASCII host, optionally followed by one path prefix. IDN hosts
# are written in their A-label form so that the comparison is a plain string comparison.
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}")
_PATH_PREFIX = re.compile(r"/[A-Za-z0-9._~%-]+(?:/[A-Za-z0-9._~%-]+)*")

_PUBLIC_SUFFIXES = PublicSuffixList()


class LocaleError(Exception):
    """The locale registry could not be loaded, or a caller passed the wrong shape.

    ``is_filesystem_error`` separates "there is no file here to validate" from "the file's
    content does not validate", for the same reason ``AllowlistError`` carries it: the CLI
    routes the first to environment-missing and the second to config-invalid.
    """

    def __init__(self, problems: list[str], *, is_filesystem_error: bool = False) -> None:
        self.problems = problems
        self.is_filesystem_error = is_filesystem_error
        super().__init__("invalid locale registry:\n" + "\n".join(f"  - {p}" for p in problems))


def _require_domain_entry(value: str) -> str:
    host, slash, path = value.partition("/")
    if not _HOST.fullmatch(host):
        raise authored_error(
            "locale_domain_invalid",
            "must be a lowercase ASCII hostname, optionally followed by one path prefix",
        )
    if _PUBLIC_SUFFIXES.privatesuffix(host) is None:
        # A bare public suffix ("com", "co.uk") would make every host under it local.
        raise authored_error(
            "locale_domain_not_registrable",
            "must name something registrable, not a bare public suffix",
        )
    if slash and not _PATH_PREFIX.fullmatch("/" + path):
        raise authored_error("locale_domain_invalid", "path prefix must be a plain URL path")
    return value


class LocaleEntry(_StrictModel):
    """One country, or one region of a country, and the publishers based there."""

    code: str
    parent: str | None = None
    triggers: list[str] = Field(min_length=1)
    languages: list[str] = Field(min_length=1)
    cctlds: list[str] = Field(default_factory=list)
    local_domains: list[str] = Field(default_factory=list)
    external_domains: list[str] = Field(default_factory=list)
    native_terms: dict[str, str] = Field(default_factory=dict)

    @field_validator("code", "parent")
    @classmethod
    def _code_shape(cls, value: str | None) -> str | None:
        if value is not None and not _CODE.fullmatch(value):
            raise authored_error(
                "locale_code_invalid", "must be ISO 3166-1 alpha-2, optionally with a region"
            )
        return value

    @field_validator("triggers")
    @classmethod
    def _triggers_are_distinct_text(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for trigger in value:
            if not trigger.strip() or trigger != trigger.strip() or "\x00" in trigger:
                raise authored_error(
                    "locale_trigger_invalid",
                    "every trigger must be non-blank text with no surrounding whitespace",
                )
            key = _normalize(trigger)
            if key in seen:
                raise authored_error(
                    "locale_trigger_duplicate", "triggers must be distinct (case-insensitive)"
                )
            seen.add(key)
        return value

    @field_validator("languages")
    @classmethod
    def _languages_shape(cls, value: list[str]) -> list[str]:
        if any(not _LANGUAGE.fullmatch(code) for code in value):
            raise authored_error("locale_language_invalid", "languages must be ISO 639-1 codes")
        if len(set(value)) != len(value):
            raise authored_error("locale_language_duplicate", "languages must be distinct")
        return value

    @field_validator("cctlds")
    @classmethod
    def _cctlds_shape(cls, value: list[str]) -> list[str]:
        if any(not _CCTLD.fullmatch(code) for code in value):
            raise authored_error("locale_cctld_invalid", "cctlds must be two lowercase letters")
        return value

    @field_validator("local_domains", "external_domains")
    @classmethod
    def _domains_shape(cls, value: list[str]) -> list[str]:
        for entry in value:
            _require_domain_entry(entry)
        if len(set(value)) != len(value):
            raise authored_error("locale_domain_duplicate", "a domain list must not repeat")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> LocaleEntry:
        if self.parent is not None and not self.code.startswith(self.parent + "-"):
            raise authored_error(
                "locale_parent_mismatch", "a region's code must begin with its parent's code"
            )
        if self.parent is None and "-" in self.code:
            raise authored_error("locale_parent_missing", "a region code requires a parent")
        if set(self.local_domains) & set(self.external_domains):
            raise authored_error(
                "locale_domain_conflict",
                "a domain cannot be both local and external to the same locale",
            )
        triggers = {_normalize(trigger) for trigger in self.triggers}
        for trigger, native in self.native_terms.items():
            if _normalize(trigger) not in triggers:
                raise authored_error(
                    "locale_native_term_orphan", "every native term must name one of the triggers"
                )
            if not native.strip() or "\x00" in native:
                raise authored_error("locale_native_term_blank", "native terms must be non-blank")
        return self


class _RegistryFile(_StrictModel):
    schema_version: Literal[1]
    locales: list[LocaleEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _codes_and_parents(self) -> _RegistryFile:
        codes = [entry.code for entry in self.locales]
        if len(set(codes)) != len(codes):
            raise authored_error("locale_code_duplicate", "locale codes must be distinct")
        countries = {entry.code for entry in self.locales if entry.parent is None}
        if any(
            entry.parent is not None and entry.parent not in countries for entry in self.locales
        ):
            raise authored_error(
                "locale_parent_unknown", "a region's parent must be a country in this registry"
            )
        return self


def _normalize(text: str) -> str:
    """The form triggers and question text are compared in: NFKC, then case-folded."""
    return unicodedata.normalize("NFKC", text).casefold()


def _trigger_pattern(trigger: str) -> re.Pattern[str]:
    """Whole-word, whitespace-tolerant pattern for one normalized trigger.

    ``(?<!\\w)`` and ``(?!\\w)`` rather than ``\\b``: ``\\b`` is defined against the
    characters *adjacent to the pattern*, so a trigger that ends in punctuation would never
    match, and one that starts with a digit would match inside a number. The lookarounds say
    what is meant -- no word character touches either end -- and are Unicode-aware, so
    ``Türkiye`` and ``Québec`` behave like ASCII words.
    """
    words = [re.escape(word) for word in _normalize(trigger).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)")


@dataclass(frozen=True)
class LocaleRegistry:
    """A validated registry, its byte identity, and the compiled trigger patterns."""

    locales: tuple[LocaleEntry, ...]
    sha256: str
    _patterns: tuple[tuple[str, tuple[tuple[str, re.Pattern[str]], ...]], ...] = field(
        repr=False, compare=False, default=()
    )

    def get(self, code: str) -> LocaleEntry | None:
        for entry in self.locales:
            if entry.code == code:
                return entry
        return None


def build_registry(data: object, *, sha256: str) -> LocaleRegistry:
    """Validate an already-parsed payload; raises :class:`LocaleError`.

    Separate from :func:`load_registry` so tests and the property suite run exactly the
    validation step without the file I/O around it.
    """
    try:
        parsed = _RegistryFile.model_validate(data)
    except ValidationError as exc:
        raise LocaleError(sanitized_problems(exc, _RegistryFile)) from None
    if type(sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise LocaleError(["registry sha256 must be 64 lowercase hex characters"])
    entries = tuple(entry.model_copy(deep=True) for entry in parsed.locales)
    patterns = tuple(
        (entry.code, tuple((trigger, _trigger_pattern(trigger)) for trigger in entry.triggers))
        for entry in entries
    )
    return LocaleRegistry(locales=entries, sha256=sha256, _patterns=patterns)


def _read_regular_file(path: Path) -> bytes:
    """Read ``path``, refusing anything but a regular file (``allowlist.py``'s reasoning)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise LocaleError(
            [f"cannot read locale registry {path}: {exc.strerror or exc}"],
            is_filesystem_error=True,
        ) from None
    except ValueError:
        # A path that cannot be encoded for the syscall; rendering it is the failure.
        raise LocaleError(
            ["the locale registry path is not a usable filename (path withheld)"],
            is_filesystem_error=True,
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LocaleError(
                [f"locale registry {path} is not a regular file"], is_filesystem_error=True
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except OSError as exc:
        raise LocaleError(
            [f"cannot read locale registry {path}: {exc.strerror or exc}"],
            is_filesystem_error=True,
        ) from None
    finally:
        os.close(fd)


def load_registry(path: Path | str) -> LocaleRegistry:
    """Load and validate ``config/locales.yaml``; raises :class:`LocaleError`."""
    path = Path(path)
    raw = _read_regular_file(path)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LocaleError(
            [f"locale registry {path} is not valid UTF-8 (detail withheld: it can echo content)"]
        ) from None
    try:
        data: Any = yaml.safe_load(text)
    except Exception:
        # Deliberately broad and scoped to this one call: PyYAML's construction stage raises
        # outside its own hierarchy (``allowlist.load_allowlist`` lists the shapes), and
        # several of those messages quote the offending value.
        raise LocaleError(
            [f"locale registry {path} is not valid YAML (detail withheld: it can echo content)"]
        ) from None
    if not isinstance(data, dict):
        raise LocaleError([f"locale registry {path} must contain a YAML mapping at the top level"])
    return build_registry(data, sha256=digest)


@dataclass(frozen=True)
class LocaleMatch:
    """One locale a question is about, and the triggers that said so."""

    code: str
    # Registry spellings, ordered by where each first appears in the question text.
    triggers: tuple[str, ...]


def _question_text(question: CanonicalQuestion) -> str:
    pieces = [question.title, question.group_parent_title, question.resolution_criteria]
    return _normalize("\n".join(piece for piece in pieces if piece))


def detect(question: CanonicalQuestion, registry: LocaleRegistry) -> tuple[LocaleMatch, ...]:
    """Return every locale whose triggers appear in ``question``; raises :class:`LocaleError`.

    Reads the title, group parent title and resolution criteria. Not ``fine_print`` or
    ``background_info``: those routinely name places only as context ("unlike Russia's
    2022 default..."), and a lens call spent on context is money spent on nothing the
    question asks.

    Ordered by the position of each locale's first matched trigger, then by code, so the
    order reflects the question rather than the registry file and is a total order: two
    locales tie on position only when one trigger is listed under both (a strait), and the
    code breaks that tie.
    """
    if not isinstance(question, _CanonicalQuestionBase):
        raise LocaleError(["question must be a canonical question"])
    if not isinstance(registry, LocaleRegistry):
        raise LocaleError(["registry must be a LocaleRegistry"])
    text = _question_text(question)
    found: list[tuple[int, str, tuple[str, ...]]] = []
    for code, patterns in registry._patterns:
        hits: list[tuple[int, str]] = []
        for trigger, pattern in patterns:
            match = pattern.search(text)
            if match is not None:
                hits.append((match.start(), trigger))
        if hits:
            hits.sort()
            found.append((hits[0][0], code, tuple(trigger for _, trigger in hits)))
    found.sort(key=lambda item: (item[0], item[1]))
    return tuple(LocaleMatch(code=code, triggers=triggers) for _, code, triggers in found)


def _host_and_path(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return host_identity(host).rstrip("."), parts.path


def _covers(entry: str, host: str, path: str) -> bool:
    domain, slash, prefix = entry.partition("/")
    if host != domain and not host.endswith("." + domain):
        return False
    if not slash:
        return True
    prefix = "/" + prefix
    return path == prefix or path.startswith(prefix + "/")


def publisher_origin(url: str, locale: LocaleEntry) -> PublisherOrigin:
    """Where ``url``'s publisher sits relative to ``locale``. Total: never raises.

    Precedence is most-specific first. An ``external_domains`` entry beats the ccTLD rule,
    which matters exactly when a foreign service is hosted under the locale's TLD. Explicit
    ``local_domains`` and the ccTLD rule both answer ``local``. Anything else is
    ``unclassified``, including a URL whose host cannot be read.

    ``host_identity`` folds the spellings of one host together (A-labels, a trailing root
    dot, a leading ``www.``), so ``https://www.tehrantimes.com/`` and
    ``https://tehrantimes.com./`` agree.
    """
    if type(url) is not str or not isinstance(locale, LocaleEntry):
        return "unclassified"
    parsed = _host_and_path(url)
    if parsed is None:
        return "unclassified"
    host, path = parsed
    if any(_covers(entry, host, path) for entry in locale.external_domains):
        return "external"
    if any(_covers(entry, host, path) for entry in locale.local_domains):
        return "local"
    if host.rpartition(".")[2] in locale.cctlds:
        return "local"
    return "unclassified"


def origins(url: str, registry: LocaleRegistry) -> Mapping[str, PublisherOrigin]:
    """``url``'s origin for every locale it is not ``unclassified`` against, by code."""
    if not isinstance(registry, LocaleRegistry):
        raise LocaleError(["registry must be a LocaleRegistry"])
    result: dict[str, PublisherOrigin] = {}
    for entry in registry.locales:
        origin = publisher_origin(url, entry)
        if origin != "unclassified":
            result[entry.code] = origin
    return result
