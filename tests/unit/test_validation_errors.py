"""The shared ValidationError sanitizer, and the claim that every module uses it (M0-008).

Four groups:

- **Partition.** "Every module that catches a ValidationError uses it" is a claim about the
  whole of ``src/``, so it is checked by scanning ``src/`` rather than by listing seven call
  sites (the T-908 lesson: a per-row check cannot see a missing row).
- **Authored errors.** The sanitizer renders a non-catalogue error's sentence. That is safe
  only while every such error comes from ``authored_error`` with a literal sentence, one per
  slug, and the AST scan here is what holds every call site to that.
- **Real entry points.** A marker planted in a value and in a key, at top level and nested,
  through each module's own entry point. Each case first confirms that the raw
  ValidationError *does* carry the marker, so the absence it then asserts is not vacuous.
- **Companion.** An authored field name, an authored sentence and a list index all survive,
  so the withholding is not blanket.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

import whiskeyjack_bot
from whiskeyjack_bot import validation_errors
from whiskeyjack_bot.config import AppConfig, ConfigError, validate_config_data
from whiskeyjack_bot.forecast.record import (
    ForecastRecord,
    ForecastRecordError,
    record_from_json,
)
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastSchemaError,
    validate_forecast_response,
)
from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.questions.normalize import NormalizationError, _sanitize
from whiskeyjack_bot.research.allowlist import AllowlistError, _AllowlistFile, _validate_payload
from whiskeyjack_bot.research.model import (
    ResearchRun,
    ResearchSchemaError,
    validate_document,
    validate_run,
)
from whiskeyjack_bot.research.model import ResearchDocument
from whiskeyjack_bot.resolution import (
    ResolutionError,
    ResolutionObservation,
    observation_from_snapshot,
)
from whiskeyjack_bot.validation_errors import (
    BUILTIN_ERROR_TYPES,
    WITHHELD,
    sanitized_problems,
)

SRC = Path(whiskeyjack_bot.__file__).parent
ROOT = SRC.parents[1]
SHARED = SRC / "validation_errors.py"

MARKER = "WJLEAKMARKER008"
# An int key: an unquoted numeric YAML key parses as one, and pydantic puts it in `loc`.
INT_MARKER = 918273645


def _modules() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(SRC.rglob("*.py"))
    ]


# --- partition --------------------------------------------------------------------------


def _catches_validation_error(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            for part in ast.walk(node.type):
                name = part.id if isinstance(part, ast.Name) else getattr(part, "attr", None)
                if name == "ValidationError":
                    return True
    return False


def _imports_sanitizer(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "whiskeyjack_bot.validation_errors":
            if any(alias.name == "sanitized_problems" for alias in node.names):
                return True
    return False


def test_no_module_renders_a_validation_error_itself() -> None:
    """`.errors(` is the call every private sanitizer was built on. Outside the shared
    module there must be none, so no second rendering can drift from the first."""
    offenders = [
        str(path.relative_to(SRC))
        for path, tree in _modules()
        if path != SHARED
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "errors"
    ]
    assert offenders == []


def test_every_module_that_catches_a_validation_error_uses_the_shared_sanitizer() -> None:
    catching = {
        str(path.relative_to(SRC)) for path, tree in _modules() if _catches_validation_error(tree)
    }
    # Anti-vacuity: the scan has to find the seven known catchers to be looking at all.
    assert {
        "config.py",
        "resolution.py",
        "questions/normalize.py",
        "research/model.py",
        "research/allowlist.py",
        "forecast/schema.py",
        "forecast/record.py",
    } <= catching
    missing = sorted(
        str(path.relative_to(SRC))
        for path, tree in _modules()
        if _catches_validation_error(tree) and not _imports_sanitizer(tree)
    )
    assert missing == []


# --- authored errors --------------------------------------------------------------------

_SLUG = re.compile(r"[a-z][a-z0-9_]*")
_CONSTANT_NAME = re.compile(r"_?[A-Z][A-Z0-9_]*")


def _module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                constants[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                constants[node.target.id] = node.value
    return constants


def _literal_sentence(node: ast.expr, constants: dict[str, ast.expr]) -> str | None:
    """The sentence's source text, or None if it could carry a runtime value.

    Accepted: a string constant; an f-string whose every interpolation is an UPPER_CASE
    name; an UPPER_CASE module constant bound to one of those. A brace in constant text is
    refused too, because pydantic formats ``{name}`` placeholders in a custom error's
    template.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return None if "{" in node.value else node.value
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if not (
                    isinstance(part.value, ast.Name)
                    and _CONSTANT_NAME.fullmatch(part.value.id)
                    and part.conversion == -1
                    and part.format_spec is None
                ):
                    return None
            elif isinstance(part, ast.Constant) and "{" in str(part.value):
                return None
        return ast.unparse(node)
    if isinstance(node, ast.Name) and _CONSTANT_NAME.fullmatch(node.id) and node.id in constants:
        bound = constants[node.id]
        if isinstance(bound, ast.Name):
            return None  # one level only: an alias of an alias is not a literal
        return _literal_sentence(bound, constants)
    return None


