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


def test_padded_domain_string_rejected(tmp_path: Path) -> None:
    # match_domain compares exactly, so " econ_data " matches no question domain.
    path = _write(tmp_path, [_base_entry(domains=[" econ_data "])])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_whitespace_only_username_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_base_entry(username="   ")])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


@pytest.mark.parametrize(
    "username",
    [
        " BLS_gov ",  # round-4 review finding: validated, stored padded, never found again
        "BLS_gov\n",  # a "$"-anchored pattern would accept this one
        "BLS gov",
        "@BLS_gov",
        "BLS\u200bgov",  # zero-width space: not whitespace to str.strip()
        "sixteen_chars_16",
        "",
    ],
)
def test_username_that_is_not_an_x_handle_is_rejected(tmp_path: Path, username: str) -> None:
    """Every one of these would validate, store as written, count as its own entry for
    uniqueness, and then be unfindable by lookup_by_username() -- failing open to the
    unverified_social default for an account the operator believed was tagged."""
    path = _write(tmp_path, [_base_entry(username=username)])
    with pytest.raises(AllowlistError):
        load_allowlist(path)


def test_padded_username_does_not_hide_from_the_uniqueness_check(tmp_path: Path) -> None:
    """The padded form used to be a *distinct* username, so this file loaded clean."""
    path = _write(tmp_path, [_base_entry(username="BLS_gov"), _base_entry(username=" BLS_gov ")])
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


# --- filesystem vs. content classification (round-2 review findings) ---


def test_missing_file_is_classified_as_filesystem_error(tmp_path: Path) -> None:
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(tmp_path / "no-such-file.yaml")
    assert excinfo.value.is_filesystem_error is True


def test_missing_file_error_suppresses_its_cause(tmp_path: Path) -> None:
    """The read failure must not carry a raw OSError as __cause__ into a traceback."""
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(tmp_path / "no-such-file.yaml")
    assert excinfo.value.__cause__ is None


def test_duplicate_username_is_classified_as_content_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_base_entry(username="BLS_gov"), _base_entry(username="bls_gov")],
    )
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert excinfo.value.is_filesystem_error is False


def test_invalid_utf8_is_classified_as_content_error(tmp_path: Path) -> None:
    path = tmp_path / "bad-utf8.yaml"
    path.write_bytes(b"accounts:\n  - username: \xff\xfe\n")
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert excinfo.value.is_filesystem_error is False


# --- secret/value hygiene ---

SECRET = "privateFAKE123456"
# A digits-only secret: an account id, a phone number, a numeric token. YAML parses an
# unquoted numeric key as an int, and pydantic's invalid_key error puts that key in the
# error's loc -- where _sanitize used to trust every int as a list index (round-5 finding 1).
SECRET_INT = 5551234567890


def _leaks(exc: AllowlistError) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    text = str(exc) + rendered
    return SECRET in text or str(SECRET_INT) in text


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

    # Non-string keys, at both levels. A string key is withheld because it is not a known
    # field name; an int key used to be rendered verbatim, because _sanitize could not tell
    # it from a list index.
    for name, payload in (
        ("int-key-top.yaml", {"accounts": [_base_entry()], SECRET_INT: "x"}),
        ("int-key-entry.yaml", {"accounts": [{**_base_entry(), SECRET_INT: "x"}]}),
    ):
        path = tmp_path / name
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        with pytest.raises(AllowlistError) as excinfo:
            load_allowlist(path)
        assert not _leaks(excinfo.value), f"{name} leaked the numeric key"


# --- error-location hygiene (round-5 review finding 1) ---


def test_integer_top_level_key_is_withheld(tmp_path: Path) -> None:
    """pydantic's invalid_key error puts the offending key in loc. For an unquoted numeric
    YAML key that key is an int, which _sanitize used to render as if it were a list index."""
    path = tmp_path / "int-key.yaml"
    path.write_text(yaml.safe_dump({"accounts": [_base_entry()], 987654321: "x"}), encoding="utf-8")
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    problems = excinfo.value.problems
    assert any(p.startswith("<withheld>:") for p in problems), problems
    assert not any("987654321" in p for p in problems), problems


def test_integer_key_inside_an_entry_is_withheld(tmp_path: Path) -> None:
    path = tmp_path / "int-key-nested.yaml"
    path.write_text(
        yaml.safe_dump({"accounts": [{**_base_entry(), 424242: "y"}]}), encoding="utf-8"
    )
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    problems = excinfo.value.problems
    # The path to the entry survives -- only the key itself is withheld.
    assert any(p.startswith("accounts.0.<withheld>:") for p in problems), problems
    assert not any("424242" in p for p in problems), problems


def test_list_indices_still_locate_the_offending_entry(tmp_path: Path) -> None:
    """The reason not to simply withhold every int: a 46-entry file whose only diagnostic
    is "<withheld>: Input should be ..." cannot be acted on. Indices under a list-valued
    field are schema-authored, not file content, and must keep rendering."""
    path = _write(
        tmp_path,
        [_base_entry(), _base_entry(username="OtherOrg", reliability_tag="not_a_real_tag")],
        name="bad-second-entry.yaml",
    )
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert any(p.startswith("accounts.1.reliability_tag:") for p in excinfo.value.problems)

    # And an index into a nested list field (domains: list[str]).
    path = _write(tmp_path, [_base_entry(domains=["econ_data", 17])], name="bad-domain.yaml")
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert any(p.startswith("accounts.0.domains.1:") for p in excinfo.value.problems)
