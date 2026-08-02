"""Invariants of the Exa fallback policy and result mapper (M1-303).

The four properties CLAUDE.md requires of any hash, tiebreak, canonicalizer or
validator before its first review, stated over the two pure surfaces this item
adds: the fallback decision and the provider-result mapper.

- nothing raises outside the module's own error types;
- the reason vocabulary has one canonical order, so the persisted list is a
  function of the triggers and not of the caller's bookkeeping;
- a mapped document is replay-stable across the persisted JSON form;
- no provider value leaks into any message.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import pytest
from hypothesis import given, strategies as st
from strategies import HOSTILE_TEXT, URL_CANDIDATES, persisted, round_trip

from whiskeyjack_bot.research.canonical import CanonicalizationError, canonicalize_url
from whiskeyjack_bot.research.exa import (
    ExaFallbackError,
    FallbackReason,
    _call_cost_usd,
    _canonical_reasons,
    _hash_source,
    _matches_official_domain,
    _published_at_utc,
    _to_document,
    _validated_domains,
    decide_fallback,
)
from whiskeyjack_bot.research.model import ResearchSchemaError, validate_document, validate_run

# The planted value every "no leak" assertion looks for. Low entropy on purpose:
# CI runs gitleaks over every branch (see the M1-301 notes).
SECRET = "privateFAKE123456"

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

REASONS: tuple[FallbackReason, ...] = (
    "primary_provider_failed",
    "primary_returned_no_documents",
    "official_source_required",
)

# Date-shaped inputs, valid and not: the API documents YYYY-MM-DD, live responses
# also carry full ISO timestamps, and a provider is free to send neither.
PUBLISHED_DATES = st.one_of(
    st.sampled_from(
        [
            "2026-07-20",
            "2026-07-20T09:30:00Z",
            "2026-07-20T09:30:00z",
            "2026-07-20T09:30:00.000+05:30",
            "2026-07-20T09:30:00",
            "  2026-07-20  ",
            "0001-01-01",
            "9999-12-31T23:59:59+14:00",
            # Boundary timestamps whose UTC conversion overflows datetime's range
            # (PR #16 round-1 finding): must degrade to None, never raise.
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59-14:00",
            "2026-13-45",
            "not a date",
            "",
        ]
    ),
    HOSTILE_TEXT,
    st.none(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.dictionaries(st.text(max_size=3), st.text(max_size=3), max_size=2),
)

COST_VALUES = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-10, max_value=10),
    # JSON integers too large for a float (PR #16 round-1 finding): float(total)
    # must degrade to None, never raise OverflowError.
    st.sampled_from([10**400, -(10**400)]),
    st.booleans(),
    st.none(),
    HOSTILE_TEXT,
    st.lists(st.floats(allow_nan=False, allow_infinity=False), max_size=2),
)


@st.composite
def exa_results(draw: st.DrawFn) -> Any:
    """A provider result, in and out of contract."""
    if draw(st.booleans()):
        # Not a mapping at all: a provider can return anything inside `results`.
        return draw(st.one_of(st.none(), HOSTILE_TEXT, st.integers(), st.lists(st.integers())))
    result: dict[str, Any] = {
        "url": draw(URL_CANDIDATES),
        "title": draw(st.none() | HOSTILE_TEXT),
        "author": draw(st.none() | HOSTILE_TEXT),
        "text": draw(st.none() | HOSTILE_TEXT),
        "publishedDate": draw(PUBLISHED_DATES),
    }
    if draw(st.booleans()):
        del result[draw(st.sampled_from(sorted(result)))]
    return result


# Errors :func:`_to_document` is allowed to raise. ValueError covers the known
# `content_sha256` lone-surrogate defect (UnicodeEncodeError is a ValueError);
# `retrieve_web` catches exactly this set and counts a drop, so a hostile result
# costs one citation rather than the run.
ALLOWED = (
    ExaFallbackError,
    ResearchSchemaError,
    CanonicalizationError,
    AttributeError,
    TypeError,
    ValueError,
    KeyError,
)


def _map(result: Any) -> dict[str, Any]:
    return _to_document(result, retrieval_run_id="run-1", retrieved_at=NOW, domains=())


# --- the fallback policy ----------------------------------------------------


@given(st.booleans(), st.integers(min_value=0, max_value=50), st.booleans())
def test_decision_is_total_and_canonical(failed: bool, documents: int, official: bool) -> None:
    decision = decide_fallback(
        primary_failed=failed,
        primary_documents=documents,
        official_source_required=official,
    )
    assert decision.should_run is bool(decision.reasons)
    assert set(decision.reasons) <= set(REASONS)
    # One canonical order, and no repeats: the tuple is persisted.
    assert list(decision.reasons) == [r for r in REASONS if r in set(decision.reasons)]
    assert len(set(decision.reasons)) == len(decision.reasons)


@given(st.booleans(), st.integers(min_value=0, max_value=50), st.booleans())
def test_decision_is_deterministic(failed: bool, documents: int, official: bool) -> None:
    kwargs = {
        "primary_failed": failed,
        "primary_documents": documents,
        "official_source_required": official,
    }
    assert decide_fallback(**kwargs) == decide_fallback(**kwargs)  # type: ignore[arg-type]


@given(st.one_of(st.integers(max_value=-1), st.booleans(), st.floats(), st.text(max_size=4)))
def test_a_non_count_raises_only_the_module_error(documents: Any) -> None:
    with pytest.raises(ExaFallbackError):
        decide_fallback(
            primary_failed=False,
            primary_documents=documents,
            official_source_required=False,
        )


# Everything a caller could hand a bool parameter that is not one. The previous
# generators drew flags with st.booleans() only, so they could not have found
# round 4's finding 3 -- a truthy non-bool authorizing a paid call.
NON_BOOLS = st.one_of(
    st.none(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    HOSTILE_TEXT,
    st.lists(st.integers(), max_size=2),
    st.dictionaries(st.text(max_size=2), st.integers(), max_size=1),
)


@given(NON_BOOLS, st.integers(min_value=0, max_value=50), st.booleans())
def test_a_non_bool_primary_failed_raises_only_the_module_error(
    failed: Any, documents: int, official: bool
) -> None:
    """Truthiness must not be the test: "false" is truthy, and 0 and "" are not bools."""
    with pytest.raises(ExaFallbackError):
        decide_fallback(
            primary_failed=failed,
            primary_documents=documents,
            official_source_required=official,
        )


@given(st.booleans(), st.integers(min_value=0, max_value=50), NON_BOOLS)
def test_a_non_bool_official_flag_raises_only_the_module_error(
    failed: bool, documents: int, official: Any
) -> None:
    with pytest.raises(ExaFallbackError):
        decide_fallback(
            primary_failed=failed,
            primary_documents=documents,
            official_source_required=official,
        )


@pytest.mark.parametrize(
    "name", ["primary_failed", "primary_documents", "official_source_required"]
)
def test_a_planted_secret_in_any_argument_never_reaches_the_message(name: str) -> None:
    """Planted, not drawn: a generator that cannot produce SECRET proves nothing about it."""
    kwargs: dict[str, Any] = {
        "primary_failed": False,
        "primary_documents": 0,
        "official_source_required": False,
    }
    kwargs[name] = SECRET
    with pytest.raises(ExaFallbackError) as excinfo:
        decide_fallback(**kwargs)
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)


@given(st.lists(st.sampled_from(REASONS), max_size=8))
def test_reason_normalization_is_permutation_invariant(reasons: list[FallbackReason]) -> None:
    """Same triggers, same persisted list, whatever order they were assembled in."""
    forward = _canonical_reasons(reasons)
    assert _canonical_reasons(list(reversed(reasons))) == forward
    assert _canonical_reasons(reasons * 2) == forward
    # Idempotent, so a caller may safely pass a decision's reasons back in.
    assert _canonical_reasons(forward) == forward


@given(st.lists(st.text(max_size=8).filter(lambda s: s.strip()), max_size=4))
def test_unknown_reasons_raise_without_echoing_the_value(reasons: list[str]) -> None:
    """Non-blank entries only: a blank one is now refused by the *container* check.

    That refusal carries a different constant message, whose words a drawn value
    could coincidentally be a substring of -- the same exclusion the vocabulary
    message already needs below. Blank and non-string entries are covered by
    ``test_a_malformed_reason_container_raises_only_the_module_error`` instead.
    """
    if all(reason in REASONS for reason in reasons):
        return
    with pytest.raises(ExaFallbackError) as excinfo:
        _canonical_reasons(reasons)
    # Stated as "the message is a constant", not as "the value is absent from
    # the message". A substring check cannot state this property: the message
    # names the module's own vocabulary *and* its own prose, so a drawn value
    # like "pro" (inside "primary_provider_failed") or "rea" (inside "fallback
    # reason is not in the vocabulary") is a substring of it without anything
    # having been echoed -- and every exclusion carved out for those weakens the
    # assertion further. Comparing against the message a fixed unknown reason
    # produces says the thing that actually matters, with no exclusions at all:
    # the message is a function of nothing the caller passed.
    with pytest.raises(ExaFallbackError) as control:
        _canonical_reasons(["not-a-reason"])
    assert str(excinfo.value) == str(control.value)
    assert repr(excinfo.value) == repr(control.value)


def test_a_planted_secret_in_a_reason_never_reaches_the_message() -> None:
    with pytest.raises(ExaFallbackError) as excinfo:
        _canonical_reasons([SECRET])
    assert SECRET not in str(excinfo.value)


# --- the result mapper ------------------------------------------------------


@given(exa_results())
def test_mapping_raises_only_what_the_caller_catches(result: Any) -> None:
    try:
        payload = _map(result)
    except ALLOWED:
        return
    # A payload that comes out must also be a valid document, or the adapter
    # would hand the ledger something the schema rejects later.
    validate_document(payload)


@given(exa_results())
def test_a_mapped_document_is_replay_stable(result: Any) -> None:
    """Store and reload the way replay does: the persisted form must not move."""
    try:
        document = validate_document(_map(result))
    except ALLOWED:
        return
    assert persisted(round_trip(document)) == persisted(document)


@given(exa_results())
def test_a_mapped_run_config_survives_the_persisted_form(result: Any) -> None:
    """provider_config is stored as TEXT; a value that cannot round-trip is a defect."""
    try:
        document = validate_document(_map(result))
    except ALLOWED:
        return
    run = validate_run(
        {
            "retrieval_run_id": document.retrieval_run_id,
            "question_id": 1,
            "provider": "exa",
            "provider_config": {"fallback_reasons": list(REASONS)},
            "queries": [],
            "started_at_utc": NOW,
        }
    )
    encoded = json.dumps(run.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
    assert json.loads(encoded)["provider_config"]["fallback_reasons"] == list(REASONS)


@given(st.sampled_from(["url", "title", "author", "text", "publishedDate"]))
def test_no_provider_field_leaks_a_planted_secret_through_any_message(field: str) -> None:
    """Whatever a hostile field does, it must not come back out in a message."""
    result: dict[str, Any] = {
        "url": "https://example.org/a",
        "title": "t",
        "author": "a",
        "text": "body",
        "publishedDate": "2026-07-20",
    }
    result[field] = SECRET
    try:
        _map(result)
    except Exception as exc:  # noqa: BLE001 - the assertion is about any raise
        assert SECRET not in str(exc)
        assert SECRET not in repr(exc)


@given(HOSTILE_TEXT, st.none() | HOSTILE_TEXT)
def test_hash_source_prefers_text_then_title(text: str, title: str | None) -> None:
    result = {"text": text, "title": title}
    source = _hash_source(result)
    if text:
        assert source == text
    elif title:
        assert source == title
    else:
        assert source == ""


# --- field parsers ----------------------------------------------------------


@given(PUBLISHED_DATES)
def test_published_dates_are_aware_utc_or_absent(value: Any) -> None:
    """Never raises, and never yields the naive datetime the schema would reject."""
    parsed = _published_at_utc(value)
    if parsed is None:
        return
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


@given(COST_VALUES)
def test_costs_are_finite_non_negative_or_absent(value: Any) -> None:
    """Anything this returns has to be storable in ResearchRun.cost_usd."""
    cost = _call_cost_usd({"costDollars": {"total": value}})
    if cost is None:
        return
    assert isinstance(cost, float)
    run = validate_run(
        {
            "retrieval_run_id": "run-1",
            "question_id": 1,
            "provider": "exa",
            "started_at_utc": NOW,
            "cost_usd": cost,
        }
    )
    assert run.cost_usd == cost


@given(st.one_of(st.none(), st.integers(), HOSTILE_TEXT, st.lists(st.integers(), max_size=2)))
def test_a_malformed_cost_block_is_never_a_raise(block: Any) -> None:
    assert _call_cost_usd({"costDollars": block}) is None
    assert _call_cost_usd({}) is None


# --- official-domain matching (PR #16 round-3 finding 2) --------------------

_DOMAIN_LABEL = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8)

_BASE_DOMAINS = ("bls.gov", "sec.gov", "who.int")


# IDN and A-label spellings of the same hosts, which the ASCII-only generators
# above could not produce -- and which is the whole of round 4's finding 5: the
# allowlist was lowercased while the result host was IDNA-encoded, so the two
# forms of one domain never met.
_IDN_DOMAINS = ("bücher.de", "xn--bcher-kva.de", "нэб.рф", "παράδειγμα.δοκιμή")

# Filter shapes Exa documents and accepts, which this module cannot verify per
# result and therefore refuses outright.
_UNVERIFIABLE_FILTERS = (
    "*.substack.com",
    "exa.ai/blog",
    "https://huggingface.co/blog",
    "bls.gov:443",
    "user@bls.gov",
    " bls.gov",
)

# One label names no site, and `_matches_official_domain`'s subdomain rule would
# make every host beneath it `official` (round 5, finding 5). The dotted
# spellings are here because the root-dot normalization must not become a way
# around the rule.
_SINGLE_LABELS = ("com", "gov", "org", "localhost", "рф", "com.", "gov.")

# What a bare host cannot contain, stated here rather than imported from the
# module under test: a post-condition asserted against the implementation's own
# constant cannot catch a mistake *in* that constant.
_NOT_IN_A_BARE_HOST = frozenset("/:@*?#% \t\r\n")


@given(URL_CANDIDATES, st.lists(HOSTILE_TEXT, max_size=4))
def test_domain_match_never_raises(url: str, domains: list[str]) -> None:
    """Total over any canonical URL and any allowlist a caller could pass."""
    try:
        canonical = canonicalize_url(url)
    except CanonicalizationError:
        return
    assert isinstance(_matches_official_domain(canonical, domains), bool)


# --- the validated allowlist (round 4, finding 5) ---------------------------


@given(
    st.one_of(
        st.lists(HOSTILE_TEXT, max_size=4),
        st.lists(st.sampled_from(_IDN_DOMAINS + _BASE_DOMAINS + _UNVERIFIABLE_FILTERS), max_size=4),
        st.lists(st.one_of(st.none(), st.integers(), st.booleans()), max_size=3),
        HOSTILE_TEXT,
        st.sampled_from(["bls.gov", b"bls.gov"]),
    )
)
def test_validated_domains_raises_only_the_module_error(domains: Any) -> None:
    """Total, and the output is always a bare host this module can match against.

    The post-condition is the discriminating half: asserting only "a non-empty
    string comes back" passes on the pre-fix code, which returned entries
    untouched. What has to hold is that nothing survives validation unless it is
    already the exact form ``_matches_official_domain`` compares hostnames to.
    """
    try:
        validated = _validated_domains(domains)
    except ExaFallbackError:
        return
    for domain in validated:
        assert isinstance(domain, str) and domain
        assert not _NOT_IN_A_BARE_HOST.intersection(domain)
        assert domain == domain.strip().lower()
        # The entry and a result's host must come out of the same canonicalizer.
        assert urlsplit(canonicalize_url(f"https://{domain}")).hostname == domain


@given(
    st.lists(st.sampled_from(_IDN_DOMAINS + _BASE_DOMAINS), max_size=4)
    | st.lists(HOSTILE_TEXT, max_size=3)
    | st.lists(st.sampled_from(_UNVERIFIABLE_FILTERS), max_size=2)
)
def test_a_validated_allowlist_is_a_fixed_point(domains: list[str]) -> None:
    """Validating twice must not move it: the stored form is what gets matched.

    Paired with the entry-shape assertion, since a fixed point alone is trivially
    true of the identity function the pre-fix code was.
    """
    try:
        once = _validated_domains(domains)
    except ExaFallbackError:
        return
    assert _validated_domains(once) == once
    assert all(not _NOT_IN_A_BARE_HOST.intersection(domain) for domain in once)


@given(st.sampled_from(_UNVERIFIABLE_FILTERS))
def test_a_filter_this_module_cannot_verify_is_refused(entry: str) -> None:
    """Forwarding one and then labelling its results `web` is silent under-attribution."""
    with pytest.raises(ExaFallbackError):
        _validated_domains((entry,))


@given(URL_CANDIDATES)
def test_a_hosts_as_written_form_matches_a_result_on_that_host(url: str) -> None:
    """The property finding 5 broke: an allowlist entry must match a result on that host.

    The entry is taken from the URL **as written**, not from its canonical form.
    That distinction is the entire finding: a caller writes ``bücher.de``, the
    result's canonical host is ``xn--bcher-kva.de``, and the two never met.
    Sourcing the entry from the already-canonicalized URL instead would pass on
    the pre-fix code, because both sides would already be A-labels.
    """
    try:
        canonical = canonicalize_url(url)
    except CanonicalizationError:
        return
    as_written = urlsplit(url).hostname
    if as_written is None:
        return
    try:
        domains = _validated_domains((as_written,))
    except ExaFallbackError:
        # An IP literal or a host urlsplit spells with syntax a bare host cannot
        # carry (an IPv6 address arrives with colons).
        return
    assert _matches_official_domain(canonical, domains) is True


@given(st.sampled_from(_IDN_DOMAINS), st.none() | _DOMAIN_LABEL)
def test_an_idn_allowlist_entry_matches_its_own_subdomains(
    domain: str, subdomain: str | None
) -> None:
    """Both spellings of one IDN domain must select the same results."""
    validated = _validated_domains((domain,))
    host = domain if subdomain is None else f"{subdomain}.{domain}"
    canonical = canonicalize_url(f"https://{host}/path")
    assert _matches_official_domain(canonical, validated) is True


def test_a_planted_secret_in_an_allowlist_entry_never_reaches_the_message() -> None:
    with pytest.raises(ExaFallbackError) as excinfo:
        _validated_domains((f"{SECRET}/path",))
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)


@given(st.sampled_from(_BASE_DOMAINS), st.none() | _DOMAIN_LABEL)
def test_domain_match_is_exact_or_subdomain(domain: str, subdomain: str | None) -> None:
    """The bare domain matches itself; any subdomain of it matches too."""
    host = domain if subdomain is None else f"{subdomain}.{domain}"
    canonical = canonicalize_url(f"https://{host}/path")
    assert _matches_official_domain(canonical, (domain,)) is True


@given(URL_CANDIDATES)
def test_domain_match_is_false_with_no_allowlist(url: str) -> None:
    try:
        canonical = canonicalize_url(url)
    except CanonicalizationError:
        return
    assert _matches_official_domain(canonical, ()) is False


def test_domain_match_rejects_a_different_domain() -> None:
    """A result outside the allowlist must not match merely because a run requested one."""
    canonical = canonicalize_url("https://example.org/june-payrolls")
    assert _matches_official_domain(canonical, ("bls.gov",)) is False


def test_domain_match_does_not_match_on_a_bare_suffix() -> None:
    """``notbls.gov`` must not match ``bls.gov``: string-suffix, not subdomain, coincidence."""
    canonical = canonicalize_url("https://notbls.gov/page")
    assert _matches_official_domain(canonical, ("bls.gov",)) is False


# --- domain label edge cases (PR #16 round-5 findings 4 and 5) --------------


@given(
    st.sampled_from(_BASE_DOMAINS + _IDN_DOMAINS),
    st.booleans(),
    st.booleans(),
    st.none() | _DOMAIN_LABEL,
)
def test_the_two_spellings_of_a_host_select_each_other(
    domain: str, dotted_entry: bool, dotted_result: bool, subdomain: str | None
) -> None:
    """One DNS host, written with and without its root dot, must match either way.

    The two spellings are drawn **independently**, which is the whole of round 5's
    finding 4. ``test_a_hosts_as_written_form_matches_a_result_on_that_host``
    takes both sides from one URL, so they always carry the same spelling as each
    other and the property holds on the pre-fix code -- which compared
    ``bls.gov.`` against ``bls.gov`` and answered ``web``.
    """
    entry = f"{domain}." if dotted_entry else domain
    host = domain if subdomain is None else f"{subdomain}.{domain}"
    result_host = f"{host}." if dotted_result else host

    validated = _validated_domains((entry,))
    canonical = canonicalize_url(f"https://{result_host}/path")
    assert _matches_official_domain(canonical, validated) is True
    # The stored filter is the dotless form however the caller spelled it, so two
    # runs written differently persist the same `provider_config`.
    assert all(not d.endswith(".") for d in validated)
    assert validated == _validated_domains((domain,))


@given(st.sampled_from(_BASE_DOMAINS), st.booleans())
def test_the_root_dot_does_not_widen_a_suffix_coincidence(domain: str, dotted: bool) -> None:
    """Normalizing the dot must not turn a string-suffix coincidence into a match."""
    host = f"not{domain}." if dotted else f"not{domain}"
    canonical = canonicalize_url(f"https://{host}/path")
    assert _matches_official_domain(canonical, _validated_domains((domain,))) is False


@given(
    st.one_of(
        st.lists(HOSTILE_TEXT, max_size=3),
        st.lists(st.sampled_from(_BASE_DOMAINS + _IDN_DOMAINS + _SINGLE_LABELS), max_size=4),
    )
)
def test_no_validated_entry_is_a_single_label(domains: list[str]) -> None:
    """A one-label entry made every host beneath it ``official`` (round 5, finding 5).

    Stated as a post-condition on the output rather than as "these inputs raise":
    a rule about which entries survive validation is the thing that has to hold,
    and it holds for entries no sampled list thought to include.
    """
    try:
        validated = _validated_domains(domains)
    except ExaFallbackError:
        return
    for domain in validated:
        assert "." in domain, "a single label would promote every host beneath it"
        assert not domain.startswith(".") and not domain.endswith(".")


@given(st.sampled_from(_SINGLE_LABELS))
def test_a_single_label_entry_is_refused(entry: str) -> None:
    with pytest.raises(ExaFallbackError):
        _validated_domains((entry,))


# --- the reason container (PR #16 round-5 finding 6) ------------------------


class _RaisingIterable:
    """A container whose ``__iter__`` raises, which ``list()`` propagates unchanged."""

    def __iter__(self) -> Any:
        raise RuntimeError("deliberately broken __iter__")


@given(
    st.one_of(
        st.none(),
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        HOSTILE_TEXT,
        st.binary(max_size=4),
        st.lists(st.one_of(st.none(), st.integers(), HOSTILE_TEXT), max_size=3),
        st.dictionaries(st.text(max_size=2), st.text(max_size=2), max_size=2),
        st.just(_RaisingIterable()),
    )
)
def test_a_malformed_reason_container_raises_only_the_module_error(reasons: Any) -> None:
    """``fallback_reasons=None`` raised a raw ``TypeError`` (round 5, finding 6).

    The container was the one caller argument round 4's ``_string_list``
    hardening never reached, so this fuzzes the *shape* of the argument rather
    than its contents -- the existing vocabulary property only ever passed lists
    of strings, which is exactly why it could not find this.
    """
    try:
        result = _canonical_reasons(reasons)
    except ExaFallbackError:
        return
    # Whatever survives has to be the canonical persisted form.
    assert result == tuple(r for r in REASONS if r in set(result))
