"""M5-806's seams in live modules: each additive, each byte-identical when left at its default.

The worker loads these modules, so the default behaviour of every changed function is pinned
here against values captured *before* the change -- a default compared only against itself pins
nothing. The rest is the structural rule a replay arm's transform is held to at the spending
site (``forecast.inputs.require_transformed_input``), one forbidden change per case.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest
import yaml

from test_forecast_generate import (  # type: ignore[import-not-found]
    MODEL_NAME,
    NOW,
    PROMPT_PATH,
    REPO_ROOT,
    _generate,
    _Model,
    good_reply,
)

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.forecast.generate import ForecastGenerationError
from whiskeyjack_bot.forecast.inputs import (
    ForecastDocumentInput,
    ForecastInputError,
    ForecastModelInput,
    LensDocumentInput,
    LensModelInput,
    ModelInput,
    SourceReference,
    require_transformed_input,
)
from whiskeyjack_bot.forecast.priced import (
    MODEL_IN_SERVICE,
    PRICED_MODELS,
    REASONING_EFFORT,
    PricedClient,
    build_request,
)
from whiskeyjack_bot.prompt import LoadedPrompt, load_prompt
from whiskeyjack_bot.tournament_state import BudgetExhausted, TournamentError, digest


@pytest.fixture()
def gen_config(tmp_path: Any) -> AppConfig:
    """``test_forecast_generate``'s config fixture, under a name this module owns."""
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = MODEL_NAME
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    return validate_config_data(data)


@pytest.fixture()
def gen_prompt(gen_config: AppConfig) -> LoadedPrompt:
    return load_prompt(
        PROMPT_PATH,
        gen_config.forecast.prompt_version,
        min_probability=gen_config.forecast.min_probability,
        max_probability=gen_config.forecast.max_probability,
    )


# --- forecast/priced.py ------------------------------------------------------------------------

PINNED_MESSAGES = [
    {"role": "system", "content": "pinned system"},
    {"role": "user", "content": "pinned user"},
]
# Captured on master (5af4776) before M5-806 touched build_request. Every live cache scope is
# keyed on this digest, so a default body that moved by one byte would make every completed
# live call look new.
PINNED_DIGESTS = {
    "openrouter/openai/gpt-5.6-sol": (
        "bb587caf005e380c62af06d3424ef256ecd7fd45e8556abc19014047a5748523"
    ),
    "openrouter/openai/gpt-6-astra": (
        "b2103213bf739c9a7015c6f227406a69055369b89c8b1a65d7db0e647bd2716e"
    ),
}


@pytest.mark.parametrize("model", sorted(PRICED_MODELS))
def test_the_default_request_body_is_byte_identical_to_before_the_seam(model: str) -> None:
    assert digest(build_request(PINNED_MESSAGES, PRICED_MODELS[model])) == PINNED_DIGESTS[model]
    assert (
        digest(
            build_request(PINNED_MESSAGES, PRICED_MODELS[model], reasoning_effort=REASONING_EFFORT)
        )
        == (PINNED_DIGESTS[model])
    )


def test_sols_registered_prices_render_as_integers() -> None:
    """M1-408 wrote them as ints so Sol's digest never moved; a float would render 2.0."""
    body = build_request(PINNED_MESSAGES, PRICED_MODELS["openrouter/openai/gpt-5.6-sol"])
    rendered = json.dumps(body["provider"]["max_price"], sort_keys=True)
    assert rendered == '{"completion": 10, "prompt": 2}'


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_an_effort_reaches_the_body_and_only_the_effort_changes(effort: str) -> None:
    model = PRICED_MODELS["openrouter/openai/gpt-6-astra"]
    body = build_request(PINNED_MESSAGES, model, reasoning_effort=effort)  # type: ignore[arg-type]
    default = build_request(PINNED_MESSAGES, model)
    assert body["reasoning"] == {"effort": effort, "exclude": True}
    assert {k: v for k, v in body.items() if k != "reasoning"} == {
        k: v for k, v in default.items() if k != "reasoning"
    }


@pytest.mark.parametrize("effort", ["xhigh", "HIGH", "", None, 1])
def test_an_effort_outside_the_vocabulary_is_refused(effort: object) -> None:
    model = PRICED_MODELS["openrouter/openai/gpt-6-astra"]
    with pytest.raises(TournamentError, match="reasoning effort"):
        build_request(PINNED_MESSAGES, model, reasoning_effort=effort)  # type: ignore[arg-type]


def _priced_config(config: AppConfig, name: str = "openrouter/openai/gpt-6-astra") -> AppConfig:
    return config.model_copy(update={"model": config.model.model_copy(update={"name": name})})


def test_the_priced_client_sends_the_live_effort_unless_told_otherwise(
    gen_config: AppConfig,
) -> None:
    assert PricedClient(_priced_config(gen_config)).reasoning_effort == "high" == REASONING_EFFORT
    low = PricedClient(_priced_config(gen_config), reasoning_effort="low")
    assert low.reasoning_effort == "low"
    with pytest.raises(TournamentError, match="reasoning effort"):
        PricedClient(_priced_config(gen_config), reasoning_effort="max")  # type: ignore[arg-type]


