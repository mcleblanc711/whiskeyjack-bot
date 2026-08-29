"""Typed configuration mirroring config.example.yaml (M0-005).

Design rules enforced here:

- Unknown keys are rejected everywhere (``extra="forbid"``): a typo in config
  must fail at startup, never silently no-op.
- No silent defaults for model identity (D27): placeholder values are rejected
  with an error naming the field.
- No reachable submission path until M2: any configuration that would enable a
  live submission is invalid, in addition to the cross-field invariants that
  hold in every milestone (approval binds to hash, refetch verification).
- Secrets never appear in config: ``*_env`` fields hold environment-variable
  *names*, and validation errors surfaced through :func:`load_config` never
  echo input values, so a mistakenly pasted credential cannot leak through a
  diagnostic.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# prompt.py imports only stdlib, so this stays acyclic. The semver rule lives
# there because M1-401 owns it; duplicating it here is what let the two drift.
from whiskeyjack_bot.prompt import BARE_VERSION_RE

PLACEHOLDER_PREFIX = "REPLACE_WITH"

# The most model invocations one forecast may cost: the initial call plus at most one
# bounded repair (M1-402's acceptance criterion). Lives here because ModelConfig is
# where the value is accepted; forecast.generate imports it rather than restating it,
# so the schema bound and the spending-site refusal cannot drift apart.
MAX_MODEL_INVOCATIONS = 2
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Question types the v1 pipeline supports; date/conditional are deferred (D20/D21).
SupportedQuestionType = Literal["binary", "multiple_choice", "numeric"]

# Verified against forecasting-tools==0.2.92 metaculus_client.GroupQuestionMode.
GroupQuestionMode = Literal["exclude", "unpack_subquestions"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_env_var_name(value: str, field_name: str) -> str:
    if not _ENV_VAR_NAME_RE.fullmatch(value):
        # Deliberately do not include the offending value: if a credential was
        # pasted here by mistake, the diagnostic must not repeat it.
        raise ValueError(
            f"{field_name} must be an UPPER_SNAKE_CASE environment variable "
            "name, not a value (offending input withheld from this message)"
        )
    return value


class TournamentConfig(_StrictModel):
    id: int | str
    use_sdk_current_id: bool = False

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, v: int | str) -> int | str:
        if isinstance(v, str) and not v.strip():
            raise ValueError("tournament id must be a non-empty slug or integer id")
        return v


class MetaculusConfig(_StrictModel):
    base_url: str = "https://www.metaculus.com/api"
    token_env: str = "METACULUS_TOKEN"
    tournament: TournamentConfig
    group_question_mode: GroupQuestionMode = "unpack_subquestions"
    request_timeout_seconds: float = Field(30, gt=0)
    request_spacing_seconds: float = Field(3.5, ge=0)
    request_jitter_seconds: float = Field(1.0, ge=0)

    @field_validator("token_env")
    @classmethod
    def _token_env_is_name(cls, v: str) -> str:
        return _require_env_var_name(v, "metaculus.token_env")


class ModelConfig(_StrictModel):
    provider: str
    name: str
    api_key_env: str
    temperature: float = Field(0.0, ge=0)
    timeout_seconds: float = Field(120, gt=0)
    max_output_tokens: int = Field(6000, gt=0)
    # Total model invocations for one forecast: 1 means no repair, 2 means one
    # initial call plus at most one repair. Bounded at 2 because M1-402's acceptance
    # criterion is a hard upper bound -- "malformed output gets at most one bounded
    # repair attempt" -- and a bound any config can lift is not a bound. The upper
    # limit is here rather than only at the call site so a wrong value fails at load
    # and at verify-env, before a forecast is ever attempted (M1-402 review round 1).
    #
    # The name deliberately matches GeneralLlm's constructor parameter, and that is
    # the footgun this bound closes: there the same word means *transport* retries,
    # so an operator reading "5" as "retry the network five times" would instead buy
    # five billed model calls.
    allowed_tries: int = Field(2, ge=1, le=MAX_MODEL_INVOCATIONS)

    @field_validator("name")
    @classmethod
    def _no_placeholder_model(cls, v: str) -> str:
        if v.startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "model.name is still the placeholder; set a verified "
                "LiteLLM-compatible model name (D27: no silent default)"
            )
        if not v.strip():
            raise ValueError("model.name must be non-empty (D27: no silent default)")
        return v

    @field_validator("api_key_env")
    @classmethod
    def _key_env_is_name(cls, v: str) -> str:
        return _require_env_var_name(v, "model.api_key_env")


class RetrievalProviderConfig(_StrictModel):
    provider: Literal["asknews", "exa"]
    api_key_env: str
    # Transport knobs shared by every general retrieval provider so the Exa
    # fallback (M1-303) inherits the same shape. Defaults match the AskNews
    # SDK's own (retries=3) and the social block's timeout_seconds.
    timeout_seconds: float = Field(60.0, gt=0)
    retries: int = Field(3, ge=0)

    @field_validator("api_key_env")
    @classmethod
    def _key_env_is_name(cls, v: str) -> str:
        return _require_env_var_name(v, "retrieval provider api_key_env")


class SocialRetrievalConfig(_StrictModel):
    """X/Twitter retrieval via the xAI X Search agent (spec: brief § B)."""

    provider: Literal["xai_x_search"]
    enabled: bool = False
    api_key_env: str = "XAI_API_KEY"
    agent_model: str
    temperature: float = Field(0.0, ge=0)
    timeout_seconds: float = Field(60, gt=0)
    account_allowlist_path: Path
    allow_open_search: bool = True
    max_agent_calls_per_question: int = Field(2, ge=1)
    max_posts_per_call: int = Field(25, ge=1)
    est_cost_per_tool_call_usd: float = Field(0.005, ge=0)

    @field_validator("api_key_env")
    @classmethod
    def _key_env_is_name(cls, v: str) -> str:
        return _require_env_var_name(v, "retrieval.social.api_key_env")

    @model_validator(mode="after")
    def _no_placeholder_when_enabled(self) -> SocialRetrievalConfig:
        # The placeholder is tolerated only while the adapter is disabled: the
        # committed example must load, but enabling social retrieval without a
        # verified Grok model name is a D27 violation.
        if self.enabled and self.agent_model.startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "retrieval.social.enabled is true but agent_model is still the "
                "placeholder; verify the current Grok model name at docs.x.ai "
                "(D27: no silent default)"
            )
        return self


class RetrievalConfig(_StrictModel):
    primary: RetrievalProviderConfig
    fallback: RetrievalProviderConfig
    max_queries_per_question: int = Field(6, ge=1)
    max_documents_per_query: int = Field(8, ge=1)
    freshness_days_default: int = Field(30, ge=1)
    retain_raw_responses: bool = True
    replay_saved_research: bool = False
    deduplicate_by: list[Literal["canonical_url", "content_sha256"]] = Field(min_length=1)
    structured_sources_enabled: bool = True
    social: SocialRetrievalConfig


# Spec (CODEX_HANDOFF.md § Configuration schema): 0.001 <= min < max <= 0.999. Named
# here because this is the single source -- ``forecast.binary``, ``forecast.multiple_choice``
# and ``forecast.generate`` all re-check the envelope against these, and a fourth copy of
# the literals is exactly the drift M1-502 was reviewed for.
PROBABILITY_BOUND_FLOOR = 0.001
PROBABILITY_BOUND_CEILING = 0.999


class ForecastConfig(_StrictModel):
    supported_question_types: list[SupportedQuestionType] = Field(min_length=1)
    # Spec (CODEX_HANDOFF.md § Configuration schema): 0.001 <= min < max <= 0.999.
    # The two ends are module constants rather than repeated literals because the
    # forecast checkers and the generation preflight re-derive the same envelope
    # (M1-502): ``model_copy(update=...)`` builds a ForecastConfig without running
    # these validators, so the envelope has to be checkable away from this class.
    min_probability: float = Field(
        PROBABILITY_BOUND_FLOOR, ge=PROBABILITY_BOUND_FLOOR, le=PROBABILITY_BOUND_CEILING
    )
    max_probability: float = Field(
        PROBABILITY_BOUND_CEILING, ge=PROBABILITY_BOUND_FLOOR, le=PROBABILITY_BOUND_CEILING
    )
    # D22: the only legal v1 policy. Community prediction is never a forecaster
    # input; changing this requires a code change, deliberately.
    community_prediction_policy: Literal["log_after_forecast_do_not_use_as_input"]
    replay_saved_model_output: bool = False
    fail_on_stale_research: bool = False
    flag_on_stale_research: bool = True
    prompt_path: Path
    prompt_version: str

    @field_validator("prompt_version")
    @classmethod
    def _bare_semver(cls, v: str) -> str:
        # M1-401: the prompt's H1 carries a 'v' prefix, config does not. Bare is
        # the canonical form -- it is what reaches forecast_records.prompt_version
        # -- so a 'v'-prefixed config value is rejected here rather than becoming
        # a confusing prompt/config mismatch at load time.
        # Shared with prompt.py: ASCII-only digits and no leading zeroes, so a
        # config value and a prompt H1 can never be accepted by different rules.
        if not BARE_VERSION_RE.fullmatch(v):
            raise ValueError(
                "forecast.prompt_version must be a bare MAJOR.MINOR.PATCH version "
                "with no 'v' prefix"
            )
        return v

    @model_validator(mode="after")
    def _probability_bounds_ordered(self) -> ForecastConfig:
        if self.min_probability >= self.max_probability:
            raise ValueError(
                "forecast.min_probability must be strictly below forecast.max_probability"
            )
        return self


class NumericCalibrationConfig(_StrictModel):
    use_forecasting_tools_standardization: bool = True
    # 201 is the verified Metaculus CDF length on forecasting-tools==0.2.92;
    # any other value would produce unsubmittable arrays, so it is a hard
    # error (stricter reading), not a tunable.
    expected_cdf_points: Literal[201]
    max_adjacent_pmf: float = Field(0.2, gt=0, le=1)
    strict_validation: bool = True
    calibration_profile: Literal["identity"]


class SubmissionConfig(_StrictModel):
    enabled: bool = False
    dry_run: bool = True
    no_submit: bool = True
    require_human_approval: bool = True
    approval_must_match_forecast_hash: bool = True
    verify_by_refetch: bool = True
    post_private_reasoning_comment: bool = False
    block_retry_on_uncertain_result: bool = True

    @model_validator(mode="after")
    def _reject_live_submit_combinations(self) -> SubmissionConfig:
        """Refuse a configuration that could post without the guarantees it claims.

        **M2-704 removed three refusals from here and added one.** Until this item there
        was no submission path at all, so ``enabled: true``, ``dry_run: false`` and
        ``no_submit: false`` were each rejected outright -- a configuration describing
        behaviour no code could perform. ``submission_live`` is that code, so the flags now
        mean what they say and the operator may set them.

        Nothing about the *defaults* changed: ``config.example.yaml`` still commits
        ``enabled: false``, ``dry_run: true`` and ``no_submit: true``, and
        ``submission_live._require_live_submission_enabled`` refuses to post unless all
        three are deliberately flipped. Turning on a live submission path is three explicit
        edits, and every safety invariant below still gates it.

        The added refusal is the contradiction the removals made reachable: ``enabled:
        true`` alongside ``dry_run: true`` or ``no_submit: true`` describes a deployment
        that both may and may not post. Resolving that at runtime -- by picking one flag as
        dominant -- would put the answer somewhere no reader of the config can see, so it
        is refused at load instead. This is the *stricter reading* of an ambiguous
        combination, per CLAUDE.md.
        """
        problems: list[str] = []
        # Invariants that hold in every milestone (hard constraints).
        if self.enabled and not self.require_human_approval:
            problems.append("submission.enabled requires require_human_approval: true")
        if self.enabled and not self.approval_must_match_forecast_hash:
            problems.append("submission.enabled requires approval_must_match_forecast_hash: true")
        if self.enabled and not self.verify_by_refetch:
            problems.append("submission.enabled requires verify_by_refetch: true")
        if self.enabled and not self.block_retry_on_uncertain_result:
            problems.append("submission.enabled requires block_retry_on_uncertain_result: true")
        # The combination that has no coherent meaning. `dry_run` and `no_submit` are two
        # independent brakes on one path and either one alone stops it; `enabled: true`
        # with a brake still on is a configuration whose author expected a post.
        if self.enabled and self.dry_run:
            problems.append(
                "submission.enabled: true requires dry_run: false; a run cannot both "
                "submit and rehearse"
            )
        if self.enabled and self.no_submit:
            problems.append(
                "submission.enabled: true requires no_submit: false; no_submit is the "
                "kill switch and it is still on"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class StorageConfig(_StrictModel):
    sqlite_path: Path
    artifact_root: Path
    export_root: Path
    sqlite_wal: bool = True
    retain_raw_model_output: bool = True
    retain_raw_research: bool = True


class LoggingConfig(_StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json"] = "json"
    file: Path
    redact_secrets: Literal[True] = True  # turning redaction off is not a configuration option
    include_provider_costs: bool = True


class RunLimitsConfig(_StrictModel):
    max_questions: int = Field(1, ge=1)
    max_cost_usd: float = Field(10.0, gt=0)
    max_parallel_questions: int = Field(1, ge=1)


class AppConfig(_StrictModel):
    environment: Literal["development", "test", "production"]
    metaculus: MetaculusConfig
    model: ModelConfig
    retrieval: RetrievalConfig
    forecast: ForecastConfig
    numeric_calibration: NumericCalibrationConfig
    submission: SubmissionConfig
    storage: StorageConfig
    logging: LoggingConfig
    run_limits: RunLimitsConfig

    def secret_env_var_names(self) -> list[str]:
        """Every environment variable name that may hold a credential.

        Used by verify-env (M0-004) for presence checks and by the logging
        redaction filter (M0-101) to scrub values; the social key is included
        only when the adapter is enabled.
        """
        names = [
            self.metaculus.token_env,
            self.model.api_key_env,
            self.retrieval.primary.api_key_env,
            self.retrieval.fallback.api_key_env,
        ]
        if self.retrieval.social.enabled:
            names.append(self.retrieval.social.api_key_env)
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(names))


class ConfigError(Exception):
    """Configuration failure with input values withheld from the message.

    Pydantic's own error rendering includes the offending input; for fields
    that could contain a mistakenly pasted credential that is a leak. Every
    consumer (CLI, verify-env) prints this exception, never the raw
    ValidationError.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


