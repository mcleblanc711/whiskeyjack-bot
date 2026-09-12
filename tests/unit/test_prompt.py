"""M1-401 acceptance: the forecaster prompt's declared version is verified against
config and its content hash is over raw bytes, so any changed byte -- including a
whitespace reflow -- produces a new hash. Errors arrive as PromptError and never
echo prompt contents."""

import traceback
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.prompt import (
    DeclaredProbabilityBounds,
    LoadedPrompt,
    PromptError,
    load_prompt,
    parse_declared_probability_bounds,
    parse_declared_version,
    probability_bounds_disagreement,
    probability_bounds_violation,
    prompt_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PROMPT = REPO_ROOT / "prompts" / "forecaster.md"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"

# The declared probability range is part of the minimum a prompt must carry since
# M1-407: ``load_prompt`` refuses a prompt that states no range rather than
# assuming the shipped one applies, so a fixture without this line is no longer a
# loadable prompt.
DECLARED_RANGE = "Probabilities must be between 0.001 and 0.999 inclusive."
MINIMAL_PROMPT = f"# MiniBench forecaster prompt — v1.1.0\n\nBody text.\n{DECLARED_RANGE}\n"


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
    loaded = load_prompt(REAL_PROMPT, declared, min_probability=0.001, max_probability=0.999)
    assert loaded.version == declared


def test_real_prompt_bytes_match_the_pinned_digest() -> None:
    """Editing the prompt body without bumping the version fails CI (D04).

    The version check above cannot see body drift: both versions stay 1.1.0
    while the bytes the model actually sees change.
    """
    loaded = load_prompt(REAL_PROMPT, "1.1.0", min_probability=0.001, max_probability=0.999)
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
    assert (
        load_prompt(a, "1.1.0", min_probability=0.001, max_probability=0.999).sha256
        == load_prompt(b, "1.1.0", min_probability=0.001, max_probability=0.999).sha256
    )


def test_single_changed_byte_changes_hash(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = write_prompt(tmp_path / "a", MINIMAL_PROMPT)
    b = write_prompt(tmp_path / "b", MINIMAL_PROMPT.replace("Body text.", "Body texts"))
    assert (
        load_prompt(a, "1.1.0", min_probability=0.001, max_probability=0.999).sha256
        != load_prompt(b, "1.1.0", min_probability=0.001, max_probability=0.999).sha256
    )


def test_whitespace_reflow_changes_hash(tmp_path: Path) -> None:
    """Pins the digest to raw bytes and away from research.hashing.content_sha256,
    whose whitespace-collapsing rule would hash these two identically."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = write_prompt(tmp_path / "a", f"# p — v1.1.0\n\nOne two three.\n{DECLARED_RANGE}\n")
    b = write_prompt(tmp_path / "b", f"# p — v1.1.0\n\nOne two\nthree.\n{DECLARED_RANGE}\n")
    assert (
        load_prompt(a, "1.1.0", min_probability=0.001, max_probability=0.999).sha256
        != load_prompt(b, "1.1.0", min_probability=0.001, max_probability=0.999).sha256
    )

    from whiskeyjack_bot.research.hashing import content_sha256

    # The rule this module deliberately does not use would collapse them.
    assert content_sha256("One two three.") == content_sha256("One two\nthree.")


def test_hash_matches_sha256_of_file_bytes(tmp_path: Path) -> None:
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    assert load_prompt(
        path, "1.1.0", min_probability=0.001, max_probability=0.999
    ).sha256 == prompt_sha256(path.read_bytes())


# --- Version parsing -------------------------------------------------------


def test_version_comes_from_h1_not_body() -> None:
    """The body's "schema_version" is a decoy the parse must not see."""
    text = '# MiniBench forecaster prompt — v1.1.0\n\n"schema_version": "9.9.9"\n'
    assert parse_declared_version(text) == "1.1.0"


def test_v_prefix_is_stripped(tmp_path: Path) -> None:
    """Config's bare form is canonical; the H1's 'v' prefix normalizes to it."""
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    assert (
        load_prompt(path, "1.1.0", min_probability=0.001, max_probability=0.999).version == "1.1.0"
    )


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
        load_prompt(path, "1.0.0", min_probability=0.001, max_probability=0.999)
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
        load_prompt(path, expected_version, min_probability=0.001, max_probability=0.999)


# --- Error hygiene ---------------------------------------------------------


def test_missing_file_raises_prompt_error(tmp_path: Path) -> None:
    with pytest.raises(PromptError):
        load_prompt(tmp_path / "absent.md", "1.1.0", min_probability=0.001, max_probability=0.999)


def test_directory_raises_prompt_error(tmp_path: Path) -> None:
    """A path that exists but is not a readable file still arrives as PromptError."""
    with pytest.raises(PromptError):
        load_prompt(tmp_path, "1.1.0", min_probability=0.001, max_probability=0.999)


def test_invalid_utf8_raises_prompt_error(tmp_path: Path) -> None:
    path = tmp_path / "forecaster.md"
    path.write_bytes(b"# p \xff\xfe v1.1.0\n")
    with pytest.raises(PromptError):
        load_prompt(path, "1.1.0", min_probability=0.001, max_probability=0.999)


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
        load_prompt(path, "1.1.0", min_probability=0.001, max_probability=0.999)
    rendered = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert PLANTED not in str(exc.value)
    assert PLANTED not in rendered


def test_repr_does_not_expose_the_prompt_body(tmp_path: Path) -> None:
    """The error paths were sanitized but the value object was not: repr() of a
    LoadedPrompt printed the whole prompt, credential included."""
    path = write_prompt(tmp_path, f"# p — v1.1.0\n\n{PLANTED}\n{DECLARED_RANGE}\n")
    loaded = load_prompt(path, "1.1.0", min_probability=0.001, max_probability=0.999)

    assert PLANTED not in repr(loaded)
    # The safe fields stay visible -- a repr with neither is useless.
    assert "1.1.0" in repr(loaded)
    assert loaded.sha256 in repr(loaded)
    # The body is still reachable through the field itself.
    assert PLANTED in loaded.text


def test_repr_leak_survives_a_rendered_traceback(tmp_path: Path) -> None:
    """The realistic leak path: a failed assertion or a frame-capturing logger
    renders locals, not just the exception message."""
    path = write_prompt(tmp_path, f"# p — v1.1.0\n\n{PLANTED}\n{DECLARED_RANGE}\n")
    loaded = load_prompt(path, "1.1.0", min_probability=0.001, max_probability=0.999)
    assert PLANTED not in f"{loaded!r}" and PLANTED not in str([loaded])


def test_loaded_prompt_is_frozen(tmp_path: Path) -> None:
    loaded = load_prompt(
        write_prompt(tmp_path, MINIMAL_PROMPT),
        "1.1.0",
        min_probability=0.001,
        max_probability=0.999,
    )
    assert isinstance(loaded, LoadedPrompt)
    with pytest.raises(AttributeError):
        loaded.version = "2.0.0"  # type: ignore[misc]


# --- Declared probability bounds (M1-407) ----------------------------------


def _prompt_with(body: str) -> str:
    """A loadable v1.1.0 prompt whose body is exactly ``body``."""
    return f"# MiniBench forecaster prompt — v1.1.0\n\n{body}\n"


def test_the_real_prompt_declares_the_committed_range() -> None:
    """The acceptance criterion's second clause: the check reads the file config
    names, not a copy of its numbers. The shipped prompt states the range three
    times, in three different sentences, and all three must agree."""
    bounds = parse_declared_probability_bounds(REAL_PROMPT.read_text(encoding="utf-8"))
    assert (bounds.low, bounds.high) == (0.001, 0.999)


def test_the_tournament_prompt_declares_the_committed_range() -> None:
    """``config/tournament.yaml`` names this file, not ``forecaster.md``. A check
    that only ever parsed the one under test would be silent about the live one."""
    tournament_prompt = REPO_ROOT / "prompts" / "forecaster-tournament.md"
    bounds = parse_declared_probability_bounds(tournament_prompt.read_text(encoding="utf-8"))
    assert (bounds.low, bounds.high) == (0.001, 0.999)


def test_every_statement_of_the_range_is_read_not_just_the_first() -> None:
    """Three sentences state the range; a parse that stopped at the first would
    accept a prompt telling the model two different things where it reads them."""
    text = _prompt_with(
        "Use probability values between 0.001 and 0.999 for binary outcomes.\n"
        "`probability_yes` must be between 0.001 and 0.999 inclusive.\n"
        "Probabilities must be between 0.001 and 0.999 and sum to 1 within `1e-6`."
    )
    assert parse_declared_probability_bounds(text) == DeclaredProbabilityBounds(
        low=0.001, high=0.999
    )


def test_disagreeing_statements_are_rejected_not_resolved() -> None:
    """Drift, not a pick-the-narrowest situation -- the same rule
    ``parse_declared_version`` applies to two versions in one H1."""
    text = _prompt_with(
        "`probability_yes` must be between 0.001 and 0.999 inclusive.\n"
        "Probabilities must be between 0.01 and 0.99 and sum to 1."
    )
    with pytest.raises(PromptError) as caught:
        parse_declared_probability_bounds(text)
    assert "more than one probability range" in str(caught.value)


def test_a_prompt_declaring_no_range_is_refused() -> None:
    """Not defaulted to the shipped pair: "reported at startup" is not satisfied
    by guessing which range an unstated prompt meant."""
    with pytest.raises(PromptError) as caught:
        parse_declared_probability_bounds(_prompt_with("Body text with no range at all."))
    assert "declares no probability range" in str(caught.value)


def test_a_range_on_a_line_that_is_not_about_probability_is_not_read() -> None:
    """The scan is scoped by line. The prompt body carries a ``1e-6`` sum
    tolerance and a percentile ladder; a document-wide decimal scan reads those."""
    with pytest.raises(PromptError):
        parse_declared_probability_bounds(
            _prompt_with("Percentile values must be between 0.01 and 0.99 and non-decreasing.")
        )


@pytest.mark.parametrize(
    "statement",
    [
        "Probabilities must be between 0.999 and 0.001.",  # inverted
        "Probabilities must be between 0.5 and 0.5.",  # empty
        "Probabilities must be between 0.001 and 2.",  # above 1
        "Probabilities must be between 0.001 and 99999999999999999999999999999999"
        "9999999999999999999999999999999999999999999999999999999999999999999999999"
        "9999999999999999999999999999999999999999999999999999999999999999999999999"
        "99999999999999999999999999999999999999999999999999999999999999999999999.",  # inf
    ],
)
def test_a_range_that_cannot_bound_a_probability_is_refused(statement: str) -> None:
    """Including the overlong digit run: ``float()`` yields ``inf`` rather than
    raising, so the range check is what keeps it from escaping as a bound."""
    with pytest.raises(PromptError):
        parse_declared_probability_bounds(_prompt_with(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "Probabilities must be between .5 and .9.",  # no digit before the point
        "Probabilities must be between 0.001 to 0.999.",  # not the 'and' spelling
        "Probabilities must be betweenish 0.001 and 0.999.",  # \b on 'between'
    ],
)
def test_a_range_the_parser_cannot_read_is_refused_not_guessed(statement: str) -> None:
    """The parser is coupled to one prose spelling and refuses rather than guessing.
    That coupling is the standing risk; these pin where its edge actually is."""
    with pytest.raises(PromptError):
        parse_declared_probability_bounds(_prompt_with(statement))


def test_parse_is_a_fixed_point_on_its_own_input() -> None:
    """Nothing in the parse mutates or normalizes the text, so two parses of one
    string are one answer -- the property replay depends on."""
    text = REAL_PROMPT.read_text(encoding="utf-8")
    assert parse_declared_probability_bounds(text) == parse_declared_probability_bounds(text)


# --- The two relations, which are deliberately different -------------------


BOUNDS = DeclaredProbabilityBounds(low=0.001, high=0.999)


def test_agreement_is_the_only_thing_disagreement_accepts() -> None:
    assert (
        probability_bounds_disagreement(BOUNDS, min_probability=0.001, max_probability=0.999)
        is None
    )


@pytest.mark.parametrize(
    "minimum,maximum",
    [
        (0.05, 0.95),  # narrower on both ends -- the row's own motivating case
        (0.05, 0.999),  # narrower on one end only
        (0.001, 0.95),
        (0.0005, 0.999),  # wider -- the criterion's literal reading
        (0.001, 0.9995),
    ],
)
def test_any_disagreement_in_either_direction_is_reported(minimum: float, maximum: float) -> None:
    """Equality, not containment. A containment test passes the first three of
    these silently, and ``ForecastConfig``'s ``ge``/``le`` clamp means the first
    three are the only ones a config loaded from YAML can even produce."""
    problem = probability_bounds_disagreement(
        BOUNDS, min_probability=minimum, max_probability=maximum
    )
    assert problem is not None
    assert "do not match" in problem


@pytest.mark.parametrize(
    "minimum,maximum",
    [(0.001, 0.999), (0.05, 0.95), (0.05, 0.999), (0.001, 0.95)],
)
def test_a_contained_pair_is_no_violation_even_when_it_is_a_disagreement(
    minimum: float, maximum: float
) -> None:
    """The two relations must actually differ, or splitting them bought nothing.
    Every pair here is a disagreement and none of them is a violation."""
    assert (
        probability_bounds_violation(BOUNDS, min_probability=minimum, max_probability=maximum)
        is None
    )


@pytest.mark.parametrize("minimum,maximum", [(0.0005, 0.999), (0.001, 0.9995), (0.0, 1.0)])
def test_a_pair_escaping_the_declared_range_is_a_violation(minimum: float, maximum: float) -> None:
    problem = probability_bounds_violation(BOUNDS, min_probability=minimum, max_probability=maximum)
    assert problem is not None
    assert "fall outside" in problem


@pytest.mark.parametrize(
    "relation", [probability_bounds_disagreement, probability_bounds_violation]
)
def test_neither_relation_names_a_configured_value(relation: Any) -> None:
    """M1-509 is open: whether a *configured* bound may be rendered at all is not
    settled, so neither message states one. The declared pair is named, because
    it has matched a strict decimal pattern and been range-checked."""
    problem = relation(BOUNDS, min_probability=0.0004, max_probability=0.9996)
    assert problem is not None
    assert "0.0004" not in problem
    assert "0.9996" not in problem
    # The declared pair is what makes the problem fixable and is named.
    assert "0.001" in problem and "0.999" in problem


# --- load_prompt binds the cross-check to the load -------------------------


def test_a_disagreeing_config_is_refused_at_load(tmp_path: Path) -> None:
    """Bound to ``load_prompt`` rather than offered as a separate function, so
    every startup path inherits it instead of remembering to call it."""
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    with pytest.raises(PromptError) as caught:
        load_prompt(path, "1.1.0", min_probability=0.05, max_probability=0.95)
    assert "do not match" in str(caught.value)


def test_the_version_check_still_wins_when_both_disagree(tmp_path: Path) -> None:
    """One PromptError per load, and M1-401's checks keep the precedence they had:
    a prompt failing both reports the version drift D04 exists to catch."""
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    with pytest.raises(PromptError) as caught:
        load_prompt(path, "1.0.0", min_probability=0.05, max_probability=0.95)
    assert "prompt_version" in str(caught.value)


@pytest.mark.parametrize(
    "minimum,maximum",
    [
        (0, 0.999),  # int, not float
        (0.001, 1),
        ("0.001", 0.999),  # str: would escape as a TypeError from the comparison
        (float("nan"), 0.999),  # NaN: every comparison is False, so it passes silently
        (0.001, float("nan")),
        (-0.5, 0.999),  # outside [0, 1]
        (0.001, 1.5),
    ],
)
def test_a_bound_that_is_not_a_float_in_zero_to_one_is_refused(
    tmp_path: Path, minimum: Any, maximum: Any
) -> None:
    """``ForecastConfig`` guarantees two floats; this is the assembled-some-other-way
    case ``forecast.generate`` repeats its own preflights for. Every malformed shape
    must arrive as this module's own error type, never a raw TypeError."""
    path = write_prompt(tmp_path, MINIMAL_PROMPT)
    with pytest.raises(PromptError) as caught:
        load_prompt(path, "1.1.0", min_probability=minimum, max_probability=maximum)
    assert "must be floats" in str(caught.value)


def test_the_loaded_prompt_carries_the_declared_bounds(tmp_path: Path) -> None:
    """They travel with the hashed text, so a caller checking them is provably
    checking the prompt whose digest reaches the ledger."""
    loaded = load_prompt(
        write_prompt(tmp_path, MINIMAL_PROMPT),
        "1.1.0",
        min_probability=0.001,
        max_probability=0.999,
    )
    assert loaded.bounds == DeclaredProbabilityBounds(low=0.001, high=0.999)
    assert "0.001" in repr(loaded) and "0.999" in repr(loaded)


@pytest.mark.parametrize(
    "body",
    [
        f"{PLANTED} and no range at all.",  # no-range path
        f"{PLANTED}\nProbabilities must be between 0.001 and 0.999.\n"
        "Probabilities must be between 0.01 and 0.99.",  # disagreement path
        f"{PLANTED}\nProbabilities must be between 0.999 and 0.001.",  # bad-range path
        f"{PLANTED}\n{DECLARED_RANGE}",  # equality-mismatch path
    ],
)
def test_the_bounds_paths_never_echo_prompt_contents(tmp_path: Path, body: str) -> None:
    """Every new failure path, not only the ones whose message obviously quotes a
    line: the rendered traceback quotes source and locals too."""
    path = write_prompt(tmp_path, _prompt_with(body))
    with pytest.raises(PromptError) as caught:
        load_prompt(path, "1.1.0", min_probability=0.05, max_probability=0.95)
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert PLANTED not in str(caught.value)
    assert PLANTED not in rendered


# --- The line-scoped scan's known blind spot (M1-407 round 1, filed as M1-409) ---


def test_a_conflicting_declaration_wrapped_across_lines_is_not_seen() -> None:
    """**Characterization, not endorsement.** This pins a known gap so it cannot widen
    silently and so nobody reads the standing-risk note as covering it.

    The scan is scoped to a line, so a declaration wrapped by an ordinary editor --
    ``must be`` / newline / ``between 0.01 and 0.99`` -- puts the range on a line with no
    ``probabilit`` in it. It is skipped, the two surviving declarations still agree, and
    the prompt loads with bounds that are *not* what it tells the model for
    ``probability_yes``. The same conflict on one line raises, which is the contrast that
    makes this a wrapping bug rather than an agreement bug.

    The oracle here is written by hand rather than reusing ``_DECLARED_RANGE_RE``: the
    agreement property in ``tests/property/`` derives its expectation from the
    implementation's own regexes and therefore cannot detect this class at all (round-1
    review). M1-409 owns the fix.
    """
    real = REAL_PROMPT.read_text(encoding="utf-8")
    binary_line = "`probability_yes` must be between 0.001 and 0.999 inclusive."
    assert binary_line in real

    wrapped = real.replace(
        binary_line, "`probability_yes` must be\nbetween 0.01 and 0.99 inclusive."
    )
    # Hand-written oracle: the conflicting pair is present in the text, plainly.
    assert "0.01 and 0.99" in wrapped
    assert parse_declared_probability_bounds(wrapped) == DeclaredProbabilityBounds(
        low=0.001, high=0.999
    )

    # The identical conflict, unwrapped, is refused -- so the gap is the newline.
    unwrapped = real.replace(
        binary_line, "`probability_yes` must be between 0.01 and 0.99 inclusive."
    )
    with pytest.raises(PromptError):
        parse_declared_probability_bounds(unwrapped)
