"""M1-308 acceptance: the account allowlist loads clean, rejects duplicate usernames and
unknown reliability tags at load time, and domain matching selects the right subset."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.research.allowlist import AllowlistEntry, AllowlistError, load_allowlist

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = REPO_ROOT / "config" / "x_accounts.yaml"

ECON_DATA_USERNAMES = {
    "BLS_gov",
    "BEA_News",
    "stlouisfed",
    "StatCan_eng",
    "ONS",
    "EU_Eurostat",
    "IMFNews",
    "EIAgov",
    "Reuters",
}
SPACE_LAUNCH_USERNAMES = {"NASA", "SpaceX", "esa", "RocketLab", "ulalaunch"}


def _base_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "username": "SomeOrg",
        "display_name": "Some Organization",
        "reliability_tag": "official_primary",
        "domains": ["econ_data"],
    }
    entry.update(overrides)
    return entry


def _write(tmp_path: Path, accounts: list[dict[str, Any]], name: str = "accounts.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump({"accounts": accounts}), encoding="utf-8")
    return path


# --- acceptance criterion 1: loads clean ---


def test_real_file_loads_clean() -> None:
    allowlist = load_allowlist(ALLOWLIST_PATH)
    assert len(allowlist.entries) == 46


# --- acceptance criterion 3: domain matching ---


def test_match_domain_econ_data() -> None:
    allowlist = load_allowlist(ALLOWLIST_PATH)
    matched = {entry.username for entry in allowlist.match_domain("econ_data")}
    assert matched == ECON_DATA_USERNAMES


def test_match_domain_space_launch() -> None:
    allowlist = load_allowlist(ALLOWLIST_PATH)
    matched = {entry.username for entry in allowlist.match_domain("space_launch")}
    assert matched == SPACE_LAUNCH_USERNAMES


def test_match_domain_no_match_returns_empty() -> None:
    allowlist = load_allowlist(ALLOWLIST_PATH)
    assert allowlist.match_domain("not_a_real_domain") == ()


def test_match_domain_preserves_file_order() -> None:
    allowlist = load_allowlist(ALLOWLIST_PATH)
    matched = allowlist.match_domain("space_launch")
    expected_order = [e.username for e in allowlist.entries if e.username in SPACE_LAUNCH_USERNAMES]
    assert [e.username for e in matched] == expected_order


def test_lookup_by_username_case_insensitive() -> None:
    allowlist = load_allowlist(ALLOWLIST_PATH)
    found = allowlist.lookup_by_username("bls_gov")
    assert found is not None
    assert found.username == "BLS_gov"
    assert allowlist.lookup_by_username("does-not-exist") is None


# --- acceptance criterion 2: duplicates and unknown tags fail validation ---


def test_duplicate_username_case_insensitive_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_base_entry(username="BLS_gov"), _base_entry(username="bls_gov")],
    )
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_unknown_reliability_tag_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_base_entry(reliability_tag="not_a_real_tag")])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_unknown_entry_key_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_base_entry(verified=True)])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_empty_domains_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_base_entry(domains=[])])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_blank_domain_string_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_base_entry(domains=["  "])])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_whitespace_only_username_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_base_entry(username="   ")])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_invalid_utf8_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad-utf8.yaml"
    path.write_bytes(b"accounts:\n  - username: \xff\xfe\n")
    with pytest.raises(AllowlistError, match="not valid UTF-8"):
        load_allowlist(path)


def test_malformed_yaml_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("accounts: [{unbalanced: [", encoding="utf-8")
    with pytest.raises(AllowlistError, match="not valid YAML"):
        load_allowlist(path)


def test_non_mapping_top_level_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(AllowlistError):
        load_allowlist(tmp_path / "no-such-file.yaml")


# --- secret/value hygiene ---

SECRET = "privateFAKE123456"


def _leaks(exc: AllowlistError) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SECRET in str(exc) or SECRET in rendered


def test_no_field_leaks_a_planted_secret_through_any_message(tmp_path: Path) -> None:
    """Plant the secret in every field, one at a time; any that reject it must not echo it."""
    for field in AllowlistEntry.model_fields:
        for planted in (SECRET, [SECRET], {SECRET: SECRET}):
            path = _write(tmp_path, [_base_entry(**{field: planted})], name=f"{field}.yaml")
            try:
                load_allowlist(path)
            except AllowlistError as exc:
                assert not _leaks(exc), f"AllowlistEntry.{field} leaked {planted!r}"

    # Duplicate-username collision: the message must name indices, not the username.
    path = _write(
        tmp_path,
        [_base_entry(username=SECRET), _base_entry(username=SECRET)],
        name="dup.yaml",
    )
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert not _leaks(excinfo.value)

    # Unknown top-level key.
    path = tmp_path / "extra-key.yaml"
    path.write_text(yaml.safe_dump({"accounts": [_base_entry()], SECRET: SECRET}), encoding="utf-8")
    try:
        load_allowlist(path)
    except AllowlistError as exc:
        assert not _leaks(exc)
