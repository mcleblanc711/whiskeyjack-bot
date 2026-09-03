"""M0-005 acceptance: the example config loads once the model placeholder is
replaced; invalid live-submit combinations and unknown keys are rejected; no
input value ever leaks into a rendered configuration error."""

import copy
import itertools
import traceback
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.config import ConfigError, load_config, validate_config_data

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"


@pytest.fixture()
def example_data() -> dict:
    return yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture()
def valid_data(example_data: dict) -> dict:
    data = copy.deepcopy(example_data)
    data["model"]["name"] = "openrouter/test-model"
    return data


def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def expect_rejection(data: dict, needle: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config_data(data)
    assert needle in str(excinfo.value), (
        f"expected rejection mentioning {needle!r}, got: {excinfo.value}"
    )


# ── the contract itself ──────────────────────────────────────────────────────


def test_example_config_is_rejected_while_placeholder_present(
    tmp_path: Path, example_data: dict
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, example_data))
    assert "model.name" in str(excinfo.value)
    assert "placeholder" in str(excinfo.value)


def test_example_config_loads_after_placeholder_replaced(tmp_path: Path, valid_data: dict) -> None:
    config = load_config(write_config(tmp_path, valid_data))
    assert config.metaculus.tournament.id == "minibench"
    assert config.metaculus.group_question_mode == "unpack_subquestions"
    assert config.submission.enabled is False
    assert config.submission.dry_run is True
    assert config.retrieval.social.enabled is False
    assert config.retrieval.social.provider == "xai_x_search"
    assert config.numeric_calibration.expected_cdf_points == 201


def test_secret_env_var_names_excludes_social_until_enabled(valid_data: dict) -> None:
    config = validate_config_data(valid_data)
    names = config.secret_env_var_names()
    assert names == ["METACULUS_TOKEN", "OPENROUTER_API_KEY", "ASKNEWS_API_KEY", "EXA_API_KEY"]

    valid_data["retrieval"]["social"]["enabled"] = True
    valid_data["retrieval"]["social"]["agent_model"] = "grok-fixture"
    config = validate_config_data(valid_data)
    assert config.secret_env_var_names()[-1] == "XAI_API_KEY"


# ── unknown keys ─────────────────────────────────────────────────────────────


def test_unknown_top_level_key_rejected(valid_data: dict) -> None:
    valid_data["surprise"] = 1
    expect_rejection(valid_data, "surprise")


def test_unknown_nested_key_rejected(valid_data: dict) -> None:
    valid_data["submission"]["auto_submit"] = True
    expect_rejection(valid_data, "auto_submit")


# ── live-submit combinations (M2-704: the path exists; the invariants remain) ─

# Every triple, and the accepted set written out. Enumerating rather than probing three
# cases is M1-308's lesson: that item's startup-validation hole moved every round because
# each round tested the case it had just thought of. A set equality fails on a rule that
# was *removed* as loudly as on one that was added, which a list of positive cases does
# not (M1-501's vacuity lesson).
_ACCEPTED_SUBMISSION_FLAGS = {
    # Submission off: the brakes are irrelevant, so every combination parses.
    (False, False, False),
    (False, False, True),
    (False, True, False),
    (False, True, True),
    # Submission on: both brakes must be off. This is the only accepted `enabled` triple.
    (True, False, False),
}


def test_the_accepted_submission_flag_triples_are_exactly_these(valid_data: dict) -> None:
    """M2-704 removed the pre-M2 refusals and added the contradiction check.

    Before this item all three of `enabled: true`, `dry_run: false` and `no_submit: false`
    were refused outright -- there was no submission path to configure. There is one now,
    so the flags mean what they say; what must not change is that `enabled` still requires
    both brakes off, which is the row this table pins.
    """
    accepted = set()
    for enabled, dry_run, no_submit in itertools.product((False, True), repeat=3):
        data = copy.deepcopy(valid_data)
        data["submission"]["enabled"] = enabled
        data["submission"]["dry_run"] = dry_run
        data["submission"]["no_submit"] = no_submit
        try:
            validate_config_data(data)
        except ConfigError:
            continue
        accepted.add((enabled, dry_run, no_submit))
    assert accepted == _ACCEPTED_SUBMISSION_FLAGS