def test_only_astra_has_an_in_service_instant() -> None:
    assert MODEL_IN_SERVICE == {
        "openrouter/openai/gpt-6-astra": datetime(2026, 9, 11, tzinfo=timezone.utc)
    }
    assert set(MODEL_IN_SERVICE) <= set(PRICED_MODELS)


# --- tournament_state.py -----------------------------------------------------------------------


def test_budget_exhausted_is_a_tournament_error_with_a_constant_message() -> None:
    assert issubclass(BudgetExhausted, TournamentError)


# --- forecast/inputs.py: the structural rule ----------------------------------------------------

AS_OF = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _base(count: int = 3) -> ModelInput:
    documents = [
        ForecastDocumentInput(
            source_id=f"src-{index:03d}",
            url=f"https://pub{index}.example/story/{index}",
            title=f"Title {index}",
            publisher=f"Publisher {index}",
            retrieved_at=AS_OF,
            evidence_summary=f"summary {index}",
            source_type="news",
            provenance="direct_api",
        )
        for index in range(1, count + 1)
    ]
    packet = ForecastModelInput(
        as_of_utc=AS_OF,
        question_id=1,
        post_id=2,
        tournament_id="minibench",
        question_type="binary",
        question_text="Will it happen?",
        research_documents=documents,
    )
    sources = tuple(
        SourceReference(
            source_id=document.source_id,
            document_id=f"doc-{document.source_id}",
            canonical_url=document.url,
            content_sha256=f"{index:064x}",
        )
        for index, document in enumerate(documents, start=1)
    )
    return ModelInput(packet=packet, sources=sources)


def _with(base: ModelInput, documents: list[Any], sources: tuple[Any, ...]) -> ModelInput:
    return ModelInput(
        packet=base.packet.model_copy(update={"research_documents": documents}), sources=sources
    )


def _accepted() -> list[tuple[str, Any]]:
    base = _base()
    docs, refs = base.packet.research_documents, base.sources
    masked = [
        doc.model_copy(update={"publisher": "publisher-01", "url": "https://source-01.invalid/x"})
        for doc in docs
    ]
    return [
        ("identity", base),
        ("subset", _with(base, [docs[0], docs[2]], (refs[0], refs[2]))),
        ("reordered", _with(base, [docs[2], docs[0], docs[1]], refs)),
        ("masked", _with(base, masked, refs)),
        ("empty", _with(base, [], ())),
    ]


@pytest.mark.parametrize(("name", "transformed"), _accepted(), ids=[n for n, _ in _accepted()])
def test_a_permitted_transform_is_accepted(name: str, transformed: ModelInput) -> None:
    result = require_transformed_input(_base(), transformed)
    assert result.sources == transformed.sources
    assert result.packet.research_documents == transformed.packet.research_documents


def _refused() -> list[tuple[str, Any]]:
    base = _base()
    docs, refs = base.packet.research_documents, base.sources
    stranger = docs[0].model_copy(update={"source_id": "src-009"})
    stranger_ref = SourceReference("src-009", None, docs[0].url, "f" * 64)
    lens_docs = [LensDocumentInput(**doc.model_dump()) for doc in docs]
    lens = ModelInput(
        packet=LensModelInput(**{**base.packet.model_dump(), "research_documents": lens_docs}),
        sources=refs,
    )
    return [
        ("not a ModelInput", base.packet),
        ("a lens packet", lens),
        (
            "a moved cutoff",
            ModelInput(base.packet.model_copy(update={"as_of_utc": NOW}), refs),
        ),
        (
            "an edited question",
            ModelInput(base.packet.model_copy(update={"question_text": "Other?"}), refs),
        ),
        ("an added document", _with(base, [*docs, stranger], (*refs, stranger_ref))),
        ("sources out of order", _with(base, list(docs), (refs[1], refs[0], refs[2]))),
        ("a duplicated source", _with(base, list(docs), (refs[0], refs[0], refs[1], refs[2]))),
        ("a dropped document still cited", _with(base, [docs[0], docs[1]], refs)),
        ("a document shown without its source", _with(base, list(docs), (refs[0], refs[1]))),
        (
            "an edited title",
            _with(base, [docs[0].model_copy(update={"title": "Other"}), docs[1], docs[2]], refs),
        ),
        (
            "an edited summary",
            _with(
                base,
                [docs[0].model_copy(update={"evidence_summary": "x"}), docs[1], docs[2]],
                refs,
            ),
        ),
        (
            "a renumbered document",
            _with(
                base,
                [docs[0].model_copy(update={"source_id": "src-002"}), docs[2]],
                (refs[1], refs[2]),
            ),
        ),
        (
            "a changed reference",
            _with(
                base,
                list(docs),
                (refs[0], refs[1], SourceReference("src-003", None, "u", "e" * 64)),
            ),
        ),
        (
            "a list of sources",
            ModelInput(base.packet, list(refs)),  # type: ignore[arg-type]
        ),
        (
            "an invalid document",
            _with(base, [docs[0].model_copy(update={"url": ""}), docs[1], docs[2]], refs),
        ),
    ]


