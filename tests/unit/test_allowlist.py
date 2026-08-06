"""M1-308 acceptance: the account allowlist loads clean, rejects duplicate usernames and
unknown reliability tags at load time, and domain matching selects the right subset."""

from __future__ import annotations

import os
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


# --- YAML that parses but cannot be *constructed* (round-7 review finding) ---
#
# The test above exercises a scanner/parser error, which is the only kind that arrives as a
# YAMLError. PyYAML's construction stage raises whatever Python raised at it, so all six
# shapes below escaped load_allowlist raw -- as ValueError, KeyError, AttributeError and
# RecursionError -- and took `whiskeyjack-bot verify-env` down with an unhandled traceback
# under the committed default `retrieval.social.enabled: false`.


def _raw_source(**fields: str) -> str:
    """One otherwise-valid entry, written as YAML *text* so tags and scalars survive.

    ``_write`` cannot be used here: it round-trips through ``yaml.safe_dump``, which quotes
    and escapes exactly the shapes these cases depend on.
    """
    entry = {
        "username": "SomeOrg",
        "display_name": "Some Organization",
        "reliability_tag": "official_primary",
        "domains": "[econ_data]",
    }
    entry.update(fields)
    return "accounts:\n  -\n" + "".join(f"    {key}: {value}\n" for key, value in entry.items())


CONSTRUCTOR_FAILURES = {
    "implicit date, day out of range": _raw_source(display_name="2026-02-30"),
    "implicit timestamp, minute out of range": _raw_source(display_name="2026-01-01 12:60:00"),
    "explicit !!bool with an unparseable scalar": _raw_source(notes="!!bool maybe"),
    "explicit !!int with an unparseable scalar": _raw_source(notes="!!int abc"),
    "explicit !!timestamp with an unparseable scalar": _raw_source(notes="!!timestamp bogus"),
    "flow nesting deeper than the recursion limit": _raw_source(
        domains="[" * 2000 + "]" * 2000,
    ),
}


@pytest.mark.parametrize("source", CONSTRUCTOR_FAILURES.values(), ids=CONSTRUCTOR_FAILURES)
def test_yaml_constructor_failure_arrives_as_an_allowlist_error(
    tmp_path: Path, source: str
) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert "not valid YAML" in str(excinfo.value)
    # A content error, not a filesystem one: cli routes it to EXIT_CONFIG_INVALID.
    assert excinfo.value.is_filesystem_error is False
    # from None -- the raw exception must not reprint the value through a traceback.
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize("source", CONSTRUCTOR_FAILURES.values(), ids=CONSTRUCTOR_FAILURES)
def test_each_constructor_case_still_escapes_pyyaml_untranslated(source: str) -> None:
    """Guard against the suite above going vacuous.

    Each case earns its place only while ``yaml.safe_load`` really does raise something
    outside its own hierarchy for it. If a future PyYAML turns one of these into a
    ``YAMLError`` -- or accepts it -- that case stops testing the new branch and starts
    testing one of the two that were already there, silently. (The round-6 permission test
    had become a test of nothing this way.)
    """
    with pytest.raises(Exception) as raw:  # noqa: B017 -- the point is that it is untyped
        yaml.safe_load(source)
    assert not isinstance(raw.value, yaml.YAMLError)


def test_a_valid_implicit_timestamp_is_still_a_schema_error(tmp_path: Path) -> None:
    """The new branch translates construction failures; it must not mask valid ones.

    ``2026-02-28`` constructs cleanly into a ``datetime.date``, so it has to reach pydantic
    and be rejected there as not-a-string -- with a schema message, not the YAML one.
    """
    path = tmp_path / "accounts.yaml"
    path.write_text(_raw_source(display_name="2026-02-28"), encoding="utf-8")

    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    rendered = str(excinfo.value)
    assert "not valid YAML" not in rendered
    assert "accounts.0.display_name" in rendered
    assert excinfo.value.is_filesystem_error is False


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


# --- non-regular targets (round-6 review finding) ---
#
# A FIFO at the allowlist path used to block load_allowlist forever: opening a pipe for
# reading waits for a writer. Everything below runs under the `deadline` fixture, because
# a regression here hangs rather than fails.

pytestmark_unix = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="POSIX special files are not available on this platform"
)


@pytestmark_unix
def test_fifo_is_rejected_rather_than_read(tmp_path: Path, deadline: None) -> None:
    """The load must *return* -- on the pre-fix code this call never came back."""
    path = tmp_path / "accounts.yaml"
    os.mkfifo(path)
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert "not a regular file" in str(excinfo.value)


@pytestmark_unix
def test_fifo_is_classified_as_filesystem_error(tmp_path: Path, deadline: None) -> None:
    """Not a content error: there is no content here to have failed validation."""
    path = tmp_path / "accounts.yaml"
    os.mkfifo(path)
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert excinfo.value.is_filesystem_error is True