def _authored_calls() -> list[tuple[str, ast.Call, dict[str, ast.expr]]]:
    calls = []
    for path, tree in _modules():
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "authored_error":
                    calls.append((str(path.relative_to(SRC)), node, constants))
    return calls


def test_every_authored_error_is_a_literal_slug_and_a_literal_sentence() -> None:
    calls = _authored_calls()
    assert len(calls) >= 30, "the scan found too few call sites to be looking in the right place"
    problems: list[str] = []
    sentences: dict[str, set[str]] = {}
    for where, call, constants in calls:
        if call.keywords or len(call.args) != 2:
            problems.append(f"{where}:{call.lineno}: authored_error takes exactly (slug, sentence)")
            continue
        slug_node, sentence_node = call.args
        if not (isinstance(slug_node, ast.Constant) and isinstance(slug_node.value, str)):
            problems.append(f"{where}:{call.lineno}: the slug must be a string literal")
            continue
        slug = slug_node.value
        if not _SLUG.fullmatch(slug) or slug in BUILTIN_ERROR_TYPES:
            problems.append(f"{where}:{call.lineno}: slug {slug!r} is malformed or pydantic's own")
        sentence = _literal_sentence(sentence_node, constants)
        if sentence is None:
            problems.append(f"{where}:{call.lineno}: the sentence could carry a runtime value")
            continue
        sentences.setdefault(slug, set()).add(sentence)
    problems.extend(
        f"slug {slug!r} has {len(texts)} different sentences"
        for slug, texts in sorted(sentences.items())
        if len(texts) != 1
    )
    assert problems == []


def test_only_the_shared_module_constructs_a_custom_pydantic_error() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path, tree in _modules()
        if path != SHARED
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "PydanticCustomError")
        or (isinstance(node, ast.Attribute) and node.attr == "PydanticCustomError")
        or (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "PydanticCustomError" for alias in node.names)
        )
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        'authored_error("slug_x", f"bad {value}")',
        'authored_error("slug_x", "bad " + value)',
        'authored_error("slug_x", message)',
        'authored_error(slug, "text")',
        'authored_error("value_error", "text")',
        'authored_error("slug_x", "has {placeholder}")',
        'authored_error("slug_x", f"{LIMIT!r} words")',
    ],
)
def test_the_scan_refuses_a_sentence_that_could_carry_a_value(source: str) -> None:
    """The scan's own mutants: each of these must be refused, or the scan is the weak link."""
    call = ast.parse(source).body[0]
    assert isinstance(call, ast.Expr) and isinstance(call.value, ast.Call)
    slug_node, sentence_node = call.value.args
    slug_ok = (
        isinstance(slug_node, ast.Constant)
        and isinstance(slug_node.value, str)
        and slug_node.value not in BUILTIN_ERROR_TYPES
    )
    assert not slug_ok or _literal_sentence(sentence_node, {}) is None