def test_the_committed_defaults_are_still_the_no_post_combination(example_data: dict) -> None:
    """The shipped config must remain one that cannot post, whatever the validator allows."""
    submission = example_data["submission"]
    assert (submission["enabled"], submission["dry_run"], submission["no_submit"]) == (
        False,
        True,
        True,
    )


def test_enabled_with_a_brake_still_on_names_both_flags(valid_data: dict) -> None:
    valid_data["submission"]["enabled"] = True
    valid_data["submission"]["dry_run"] = True
    valid_data["submission"]["no_submit"] = True
    with pytest.raises(ConfigError) as excinfo:
        validate_config_data(valid_data)
    message = str(excinfo.value)
    assert "dry_run: false" in message
    assert "no_submit: false" in message


def test_the_pre_milestone_refusal_is_gone(valid_data: dict) -> None:
    """The removal is asserted, not just the addition.

    A test suite that only checks what the new rule accepts would still pass if the old
    refusal had been left in place under a different message, and `submit` would then be
    unreachable for a reason no one could see from the config layer.
    """
    valid_data["submission"]["dry_run"] = False
    validate_config_data(valid_data)


def test_enabled_without_human_approval_names_every_violation(valid_data: dict) -> None:
    valid_data["submission"]["enabled"] = True
    valid_data["submission"]["require_human_approval"] = False
    valid_data["submission"]["approval_must_match_forecast_hash"] = False
    with pytest.raises(ConfigError) as excinfo:
        validate_config_data(valid_data)
    message = str(excinfo.value)
    assert "require_human_approval" in message
    assert "approval_must_match_forecast_hash" in message


# ── D27: no silent model defaults ────────────────────────────────────────────


def test_social_placeholder_tolerated_while_disabled(valid_data: dict) -> None:
    assert validate_config_data(valid_data).retrieval.social.enabled is False


def test_social_enabled_with_placeholder_rejected(valid_data: dict) -> None:
    valid_data["retrieval"]["social"]["enabled"] = True
    expect_rejection(valid_data, "agent_model")


def test_social_enabled_with_real_model_accepted(valid_data: dict) -> None:
    valid_data["retrieval"]["social"]["enabled"] = True
    valid_data["retrieval"]["social"]["agent_model"] = "grok-fixture"
    config = validate_config_data(valid_data)
    assert config.retrieval.social.agent_model == "grok-fixture"


# ── bounds and enums ─────────────────────────────────────────────────────────


def test_probability_bounds_must_be_ordered(valid_data: dict) -> None:
    valid_data["forecast"]["min_probability"] = 0.999
    valid_data["forecast"]["max_probability"] = 0.001
    expect_rejection(valid_data, "min_probability")


def test_probability_bounds_clamped_to_spec_range(valid_data: dict) -> None:
    # Spec: 0.001 <= min < max <= 0.999. Cross-review finding 4: values like
    # 0.0001/0.9999 validated under the looser 0 < p < 1 reading.
    valid_data["forecast"]["min_probability"] = 0.0001
    expect_rejection(valid_data, "min_probability")
    valid_data["forecast"]["min_probability"] = 0.001
    valid_data["forecast"]["max_probability"] = 0.9999
    expect_rejection(valid_data, "max_probability")


def test_probability_bounds_accept_exact_spec_boundaries(valid_data: dict) -> None:
    valid_data["forecast"]["min_probability"] = 0.001
    valid_data["forecast"]["max_probability"] = 0.999
    config = validate_config_data(valid_data)
    assert config.forecast.min_probability == 0.001
    assert config.forecast.max_probability == 0.999


