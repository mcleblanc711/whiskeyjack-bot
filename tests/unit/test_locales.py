"""Locale registry, detection and publisher origin (M1-356)."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.locales import (
    LocaleError,
    LocaleRegistry,
    build_registry,
    detect,
    load_registry,
    origins,
    publisher_origin,
)

# Mathematical Bold small i as its UTF-16 surrogate pair: two code points. Built with chr()
# because `ruff format` rewrites a "\ud835\udc22" escape pair into the one-scalar literal,
# which silently turned these round-1 regressions into the case that already worked.
PAIR_I = chr(0xD835) + chr(0xDC22)
REPO = Path(__file__).resolve().parents[2]
COMMITTED = REPO / "config" / "locales.yaml"
DIGEST = "0" * 64


def _payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "locales": [
            {
                "code": "IR",
                "triggers": ["Iran", "Iranian", "Strait of Hormuz", "Hormuz"],
                "languages": ["fa"],
                "cctlds": ["ir"],
                "local_domains": ["mehrnews.com", "tehrantimes.com"],
                "external_domains": ["iranintl.com", "bbc.com/persian"],
                "native_terms": {"Strait of Hormuz": "تنگه هرمز"},
            },
            {
                "code": "OM",
                "triggers": ["Oman", "Strait of Hormuz", "Hormuz"],
                "languages": ["ar"],
                "cctlds": ["om"],
                "local_domains": ["timesofoman.com"],
            },
            {
                "code": "CA",
                "triggers": ["Canada"],
                "languages": ["en", "fr"],
                "cctlds": ["ca"],
            },
            {
                "code": "CA-QC",
                "parent": "CA",
                "triggers": ["Quebec", "Québec", "Montreal"],
                "languages": ["fr"],
                "local_domains": ["ledevoir.com"],
            },
        ],
    }


def _registry(payload: dict[str, Any] | None = None) -> LocaleRegistry:
    return build_registry(payload if payload is not None else _payload(), sha256=DIGEST)


def _question(
    title: str, *, parent: str | None = None, criteria: str | None = None, fine: str | None = None
) -> CanonicalBinaryQuestion:
    return CanonicalBinaryQuestion(
        question_id=1,
        post_id=1,
        title=title,
        group_parent_title=parent,
        resolution_criteria=criteria,
        fine_print=fine,
    )


# --- the committed registry ---------------------------------------------------


def test_the_committed_registry_loads_and_its_identity_is_the_file_bytes() -> None:
    registry = load_registry(COMMITTED)
    assert registry.sha256 == hashlib.sha256(COMMITTED.read_bytes()).hexdigest()
    assert {"IR", "OM", "AE", "RU", "CN"} <= {entry.code for entry in registry.locales}


def test_the_committed_registry_puts_the_probe_publishers_on_the_right_side() -> None:
    """The 2026-10-16 probe's Persian results, classified as the owner's correction requires."""
    iran = load_registry(COMMITTED).get("IR")
    assert iran is not None
    local = [
        "https://www.mehrnews.com/news/6974299/x",
        "https://defapress.ir/fa/news/857044/x",
        "https://snn.ir/fa/news/1435973/x",
        "https://www.tabnak.ir/fa/news/1361451/x",
    ]
    external = [
        "https://www.bbc.com/persian/articles/c2lwjj0z2lzo",
        "https://www.iranintl.com/202607316518",
    ]
    assert [publisher_origin(url, iran) for url in local] == ["local"] * 4
    assert [publisher_origin(url, iran) for url in external] == ["external"] * 2
    # AFP copy that AskNews tagged country=IR: a French agency, so not local.
    assert publisher_origin("http://doc.afp.com/D3DM4KE", iran) == "unclassified"


def test_a_hormuz_question_selects_every_littoral_locale_in_the_committed_registry() -> None:
    found = detect(
        _question("Will Iran close the Strait of Hormuz before November 1, 2026?"),
        load_registry(COMMITTED),
    )
    assert [match.code for match in found][:1] == ["IR"]
    assert {"IR", "OM", "AE"} <= {match.code for match in found}


def test_a_us_only_question_selects_nothing_in_the_committed_registry() -> None:
    question = _question("Will US CPI inflation exceed 3% in October 2026?")
    assert detect(question, load_registry(COMMITTED)) == ()


# --- detection ------------------------------------------------------------------


def test_detection_is_whole_word_and_case_insensitive() -> None:
    registry = _registry()
    assert [m.code for m in detect(_question("Will IRAN's navy act?"), registry)] == ["IR"]
    # "Iranian" is its own trigger; "Iranic" contains "Iran" but is not that word.
    assert detect(_question("Will an Iranic language be recognised?"), registry) == ()
    assert detect(_question("Will Omaninvest list shares?"), registry) == ()
    # A word character on the left is a different word too: "Kiran" is not "Iran".
    assert detect(_question("Will Kiran Rao resign?"), registry) == ()


def test_detection_reports_triggers_in_order_of_appearance() -> None:
    found = detect(_question("Hormuz: will Iranian forces stop traffic near Iran?"), _registry())
    iran = next(m for m in found if m.code == "IR")
    assert iran.triggers == ("Hormuz", "Iranian", "Iran")


def test_a_cross_border_trigger_selects_every_locale_listing_it_in_code_order() -> None:
    found = detect(_question("Will the Strait of Hormuz be closed?"), _registry())
    assert [m.code for m in found] == ["IR", "OM"]


def test_locales_are_ordered_by_first_appearance_in_the_question() -> None:
    found = detect(_question("Will Oman mediate between Canada and Iran?"), _registry())
    assert [m.code for m in found] == ["OM", "CA", "IR"]


def test_detection_reads_parent_title_and_criteria_but_not_fine_print() -> None:
    registry = _registry()
    assert [m.code for m in detect(_question("Option A", parent="Iran talks"), registry)] == ["IR"]
    assert [m.code for m in detect(_question("Q?", criteria="per Oman's ministry"), registry)] == [
        "OM"
    ]
    assert detect(_question("Will rates rise?", fine="unlike Canada in 2022"), registry) == ()


def test_detection_folds_unicode_spellings() -> None:
    registry = _registry()
    # NFKC: a compatibility-form "Ｑuebec" (fullwidth Q) is the same word.
    assert [m.code for m in detect(_question("Will Ｑuebec vote?"), registry)] == ["CA-QC"]
    assert [m.code for m in detect(_question("Will QUÉBEC vote?"), registry)] == ["CA-QC"]


def test_detection_tolerates_a_line_break_inside_a_multiword_trigger() -> None:
    # Asserted on the trigger, not the locale: the lone "Hormuz" trigger would select both
    # locales anyway, so a locale-level assertion passes whether or not the multi-word
    # trigger matched (it did, against a single-space mutant).
    found = detect(_question("Q?", criteria="traffic through the Strait of\nHormuz"), _registry())
    assert {m.code for m in found} == {"IR", "OM"}
    assert all("Strait of Hormuz" in m.triggers for m in found)


@pytest.mark.parametrize("bad", [None, "Iran", object()])
def test_detect_refuses_a_non_question_with_its_own_error(bad: object) -> None:
    with pytest.raises(LocaleError):
        detect(bad, _registry())  # type: ignore[arg-type]


def test_detect_refuses_a_non_registry_with_its_own_error() -> None:
    with pytest.raises(LocaleError):
        detect(_question("Iran?"), {"locales": []})  # type: ignore[arg-type]


# --- publisher origin -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.mehrnews.com/news/1", "local"),
        ("https://mehrnews.com./news/1", "local"),  # terminal root dot
        ("https://en.mehrnews.com/news/1", "local"),  # subdomain
        ("https://notmehrnews.com/news/1", "unclassified"),  # suffix without a dot boundary
        ("https://irna.ir/news/1", "local"),  # ccTLD rule
        ("https://www.bbc.com/persian/articles/x", "external"),
        ("https://www.bbc.com/persian", "external"),
        ("https://www.bbc.com/persianfood/x", "unclassified"),  # prefix needs a path boundary
        ("https://www.bbc.com/news/world", "unclassified"),
        ("https://www.iranintl.com/en/x", "external"),
        ("https://example.org/iran", "unclassified"),
        ("https://[abc/x", "unclassified"),  # unreadable host
        ("not a url", "unclassified"),
    ],
)
def test_publisher_origin(url: str, expected: str) -> None:
    iran = _registry().get("IR")
    assert iran is not None
    assert publisher_origin(url, iran) == expected


def test_an_external_entry_beats_the_cctld_rule() -> None:
    payload = _payload()
    payload["locales"][0]["external_domains"] = ["exile.ir"]
    iran = _registry(payload).get("IR")
    assert iran is not None
    assert publisher_origin("https://exile.ir/x", iran) == "external"


def test_publisher_origin_is_total_over_wrong_types() -> None:
    iran = _registry().get("IR")
    assert iran is not None
    assert publisher_origin(None, iran) == "unclassified"  # type: ignore[arg-type]
    assert publisher_origin("https://irna.ir/", None) == "unclassified"  # type: ignore[arg-type]


def test_origins_names_only_the_locales_a_url_is_classified_against() -> None:
    registry = _registry()
    assert origins("https://www.ledevoir.com/x", registry) == {"CA-QC": "local"}
    assert origins("https://www.cbc.ca/x", registry) == {"CA": "local"}
    assert origins("https://example.org/x", registry) == {}


# --- validation -------------------------------------------------------------------------


def _mutated(path: list[Any], value: Any) -> dict[str, Any]:
    payload = copy.deepcopy(_payload())
    target: Any = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return payload


@pytest.mark.parametrize(
    ("path", "value", "slug"),
    [
        (["locales", 0, "code"], "Iran", "locale_code_invalid"),
        (["locales", 0, "triggers"], ["Iran", "iran"], "locale_trigger_duplicate"),
        (["locales", 0, "triggers"], [" Iran"], "locale_trigger_invalid"),
        (["locales", 0, "triggers"], ["   "], "locale_trigger_invalid"),
        (["locales", 0, "languages"], ["fas"], "locale_language_invalid"),
        (["locales", 0, "languages"], ["fa", "fa"], "locale_language_duplicate"),
        (["locales", 0, "cctlds"], [".ir"], "locale_cctld_invalid"),
        (["locales", 0, "local_domains"], ["com"], "locale_domain_invalid"),
        (["locales", 0, "local_domains"], ["co.uk"], "locale_domain_not_registrable"),
        (["locales", 0, "local_domains"], ["Mehrnews.com"], "locale_domain_invalid"),
        (["locales", 0, "local_domains"], ["mehrnews.com/a b"], "locale_domain_path_invalid"),
        (["locales", 0, "local_domains"], ["x.com", "x.com"], "locale_domain_duplicate"),
        (["locales", 0, "external_domains"], ["mehrnews.com"], "locale_domain_conflict"),
        (["locales", 0, "native_terms"], {"Tehran": "تهران"}, "locale_native_term_orphan"),
        (["locales", 0, "native_terms"], {"Iran": " "}, "locale_native_term_blank"),
        (["locales", 3, "parent"], "IR", "locale_parent_mismatch"),
        (["locales", 3, "parent"], None, "locale_parent_missing"),
        (["locales", 1, "code"], "IR", "locale_code_duplicate"),
    ],
)
def test_each_invalid_shape_is_refused_with_its_slug(
    path: list[Any], value: Any, slug: str
) -> None:
    with pytest.raises(LocaleError) as caught:
        _registry(_mutated(path, value))
    assert slug in str(caught.value)


def test_a_region_whose_parent_is_absent_is_refused() -> None:
    payload = _payload()
    payload["locales"] = [payload["locales"][3]]
    with pytest.raises(LocaleError) as caught:
        _registry(payload)
    assert "locale_parent_unknown" in str(caught.value)


def test_an_unknown_key_is_refused_without_being_named() -> None:
    payload = _payload()
    payload["locales"][0]["privateFAKE123456"] = 1
    with pytest.raises(LocaleError) as caught:
        _registry(payload)
    assert "privateFAKE123456" not in str(caught.value)


SECRET = "privateFAKE123456"


@pytest.mark.parametrize(
    "path",
    [
        ["locales", 0, "code"],
        ["locales", 0, "parent"],
        ["locales", 0, "triggers"],
        ["locales", 0, "languages"],
        ["locales", 0, "cctlds"],
        ["locales", 0, "local_domains"],
        ["locales", 0, "external_domains"],
        ["locales", 0, "native_terms"],
        ["schema_version"],
    ],
)
@pytest.mark.parametrize("planted", [SECRET, [SECRET], {SECRET: SECRET}, [" " + SECRET]])
def test_no_field_leaks_a_planted_value_through_any_message(path: list[Any], planted: Any) -> None:
    try:
        _registry(_mutated(path, planted))
    except LocaleError as exc:
        assert SECRET not in str(exc)
        assert exc.__cause__ is None and exc.__suppress_context__


def test_a_bad_digest_is_refused() -> None:
    with pytest.raises(LocaleError):
        build_registry(_payload(), sha256="nothex")


# --- loading -------------------------------------------------------------------


def test_a_missing_file_is_a_filesystem_error_naming_the_path(tmp_path: Path) -> None:
    target = tmp_path / "absent.yaml"
    with pytest.raises(LocaleError) as caught:
        load_registry(target)
    assert caught.value.is_filesystem_error
    assert str(target) in str(caught.value)


def test_a_directory_is_a_filesystem_error(tmp_path: Path) -> None:
    with pytest.raises(LocaleError) as caught:
        load_registry(tmp_path)
    assert caught.value.is_filesystem_error


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.yaml"
    os.mkfifo(fifo)
    with pytest.raises(LocaleError) as caught:
        load_registry(fifo)
    assert caught.value.is_filesystem_error


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe" + SECRET.encode(),
        f"locales: [{SECRET}\n".encode(),
        f"x: !!int {SECRET}\n".encode(),
        f"x: !!bool {SECRET}\n".encode(),
        b"- just\n- a list\n",
    ],
)
def test_content_failures_are_not_filesystem_errors_and_do_not_echo(
    tmp_path: Path, body: bytes
) -> None:
    target = tmp_path / "locales.yaml"
    target.write_bytes(body)
    with pytest.raises(LocaleError) as caught:
        load_registry(target)
    assert not caught.value.is_filesystem_error
    assert SECRET not in str(caught.value)


def test_the_file_schema_version_is_the_documented_constant() -> None:
    from typing import get_args

    from whiskeyjack_bot.research.locales import REGISTRY_SCHEMA_VERSION, _RegistryFile

    annotation = _RegistryFile.model_fields["schema_version"].annotation
    assert get_args(annotation) == (REGISTRY_SCHEMA_VERSION,)


def test_a_surrogate_pair_letter_matches_before_and_after_replay() -> None:
    """Round 1, B1: the reviewer's counterexample, pinned."""
    import json

    question = _question(
        "Will the strait close?", criteria="According to " + PAIR_I + "ran officials"
    )
    stored = json.dumps(question.model_dump(mode="json"), ensure_ascii=True)
    reloaded = CanonicalBinaryQuestion.model_validate(json.loads(stored))
    registry = load_registry(COMMITTED)
    assert [m.code for m in detect(question, registry)] == ["IR"]
    assert detect(question, registry) == detect(reloaded, registry)


def test_comparison_form_is_the_json_replay_form_and_keeps_lone_surrogates() -> None:
    import json

    from whiskeyjack_bot.research.locales import comparison_form

    for text in (PAIR_I + "ran", "\U0001d422ran", "x\ud800", "\udc22\ud835", "IRAN"):
        replayed = json.loads(json.dumps(text, ensure_ascii=True))
        assert comparison_form(text) == comparison_form(replayed)
    assert comparison_form(PAIR_I + "ran") == "iran"
    assert comparison_form("x\ud800") == "x\ud800"