def test_the_scan_accepts_the_three_literal_forms() -> None:
    constants = _module_constants(ast.parse('_BAD = "must be x"\nLIMIT = 5\n'))
    for source in ('"plain text"', 'f"at most {LIMIT} words"', "_BAD"):
        node = ast.parse(source, mode="eval").body
        assert _literal_sentence(node, constants) is not None, source


# --- real entry points ------------------------------------------------------------------


def _raw_leaks(validate: Callable[[], object]) -> bool:
    """Whether pydantic's own rendering carries a marker: the leak channel is live."""
    try:
        validate()
    except ValidationError as exc:
        text = str(exc) + json.dumps(
            exc.errors(include_input=False, include_url=False), default=str
        )
        return MARKER in text or str(INT_MARKER) in text
    raise AssertionError("expected the raw validation to fail")


def _example_config() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openrouter/test-model"
    return dict(data)


def _config_cases() -> list[dict[str, Any]]:
    top = _example_config()
    top[MARKER] = 1
    nested = _example_config()
    nested["forecast"][MARKER] = 1
    nested_int = _example_config()
    nested_int["forecast"][INT_MARKER] = 1
    value = _example_config()
    value["forecast"]["prompt_version"] = MARKER
    return [top, nested, nested_int, value]


def _cases() -> list[tuple[str, Callable[[], object], Callable[[], object], type[Exception]]]:
    """(name, raw pydantic call, module entry point, the module's own error)."""
    cases: list[tuple[str, Callable[[], object], Callable[[], object], type[Exception]]] = []
    for index, data in enumerate(_config_cases()):
        cases.append(
            (
                f"config-{index}",
                lambda data=data: AppConfig.model_validate(data),
                lambda data=data: validate_config_data(data),
                ConfigError,
            )
        )
    document = {MARKER: 1, "title": {MARKER: MARKER}}
    cases.append(
        (
            "research-document",
            lambda: ResearchDocument.model_validate(document),
            lambda: validate_document(document),
            ResearchSchemaError,
        )
    )
    run = {MARKER: 1, "provider_config": {MARKER: float("nan"), "ok": {MARKER: float("inf")}}}
    cases.append(
        (
            "research-run",
            lambda: ResearchRun.model_validate(run),
            lambda: validate_run(run),
            ResearchSchemaError,
        )
    )
    response = {MARKER: 1, "base_rate": {MARKER: MARKER}, "question_type": "binary"}
    cases.append(
        (
            "forecast-response",
            lambda: BinaryForecastResponse.model_validate(response),
            lambda: validate_forecast_response(response, BinaryForecastResponse),
            ForecastSchemaError,
        )
    )
    # The discriminated union: `union_tag_invalid`'s msg quotes the tag, which is how B1
    # first leaked.
    record = {MARKER: 1, "forecast": {"question_type": MARKER, MARKER: 1}}
    cases.append(
        (
            "forecast-record",
            lambda: TypeAdapter(ForecastRecord).validate_python(record),
            lambda: record_from_json(json.dumps(record)),
            ForecastRecordError,
        )
    )
    allowlist = {"accounts": [{MARKER: 1, "username": "ok"}], MARKER: 1, INT_MARKER: 2}
    cases.append(
        (
            "allowlist",
            lambda: _AllowlistFile.model_validate(allowlist),
            lambda: _validate_payload(allowlist),
            AllowlistError,
        )
    )
    question = {"qtype": MARKER, MARKER: 1}

    def normalize_entry() -> object:
        try:
            return TypeAdapter(CanonicalQuestion).validate_python(question)
        except ValidationError as exc:
            raise _sanitize(exc) from None

    cases.append(
        (
            "canonical-question",
            lambda: TypeAdapter(CanonicalQuestion).validate_python(question),
            normalize_entry,
            NormalizationError,
        )
    )
    observation = {MARKER: 1, "question_type": MARKER}
    cases.append(
        (
            "resolution",
            lambda: ResolutionObservation.model_validate(observation),
            lambda: observation_from_snapshot(json.dumps(observation)),
            ResolutionError,
        )
    )
    return cases


_CASES = _cases()