def test_the_research_gate_cannot_be_silenced_entirely(valid_data: dict) -> None:
    """M1-504's round-1 finding: with both false, a stale or empty packet would pass
    through neither the ``fail`` branch nor the ``flag`` branch and reach ``validated``
    with nothing recorded -- the acceptance criterion's ``never pass silently`` forbids
    exactly that, so the combination is rejected here rather than left for the gate's
    call sites to invent a silent-by-default behavior for."""
    valid_data["forecast"]["fail_on_stale_research"] = False
    valid_data["forecast"]["flag_on_stale_research"] = False
    expect_rejection(valid_data, "fail_on_stale_research")


@pytest.mark.parametrize(
    ("fail_on_stale_research", "flag_on_stale_research"),
    [(True, False), (False, True), (True, True)],
)
def test_the_research_gate_accepts_every_other_combination(
    valid_data: dict, fail_on_stale_research: bool, flag_on_stale_research: bool
) -> None:
    valid_data["forecast"]["fail_on_stale_research"] = fail_on_stale_research
    valid_data["forecast"]["flag_on_stale_research"] = flag_on_stale_research
    config = validate_config_data(valid_data)
    assert config.forecast.fail_on_stale_research == fail_on_stale_research
    assert config.forecast.flag_on_stale_research == flag_on_stale_research


def test_cdf_points_other_than_201_rejected(valid_data: dict) -> None:
    valid_data["numeric_calibration"]["expected_cdf_points"] = 200
    expect_rejection(valid_data, "numeric_calibration.expected_cdf_points")


def test_unsupported_question_type_rejected(valid_data: dict) -> None:
    valid_data["forecast"]["supported_question_types"].append("date")
    expect_rejection(valid_data, "supported_question_types")


def test_community_prediction_policy_is_locked(valid_data: dict) -> None:
    valid_data["forecast"]["community_prediction_policy"] = "use_as_prior"
    expect_rejection(valid_data, "community_prediction_policy")


def test_redaction_cannot_be_disabled(valid_data: dict) -> None:
    valid_data["logging"]["redact_secrets"] = False
    expect_rejection(valid_data, "redact_secrets")


def test_group_question_mode_must_match_sdk_literal(valid_data: dict) -> None:
    valid_data["metaculus"]["group_question_mode"] = "flatten"
    expect_rejection(valid_data, "group_question_mode")


@pytest.mark.parametrize(
    "value",
    [
        "v1.1.0",
        "1.1",
        "1.1.0-rc1",
        "latest",
        "",
        "01.1.0",  # leading zero is not canonical SemVer
        "1.01.0",
        "١.١.٠",  # Unicode digits: \d matched these before re.ASCII
        "1.1.0\n",  # terminal newline
    ],
)
def test_prompt_version_must_be_bare_semver(valid_data: dict, value: str) -> None:
    """M1-401: config holds the bare form; the 'v' prefix lives only in the
    prompt's H1, so a prefixed config value is rejected at load.

    Shares one compiled pattern with prompt.py -- these two checks accepting
    different strings is how a config value and a prompt H1 silently disagree.
    """
    valid_data["forecast"]["prompt_version"] = value
    expect_rejection(valid_data, "prompt_version")


# ── secret safety in diagnostics ─────────────────────────────────────────────


def test_pasted_secret_value_never_appears_in_error(valid_data: dict) -> None:
    fake_secret = "sk-or-v1-0123456789abcdef-FAKE"
    valid_data["model"]["api_key_env"] = fake_secret
    with pytest.raises(ConfigError) as excinfo:
        validate_config_data(valid_data)
    message = str(excinfo.value)
    assert fake_secret not in message
    assert "environment variable" in message
    # The full traceback rendering must also be clean: a chained __cause__
    # would reprint pydantic's error, input values included.
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert fake_secret not in rendered


