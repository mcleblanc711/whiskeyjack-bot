"""M1-401 acceptance: the forecaster prompt's declared version is verified against
config and its content hash is over raw bytes, so any changed byte -- including a
whitespace reflow -- produces a new hash. Errors arrive as PromptError and never
echo prompt contents."""

import traceback
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.prompt import (
    LoadedPrompt,
    PromptError,
    load_prompt,
    parse_declared_version,
    prompt_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PROMPT = REPO_ROOT / "prompts" / "forecaster.md"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"

MINIMAL_PROMPT = "# MiniBench forecaster prompt — v1.1.0\n\nBody text.\n"


def write_prompt(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "forecaster.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- The drift guard -------------------------------------------------------

# Every released prompt version pinned to the sha256 of its exact bytes.
#
# Without this, the guard below compared only H1-vs-config, so any body byte
# could change while both stayed at 1.1.0 -- the version was pinned but the
# content it names was not, which is the drift D04 exists to catch. Editing the
# prompt now fails CI until the version is bumped *and* a digest pinned here.
RELEASED_PROMPT_SHA256 = {
    "1.1.0": "7ce2e9ea2a6df73e90e224bafc7402071f16878339cd177f57cad135516958da",
}


def test_real_prompt_and_example_config_agree() -> None:
    """Editing the prompt without bumping config.example.yaml fails CI (D04)."""
    config = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    declared = config["forecast"]["prompt_version"]
    loaded = load_prompt(REAL_PROMPT, declared)
    assert loaded.version == declared


def test_real_prompt_bytes_match_the_pinned_digest() -> None:
    """Editing the prompt body without bumping the version fails CI (D04).

    The version check above cannot see body drift: both versions stay 1.1.0
    while the bytes the model actually sees change.
    """
    loaded = load_prompt(REAL_PROMPT, "1.1.0")
    assert loaded.version in RELEASED_PROMPT_SHA256, (
        f"prompt declares v{loaded.version} with no pinned digest; add its sha256 to "
        "RELEASED_PROMPT_SHA256 when releasing a new prompt version"
    )
    assert loaded.sha256 == RELEASED_PROMPT_SHA256[loaded.version], (
        f"prompts/forecaster.md bytes changed but it still declares v{loaded.version}; "
        "bump forecast.prompt_version and pin the new digest"
    )


def test_real_prompt_is_at_v1_1_0() -> None:
    """The v1.1.0 patch (CLAUDE_CODE_PROMPT.md § B) is applied."""
    text = REAL_PROMPT.read_text(encoding="utf-8")
    assert parse_declared_version(text) == "1.1.0"
    assert "reliability_tag" in text
    assert "llm_reported" in text


# --- Hashing: raw bytes ----------------------------------------------------


def test_identical_bytes_hash_identically(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = write_prompt(tmp_path / "a", MINIMAL_PROMPT)
    b = write_prompt(tmp_path / "b", MINIMAL_PROMPT)
    assert load_prompt(a, "1.1.0").sha256 == load_prompt(b, "1.1.0").sha256


def test_single_changed_byte_changes_hash(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = write_prompt(tmp_path / "a", MINIMAL_PROMPT)
    b = write_prompt(tmp_path / "b", MINIMAL_PROMPT.replace("Body text.", "Body texts"))
    assert load_prompt(a, "1.1.0").sha256 != load_prompt(b, "1.1.0").sha256


def test_whitespace_reflow_changes_hash(tmp_path: Path) -> None:
    """Pins the digest to raw bytes and away from research.hashing.content_sha256,
    whose whitespace-collapsing rule would hash these two identically."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = write_prompt(tmp_path / "a", "# p — v1.1.0\n\nOne two three.\n")
    b = write_prompt(tmp_path / "b", "# p — v1.1.0\n\nOne two\nthree.\n")
    assert load_prompt(a, "1.1.0").sha256 != load_prompt(b, "1.1.0").sha256

    from whiskeyjack_bot.research.hashing import content_sha256

    # The rule this module deliberately does not use would collapse them.
    assert content_sha256("One two three.") == content_sha256("One two\nthree.")


def test_hash_matches_sha256_of_file_bytes(tmp_path: Path) -> None:
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    assert load_prompt(path, "1.1.0").sha256 == prompt_sha256(path.read_bytes())


# --- Version parsing -------------------------------------------------------


def test_version_comes_from_h1_not_body() -> None:
    """The body's "schema_version" is a decoy the parse must not see."""
    text = '# MiniBench forecaster prompt — v1.1.0\n\n"schema_version": "9.9.9"\n'
    assert parse_declared_version(text) == "1.1.0"


def test_v_prefix_is_stripped(tmp_path: Path) -> None:
    """Config's bare form is canonical; the H1's 'v' prefix normalizes to it."""
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    assert load_prompt(path, "1.1.0").version == "1.1.0"


@pytest.mark.parametrize(
    "first_line",
    [
        "MiniBench forecaster prompt — v1.1.0",  # not an H1
        "## MiniBench forecaster prompt — v1.1.0",  # H2, not H1
        "# MiniBench forecaster prompt",  # no version
        "# MiniBench forecaster prompt — v1.1",  # not MAJOR.MINOR.PATCH
        "# MiniBench forecaster prompt — v1.1.0 (draft)",  # version not trailing
        "#",  # degenerate
        "",  # empty file
        "# MiniBench forecaster prompt — v01.1.0",  # leading zero is not SemVer
        "# MiniBench forecaster prompt — v1.01.0",  # leading zero, minor
        "# MiniBench forecaster prompt — v١.١.٠",  # Unicode digits, not ASCII
        "# MiniBench forecaster prompt — v1.1.0.0",  # four components
    ],
)
def test_malformed_h1_raises_prompt_error(first_line: str) -> None:
    with pytest.raises(PromptError):
        parse_declared_version(f"{first_line}\n\nBody.\n")


def test_ambiguous_h1_is_rejected_not_silently_resolved() -> None:
    """Two versions in one H1 is drift, not a pick-the-last-one situation.

    An anchored '.*v(...)$' scan resolved this to 2.0.0 -- the *superseded*
    version -- and recorded it against every forecast.
    """
    with pytest.raises(PromptError) as exc:
        parse_declared_version("# forecaster prompt v1.1.0 supersedes v2.0.0\n\nBody.\n")
    assert "more than one version" in str(exc.value)


def test_version_mismatch_is_a_hard_error(tmp_path: Path) -> None:
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    with pytest.raises(PromptError) as exc:
        load_prompt(path, "1.0.0")
    # Both versions are safe to echo: each matched a strict semver pattern.
    assert "1.1.0" in str(exc.value)
    assert "1.0.0" in str(exc.value)


@pytest.mark.parametrize(
    "expected_version",
    [
        "v1.1.0",  # 'v' prefix; config's form is bare
        "1.1.0\n",  # terminal newline: 'match' + '$' used to accept this
        "1.1.0 ",  # trailing space
        "01.1.0",  # leading zero is not SemVer
        "١.١.٠",  # Unicode digits, not ASCII
        "1.1",  # not MAJOR.MINOR.PATCH
    ],
)
def test_malformed_expected_version_rejected(tmp_path: Path, expected_version: str) -> None:
    """The guard exists because this value is echoed in the mismatch message."""
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    with pytest.raises(PromptError):
        load_prompt(path, expected_version)


# --- Error hygiene ---------------------------------------------------------


def test_missing_file_raises_prompt_error(tmp_path: Path) -> None:
    with pytest.raises(PromptError):
        load_prompt(tmp_path / "absent.md", "1.1.0")


def test_directory_raises_prompt_error(tmp_path: Path) -> None:
    """A path that exists but is not a readable file still arrives as PromptError."""
    with pytest.raises(PromptError):
        load_prompt(tmp_path, "1.1.0")


def test_invalid_utf8_raises_prompt_error(tmp_path: Path) -> None:
    path = tmp_path / "forecaster.md"
    path.write_bytes(b"# p \xff\xfe v1.1.0\n")
    with pytest.raises(PromptError):
        load_prompt(path, "1.1.0")


# Low-entropy on purpose: gitleaks scans every branch in CI, so a realistic-looking
# planted secret would fail CI on unrelated PRs until fingerprint-pinned (M1-301).
PLANTED = "privateFAKE123456"


@pytest.mark.parametrize(
    "text",
    [
        f"No heading here\n\n{PLANTED}\n",  # malformed H1 path
        f"# p — v9.9.9\n\n{PLANTED}\n",  # version-mismatch path
    ],
)
def test_errors_never_echo_prompt_contents(tmp_path: Path, text: str) -> None:
    path = write_prompt(tmp_path, text)
    with pytest.raises(PromptError) as exc:
        load_prompt(path, "1.1.0")
    rendered = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert PLANTED not in str(exc.value)
    assert PLANTED not in rendered


def test_repr_does_not_expose_the_prompt_body(tmp_path: Path) -> None:
    """The error paths were sanitized but the value object was not: repr() of a
    LoadedPrompt printed the whole prompt, credential included."""
    path = write_prompt(tmp_path, f"# p — v1.1.0\n\n{PLANTED}\n")
    loaded = load_prompt(path, "1.1.0")

    assert PLANTED not in repr(loaded)
    # The safe fields stay visible -- a repr with neither is useless.
    assert "1.1.0" in repr(loaded)
    assert loaded.sha256 in repr(loaded)
    # The body is still reachable through the field itself.
    assert PLANTED in loaded.text


def test_repr_leak_survives_a_rendered_traceback(tmp_path: Path) -> None:
    """The realistic leak path: a failed assertion or a frame-capturing logger
    renders locals, not just the exception message."""
    path = write_prompt(tmp_path, f"# p — v1.1.0\n\n{PLANTED}\n")
    loaded = load_prompt(path, "1.1.0")
    assert PLANTED not in f"{loaded!r}" and PLANTED not in str([loaded])


def test_loaded_prompt_is_frozen(tmp_path: Path) -> None:
    loaded = load_prompt(write_prompt(tmp_path, MINIMAL_PROMPT), "1.1.0")
    assert isinstance(loaded, LoadedPrompt)
    with pytest.raises(AttributeError):
        loaded.version = "2.0.0"  # type: ignore[misc]
