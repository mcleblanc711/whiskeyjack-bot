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

# Valid domains and near-misses of one, alongside the hostile text: a payload built only
# from hostile text is rejected on nearly every draw, which would leave the properties
# that assert something about an *accepted* allowlist testing an empty case.
_DOMAINS = st.sampled_from(["econ_data", "space_launch", "science"])
_DOMAIN_LIST = st.one_of(
    st.just([]),
    st.lists(
        st.one_of(_DOMAINS, _DOMAINS.map(" {} ".format), HOSTILE_TEXT), min_size=1, max_size=3
    ),
)

# Handles, near-misses of one (padded, prefixed, over-length -- the round-4 finding's
# shapes), and hostile text.
_HANDLES = st.text(
    min_size=1, max_size=15, alphabet=st.characters(min_codepoint=97, max_codepoint=122)
)
_NEAR_MISS_HANDLES = st.one_of(
    _HANDLES.map(" {} ".format),
    _HANDLES.map("@{}".format),
    _HANDLES.map(lambda handle: handle * 3),
    HOSTILE_TEXT,
)
_USERNAMES = st.one_of(_HANDLES, _NEAR_MISS_HANDLES)

# A deliberately tiny pool for the accepted-allowlist properties. One of those properties
# needs two entries in the same file whose usernames differ only by padding -- the exact
# round-4 shape -- and independent draws from _HANDLES essentially never collide, so with
# free handles it passed against the pre-fix validator at 800 examples.
# Eight, not four: a smaller pool collides so often that most allowlists are rejected for
# duplication and the findability property runs on too few accepted draws (18% of draws
# accepted at eight handles, 8% at four). Both properties were checked against the pre-fix
# validator at this size and fail there, which is the evidence that they are not vacuous.
_POOL_HANDLES = st.sampled_from(
    ["blsgov", "beanews", "nasa", "spacex", "onsgov", "imfnews", "eiagov", "esa"]
)


def _mostly(
    valid: st.SearchStrategy[str], near_miss: st.SearchStrategy[str]
) -> st.SearchStrategy[str]:
    """``valid`` seven draws in eight, ``near_miss`` the eighth.

    Field validity compounds: an allowlist of three entries with three domains each is
    accepted only if all twelve values are, so an even split per field leaves acceptance
    in the low percent and the accepted-allowlist properties barely run.
    """
    # A named picker, not an inline lambda: hypothesis prints the strategy's callable in
    # every counterexample, and a lambda turns a failing allowlist into unreadable output.
    return st.tuples(st.integers(min_value=0, max_value=7).map(bool), valid, near_miss).map(_pick)


def _pick(choice: tuple[bool, str, str]) -> str:
    take_valid, valid, near_miss = choice
    return valid if take_valid else near_miss


@st.composite
def _entry_payload(draw: st.DrawFn) -> dict[str, object]:
    entry: dict[str, object] = {
        "username": draw(_USERNAMES),
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


@st.composite
def _plausible_entry_payload(draw: st.DrawFn) -> dict[str, object]:
    """An entry that mostly validates, with the near-misses still in the username and
    domain draws.

    _entry_payload is built to fuzz the *rejection* paths -- hostile text in every field
    and a forbidden extra key half the time -- so only 1 in ~300 of its allowlists is
    accepted. A property about what an accepted allowlist guarantees needs draws that
    reach acceptance, or it passes by never testing anything.
    """
    entry: dict[str, object] = {
        "username": draw(_mostly(_POOL_HANDLES, _POOL_HANDLES.map(" {} ".format))),
        "display_name": draw(st.text(min_size=1, max_size=20)),
        "reliability_tag": draw(RELIABILITY_TAGS),
        "domains": draw(
            st.lists(_mostly(_DOMAINS, _DOMAINS.map(" {} ".format)), min_size=1, max_size=3)
        ),
    }
    if draw(st.booleans()):
        entry["notes"] = draw(st.none() | st.text(max_size=20))
    return entry


@st.composite
def _plausible_payload(draw: st.DrawFn) -> dict[str, object]:
    count = draw(st.integers(min_value=1, max_value=3))
    return {"accounts": [draw(_plausible_entry_payload()) for _ in range(count)]}


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
def test_content_validation_never_produces_a_filesystem_error(payload: dict[str, object]) -> None:
    """_validate has no file I/O -- every AllowlistError it can raise is a content
    error (round-3 review finding: filesystem vs. content classification)."""
    try:
        _validate(payload)
    except AllowlistError as exc:
        assert exc.is_filesystem_error is False


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


@given(st.one_of(_plausible_payload(), _allowlist_payload()))
def test_every_accepted_entry_is_findable_by_the_key_a_caller_would_use(
    payload: dict[str, object],
) -> None:
    """The invariant whose absence let the round-4 finding through.

    Stated against the *normalized* key, not the stored one: looking an entry up by the
    exact bytes it was stored with succeeds even for " BLS_gov ", which is why a
    round-trip property would have passed on the broken code too. A caller has the handle
    ("BLS_gov"), not the operator's typo, and an entry it cannot reach fails *open* --
    M1-307 finds no match and applies the unverified_social default to an account the
    operator believed was tagged official_primary.
    """
    try:
        allowlist = _validate(payload)
    except AllowlistError:
        return
    for entry in allowlist.entries:
        found = allowlist.lookup_by_username(entry.username.strip())
        assert found is not None
        assert found.username.casefold() == entry.username.casefold()
        for domain in entry.domains:
            assert any(match is entry for match in allowlist.match_domain(domain.strip()))


@given(st.one_of(_plausible_payload(), _allowlist_payload()))
def test_accepted_usernames_do_not_collide_once_normalized(payload: dict[str, object]) -> None:
    """The other half of the same finding: the uniqueness check keys on the stored value,
    so " BLS_gov " and "BLS_gov" used to be two entries -- one of which was unreachable."""
    try:
        allowlist = _validate(payload)
    except AllowlistError:
        return
    keys = [entry.username.strip().casefold() for entry in allowlist.entries]
    assert len(set(keys)) == len(keys)


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