def test_malformed_yaml_error_withholds_file_content(tmp_path: Path) -> None:
    # Cross-review finding 2: PyYAML's message quotes the offending source
    # line, so a pasted credential next to a syntax error would be echoed.
    fake_secret = "sk-or-v1-0123456789abcdef-FAKE"
    bad = tmp_path / "bad.yaml"
    bad.write_text(f'model:\n  api_key_env: "{fake_secret}\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(bad)
    message = str(excinfo.value)
    assert "is not valid YAML" in message
    assert "line" in message  # position survives so the file is still debuggable
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert fake_secret not in rendered


def test_yaml_and_missing_file_errors_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("just a string", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


# --- YAML that parses but cannot be *constructed* (M0-007, M1-308 round-7 finding) ---
#
# The tests above exercise scanner/parser errors, which are the only kind that arrive as a
# YAMLError. PyYAML's construction stage raises whatever Python raised at it, so all six
# shapes below escaped load_config raw -- as ValueError, KeyError, AttributeError and
# RecursionError. Two of them carry the offending value in their message, so an operator's
# pasted secret would reach the terminal via an unhandled traceback.


def _raw_source(**fields: str) -> str:
    """A one-line config document, written as YAML *text* so tags and scalars survive.

    ``write_config`` cannot be used here: it round-trips through ``yaml.safe_dump``, which
    quotes and escapes exactly the shapes these cases depend on. The document never reaches
    schema validation -- construction fails inside ``yaml.safe_load`` regardless of which
    top-level key holds the bad value -- so a single-key document is enough.
    """
    entry = {"environment": "development"}
    entry.update(fields)
    return "".join(f"{key}: {value}\n" for key, value in entry.items())


CONSTRUCTOR_FAILURES = {
    "implicit date, day out of range": _raw_source(environment="2026-02-30"),
    "implicit timestamp, minute out of range": _raw_source(environment="2026-01-01 12:60:00"),
    "explicit !!bool with an unparseable scalar": _raw_source(environment="!!bool maybe"),
    "explicit !!int with an unparseable scalar": _raw_source(environment="!!int abc"),
    "explicit !!timestamp with an unparseable scalar": _raw_source(environment="!!timestamp bogus"),
    "flow nesting deeper than the recursion limit": _raw_source(
        environment="[" * 2000 + "]" * 2000,
    ),
}


@pytest.mark.parametrize("source", CONSTRUCTOR_FAILURES.values(), ids=CONSTRUCTOR_FAILURES)
def test_yaml_constructor_failure_arrives_as_a_config_error(tmp_path: Path, source: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "not valid YAML" in str(excinfo.value)
    # from None -- the raw exception must not reprint the value through a traceback.
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize("source", CONSTRUCTOR_FAILURES.values(), ids=CONSTRUCTOR_FAILURES)
def test_each_constructor_case_still_escapes_pyyaml_untranslated(source: str) -> None:
    """Guard against the suite above going vacuous.

    Each case earns its place only while ``yaml.safe_load`` really does raise something
    outside its own hierarchy for it. If a future PyYAML turns one of these into a
    ``YAMLError`` -- or accepts it -- that case stops testing the new branch and starts
    testing one of the two that were already there, silently.
    """
    with pytest.raises(Exception) as raw:  # noqa: B017 -- the point is that it is untyped
        yaml.safe_load(source)
    assert not isinstance(raw.value, yaml.YAMLError)


def test_a_valid_implicit_date_is_still_a_schema_error(tmp_path: Path) -> None:
    """The new branch translates construction failures; it must not mask valid ones.

    ``2026-02-28`` constructs cleanly into a ``datetime.date``, so it has to reach
    pydantic and be rejected there as a schema mismatch -- with a schema message, not
    the YAML one.
    """
    path = tmp_path / "config.yaml"
    path.write_text(_raw_source(environment="2026-02-28"), encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "not valid YAML" not in str(excinfo.value)