@pytestmark_unix
def test_fifo_error_suppresses_its_cause(tmp_path: Path, deadline: None) -> None:
    path = tmp_path / "accounts.yaml"
    os.mkfifo(path)
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert excinfo.value.__cause__ is None


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="no /dev/zero on this platform")
def test_character_device_is_rejected_rather_than_read(tmp_path: Path, deadline: None) -> None:
    """The other half of the hazard: /dev/zero reads forever without ever blocking, so a
    size check or a timeout would not catch it -- only the file-type check does."""
    path = tmp_path / "accounts.yaml"
    path.symlink_to("/dev/zero")
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    assert excinfo.value.is_filesystem_error is True
    assert "not a regular file" in str(excinfo.value)


@pytestmark_unix
def test_non_regular_message_names_only_the_path(tmp_path: Path, deadline: None) -> None:
    """The path is this project's one carve-out; what the object *is* stays unsaid."""
    path = tmp_path / "accounts.yaml"
    os.mkfifo(path)
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)
    rendered = str(excinfo.value)
    for leaked in ("fifo", "FIFO", "pipe", "S_ISREG", "st_mode"):
        assert leaked not in rendered


def test_symlink_to_a_regular_file_still_loads(tmp_path: Path, deadline: None) -> None:
    """The guard rejects non-regular *targets*, not indirection: a config file reached
    through a symlink is ordinary, and breaking it would be a regression of its own."""
    real = tmp_path / "real_accounts.yaml"
    real.write_text(ALLOWLIST_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "linked_accounts.yaml"
    link.symlink_to(real)
    assert load_allowlist(link).entries == load_allowlist(real).entries


def test_a_large_regular_file_is_read_in_full(tmp_path: Path, deadline: None) -> None:
    """The read loop replaced read_bytes(); a file past one 64KiB chunk proves it does not
    stop at the first short read. Keyed on entry count, which a truncated read changes."""
    data: Any = yaml.safe_load(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    entries = data["accounts"]
    padded = [
        dict(entry, notes=f"padding {index} " + "x" * 4096)
        for index, entry in enumerate(entries)
        for _ in range(4)
    ]
    for index, entry in enumerate(padded):
        entry["username"] = f"pad{index}"
    path = tmp_path / "big.yaml"
    path.write_text(yaml.safe_dump({"accounts": padded}), encoding="utf-8")
    assert path.stat().st_size > 65536
    assert len(load_allowlist(path).entries) == len(padded)


def test_mid_read_failure_is_a_filesystem_error_and_closes_the_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read loop's own failure path, and the ``finally`` that pairs with ``os.open``.

    A failure *after* the open is not reachable from a test fixture -- an I/O error on a
    regular file, a truncation mid-read -- so it is simulated. Every wrapper is keyed on the
    one descriptor this load opens, because patching ``os.read``/``os.close`` wholesale would
    break pytest's own output capture. ``intercepted`` is asserted for the same reason
    test_env_verify's permission test asserts it: a simulated failure that simulates nothing
    proves nothing, and this suite has shipped one of those before.

    The close count is the point. ``_read_regular_file`` closes in a ``finally``, so the
    error path must not close twice (which could later be a close of an unrelated,
    since-reused fd) nor leak the descriptor.
    """
    path = tmp_path / "accounts.yaml"
    path.write_text(ALLOWLIST_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    original_open, original_read, original_close = os.open, os.read, os.close
    target_fd: int | None = None
    intercepted = False
    closes = 0

    def _tracking_open(target: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal target_fd
        fd = original_open(target, flags, *args, **kwargs)
        if Path(target) == path:
            target_fd = fd
        return fd

    def _failing_read(fd: int, length: int) -> bytes:
        nonlocal intercepted
        if fd == target_fd:
            intercepted = True
            raise OSError(5, "Input/output error")
        return original_read(fd, length)

    def _counting_close(fd: int) -> None:
        nonlocal closes
        if fd == target_fd:
            closes += 1
        original_close(fd)

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "read", _failing_read)
    monkeypatch.setattr(os, "close", _counting_close)

    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist(path)

    assert intercepted, "os.read was never reached -- this test no longer simulates anything"
    assert closes == 1, f"descriptor closed {closes} times"
    assert excinfo.value.is_filesystem_error is True
    assert excinfo.value.__cause__ is None
    assert "Input/output error" in str(excinfo.value)


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


def test_a_yaml_constructor_failure_does_not_echo_the_offending_value(tmp_path: Path) -> None:
    """Two of the six constructor failures put the scalar in their own message.

    ``!!bool <x>`` raises ``KeyError(<x>)`` and ``!!int <x>`` raises
    ``ValueError("invalid literal for int() with base 10: '<x>'")``, so a secret pasted into
    the file reached the terminal in the raw traceback -- this is a leak channel, not only a
    raw-type escape. Both messages *and* the rendered traceback are checked, because
    ``from None`` is what keeps the second one clean.
    """
    for name, tagged in (
        ("bool-tag.yaml", f"!!bool {SECRET}"),
        ("int-tag.yaml", f"!!int {SECRET}"),
        ("bool-tag-int.yaml", f"!!bool {SECRET_INT}"),
    ):
        path = tmp_path / name
        path.write_text(_raw_source(notes=tagged), encoding="utf-8")
        with pytest.raises(AllowlistError) as excinfo:
            load_allowlist(path)
        assert not _leaks(excinfo.value), f"{name} leaked the tagged scalar"


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