def _sanitize_validation_error(exc: ValidationError) -> ConfigError:
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        message = err["msg"]
        problems.append(f"{location}: {message}")
    return ConfigError(problems)


def validate_config_data(data: Any) -> AppConfig:
    """Validate already-parsed config data; raises ConfigError on failure.

    The only sanctioned validation entry point: unlike a bare
    ``AppConfig.model_validate`` call, its errors never echo input values.
    """
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        # from None: a chained __cause__ would re-expose the raw ValidationError
        # (which echoes input values) whenever the ConfigError itself reaches a
        # traceback renderer.
        raise _sanitize_validation_error(exc) from None


def load_config(path: Path | str) -> AppConfig:
    """Load and validate a YAML config file; raises ConfigError on failure."""
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read config file {path}: {exc.strerror or exc}"]) from exc
    try:
        data: Any = yaml.safe_load(raw_text)
    except yaml.MarkedYAMLError as exc:
        # PyYAML's message embeds a snippet of the offending source line, so a
        # credential pasted into the file would be echoed back. Report the
        # position only, and suppress the cause chain for the same reason.
        mark = exc.problem_mark or exc.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(
            [
                f"config file {path} is not valid YAML{where} "
                "(parser detail withheld: it can echo file contents)"
            ]
        ) from None
    except yaml.YAMLError:
        raise ConfigError(
            [
                f"config file {path} is not valid YAML "
                "(parser detail withheld: it can echo file contents)"
            ]
        ) from None
    except Exception:
        # PyYAML's *construction* stage sits outside its own error hierarchy: only the
        # scanner, parser and composer raise YAMLError. Reproduced on the pinned version,
        # every one of these from a one-line config edit -- ``2026-02-30`` raises
        # ValueError("day is out of range for month"), ``!!int abc`` raises
        # ValueError("... 'abc'"), ``!!bool maybe`` raises KeyError('maybe'),
        # ``!!timestamp bogus`` raises AttributeError, and a deeply nested flow sequence
        # raises RecursionError. Two of those carry the offending value in their message,
        # so the detail is withheld for the same reason the MarkedYAMLError branch withholds
        # its source snippet, and ``from None`` keeps the raw exception from reprinting it
        # through a rendered traceback. Same defect and fix as research/allowlist.py
        # (M1-308 round 7); filed here as M0-007 because this hole predates that branch.
        #
        # Deliberately not an enumerated tuple of types: the enumeration belongs to PyYAML,
        # and any shape missing from the list escapes raw. Only ``yaml.safe_load`` is inside
        # the ``try``, so this cannot swallow a failure from any other statement.
        raise ConfigError(
            [
                f"config file {path} is not valid YAML "
                "(parser detail withheld: it can echo file contents)"
            ]
        ) from None
    if not isinstance(data, dict):
        raise ConfigError([f"config file {path} must contain a YAML mapping at the top level"])
    return validate_config_data(data)
