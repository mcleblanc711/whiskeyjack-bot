"""M0-005 acceptance: the example config loads once the model placeholder is
replaced; invalid live-submit combinations and unknown keys are rejected; no
input value ever leaks into a rendered configuration error."""

import copy
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


def test_example_config_loads_after_placeholder_replaced(
    tmp_path: Path, valid_data: dict
) -> None:
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


# ── live-submit combinations (all invalid before M2) ─────────────────────────


def test_submission_enabled_rejected(valid_data: dict) -> None:
    valid_data["submission"]["enabled"] = True
    expect_rejection(valid_data, "Milestone 2")


def test_dry_run_false_rejected(valid_data: dict) -> None:
    valid_data["submission"]["dry_run"] = False
    expect_rejection(valid_data, "dry_run")


def test_no_submit_false_rejected(valid_data: dict) -> None:
    valid_data["submission"]["no_submit"] = False
    expect_rejection(valid_data, "no_submit")


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


# ── secret safety in diagnostics ─────────────────────────────────────────────


def test_pasted_secret_value_never_appears_in_error(valid_data: dict) -> None:
    fake_secret = "sk-or-v1-0123456789abcdef-FAKE"
    valid_data["model"]["api_key_env"] = fake_secret
    with pytest.raises(ConfigError) as excinfo:
        validate_config_data(valid_data)
    message = str(excinfo.value)
    assert fake_secret not in message
    assert "environment variable" in message


def test_yaml_and_missing_file_errors_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("just a string", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