@pytest.mark.parametrize(
    ("raw", "entry", "error"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_a_planted_marker_never_reaches_a_modules_error(
    raw: Callable[[], object], entry: Callable[[], object], error: type[Exception]
) -> None:
    assert _raw_leaks(raw), "vacuous: pydantic's own rendering does not carry the marker here"
    with pytest.raises(error) as excinfo:
        entry()
    assert excinfo.value.__cause__ is None
    rendered = str(excinfo.value) + repr(getattr(excinfo.value, "problems", ""))
    assert MARKER not in rendered
    assert str(INT_MARKER) not in rendered
    assert WITHHELD in rendered or "<root>" in rendered or ":" in rendered


# --- companion: the withholding is not blanket ------------------------------------------


def test_an_authored_config_sentence_and_its_field_path_survive() -> None:
    data = _example_config()
    data["forecast"]["min_probability"] = 0.9
    data["forecast"]["max_probability"] = 0.1
    with pytest.raises(ConfigError) as excinfo:
        validate_config_data(data)
    assert (
        "forecast: forecast.min_probability must be strictly below forecast.max_probability "
        "[probability_bounds_unordered]"
    ) in excinfo.value.problems


def test_a_nested_authored_field_name_survives_with_the_builtin_type() -> None:
    data = _example_config()
    data["forecast"]["min_probability"] = "not a number"
    with pytest.raises(ConfigError) as excinfo:
        validate_config_data(data)
    assert "forecast.min_probability: float_parsing" in excinfo.value.problems


def test_a_list_index_after_a_sequence_field_survives() -> None:
    payload = {"accounts": [{"username": "bad handle!"}]}
    with pytest.raises(AllowlistError) as excinfo:
        _validate_payload(payload)
    joined = "\n".join(excinfo.value.problems)
    assert "accounts.0.username:" in joined
    assert "[username_not_a_handle]" in joined


def test_a_deep_response_field_path_survives() -> None:
    response = {"base_rate": {"prior_probability": 2.0}}
    with pytest.raises(ForecastSchemaError) as excinfo:
        validate_forecast_response(response, BinaryForecastResponse)
    assert "base_rate.prior_probability: less_than_equal" in excinfo.value.problems


# --- the renderer directly --------------------------------------------------------------


def test_an_int_is_an_index_only_after_a_sequence_field() -> None:
    from pydantic import BaseModel, ConfigDict

    class Leaf(BaseModel):
        model_config = ConfigDict(extra="forbid")
        n: int

    class Tree(BaseModel):
        model_config = ConfigDict(extra="forbid")
        items: list[Leaf]
        table: dict[int, Leaf]

    with pytest.raises(ValidationError) as excinfo:
        Tree.model_validate({"items": [{"n": "x"}], "table": {INT_MARKER: {"n": "x"}}})
    problems = sanitized_problems(excinfo.value, Tree)
    assert "items.0.n: int_parsing" in problems
    assert f"table.{WITHHELD}.n: int_parsing" in problems
    assert all(str(INT_MARKER) not in problem for problem in problems)


def test_the_builtin_catalogue_is_pydantics_and_nonempty() -> None:
    assert {"missing", "extra_forbidden", "union_tag_invalid", "value_error"} <= set(
        BUILTIN_ERROR_TYPES
    )
    assert validation_errors.authored_error("x_y", "z").type == "x_y"


def test_a_plain_value_error_renders_as_its_type_only() -> None:
    """The safe default: a validator that raises ValueError instead of authored_error has
    its text dropped, whatever it says."""
    from pydantic import BaseModel, field_validator

    class Model(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def _check(cls, value: str) -> str:
            raise ValueError(f"leaked {value}")

    with pytest.raises(ValidationError) as excinfo:
        Model.model_validate({"value": MARKER})
    assert sanitized_problems(excinfo.value, Model) == ["value: value_error"]


def test_copy_of_example_config_is_valid() -> None:
    """Guard for the fixtures above: the baseline itself validates, so each case's failure
    is the planted marker's and not the baseline's."""
    validate_config_data(copy.deepcopy(_example_config()))