# The rule each forbidden case must be refused by: several cases would also fail a later
# check (a lens packet does not re-validate as a plain one), so naming the rule is what keeps
# each check load-bearing on its own.
_REFUSED_BY = {
    "not a ModelInput": "plain ModelInput",
    "a lens packet": "plain ModelInput",
    "a moved cutoff": "only the research documents",
    "an edited question": "only the research documents",
    "an added document": "names a source the base packet lacks",
    "sources out of order": "keep base order",
    "a duplicated source": "keep base order",
    "a dropped document still cited": "render exactly the sources",
    "a document shown without its source": "render exactly the sources",
    "an edited title": "identity or content",
    "an edited summary": "identity or content",
    "a renumbered document": "identity or content",
    "a changed reference": "differs from its base",
    "a list of sources": "tuple of references",
    "an invalid document": "does not validate",
}


@pytest.mark.parametrize(("name", "transformed"), _refused(), ids=[n for n, _ in _refused()])
def test_a_forbidden_transform_is_refused(name: str, transformed: Any) -> None:
    with pytest.raises(ForecastInputError, match=_REFUSED_BY[name]):
        require_transformed_input(_base(), transformed)


def test_every_refused_case_names_its_rule() -> None:
    assert {name for name, _ in _refused()} == set(_REFUSED_BY)


def test_a_lens_base_cannot_be_transformed() -> None:
    base = _base()
    lens_docs = [LensDocumentInput(**doc.model_dump()) for doc in base.packet.research_documents]
    lens = ModelInput(
        packet=LensModelInput(**{**base.packet.model_dump(), "research_documents": lens_docs}),
        sources=base.sources,
    )
    with pytest.raises(ForecastInputError, match="plain reasoning packet"):
        require_transformed_input(lens, base)


# --- forecast/generate.py: the transform keyword ------------------------------------------------


def test_no_transform_and_the_identity_transform_send_the_same_bytes(
    gen_config: AppConfig, gen_prompt: LoadedPrompt
) -> None:
    plain, identity = _Model(good_reply()), _Model(good_reply())
    first = _generate(plain, gen_config, gen_prompt)
    second = _generate(identity, gen_config, gen_prompt, model_input_transform=lambda base: base)
    assert plain.calls == identity.calls, "the identity transform is invisible to the model"
    assert first.request == second.request and first.forecast == second.forecast


def test_a_refused_transform_spends_nothing(
    gen_config: AppConfig, gen_prompt: LoadedPrompt
) -> None:
    client = _Model(good_reply())

    def edit(base: ModelInput) -> ModelInput:
        return ModelInput(base.packet.model_copy(update={"question_text": "Edited?"}), base.sources)

    with pytest.raises(ForecastGenerationError, match="only the research documents"):
        _generate(client, gen_config, gen_prompt, model_input_transform=edit)
    assert client.calls == []


def test_a_transform_cannot_be_combined_with_rendered_locales(
    gen_config: AppConfig, gen_prompt: LoadedPrompt
) -> None:
    client = _Model(good_reply())
    with pytest.raises(ForecastGenerationError, match="locales"):
        _generate(
            client,
            gen_config,
            gen_prompt,
            render_locales=True,
            model_input_transform=lambda base: base,
        )
    assert client.calls == []


def test_a_dropped_documents_citation_is_refused_by_the_attribution_check(
    gen_config: AppConfig, gen_prompt: LoadedPrompt
) -> None:
    """The prompt's example reply cites src-001 and src-002; with src-002 dropped it cannot."""
    client = _Model(good_reply())

    def drop_second(base: ModelInput) -> ModelInput:
        documents = [d for d in base.packet.research_documents if d.source_id != "src-002"]
        sources = tuple(r for r in base.sources if r.source_id != "src-002")
        return ModelInput(base.packet.model_copy(update={"research_documents": documents}), sources)

    result = _generate(client, gen_config, gen_prompt, model_input_transform=drop_second)
    assert result.forecast is None and result.failure_code == "schema_invalid"
    shown = json.loads(client.calls[0][1]["content"])["research_documents"]
    assert [d["source_id"] for d in shown] == ["src-001"]


def test_the_priced_client_posts_the_effort_it_was_built_with(
    gen_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    posted: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.01}}
        )

    original = httpx.AsyncClient
    configured = _priced_config(gen_config)
    monkeypatch.setenv(configured.model.api_key_env, "test-secret")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    asyncio.run(PricedClient(configured, reasoning_effort="medium").invoke(PINNED_MESSAGES))
    asyncio.run(PricedClient(configured).invoke(PINNED_MESSAGES))
    assert [body["reasoning"]["effort"] for body in posted] == ["medium", "high"]
