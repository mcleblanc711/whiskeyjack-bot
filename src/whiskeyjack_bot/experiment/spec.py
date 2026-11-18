"""A replay arm: one strict, hashed specification of a controlled change (M5-806, D62).

An arm names everything about a replay forecast that is not the stored evidence: the model, its
reasoning effort, the prompt (by path, version and sha256), one packet transform with its
parameters, the number of samples, and which resolved questions it runs over. Its canonical
sha256 is stored on every row the arm writes, so two rows are comparable exactly when they carry
the same hash.

**The hash covers the whole validated spec**, the prompt path and the label included. Any edit
to the file is a new arm; spec files are committed, so paths are stable, and an arm that moved
its prompt is honestly a different run. It is taken over the *validated* model, so two spellings
of one value (``0.50`` and ``0.5``) are one arm. Lists that are sets in meaning -- providers,
classes, question types, tournaments, record ids -- must be written sorted and without repeats,
so one arm has one spelling and one hash.

**JSON, not YAML.** PyYAML's construction stage raises outside its own error hierarchy, which
cost M1-308 and M0-007 a round each; an arm spec is small and machine-written, so the narrower
parser is the better trade. NaN, Infinity and an overflowing number are refused.

**The model must have a registered in-service instant** (``forecast.priced.MODEL_IN_SERVICE``):
the leakage rule of D62 cannot be applied to a model with no date, so such a model is refused
here rather than later.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator

from whiskeyjack_bot.config import AppConfig, SupportedQuestionType, _StrictModel
from whiskeyjack_bot.experiment.errors import ExperimentError
from whiskeyjack_bot.forecast.priced import MODEL_IN_SERVICE, PRICED_MODELS, ReasoningEffort
from whiskeyjack_bot.prompt import BARE_VERSION_RE, LoadedPrompt, PromptError, load_prompt
from whiskeyjack_bot.research.model import RetrievalProvider
from whiskeyjack_bot.validation_errors import authored_error, sanitized_problems

ARM_SPEC_SCHEMA_VERSION: Final = "1.0.0"

# The most samples one arm may draw per record. A0 needs three and A2 six; ten bounds what one
# typo in a spec file can multiply the arm's cost by.
MAX_SAMPLES: Final = 10

# The hard maximum for one arm's spending ceiling, in USD (owner decision, 2026-11-17). The
# ceiling given on the command line is cumulative over every run of the arm and is refused above
# this. A code constant rather than configuration for the reason MAX_ACTIVATION_BUDGET_USD is
# one: the limit must not live in a file the operator edits to authorize the spend. 400 covers
# A2, the six-subset arm (about USD 340 over the population).
MAX_EXPERIMENT_CEILING_USD: Final = 400

# The largest spec file read, in bytes. A real spec is under 2 KB.
_MAX_SPEC_BYTES: Final = 65_536

_LABEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}", re.ASCII)
_RECORD_ID_RE: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,199}", re.ASCII)

Seed = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Label = Annotated[str, Field(strict=True, min_length=1, max_length=64)]


class _SpecModel(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_sorted_unique(values: tuple[Any, ...]) -> tuple[Any, ...]:
    if list(values) != sorted(set(values)):
        raise authored_error(
            "arm_spec_set_not_canonical",
            "a set-valued arm spec field must be written sorted and without repeats",
        )
    return values


def _require_label(value: str) -> str:
    if _LABEL_RE.fullmatch(value) is None:
        raise authored_error(
            "arm_spec_label_invalid",
            "an arm spec label must be 1 to 64 letters, digits, dots, dashes or underscores",
        )
    return value


class IdentityTransform(_SpecModel):
    """The full packet, unchanged: the control (A0)."""

    kind: Literal["identity"]


class NoResearchTransform(_SpecModel):
    """No research documents at all: the question alone (A1)."""

    kind: Literal["no_research"]


class DropProviderTransform(_SpecModel):
    """Every document retrieved by one of ``providers`` removed (A6)."""

    kind: Literal["drop_provider"]
    providers: tuple[RetrievalProvider, ...] = Field(min_length=1)

    @field_validator("providers")
    @classmethod
    def _canonical(cls, value: tuple[RetrievalProvider, ...]) -> tuple[RetrievalProvider, ...]:
        return _require_sorted_unique(value)


class MaskPublisherTransform(_SpecModel):
    """Publisher names and URL hosts replaced by neutral per-packet tokens (A3)."""

    kind: Literal["mask_publisher"]


class ShuffleOrderTransform(_SpecModel):
    """The full packet in an order fixed by ``seed``, the question and the sample index (A5)."""

    kind: Literal["shuffle_order"]
    seed: Seed


class RandomSubsetTransform(_SpecModel):
    """Each document (or source cluster) kept independently with ``inclusion_probability`` (A2).

    The sample index is the subset index, so an arm with six samples draws six subsets per
    question. A subset that keeps nothing is allowed (SOURCE-ALPHA A2).
    """

    kind: Literal["random_subset"]
    seed: Seed
    inclusion_probability: float = Field(strict=True, gt=0.0, lt=1.0)
    unit: Literal["document", "cluster"]
    labeller: Label | None = None

    @field_validator("labeller")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return None if value is None else _require_label(value)


class DropClusterTransform(_SpecModel):
    """Leave one source cluster out: sample ``k`` drops the ``k``-th cluster of the packet."""

    kind: Literal["drop_cluster"]
    labeller: Label

    @field_validator("labeller")
    @classmethod
    def _label(cls, value: str) -> str:
        return _require_label(value)


class DropPublisherClassTransform(_SpecModel):
    """Every document whose publisher class is one of ``classes`` removed."""

    kind: Literal["drop_publisher_class"]
    labeller: Label
    classes: tuple[Label, ...] = Field(min_length=1)

    @field_validator("labeller")
    @classmethod
    def _label(cls, value: str) -> str:
        return _require_label(value)

    @field_validator("classes")
    @classmethod
    def _canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _require_label(item)
        return _require_sorted_unique(value)


class CollapseSyndicationTransform(_SpecModel):
    """One document kept per source cluster, the one with the lowest source id (A4)."""

    kind: Literal["collapse_syndication"]
    labeller: Label

    @field_validator("labeller")
    @classmethod
    def _label(cls, value: str) -> str:
        return _require_label(value)


Transform = Annotated[
    IdentityTransform
    | NoResearchTransform
    | DropProviderTransform
    | MaskPublisherTransform
    | ShuffleOrderTransform
    | RandomSubsetTransform
    | DropClusterTransform
    | DropPublisherClassTransform
    | CollapseSyndicationTransform,
    Field(discriminator="kind"),
]


class PromptRef(_SpecModel):
    """The prompt an arm is run under, by path (relative to the spec file), version and hash."""

    path: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    version: Annotated[str, Field(strict=True, min_length=1, max_length=32)]
    sha256: Annotated[str, Field(strict=True, min_length=64, max_length=64)]

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if BARE_VERSION_RE.fullmatch(value) is None:
            raise authored_error(
                "arm_spec_prompt_version_invalid",
                "the arm prompt version must be a bare MAJOR.MINOR.PATCH version",
            )
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        # Review round 1, B1. A NUL is valid JSON and valid text, and no filesystem path can
        # hold one: ``Path.resolve`` raises a bare ``ValueError`` on it. Refused here, as the
        # spec's own error, rather than discovered later as a traceback.
        if "\x00" in value:
            raise authored_error(
                "arm_spec_prompt_path_nul",
                "the arm prompt path must not contain a NUL character",
            )
        return value

    @field_validator("sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise authored_error(
                "arm_spec_prompt_sha256_invalid",
                "the arm prompt sha256 must be 64 lowercase hexadecimal characters",
            )
        return value


class Selection(_SpecModel):
    """Which of the population's records an arm runs over, applied in record order."""

    question_types: tuple[SupportedQuestionType, ...] | None = Field(default=None, min_length=1)
    tournaments: tuple[Label, ...] | None = Field(default=None, min_length=1)
    record_ids: tuple[Annotated[str, Field(strict=True)], ...] | None = Field(
        default=None, min_length=1
    )
    limit: Annotated[int, Field(strict=True, ge=1)] | None = None

    @field_validator("question_types")
    @classmethod
    def _types(
        cls, value: tuple[SupportedQuestionType, ...] | None
    ) -> tuple[SupportedQuestionType, ...] | None:
        return None if value is None else _require_sorted_unique(value)

    @field_validator("tournaments")
    @classmethod
    def _tournaments(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        for item in value:
            _require_label(item)
        return _require_sorted_unique(value)

    @field_validator("record_ids")
    @classmethod
    def _records(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if any(_RECORD_ID_RE.fullmatch(item) is None for item in value):
            raise authored_error(
                "arm_spec_record_id_invalid",
                "an arm spec record id must be 1 to 200 letters, digits, dots, dashes or "
                "underscores",
            )
        return _require_sorted_unique(value)


class ArmSpec(_SpecModel):
    """One replay arm (M5-806, AC2)."""

    spec_schema_version: Literal["1.0.0"]
    name: Label
    model: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    reasoning_effort: ReasoningEffort
    prompt: PromptRef
    transform: Transform
    samples: Annotated[int, Field(strict=True, ge=1, le=MAX_SAMPLES)]
    selection: Selection

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _require_label(value)

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        if value not in PRICED_MODELS:
            raise authored_error(
                "arm_spec_model_unpriced", "the arm model is not a registered priced model"
            )
        if value not in MODEL_IN_SERVICE:
            raise authored_error(
                "arm_spec_model_no_service_date",
                "the arm model has no registered in-service instant, so the leakage rule "
                "cannot be applied to it",
            )
        return value


def canonical_spec_json(spec: ArmSpec) -> str:
    """The persisted form an arm is hashed over: validated, then canonical JSON."""
    if not isinstance(spec, ArmSpec):
        raise ExperimentError("an arm spec must be an ArmSpec")
    return json.dumps(
        spec.model_dump(mode="json", warnings=False),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def arm_sha256(spec: ArmSpec) -> str:
    """The arm's identity: the sha256 of :func:`canonical_spec_json`."""
    return hashlib.sha256(canonical_spec_json(spec).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class LoadedArmSpec:
    """A validated arm, the file it came from, its identity, and its prompt's resolved path.

    ``prompt_path`` is the spec's prompt path resolved against the spec file's directory, not
    the working one. It is resolved once, by :func:`load_arm_spec`, inside that function's
    refusal handling: resolving it lazily put ``Path.resolve`` -- which raises ``RuntimeError``
    on a symlink loop -- on every caller's path, outside any handler (review round 1, B1).
    """

    path: Path
    spec: ArmSpec
    spec_json: str
    sha256: str
    prompt_path: Path


def _refuse_constant(token: str) -> object:
    raise ValueError("non-finite number")


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def parse_arm_spec(data: object) -> ArmSpec:
    """Validate an already-parsed spec; refuses as :class:`ExperimentError`, values withheld."""
    if type(data) is not dict:
        raise ExperimentError("an arm spec must be a JSON object at the top level")
    try:
        return ArmSpec.model_validate(data)
    except ValidationError as exc:
        raise ExperimentError(
            "arm spec is invalid (" + "; ".join(sanitized_problems(exc, ArmSpec)) + ")"
        ) from None


def load_arm_spec(path: Path) -> LoadedArmSpec:
    """Read, parse and validate the arm spec at ``path``; refuses as :class:`ExperimentError`."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_SPEC_BYTES + 1)
    except (OSError, ValueError):
        raise ExperimentError(f"cannot read arm spec {path}") from None
    if len(raw) > _MAX_SPEC_BYTES:
        raise ExperimentError(f"arm spec {path} is larger than {_MAX_SPEC_BYTES} bytes")
    try:
        data: object = json.loads(
            raw.decode("utf-8"), parse_constant=_refuse_constant, parse_float=_finite_float
        )
    except (ValueError, RecursionError):
        # UnicodeDecodeError is a ValueError. Neither message is rendered: both can quote the
        # file's bytes.
        raise ExperimentError(
            f"arm spec {path} is not valid UTF-8 JSON (parser detail withheld)"
        ) from None
    spec = parse_arm_spec(data)
    try:
        resolved = path.resolve()
        prompt_path = (resolved.parent / spec.prompt.path).resolve()
    except (OSError, RuntimeError, ValueError):
        # RuntimeError is a symlink loop (Python 3.11's non-strict resolve); ValueError a path
        # the platform cannot represent. Neither message is rendered beyond the path.
        raise ExperimentError(
            f"cannot resolve the prompt path of arm spec {path} (a symlink loop, or a path "
            "the filesystem cannot represent)"
        ) from None
    return LoadedArmSpec(
        path=resolved,
        spec=spec,
        spec_json=canonical_spec_json(spec),
        sha256=arm_sha256(spec),
        prompt_path=prompt_path,
    )


def load_arm_prompt(loaded: LoadedArmSpec, config: AppConfig) -> LoadedPrompt:
    """Load the arm's prompt through the one prompt loader, and require its declared sha256.

    ``load_prompt`` checks the declared version and the probability bounds against ``config``;
    this adds the hash, so the bytes the arm runs under are the bytes the spec names.
    """
    try:
        prompt = load_prompt(
            loaded.prompt_path,
            loaded.spec.prompt.version,
            min_probability=config.forecast.min_probability,
            max_probability=config.forecast.max_probability,
        )
    except PromptError as exc:
        # PromptError's own messages are value-free and render only the path.
        raise ExperimentError(f"arm prompt refused: {exc}") from None
    if prompt.sha256 != loaded.spec.prompt.sha256:
        raise ExperimentError(
            f"arm prompt {loaded.prompt_path} does not match the sha256 the arm spec names"
        )
    return prompt
