"""Only the module's own error escapes `research/artifacts.py`'s reader (M1-314).

The sibling artifact kind (`forecast/artifacts.py`) has a full property suite
(`tests/property/test_model_artifact_properties.py`); this one exists to close the one
gap M1-314 was filed against: a lone surrogate in the stored ``relative_path`` made
``Path.read_bytes()`` raise a raw ``UnicodeEncodeError`` instead of ``ArtifactError``,
before any I/O happened. The property below was run against the pre-fix reader and
observed to fail -- a property that cannot fail is worse than no property, because it
reads as coverage it does not have.
"""

from __future__ import annotations

import json

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.research.artifacts import ArtifactError, read_raw_responses

# Anything at all, in the relative_path field. The point is that no input produces
# anything but this module's own error -- HOSTILE_TEXT alone would only exercise the
# happy shapes (surrogates, astral scalars); this also covers non-string shapes the
# writer never emits but a caller could still pass.
ANYTHING = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=True, allow_infinity=True),
    HOSTILE_TEXT,
    st.binary(max_size=8),
    st.lists(HOSTILE_TEXT, max_size=3),
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(body=ANYTHING, relative=ANYTHING)
def test_the_reader_raises_only_its_own_error(
    tmp_path_factory: pytest.TempPathFactory, body: object, relative: object
) -> None:
    root = tmp_path_factory.mktemp("artifacts")
    (root / "research" / "42").mkdir(parents=True, exist_ok=True)
    try:
        (root / "research" / "42" / "run-1.json").write_text(
            body if isinstance(body, str) else json.dumps(body, default=repr),
            encoding="utf-8",
        )
    except (UnicodeEncodeError, ValueError):
        # A lone surrogate cannot be written as UTF-8, which is a property of the
        # *test's* own file write, not of the module under test.
        return
    for candidate in (relative, "research/42/run-1.json"):
        try:
            read_raw_responses(root, candidate)  # type: ignore[arg-type]
        except ArtifactError:
            continue
