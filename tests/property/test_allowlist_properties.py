"""Invariants of the account allowlist validator and its domain matcher (M1-308).

Both are pure over arbitrary operator-edited content once the YAML has been parsed into
a dict, so this suite skips file I/O (already covered by tests/unit/test_allowlist.py)
and fuzzes the validation/matching layer directly -- the same split
test_canonical_properties.py and test_dedup_properties.py use for their pure functions.

The "no value leak" property is covered deterministically in
tests/unit/test_allowlist.py::test_no_field_leaks_a_planted_secret_through_any_message
(every field, several shapes) rather than re-derived here: every raise in this module is
either a pydantic error rendered with include_input=False, or one of this module's own
constant-shaped, index-only messages, so leak-freedom does not depend on which value was
generated -- fuzzing it again would test the same invariant less precisely.
"""

from __future__ import annotations

from hypothesis import given, strategies as st
from strategies import HOSTILE_TEXT, RELIABILITY_TAGS
from pydantic import ValidationError

from whiskeyjack_bot.research.allowlist import (
    AccountAllowlist,
    AllowlistEntry,
    AllowlistError,
    _AllowlistFile,
    _sanitize,
)

_MAYBE_VALID_RELIABILITY = st.one_of(RELIABILITY_TAGS, HOSTILE_TEXT)
_DOMAIN_LIST = st.one_of(st.just([]), st.lists(HOSTILE_TEXT, min_size=1, max_size=3))


@st.composite
def _entry_payload(draw: st.DrawFn) -> dict[str, object]:
    entry: dict[str, object] = {
        "username": draw(HOSTILE_TEXT),
        "display_name": draw(HOSTILE_TEXT),
        "reliability_tag": draw(_MAYBE_VALID_RELIABILITY),
        "domains": draw(_DOMAIN_LIST),
    }
    if draw(st.booleans()):
        entry["notes"] = draw(HOSTILE_TEXT)
    if draw(st.booleans()):
        entry["unexpected_field"] = draw(HOSTILE_TEXT)
    return entry


@st.composite
def _allowlist_payload(draw: st.DrawFn) -> dict[str, object]:
    count = draw(st.integers(min_value=1, max_value=3))
    return {"accounts": [draw(_entry_payload()) for _ in range(count)]}


def _validate(data: object) -> AccountAllowlist:
    """Mirrors load_allowlist's validation step, without the file I/O around it."""
    try:
        parsed = _AllowlistFile.model_validate(data)
    except ValidationError as exc:
        raise _sanitize(exc) from None
    return AccountAllowlist(entries=tuple(parsed.accounts))


@given(_allowlist_payload())
def test_validate_raises_only_its_own_error_type(payload: dict[str, object]) -> None:
    """Any of pydantic's own exceptions escaping here is the finding: callers only
    handle AllowlistError."""
    try:
        _validate(payload)
    except AllowlistError:
        pass


@given(_allowlist_payload())
def test_a_second_validation_pass_agrees_with_the_first(payload: dict[str, object]) -> None:
    """Validation has no hidden state: the same payload is accepted or rejected the
    same way every time."""
    try:
        first: AccountAllowlist | None = _validate(payload)
    except AllowlistError:
        first = None
    try:
        second: AccountAllowlist | None = _validate(payload)
    except AllowlistError:
        second = None
    assert (first is None) == (second is None)
    if first is not None and second is not None:
        assert [e.model_dump() for e in first.entries] == [e.model_dump() for e in second.entries]


def _accounts() -> st.SearchStrategy[AllowlistEntry]:
    return st.builds(
        AllowlistEntry,
        username=st.text(
            min_size=1, max_size=12, alphabet=st.characters(min_codepoint=97, max_codepoint=122)
        ),
        display_name=st.text(min_size=1, max_size=20),
        reliability_tag=RELIABILITY_TAGS,
        domains=st.lists(
            st.sampled_from(["econ_data", "space_launch", "science"]), min_size=1, max_size=3
        ),
        notes=st.none() | st.text(max_size=20),
    )


@given(st.lists(_accounts(), max_size=8), HOSTILE_TEXT)
def test_match_domain_never_raises(entries: list[AllowlistEntry], domain: str) -> None:
    allowlist = AccountAllowlist(entries=tuple(entries))
    allowlist.match_domain(domain)


@given(st.lists(_accounts(), max_size=8), st.sampled_from(["econ_data", "space_launch", "science"]))
def test_match_domain_result_is_a_subset_and_deterministic(
    entries: list[AllowlistEntry], domain: str
) -> None:
    allowlist = AccountAllowlist(entries=tuple(entries))
    matched = allowlist.match_domain(domain)
    # AllowlistEntry is not hashable (plain _StrictModel, not frozen), so subset is
    # checked by identity membership rather than set containment.
    assert all(any(entry is candidate for candidate in allowlist.entries) for entry in matched)
    assert all(domain in entry.domains for entry in matched)
    assert allowlist.match_domain(domain) == matched


@given(st.lists(_accounts(), min_size=1, max_size=8))
def test_lookup_by_username_is_case_insensitive_and_never_raises(
    entries: list[AllowlistEntry],
) -> None:
    allowlist = AccountAllowlist(entries=tuple(entries))
    for entry in entries:
        found = allowlist.lookup_by_username(entry.username.swapcase())
        assert found is not None
        assert found.username.casefold() == entry.username.casefold()
    allowlist.lookup_by_username("definitely-not-present-xyz")
